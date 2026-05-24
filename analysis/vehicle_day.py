"""Per-day, per-feed vehicle motion + dwell analysis.

Loads the curated `vehicles` parquet for one (feed, date) and exposes three
collaborating objects:

  VehicleDay  — owns I/O and grouping; yields Vehicles and Stops.
  Vehicle     — one per vehicle_id; detects its own dwell visits from its
                ordered ping sequence.
  Stop        — one per stop_id; aggregates Visits from every Vehicle that
                stopped there. Answers headway/dwell questions.

Visit is the unit of currency that flows between them.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

DEFAULT_CURATED_DIR = Path("curated")

# Curated parquet uses dotted protobuf paths. Map to the names we use internally.
_COLUMN_MAP = {
    "vehicle.vehicle.id": "vehicle_id",
    "vehicle.trip.trip_id": "trip_id",
    "vehicle.trip.route_id": "route_id",
    "vehicle.trip.direction_id": "direction_id",
    "vehicle.trip.start_date": "start_date",
    "vehicle.stop_id": "stop_id",
    "vehicle.current_status": "current_status",
    "vehicle.current_stop_sequence": "stop_sequence",
    "vehicle.timestamp": "vehicle_timestamp",
    "feed_timestamp": "feed_timestamp",
    "latitude": "latitude",
    "longitude": "longitude",
}


@dataclass(frozen=True, slots=True)
class Visit:
    """A single dwell of one vehicle at one stop.

    arrival_ts and departure_ts are the first and last STOPPED_AT pings in the
    run; for a single-ping visit they are equal.
    """

    vehicle_id: str
    stop_id: str
    arrival_ts: int
    departure_ts: int
    ping_count: int
    route_id: str | None = None
    trip_id: str | None = None
    direction_id: int | None = None
    stop_sequence: int | None = None

    @property
    def duration_s(self) -> int:
        return self.departure_ts - self.arrival_ts


DEFAULT_MERGE_GAP_S = 60


class Vehicle:
    """One physical vehicle's ping sequence for the day."""

    def __init__(
        self,
        vehicle_id: str,
        rows: list[dict],
        merge_gap_seconds: int = DEFAULT_MERGE_GAP_S,
    ) -> None:
        self.vehicle_id = vehicle_id
        # Rows already sorted by VehicleDay before construction.
        self._rows = rows
        self.merge_gap_seconds = merge_gap_seconds

    def __repr__(self) -> str:
        return f"Vehicle({self.vehicle_id!r}, pings={len(self._rows)})"

    @property
    def ping_count(self) -> int:
        return len(self._rows)

    @property
    def first_seen_ts(self) -> int | None:
        return self._rows[0]["vehicle_timestamp"] if self._rows else None

    @property
    def last_seen_ts(self) -> int | None:
        return self._rows[-1]["vehicle_timestamp"] if self._rows else None

    @cached_property
    def trip_ids(self) -> list[str]:
        seen: list[str] = []
        last: str | None = None
        for r in self._rows:
            tid = r.get("trip_id")
            if tid and tid != last:
                seen.append(tid)
                last = tid
        return seen

    @cached_property
    def dwells(self) -> list[Visit]:
        """Detected dwell visits, with brief flickers merged.

        Two-pass: first find raw runs of consecutive STOPPED_AT pings at the
        same stop_id, then merge adjacent visits at the same stop separated by
        a gap <= self.merge_gap_seconds. The merge collapses brief
        STOPPED_AT → IN_TRANSIT_TO → STOPPED_AT flickers (common when a train
        edges forward at a terminal) into a single visit.
        """
        return merge_close_visits(self._raw_dwells, self.merge_gap_seconds)

    @cached_property
    def _raw_dwells(self) -> list[Visit]:
        """Detect STOPPED_AT runs and emit one Visit per run, no merging.

        A run is consecutive pings (in vehicle_timestamp order) where
        current_status == 'STOPPED_AT' AND stop_id is the same. Any change in
        status or stop_id closes the current run.
        """
        visits: list[Visit] = []
        run: list[dict] = []

        def flush() -> None:
            if not run:
                return
            first, last = run[0], run[-1]
            visits.append(
                Visit(
                    vehicle_id=self.vehicle_id,
                    stop_id=first["stop_id"],
                    arrival_ts=first["vehicle_timestamp"],
                    departure_ts=last["vehicle_timestamp"],
                    ping_count=len(run),
                    route_id=first.get("route_id"),
                    trip_id=first.get("trip_id"),
                    direction_id=first.get("direction_id"),
                    stop_sequence=first.get("stop_sequence"),
                )
            )

        for row in self._rows:
            stopped = row.get("current_status") == "STOPPED_AT"
            stop_id = row.get("stop_id")
            if stopped and stop_id is not None:
                if run and run[-1]["stop_id"] == stop_id:
                    run.append(row)
                else:
                    flush()
                    run = [row]
            else:
                flush()
                run = []
        flush()
        return visits


def merge_close_visits(visits: list[Visit], gap_seconds: int) -> list[Visit]:
    """Collapse adjacent same-stop visits separated by <= gap_seconds.

    Operates on a list of visits from one vehicle, sorted by arrival_ts (which
    `_raw_dwells` produces naturally). The merged visit keeps the first
    visit's identity fields (route/trip/direction/stop_sequence) and extends
    departure_ts to the last merged visit's departure_ts.

    A gap_seconds <= 0 is a no-op.
    """
    if gap_seconds <= 0 or len(visits) < 2:
        return list(visits)
    merged: list[Visit] = [visits[0]]
    for v in visits[1:]:
        prev = merged[-1]
        same_stop = prev.stop_id == v.stop_id
        gap = v.arrival_ts - prev.departure_ts
        if same_stop and 0 <= gap <= gap_seconds:
            merged[-1] = Visit(
                vehicle_id=prev.vehicle_id,
                stop_id=prev.stop_id,
                arrival_ts=prev.arrival_ts,
                departure_ts=v.departure_ts,
                ping_count=prev.ping_count + v.ping_count,
                route_id=prev.route_id,
                trip_id=prev.trip_id,
                direction_id=prev.direction_id,
                stop_sequence=prev.stop_sequence,
            )
        else:
            merged.append(v)
    return merged


class Stop:
    """One stop's aggregated visits across all vehicles that stopped there."""

    def __init__(self, stop_id: str, visits: list[Visit]) -> None:
        self.stop_id = stop_id
        # Sort by arrival so headway diffs are well-defined.
        self.visits = sorted(visits, key=lambda v: v.arrival_ts)

    def __repr__(self) -> str:
        return f"Stop({self.stop_id!r}, visits={len(self.visits)})"

    def _filtered(self, route_id: str | None) -> list[Visit]:
        if route_id is None:
            return self.visits
        return [v for v in self.visits if v.route_id == route_id]

    @property
    def visit_count(self) -> int:
        return len(self.visits)

    @cached_property
    def routes_served(self) -> set[str]:
        return {v.route_id for v in self.visits if v.route_id is not None}

    def headways(self, route_id: str | None = None) -> list[int]:
        """Inter-arrival times in seconds between successive visits.

        With route_id=None, mixes all routes serving the stop (rarely what you
        want at multi-route stations). Pass a route_id for the per-line headway.
        """
        vs = self._filtered(route_id)
        return [b.arrival_ts - a.arrival_ts for a, b in zip(vs, vs[1:])]

    def dwell_durations(self, route_id: str | None = None) -> list[int]:
        return [v.duration_s for v in self._filtered(route_id)]


class VehicleDay:
    """Loads one (feed, date) parquet partition and yields Vehicles / Stops."""

    def __init__(
        self,
        feed: str,
        date: dt.date | str,
        base_dir: Path | str = DEFAULT_CURATED_DIR,
        merge_gap_seconds: int = DEFAULT_MERGE_GAP_S,
    ) -> None:
        self.feed = feed
        self.date = _coerce_date(date)
        self.base_dir = Path(base_dir)
        self.merge_gap_seconds = merge_gap_seconds

    def __repr__(self) -> str:
        return f"VehicleDay(feed={self.feed!r}, date={self.date.isoformat()})"

    @property
    def partition_path(self) -> Path:
        return (
            self.base_dir
            / "vehicles"
            / f"feed={self.feed}"
            / f"year={self.date.year}"
            / f"month={self.date.month}"
            / f"day={self.date.day}"
        )

    @cached_property
    def _rows_by_vehicle(self) -> dict[str, list[dict]]:
        """Load parquet, normalize column names, group rows by vehicle_id.

        Each group is sorted ascending by vehicle_timestamp so Vehicle can rely
        on ordering. Rows with no vehicle_id or no vehicle_timestamp are dropped.
        """
        path = self.partition_path
        if not path.exists():
            raise FileNotFoundError(f"No partition at {path}")
        files = sorted(path.glob("*.parquet"))
        if not files:
            raise FileNotFoundError(f"No .parquet files under {path}")

        table = (
            pq.read_table(files[0])
            if len(files) == 1
            else pa.concat_tables([pq.read_table(f) for f in files])
        )

        present = [c for c in _COLUMN_MAP if c in table.column_names]
        df = (
            table.select(present)
            .rename_columns([_COLUMN_MAP[c] for c in present])
            .to_pylist()
        )

        grouped: dict[str, list[dict]] = defaultdict(list)
        for r in df:
            vid = r.get("vehicle_id")
            ts = r.get("vehicle_timestamp")
            if vid is None or ts is None:
                continue
            grouped[vid].append(r)

        for vid in grouped:
            grouped[vid].sort(key=lambda r: r["vehicle_timestamp"])
        return dict(grouped)

    @cached_property
    def vehicles(self) -> list[Vehicle]:
        return [
            Vehicle(vid, rows, merge_gap_seconds=self.merge_gap_seconds)
            for vid, rows in self._rows_by_vehicle.items()
        ]

    @cached_property
    def stops(self) -> list[Stop]:
        by_stop: dict[str, list[Visit]] = defaultdict(list)
        for v in self.vehicles:
            for visit in v.dwells:
                by_stop[visit.stop_id].append(visit)
        return [Stop(sid, visits) for sid, visits in by_stop.items()]

    def vehicle(self, vehicle_id: str) -> Vehicle:
        rows = self._rows_by_vehicle.get(vehicle_id)
        if rows is None:
            raise KeyError(vehicle_id)
        return Vehicle(vehicle_id, rows, merge_gap_seconds=self.merge_gap_seconds)

    def stop(self, stop_id: str) -> Stop:
        visits = [
            visit
            for v in self.vehicles
            for visit in v.dwells
            if visit.stop_id == stop_id
        ]
        if not visits:
            raise KeyError(stop_id)
        return Stop(stop_id, visits)

    @property
    def vehicle_count(self) -> int:
        return len(self._rows_by_vehicle)

    @property
    def ping_count(self) -> int:
        return sum(len(rs) for rs in self._rows_by_vehicle.values())


def _coerce_date(d: dt.date | str) -> dt.date:
    if isinstance(d, dt.date):
        return d
    return dt.date.fromisoformat(d)
