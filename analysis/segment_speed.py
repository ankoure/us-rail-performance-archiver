"""Gold tier: inter-stop segment speed mart from a Visit stream + GTFS stop coordinates.

`compute_segment_speeds` pairs consecutive Visit dwell events within the same trip
and derives the in-motion speed from transit time and inter-stop distance.
Produces two marts:

  segment_speed — trip-stop-pair fact grain: one row per inter-stop traversal.
  segment_day   — one row per (route_id, direction_id, from_stop_id, to_stop_id,
                  service_date) with aggregated speed statistics.

Distance prefers shape-following: when `shape_by_trip`/`shape_points` are
given (see `StaticGtfs.trip_shapes`/`StaticGtfs.shape_points`), each stop is
projected onto its trip's GTFS shape polyline and the along-shape distance
between the two projections is used — this matters for curvy alignments,
where straight-line distance meaningfully understates true track distance.
The projection is sanity-checked against the straight-line distance (a real
track can only be as long or longer, never much longer) and falls back to
straight-line haversine whenever it's missing, absent, or fails that check —
see `_shape_distance_m`. Passing neither argument (or omitting a trip's
shape) means every segment uses straight-line distance, matching the
module's original behavior.

Requires GTFS stop coordinates regardless: pass a {stop_id: (lat, lon)} dict
(see `StaticGtfs.stop_coords`). Segments where either stop has no coordinate,
where transit_s <= 0 (arrival before or equal to departure — ordering
artifact), or where the implied speed exceeds `max_speed_mph` (GPS/timestamp
artifact) are dropped.

Like `adherence.py`, this module is pure — no I/O, no GTFS file access. `gold.py`
owns resolving the StaticGtfs snapshot and the atomic parquet write.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from zoneinfo import ZoneInfo

import pyarrow as pa

from analysis.geo import (
    cumulative_arc_length_m,
    haversine_m as _haversine_m,
    project_point_to_polyline,
)
from analysis.metrics import _pct, _round
from analysis.timeutil import service_date as _service_date
from analysis.vehicle_day import Visit

_MPS_TO_MPH = 2.23694

# Trains rarely exceed 200 mph in revenue service; anything higher is almost
# certainly a GPS artifact or a bad timestamp pair.
DEFAULT_MAX_SPEED_MPH = 200.0

# A genuine inter-stop segment takes at most an hour. Anything longer is almost
# certainly a trip_id reuse artifact (same id assigned across multiple runs) or
# a vehicle that sat at a terminus overnight and then moved.
DEFAULT_MAX_TRANSIT_S = 3600

# A shape-following distance can never legitimately be shorter than the
# straight line between the same two points (minus a little slack for
# projection/rounding error), and a factor much beyond this on adjacent stops
# points at a projection gone wrong (e.g. a looping shape where both stops
# snap near the same point) rather than a genuinely winding alignment.
_SHAPE_DISTANCE_MIN_RATIO = 0.95
_SHAPE_DISTANCE_MAX_RATIO = 4.0


class _ShapeProjector:
    """Memoized (shape_id, stop_id) -> distance-along-shape lookup for one run.

    Many trips share the same shape and many trips revisit the same stops, so
    caching both the per-shape cumulative arc length and the per-(shape,
    stop) projection avoids redoing the O(shape points) projection work for
    every trip that happens to share a shape and a stop.
    """

    def __init__(self, shape_points: dict[str, list[tuple[float, float]]]) -> None:
        self._shape_points = shape_points
        self._cumulative: dict[str, list[float]] = {}
        self._projections: dict[tuple[str, str], float | None] = {}

    def distance_along(
        self, shape_id: str, stop_id: str, lat: float, lon: float
    ) -> float | None:
        key = (shape_id, stop_id)
        if key in self._projections:
            return self._projections[key]
        points = self._shape_points.get(shape_id)
        if not points or len(points) < 2:
            self._projections[key] = None
            return None
        cumulative = self._cumulative.get(shape_id)
        if cumulative is None:
            cumulative = cumulative_arc_length_m(points)
            self._cumulative[shape_id] = cumulative
        result = project_point_to_polyline(lat, lon, points, cumulative)
        self._projections[key] = result
        return result


def _shape_distance_m(
    projector: _ShapeProjector,
    shape_id: str | None,
    a: Visit,
    b: Visit,
    coords_a: tuple[float, float],
    coords_b: tuple[float, float],
    haversine_distance_m: float,
) -> float | None:
    """Shape-following distance between two stops, or None to fall back.

    Returns None whenever the trip has no shape, either stop fails to
    project onto it, or the result fails the sanity check against the
    straight-line distance (see `_SHAPE_DISTANCE_MIN_RATIO`/`_MAX_RATIO`).
    """
    if not shape_id:
        return None
    dist_a = projector.distance_along(shape_id, a.stop_id, *coords_a)
    dist_b = projector.distance_along(shape_id, b.stop_id, *coords_b)
    if dist_a is None or dist_b is None:
        return None
    shape_distance = abs(dist_b - dist_a)
    if haversine_distance_m == 0:
        return None
    ratio = shape_distance / haversine_distance_m
    if ratio < _SHAPE_DISTANCE_MIN_RATIO or ratio > _SHAPE_DISTANCE_MAX_RATIO:
        return None
    return shape_distance


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
    shape_by_trip: dict[str, str] | None = None,
    shape_points: dict[str, list[tuple[float, float]]] | None = None,
    max_speed_mph: float = DEFAULT_MAX_SPEED_MPH,
    max_transit_s: int = DEFAULT_MAX_TRANSIT_S,
) -> tuple[list[dict], list[dict]]:
    """Pair consecutive dwell visits within the same trip and compute inter-stop speeds.

    Groups visits by trip_id (sorted by arrival_ts within each trip) and pairs
    consecutive stops. The departure_unix of the first stop and the arrival_unix of
    the second bound the in-motion time. Distance prefers shape-following (see
    module docstring) when `shape_by_trip`/`shape_points` are given, falling
    back to the haversine between GTFS stop coordinates otherwise.

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

    shape_by_trip = shape_by_trip or {}
    projector = _ShapeProjector(shape_points or {})

    fact_rows: list[dict] = []
    for trip_id, trip_visits in by_trip.items():
        shape_id = shape_by_trip.get(trip_id)
        for a, b in zip(trip_visits, trip_visits[1:]):
            coords_a = stop_coords.get(a.stop_id)
            coords_b = stop_coords.get(b.stop_id)
            if coords_a is None or coords_b is None:
                continue
            transit_s = b.arrival_ts - a.departure_ts
            if transit_s <= 0 or transit_s > max_transit_s:
                continue
            haversine_distance_m = _haversine_m(*coords_a, *coords_b)
            distance_m = _shape_distance_m(
                projector, shape_id, a, b, coords_a, coords_b, haversine_distance_m
            )
            if distance_m is None:
                distance_m = haversine_distance_m
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
