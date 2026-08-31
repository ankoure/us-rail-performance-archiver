# dashboard/api/services/trip_metrics.py

"""Travel times and headways between an ordered pair of stops.

Both are derived from the events mart (`compute_events` in analysis/metrics.py),
which is one ARR/DEP row per stop visit — the same shape TransitMatters' gobble
CSVs use, and the reason a stop-pair question is cheap to answer: the scan is
pruned to the two stops involved, not the whole route.

A rider's trip from A to B is one vehicle's run, so travel time is that
vehicle's DEP at A to its own ARR at B, matched on trip_id. Headway is the gap
between successive vehicles arriving at A, matching how analysis/metrics.py
already defines it (`_headways`, off arrival times) so the two agree.

Deliberately not computed here: dwell time. The events mart takes ARR and DEP
from the same inferred visit, and for feeds whose visits come from vehicle
positions the poll cadence is coarser than a real dwell -- MBTA's Red Line
reports a zero dwell at 95 of 99 stops, and the handful of non-zero ones are
terminal layovers. A dwell chart there would be reporting the polling interval.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date

from api.services import data


def _pct(values: list[int], q: int) -> int | None:
    """The q-th percentile, rounded. Mirrors analysis/metrics.py::_pct so the
    numbers here line up with the stop_day/route_day marts."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return round(statistics.quantiles(values, n=100, method="inclusive")[q - 1])


def _headways_by_day(
    arrivals: dict[str, list[int]],
) -> dict[str, dict[int, int]]:
    """Gap to the previous arrival at the origin stop, keyed by service date
    then by arrival timestamp, so a run can look up its own wait."""
    out: dict[str, dict[int, int]] = {}
    for service_date, times in arrivals.items():
        ordered = sorted(times)
        out[service_date] = {
            later: later - earlier for earlier, later in zip(ordered, ordered[1:])
        }
    return out


def compute(
    feed_names: list[str],
    route_id: str,
    from_stop_id: str,
    to_stop_id: str,
    start_date: date,
    end_date: date,
    direction_id: int | None = None,
) -> dict:
    """Per-run travel times plus per-day aggregates for one ordered stop pair.

    Returns both shapes; the router hands the caller whichever it asked for.
    """
    table = data.read_kind(
        "events",
        feed_names,
        start_date,
        end_date,
        filters={"route_id": [route_id], "stop_id": [from_stop_id, to_stop_id]},
    )

    # Earliest departure from the origin and, separately, every arrival at the
    # destination -- a loop route can visit a stop twice on one trip, so the
    # destination arrival is chosen below as the first one *after* the origin
    # departure rather than just the earliest.
    departures: dict[tuple[str, str], int] = {}
    arrivals: dict[tuple[str, str], list[int]] = defaultdict(list)
    origin_arrivals: dict[str, list[int]] = defaultdict(list)
    meta: dict[tuple[str, str], tuple[str | None, int | None]] = {}

    for row in table.to_pylist():
        if direction_id is not None and row["direction_id"] != direction_id:
            continue
        key = (row["service_date"], row["trip_id"])
        stop, event, ts = row["stop_id"], row["event_type"], row["event_unix"]

        if stop == from_stop_id:
            if event == "DEP":
                current = departures.get(key)
                if current is None or ts < current:
                    departures[key] = ts
                    meta[key] = (row["vehicle_id"], row["direction_id"])
            elif event == "ARR":
                origin_arrivals[row["service_date"]].append(ts)
        elif stop == to_stop_id and event == "ARR":
            arrivals[key].append(ts)

    headways = _headways_by_day(origin_arrivals)

    runs: list[dict] = []
    for key, departure in departures.items():
        service_date, trip_id = key
        later = [ts for ts in arrivals.get(key, ()) if ts > departure]
        if not later:
            # The vehicle never reached the destination in this window, or the
            # pair was given in the wrong order for this direction.
            continue
        arrival = min(later)
        vehicle_id, row_direction = meta[key]
        runs.append(
            {
                "service_date": service_date,
                "trip_id": trip_id,
                "vehicle_id": vehicle_id,
                "direction_id": row_direction,
                "departure_unix": departure,
                "arrival_unix": arrival,
                "travel_time_s": arrival - departure,
                "headway_s": headways.get(service_date, {}).get(departure),
            }
        )
    runs.sort(key=lambda r: r["departure_unix"])

    by_date: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        by_date[run["service_date"]].append(run)

    days: list[dict] = []
    for service_date in sorted(by_date):
        rows = by_date[service_date]
        travel = sorted(r["travel_time_s"] for r in rows)
        waits = sorted(r["headway_s"] for r in rows if r["headway_s"] is not None)
        days.append(
            {
                "service_date": service_date,
                "trip_count": len(rows),
                "travel_time_p10_s": _pct(travel, 10),
                "travel_time_p50_s": _pct(travel, 50),
                "travel_time_p90_s": _pct(travel, 90),
                "headway_p50_s": _pct(waits, 50),
                "headway_p90_s": _pct(waits, 90),
            }
        )

    return {
        "from_stop_id": from_stop_id,
        "to_stop_id": to_stop_id,
        "route_id": route_id,
        "direction_id": direction_id,
        "runs": runs,
        "days": days,
    }
