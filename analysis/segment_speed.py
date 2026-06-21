"""Gold tier: inter-stop segment speed mart from a Visit stream + GTFS stop coordinates.

`compute_segment_speeds` pairs consecutive Visit dwell events within the same trip,
computes the haversine distance between the two stops (from the static GTFS stop
coordinate table), and derives the in-motion speed from transit time and distance.
Produces two marts:

  segment_speed — trip-stop-pair fact grain: one row per inter-stop traversal.
  segment_day   — one row per (route_id, direction_id, from_stop_id, to_stop_id,
                  service_date) with aggregated speed statistics.

Requires GTFS stop coordinates: pass a {stop_id: (lat, lon)} dict (see
`StaticGtfs.stop_coords`). Segments where either stop has no coordinate, where
transit_s <= 0 (arrival before or equal to departure — ordering artifact), or where
the implied speed exceeds `max_speed_mph` (GPS/timestamp artifact) are dropped.

Like `adherence.py`, this module is pure — no I/O, no GTFS file access. `gold.py`
owns resolving the StaticGtfs snapshot and the atomic parquet write.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable
from zoneinfo import ZoneInfo

import pyarrow as pa

from analysis.metrics import _pct, _round
from analysis.timeutil import service_date as _service_date
from analysis.vehicle_day import Visit

_MPS_TO_MPH = 2.23694
_EARTH_R_M = 6_371_000.0

# Trains rarely exceed 200 mph in revenue service; anything higher is almost
# certainly a GPS artifact or a bad timestamp pair.
DEFAULT_MAX_SPEED_MPH = 200.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * _EARTH_R_M * math.asin(math.sqrt(a))


# Partition keys (feed, year, month, day) live in the path only, matching the
# silver + schedule-free gold layout; rows still carry `feed` for in-memory use.
SEGMENT_SPEED_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("vehicle_id", pa.string(), nullable=True),
        pa.field("from_stop_id", pa.string(), nullable=False),
        pa.field("to_stop_id", pa.string(), nullable=False),
        pa.field("from_stop_sequence", pa.int32(), nullable=True),
        pa.field("to_stop_sequence", pa.int32(), nullable=True),
        pa.field("departure_unix", pa.int64(), nullable=False),
        pa.field("arrival_unix", pa.int64(), nullable=False),
        pa.field("transit_s", pa.int32(), nullable=False),
        pa.field("distance_m", pa.float64(), nullable=False),
        pa.field("speed_mph", pa.float64(), nullable=False),
        pa.field("service_date", pa.string(), nullable=False),
    ]
)

SEGMENT_DAY_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("from_stop_id", pa.string(), nullable=False),
        pa.field("to_stop_id", pa.string(), nullable=False),
        pa.field("service_date", pa.string(), nullable=False),
        pa.field("sample_count", pa.int32(), nullable=False),
        pa.field("speed_p50_mph", pa.float64(), nullable=True),
        pa.field("speed_p90_mph", pa.float64(), nullable=True),
        pa.field("speed_mean_mph", pa.float64(), nullable=True),
        pa.field("transit_p50_s", pa.int32(), nullable=True),
        pa.field("distance_m", pa.float64(), nullable=False),
    ]
)


def compute_segment_speeds(
    visits: Iterable[Visit],
    stop_coords: dict[str, tuple[float, float]],
    feed: str,
    local_tz: ZoneInfo,
    *,
    max_speed_mph: float = DEFAULT_MAX_SPEED_MPH,
) -> tuple[list[dict], list[dict]]:
    """Pair consecutive dwell visits within the same trip and compute inter-stop speeds.

    Groups visits by trip_id (sorted by arrival_ts within each trip) and pairs
    consecutive stops. The departure_unix of the first stop and the arrival_unix of
    the second bound the in-motion time. Distance is the haversine between GTFS stop
    coordinates.

    Drops segments where:
      - either stop has no GTFS coordinate
      - transit_s <= 0
      - implied speed > max_speed_mph

    Returns (fact_rows, segment_day_rows). Rows are plain dicts keyed exactly by
    SEGMENT_SPEED_SCHEMA / SEGMENT_DAY_SCHEMA.
    """
    by_trip: dict[str, list[Visit]] = defaultdict(list)
    for v in visits:
        if v.trip_id and v.route_id:
            by_trip[v.trip_id].append(v)
    for tid in by_trip:
        by_trip[tid].sort(key=lambda v: v.arrival_ts)

    fact_rows: list[dict] = []
    for trip_id, trip_visits in by_trip.items():
        for a, b in zip(trip_visits, trip_visits[1:]):
            coords_a = stop_coords.get(a.stop_id)
            coords_b = stop_coords.get(b.stop_id)
            if coords_a is None or coords_b is None:
                continue
            transit_s = b.arrival_ts - a.departure_ts
            if transit_s <= 0:
                continue
            distance_m = _haversine_m(*coords_a, *coords_b)
            if distance_m == 0:
                continue
            speed_mph = (distance_m / transit_s) * _MPS_TO_MPH
            if speed_mph > max_speed_mph:
                continue
            sd = _service_date(a.departure_ts, local_tz).isoformat()
            fact_rows.append(
                {
                    "feed": feed,
                    "route_id": a.route_id,
                    "direction_id": a.direction_id,
                    "trip_id": trip_id,
                    "vehicle_id": a.vehicle_id,
                    "from_stop_id": a.stop_id,
                    "to_stop_id": b.stop_id,
                    "from_stop_sequence": a.stop_sequence,
                    "to_stop_sequence": b.stop_sequence,
                    "departure_unix": a.departure_ts,
                    "arrival_unix": b.arrival_ts,
                    "transit_s": transit_s,
                    "distance_m": distance_m,
                    "speed_mph": speed_mph,
                    "service_date": sd,
                }
            )

    seg_buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in fact_rows:
        key = (
            r["route_id"],
            r["direction_id"],
            r["from_stop_id"],
            r["to_stop_id"],
            r["service_date"],
        )
        seg_buckets[key].append(r)

    seg_day_rows: list[dict] = []
    for (route_id, direction_id, from_stop, to_stop, sd), rows in seg_buckets.items():
        speeds = [r["speed_mph"] for r in rows]
        transits = [r["transit_s"] for r in rows]
        seg_day_rows.append(
            {
                "feed": feed,
                "route_id": route_id,
                "direction_id": direction_id,
                "from_stop_id": from_stop,
                "to_stop_id": to_stop,
                "service_date": sd,
                "sample_count": len(rows),
                "speed_p50_mph": _pct(speeds, 50),
                "speed_p90_mph": _pct(speeds, 90),
                "speed_mean_mph": statistics.fmean(speeds) if speeds else None,
                "transit_p50_s": _round(_pct(transits, 50)),
                "distance_m": rows[0]["distance_m"],
            }
        )

    return fact_rows, seg_day_rows
