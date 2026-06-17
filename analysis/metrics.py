"""Gold tier: daily performance-metrics marts from a Visit stream.

`compute_marts` folds an iterable of `Visit` (the common currency produced by
both `VehicleDay` and `TripUpdatesDay`) into two rider-facing daily marts:

  stop-day  — one row per (feed, route_id, direction_id, stop_id, service_date)
  route-day — one row per (feed, route_id, direction_id, service_date)

Everything here is schedule-free: headway, dwell, throughput, regularity and
service span are all computable from realtime alone. On-time performance (a
GTFS-timetable join) is deliberately out of scope for v1.

The module is pure and I/O-free — no parquet, no disk, no GTFS. `gold.py` owns
discovery, the source choice and the atomic parquet write; the schemas below
(`STOP_DAY_SCHEMA`, `ROUTE_DAY_SCHEMA`) are the contract between the two.

Null-handling:
  - a visit with no route_id is dropped (route-keyed marts are meaningless
    without it, and v1 does no GTFS backfill);
  - a visit with no direction_id is kept under a null direction bucket, so feeds
    that omit direction (NYCT subway, TriMet, ...) still get honest aggregate
    rows. Direction split arrives in v2 with GTFS.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable
from zoneinfo import ZoneInfo

import pyarrow as pa

from analysis.timeutil import service_date as _service_date
from analysis.vehicle_day import Visit

# The in-file schemas deliberately omit the hive partition keys (feed, year,
# month, day) — those live only in the path, matching the silver layout. Storing
# `feed` in-file too would collide (string vs path-derived dictionary) on a
# dataset read. `compute_marts` still returns `feed` in each row dict for
# in-memory use; `from_pylist(rows, schema=...)` ignores that extra key on write.
STOP_DAY_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("stop_id", pa.string(), nullable=False),
        pa.field("service_date", pa.string(), nullable=False),
        pa.field("visit_count", pa.int32(), nullable=False),
        pa.field("trip_count", pa.int32(), nullable=False),
        pa.field("distinct_vehicle_count", pa.int32(), nullable=False),
        pa.field("headway_p50_s", pa.int32(), nullable=True),
        pa.field("headway_p90_s", pa.int32(), nullable=True),
        pa.field("headway_mean_s", pa.float64(), nullable=True),
        pa.field("headway_cov", pa.float64(), nullable=True),
        pa.field("dwell_p50_s", pa.int32(), nullable=True),
        pa.field("dwell_p90_s", pa.int32(), nullable=True),
        pa.field("first_service_unix", pa.int64(), nullable=False),
        pa.field("last_service_unix", pa.int64(), nullable=False),
        pa.field("service_span_s", pa.int32(), nullable=False),
    ]
)

ROUTE_DAY_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("service_date", pa.string(), nullable=False),
        pa.field("visit_count", pa.int32(), nullable=False),
        pa.field("trip_count", pa.int32(), nullable=False),
        pa.field("distinct_vehicle_count", pa.int32(), nullable=False),
        pa.field("distinct_stop_count", pa.int32(), nullable=False),
        pa.field("headway_p50_s", pa.int32(), nullable=True),
        pa.field("dwell_p50_s", pa.int32(), nullable=True),
        pa.field("dwell_p90_s", pa.int32(), nullable=True),
        pa.field("first_service_unix", pa.int64(), nullable=False),
        pa.field("last_service_unix", pa.int64(), nullable=False),
        pa.field("service_span_s", pa.int32(), nullable=False),
    ]
)

# The event-grain mart: one row per ARR/DEP, every route in one partition. The
# columnar, single-file-per-day counterpart to the gobble events/*.csv tree.
EVENTS_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("stop_id", pa.string(), nullable=False),
        pa.field("stop_sequence", pa.int32(), nullable=True),
        pa.field("trip_id", pa.string(), nullable=True),
        pa.field("vehicle_id", pa.string(), nullable=True),
        pa.field("event_type", pa.string(), nullable=False),  # "ARR" | "DEP"
        pa.field("event_unix", pa.int64(), nullable=False),
        pa.field("service_date", pa.string(), nullable=False),
    ]
)


def _pct(values: list[int], q: int) -> float | None:
    """The q-th percentile (q in 1..99) by linear interpolation, or None.

    Empty -> None; a single value -> that value (every percentile collapses to
    it). Otherwise `statistics.quantiles(n=100, inclusive)`, whose i-th cut
    point is the i-th percentile.
    """
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


def _round(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def _cov(values: list[int]) -> float | None:
    """Coefficient of variation (pstdev/mean) — a unitless regularity score.

    Needs >= 2 samples and a non-zero mean; otherwise None. 0.0 means perfectly
    even spacing; higher means burstier service.
    """
    if len(values) < 2:
        return None
    mean = statistics.fmean(values)
    if mean == 0:
        return None
    return statistics.pstdev(values) / mean


def _headways(visits: list[Visit]) -> list[int]:
    """Inter-arrival seconds between successive visits, sorted by arrival.

    Same definition as `Stop.headways`, but applied to the (route, direction)-
    filtered bucket we build here — `Stop` filters by route only.
    """
    vs = sorted(visits, key=lambda v: v.arrival_ts)
    return [b.arrival_ts - a.arrival_ts for a, b in zip(vs, vs[1:])]


def _distinct(values: Iterable) -> int:
    """Count of distinct truthy values (drops None and empty strings)."""
    return len({v for v in values if v})


def compute_marts(
    visits: Iterable[Visit],
    feed: str,
    local_tz: ZoneInfo,
) -> tuple[list[dict], list[dict]]:
    """Fold visits into (stop_day_rows, route_day_rows).

    Rows are plain dicts keyed exactly by STOP_DAY_SCHEMA / ROUTE_DAY_SCHEMA so
    a caller can `pa.Table.from_pylist(rows, schema=...)` directly. Visits with
    no route_id are skipped; null direction_id is preserved as a null bucket.
    """
    stop_buckets: dict[tuple, list[Visit]] = defaultdict(list)
    route_buckets: dict[tuple, list[Visit]] = defaultdict(list)
    for v in visits:
        if not v.route_id:
            continue
        sd = _service_date(v.arrival_ts, local_tz)
        stop_buckets[(v.route_id, v.direction_id, v.stop_id, sd)].append(v)
        route_buckets[(v.route_id, v.direction_id, sd)].append(v)

    # Per-stop headway medians, gathered per route bucket so route-day headway
    # can summarise as the median of its stops' medians (pooling raw inter-
    # arrivals across stops mixes unrelated cadences and is misleading).
    stop_p50_by_route: dict[tuple, list[int]] = defaultdict(list)

    stop_rows: list[dict] = []
    for (route_id, direction_id, stop_id, sd), bucket in stop_buckets.items():
        headways = _headways(bucket)
        dwells = [v.duration_s for v in bucket]
        first = min(v.arrival_ts for v in bucket)
        last = max(v.departure_ts for v in bucket)
        hw_p50 = _round(_pct(headways, 50))
        stop_rows.append(
            {
                "feed": feed,
                "route_id": route_id,
                "direction_id": direction_id,
                "stop_id": stop_id,
                "service_date": sd.isoformat(),
                "visit_count": len(bucket),
                "trip_count": _distinct(v.trip_id for v in bucket),
                "distinct_vehicle_count": _distinct(v.vehicle_id for v in bucket),
                "headway_p50_s": hw_p50,
                "headway_p90_s": _round(_pct(headways, 90)),
                "headway_mean_s": statistics.fmean(headways) if headways else None,
                "headway_cov": _cov(headways),
                "dwell_p50_s": _round(_pct(dwells, 50)),
                "dwell_p90_s": _round(_pct(dwells, 90)),
                "first_service_unix": first,
                "last_service_unix": last,
                "service_span_s": last - first,
            }
        )
        if hw_p50 is not None:
            stop_p50_by_route[(route_id, direction_id, sd)].append(hw_p50)

    route_rows: list[dict] = []
    for (route_id, direction_id, sd), bucket in route_buckets.items():
        dwells = [v.duration_s for v in bucket]
        first = min(v.arrival_ts for v in bucket)
        last = max(v.departure_ts for v in bucket)
        p50s = stop_p50_by_route.get((route_id, direction_id, sd))
        route_rows.append(
            {
                "feed": feed,
                "route_id": route_id,
                "direction_id": direction_id,
                "service_date": sd.isoformat(),
                "visit_count": len(bucket),
                "trip_count": _distinct(v.trip_id for v in bucket),
                "distinct_vehicle_count": _distinct(v.vehicle_id for v in bucket),
                "distinct_stop_count": _distinct(v.stop_id for v in bucket),
                "headway_p50_s": _round(statistics.median(p50s)) if p50s else None,
                "dwell_p50_s": _round(_pct(dwells, 50)),
                "dwell_p90_s": _round(_pct(dwells, 90)),
                "first_service_unix": first,
                "last_service_unix": last,
                "service_span_s": last - first,
            }
        )

    return stop_rows, route_rows


def compute_events(
    visits: Iterable[Visit],
    feed: str,
    local_tz: ZoneInfo,
) -> list[dict]:
    """Explode visits into ARR/DEP event rows for the events mart.

    Each Visit yields two rows: an ARR at arrival_ts and a DEP at departure_ts
    (equal timestamps for a single-ping visit). All routes share one table;
    `gold.py` writes it to one parquet per (feed, day). Same universe as
    `compute_marts` — visits with no route_id are dropped, null direction_id is
    kept. service_date is computed per event so a visit straddling local
    midnight files its two events on the dates they actually fall.

    Rows are sorted by event_unix so the partition is time-ordered.
    """
    rows: list[dict] = []
    for v in visits:
        if not v.route_id:
            continue
        for event_type, ts in (("ARR", v.arrival_ts), ("DEP", v.departure_ts)):
            rows.append(
                {
                    "feed": feed,
                    "route_id": v.route_id,
                    "direction_id": v.direction_id,
                    "stop_id": v.stop_id,
                    "stop_sequence": v.stop_sequence,
                    "trip_id": v.trip_id,
                    "vehicle_id": v.vehicle_id,
                    "event_type": event_type,
                    "event_unix": ts,
                    "service_date": _service_date(ts, local_tz).isoformat(),
                }
            )
    rows.sort(key=lambda r: r["event_unix"])
    return rows
