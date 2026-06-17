"""Gold tier v2: on-time performance (schedule adherence) from a Visit stream.

Where `metrics.py` is deliberately schedule-free, this module is the GTFS-join
half: it compares each realtime Visit against the *scheduled* arrival/departure
for its (trip_id, stop_id) and folds the result into three marts:

  adherence      — trip-stop fact grain: one row per matched visit, carrying both
                   actual and scheduled times plus arrival/departure delays and
                   the on-time/early/late verdict. The OTP analog of `events`.
  stop_day_otp   — one row per (route_id, direction_id, stop_id, service_date).
  route_day_otp  — one row per (route_id, direction_id, service_date).

Matching follows `static_gtfs.enrich_events`: the join key is (trip_id, stop_id),
not the realtime time — using the observed time would attribute a neighbouring
trip's schedule whenever a vehicle ran early or late. route_id / direction_id /
stop_sequence are taken from the *schedule* (authoritative), so OTP also covers
trip_id-only feeds (NYCT subway, TriMet) that omit them in their realtime payload.

Definitions (the on-time window is the caller's policy; defaults below):
  arrival_delay_s   = observed arrival_ts - scheduled arrival (seconds; +late/-early)
  status            = early  if arrival_delay_s < -early_threshold_s
                      late   if arrival_delay_s >  late_threshold_s
                      on_time otherwise
                      (classified on arrival; falls back to departure only when a
                       stop has no scheduled arrival_time)
  on_time_pct       = on_time_count / matched_count  (denominator = matched trips)

`compute_adherence` is pure and pandas-free — it takes a pre-built `schedules`
lookup (see `schedule_index`), so it unit-tests without any GTFS files. `gold.py`
owns resolving the StaticGtfs snapshot and the atomic parquet write.

Scheduled times are anchored to the trip's *service day*, not the wall-clock date
of the ping: a visit's local arrival date is tried first, then the prior day, so
an owl trip scheduled as e.g. 25:30:00 on the previous service day still matches.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, NamedTuple
from zoneinfo import ZoneInfo

import pyarrow as pa

from analysis.metrics import _pct, _round
from analysis.timeutil import service_date as _service_date
from analysis.vehicle_day import Visit

if TYPE_CHECKING:  # pandas-backed; only imported for type hints, never at load.
    from analysis.static_gtfs import StaticGtfs

DEFAULT_EARLY_THRESHOLD_S = 60
DEFAULT_LATE_THRESHOLD_S = 300

# Partition keys (feed, year, month, day) live in the path only, matching the
# silver + schedule-free gold layout; rows still carry `feed` for in-memory use.
ADHERENCE_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("stop_id", pa.string(), nullable=False),
        pa.field("stop_sequence", pa.int32(), nullable=True),
        pa.field("trip_id", pa.string(), nullable=False),
        pa.field("vehicle_id", pa.string(), nullable=True),
        pa.field("route_mode", pa.string(), nullable=True),
        pa.field("service_date", pa.string(), nullable=False),
        pa.field("arrival_unix", pa.int64(), nullable=False),
        pa.field("scheduled_arrival_unix", pa.int64(), nullable=True),
        pa.field("arrival_delay_s", pa.int32(), nullable=True),
        pa.field("departure_unix", pa.int64(), nullable=False),
        pa.field("scheduled_departure_unix", pa.int64(), nullable=True),
        pa.field("departure_delay_s", pa.int32(), nullable=True),
        pa.field("status", pa.string(), nullable=False),  # early | on_time | late
        pa.field("on_time", pa.bool_(), nullable=False),
    ]
)

_AGG_FIELDS = [
    pa.field("matched_count", pa.int32(), nullable=False),
    pa.field("on_time_count", pa.int32(), nullable=False),
    pa.field("early_count", pa.int32(), nullable=False),
    pa.field("late_count", pa.int32(), nullable=False),
    pa.field("on_time_pct", pa.float64(), nullable=True),
    pa.field("arr_delay_p50_s", pa.int32(), nullable=True),
    pa.field("arr_delay_p90_s", pa.int32(), nullable=True),
    pa.field("arr_delay_mean_s", pa.float64(), nullable=True),
    pa.field("dep_delay_p50_s", pa.int32(), nullable=True),
]

STOP_DAY_OTP_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("stop_id", pa.string(), nullable=False),
        pa.field("service_date", pa.string(), nullable=False),
        *_AGG_FIELDS,
    ]
)

ROUTE_DAY_OTP_SCHEMA = pa.schema(
    [
        pa.field("route_id", pa.string(), nullable=False),
        pa.field("direction_id", pa.int8(), nullable=True),
        pa.field("service_date", pa.string(), nullable=False),
        pa.field("distinct_stop_count", pa.int32(), nullable=False),
        *_AGG_FIELDS,
    ]
)


class SchedRow(NamedTuple):
    """One scheduled stop. `*_seconds` are seconds from service-day midnight
    (GTFS allows ≥ 86400 for next-day continuations); -1 means absent."""

    arrival_seconds: int
    departure_seconds: int
    route_id: str | None
    direction_id: int | None
    stop_sequence: int | None


def _int_or_none(value) -> int | None:
    """Coerce a pandas cell (int, float, NaN, None) to int or None."""
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def schedule_index(
    static_gtfs: StaticGtfs, service_date: dt.date
) -> dict[tuple[str, str], SchedRow]:
    """Build a (trip_id, stop_id) → SchedRow lookup for one service day.

    Mirrors `static_gtfs.enrich_events`'s keep-first dedup. Reads the pandas
    frame returned by `static_gtfs.scheduled_stops` but imports no pandas itself,
    so this module stays import-light.
    """
    df = static_gtfs.scheduled_stops(service_date)
    index: dict[tuple[str, str], SchedRow] = {}
    if df.empty:
        return index
    for row in df.itertuples(index=False):
        trip_id, stop_id = row.trip_id, row.stop_id
        if not isinstance(trip_id, str) or not isinstance(stop_id, str):
            continue
        key = (trip_id, stop_id)
        if key in index:  # keep first, matching enrich_events
            continue
        arr = _int_or_none(row.arrival_seconds)
        dep = _int_or_none(row.departure_seconds)
        index[key] = SchedRow(
            arrival_seconds=-1 if arr is None else arr,
            departure_seconds=-1 if dep is None else dep,
            route_id=row.route_id if isinstance(row.route_id, str) else None,
            direction_id=_int_or_none(row.direction_id),
            stop_sequence=_int_or_none(row.stop_sequence),
        )
    return index


def _service_midnight_unix(sd: dt.date, tz: ZoneInfo) -> int:
    """Unix seconds for the GTFS reference 'midnight' (noon - 12h) of `sd`.

    Anchoring on noon-12h rather than literal local midnight makes the +seconds
    arithmetic correct across DST transition days, per the GTFS time convention.
    """
    noon = dt.datetime.combine(sd, dt.time(12), tzinfo=tz)
    return int(noon.timestamp()) - 12 * 3600


def _classify(delay_s: int, early_threshold_s: int, late_threshold_s: int) -> str:
    if delay_s < -early_threshold_s:
        return "early"
    if delay_s > late_threshold_s:
        return "late"
    return "on_time"


def compute_adherence(
    visits: Iterable[Visit],
    schedules: Mapping[dt.date, Mapping[tuple[str, str], SchedRow]],
    feed: str,
    local_tz: ZoneInfo,
    *,
    early_threshold_s: int = DEFAULT_EARLY_THRESHOLD_S,
    late_threshold_s: int = DEFAULT_LATE_THRESHOLD_S,
    route_modes: Mapping[str, str] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fold visits into (adherence_rows, stop_day_otp_rows, route_day_otp_rows).

    `schedules` maps a service date to its (trip_id, stop_id) → SchedRow index.
    A visit with no trip_id, or no schedule match on its arrival date or the
    prior day, is dropped (it can't be scored). Aggregates are over matched
    visits only. Rows are dicts keyed exactly by the module schemas.
    """
    modes = route_modes or {}
    fact_rows: list[dict] = []
    for v in visits:
        if not v.trip_id:
            continue
        # trip_id alone is ambiguous across service days: a recurring trip and
        # its own post-midnight continuation can share an id. Among the candidate
        # days that schedule this (trip_id, stop_id), take the *nearest*
        # occurrence — smallest |delay| — so an owl visit just after midnight
        # anchors to the prior service day rather than today's same-id trip
        # (which would read as a spurious ~24 h error).
        arr_date = _service_date(v.arrival_ts, local_tz)
        best = None  # (abs_basis, sd, hit, sched_arr, sched_dep, arr_delay, dep_delay)
        for sd in (arr_date, arr_date - dt.timedelta(days=1)):
            idx = schedules.get(sd)
            if idx is None:
                continue
            hit = idx.get((v.trip_id, v.stop_id))
            if hit is None:
                continue
            anchor = _service_midnight_unix(sd, local_tz)
            s_arr = anchor + hit.arrival_seconds if hit.arrival_seconds >= 0 else None
            s_dep = (
                anchor + hit.departure_seconds if hit.departure_seconds >= 0 else None
            )
            a_delay = v.arrival_ts - s_arr if s_arr is not None else None
            d_delay = v.departure_ts - s_dep if s_dep is not None else None
            # Judge on arrival; fall back to departure only when a stop has no
            # scheduled arrival_time.
            basis = a_delay if a_delay is not None else d_delay
            if basis is None:
                continue
            cand = (abs(basis), sd, hit, s_arr, s_dep, a_delay, d_delay)
            if best is None or cand[0] < best[0]:
                best = cand
        if best is None:
            continue
        _, matched_sd, sched, sched_arr, sched_dep, arr_delay, dep_delay = best

        route_id = sched.route_id or v.route_id
        if not route_id:  # route-keyed marts are meaningless without it
            continue
        basis = arr_delay if arr_delay is not None else dep_delay
        status = _classify(basis, early_threshold_s, late_threshold_s)

        direction_id = (
            sched.direction_id if sched.direction_id is not None else v.direction_id
        )
        stop_sequence = (
            sched.stop_sequence if sched.stop_sequence is not None else v.stop_sequence
        )
        fact_rows.append(
            {
                "feed": feed,
                "route_id": route_id,
                "direction_id": direction_id,
                "stop_id": v.stop_id,
                "stop_sequence": stop_sequence,
                "trip_id": v.trip_id,
                "vehicle_id": v.vehicle_id or None,
                "route_mode": modes.get(route_id),
                "service_date": matched_sd.isoformat(),
                "arrival_unix": v.arrival_ts,
                "scheduled_arrival_unix": sched_arr,
                "arrival_delay_s": arr_delay,
                "departure_unix": v.departure_ts,
                "scheduled_departure_unix": sched_dep,
                "departure_delay_s": dep_delay,
                "status": status,
                "on_time": status == "on_time",
            }
        )

    stop_rows = _aggregate(
        fact_rows, ("route_id", "direction_id", "stop_id", "service_date"), feed
    )
    route_rows = _aggregate(
        fact_rows, ("route_id", "direction_id", "service_date"), feed, with_stops=True
    )
    return fact_rows, stop_rows, route_rows


def _aggregate(
    fact_rows: list[dict],
    keys: tuple[str, ...],
    feed: str,
    *,
    with_stops: bool = False,
) -> list[dict]:
    """Roll matched fact rows up to a grain defined by `keys`.

    `with_stops` adds distinct_stop_count (the route grain drops stop_id from the
    key, so it needs the count back as a column).
    """
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for r in fact_rows:
        buckets[tuple(r[k] for k in keys)].append(r)

    out: list[dict] = []
    for key, rows in buckets.items():
        arr_delays = [
            r["arrival_delay_s"] for r in rows if r["arrival_delay_s"] is not None
        ]
        dep_delays = [
            r["departure_delay_s"] for r in rows if r["departure_delay_s"] is not None
        ]
        matched = len(rows)
        on_time = sum(1 for r in rows if r["status"] == "on_time")
        row: dict = {"feed": feed, **dict(zip(keys, key))}
        if with_stops:
            row["distinct_stop_count"] = len({r["stop_id"] for r in rows})
        row.update(
            {
                "matched_count": matched,
                "on_time_count": on_time,
                "early_count": sum(1 for r in rows if r["status"] == "early"),
                "late_count": sum(1 for r in rows if r["status"] == "late"),
                "on_time_pct": on_time / matched if matched else None,
                "arr_delay_p50_s": _round(_pct(arr_delays, 50)),
                "arr_delay_p90_s": _round(_pct(arr_delays, 90)),
                "arr_delay_mean_s": statistics.fmean(arr_delays)
                if arr_delays
                else None,
                "dep_delay_p50_s": _round(_pct(dep_delays, 50)),
            }
        )
        out.append(row)
    return out
