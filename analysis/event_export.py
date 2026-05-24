"""Export per-stop ARR/DEP events in TransitMatters gobble's CSV format.

Each Visit becomes two rows: one ARR at arrival_ts, one DEP at departure_ts.
Rows are grouped by (route_id, direction_id, stop_id, local-service-date)
and written to:

    {base_dir}/events/feed={feed}/{route}-{dir}-{stop}/Year=YYYY/Month=M/Day=D/events.csv

Schema matches gobble's CSV_FIELDS exactly. Fields we can't fill from realtime
alone (scheduled_headway, scheduled_tt) are emitted as empty cells. Existing
CSV files for a (stop, day) are overwritten — this is a batch export from
complete-day parquet, not the streaming-append model gobble uses.
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

from analysis.static_gtfs import StaticGtfs
from analysis.vehicle_day import VehicleDay, Visit

CSV_FIELDS = [
    "service_date",
    "route_id",
    "trip_id",
    "direction_id",
    "stop_id",
    "stop_sequence",
    "vehicle_id",
    "vehicle_label",
    "event_type",
    "event_time",
    "scheduled_headway",
    "scheduled_tt",
    "vehicle_consist",
    "occupancy_status",
    "occupancy_percentage",
]


def export_events_csv(
    day: VehicleDay,
    local_tz: ZoneInfo,
    base_dir: Path | str = "curated",
    gtfs: StaticGtfs | None = None,
) -> tuple[int, int]:
    """Expand visits to ARR/DEP events and write per-stop CSVs.

    Returns (rows_written, files_written).

    If `gtfs` is provided, each event row is enriched with scheduled_headway
    and scheduled_tt looked up from the static GTFS by (route, dir, stop) and
    event-time-of-day, matching gobble's semantics.
    """
    base = Path(base_dir)

    # Bucket events by (route_id, direction_id, stop_id, local_service_date).
    # Skip visits without enough identity to group on.
    buckets: dict[tuple[str, int, str, dt.date], list[dict]] = defaultdict(list)

    for vehicle in day.vehicles:
        for visit in vehicle.dwells:
            if not visit.route_id or visit.direction_id is None:
                continue
            for event in _visit_to_events(visit, local_tz):
                key = (
                    event["route_id"],
                    event["direction_id"],
                    event["stop_id"],
                    event["_service_date"],
                )
                buckets[key].append(event)

    # GTFS enrichment runs per service_date (one merge_asof per date is cheaper
    # than one per (route, dir, stop) bucket).
    if gtfs is not None:
        by_date: dict[dt.date, list[dict]] = defaultdict(list)
        for events in buckets.values():
            for ev in events:
                by_date[ev["_service_date"]].append(ev)
        for sd, evs in by_date.items():
            gtfs.enrich_events(evs, sd, local_tz)

    rows_written = 0
    files_written = 0
    for (route_id, direction_id, stop_id, service_date), events in buckets.items():
        events.sort(key=lambda e: e["event_time"])
        path = (
            base
            / "events"
            / f"feed={day.feed}"
            / f"{route_id}-{direction_id}-{stop_id}"
            / f"Year={service_date.year}"
            / f"Month={service_date.month}"
            / f"Day={service_date.day}"
            / "events.csv"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fd:
            writer = csv.DictWriter(fd, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for event in events:
                writer.writerow(event)
        rows_written += len(events)
        files_written += 1

    return rows_written, files_written


def _visit_to_events(visit: Visit, local_tz: ZoneInfo) -> list[dict]:
    """Two events per visit: ARR at arrival_ts, DEP at departure_ts."""
    base = _base_event(visit)
    return [
        {
            **base,
            "event_type": event_type,
            "event_time": _fmt_local(ts, local_tz),
            "service_date": _service_date(ts, local_tz).isoformat(),
            "_service_date": _service_date(ts, local_tz),
        }
        for event_type, ts in (("ARR", visit.arrival_ts), ("DEP", visit.departure_ts))
    ]


def _base_event(visit: Visit) -> dict:
    return {
        "route_id": visit.route_id,
        "trip_id": visit.trip_id,
        "direction_id": visit.direction_id,
        "stop_id": visit.stop_id,
        "stop_sequence": visit.stop_sequence,
        "vehicle_id": "0",  # gobble convention
        "vehicle_label": visit.vehicle_id,
        "scheduled_headway": "",
        "scheduled_tt": "",
        "vehicle_consist": visit.vehicle_id,
        "occupancy_status": "",
        "occupancy_percentage": "",
    }


def _service_date(unix_ts: int, local_tz: ZoneInfo) -> dt.date:
    return dt.datetime.fromtimestamp(unix_ts, tz=local_tz).date()


def _fmt_local(unix_ts: int, local_tz: ZoneInfo) -> str:
    """ISO-ish local-time string matching gobble: '2026-03-23 06:30:39-04:00'."""
    local = dt.datetime.fromtimestamp(unix_ts, tz=local_tz)
    return local.isoformat(sep=" ", timespec="seconds")
