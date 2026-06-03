"""Derive ARR events from one day of MARTA prediction snapshots.

MARTA exposes a per-station countdown stream rather than vehicle positions —
each ping says "train T predicted to arrive at station S in N seconds." Real
arrivals are implicit: the (train_id, station, destination) tuple's
`waiting_seconds` walks down toward zero, then the tuple drops out of the
prediction stream when the train leaves the station.

This module reconstructs ARR events by, for each (train_id, station,
destination) group, taking the row with the smallest observed `waiting_seconds`
and treating its `next_arr` as the realized arrival time. Predictions that
never settled close to zero are filtered out (default cutoff 120s) to avoid
fabricating arrivals from stale long-horizon predictions at the data's edges.

DEP events aren't recoverable from prediction-only data, so we emit ARR only.
The output mirrors `analysis.event_export`'s gobble-format CSVs, landing under

    curated/events/feed={feed}/daily-rapid-data/{line}-{dir}-{station_slug}/Year=Y/Month=M/Day=D/events.csv
"""

from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from analysis.event_export import CSV_FIELDS

DEFAULT_CURATED_DIR = Path("curated")
DEFAULT_MAX_WAIT_FOR_ARRIVAL_S = 120
# When a (train, station, destination)'s predicted next_arr jumps by more than
# this many seconds between consecutive pings, treat it as a separate approach
# (the train is making another round trip through the station, not the same one).
DEFAULT_APPROACH_SPLIT_GAP_S = 600
# MARTA realtime DIRECTION values; map to GTFS-style 0/1.
DIRECTION_MAP = {"N": 0, "E": 0, "S": 1, "W": 1}


@dataclass(frozen=True, slots=True)
class MartaArrival:
    train_id: str
    station: str
    line: str
    destination: str
    direction: str  # raw N/S/E/W
    arrival_ts: int
    last_waiting_seconds: int  # smallest waiting_seconds observed for this group

    @property
    def direction_id(self) -> int | None:
        return DIRECTION_MAP.get(self.direction)


class MartaDay:
    """Loads one (feed, date) marta_predictions partition and derives ARR events."""

    def __init__(
        self,
        feed: str,
        date: dt.date | str,
        base_dir: Path | str = DEFAULT_CURATED_DIR,
        max_wait_for_arrival_s: int = DEFAULT_MAX_WAIT_FOR_ARRIVAL_S,
        approach_split_gap_s: int = DEFAULT_APPROACH_SPLIT_GAP_S,
    ) -> None:
        self.feed = feed
        self.date = _coerce_date(date)
        self.base_dir = Path(base_dir)
        self.max_wait_for_arrival_s = max_wait_for_arrival_s
        self.approach_split_gap_s = approach_split_gap_s

    def __repr__(self) -> str:
        return f"MartaDay(feed={self.feed!r}, date={self.date.isoformat()})"

    @property
    def partition_path(self) -> Path:
        return (
            self.base_dir
            / "marta_predictions"
            / f"feed={self.feed}"
            / f"year={self.date.year}"
            / f"month={self.date.month}"
            / f"day={self.date.day}"
        )

    @cached_property
    def _df(self) -> pd.DataFrame:
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
        return table.to_pandas()

    @cached_property
    def arrivals(self) -> list[MartaArrival]:
        """One ARR per detected approach, ordered by arrival time.

        Per (train_id, station, destination), pings are sorted by feed_timestamp
        and split into 'approaches' wherever `next_arr` jumps by more than
        `approach_split_gap_s` (default 600s). A jump indicates the train made
        another round trip through this station in the same direction. Within
        each approach, the ping with the smallest `waiting_seconds` is taken as
        the arrival; its `next_arr` is the arrival timestamp.

        Pings with empty train_id are dropped — MARTA emits these when the
        train assignment isn't known yet. Approaches whose smallest observed
        `waiting_seconds` exceeds `max_wait_for_arrival_s` are also dropped:
        the train probably never actually arrived.
        """
        df = self._df
        if df.empty:
            return []
        df = df.copy()
        df["train_id"] = df["train_id"].str.strip()
        df = df[df["train_id"] != ""]
        if df.empty:
            return []
        df = df.sort_values(["train_id", "station", "destination", "feed_timestamp"])

        arrivals: list[MartaArrival] = []
        group_cols = ["train_id", "station", "destination"]
        for _, group in df.groupby(group_cols, sort=False):
            arrivals.extend(self._approaches_in_group(group))
        arrivals.sort(key=lambda a: a.arrival_ts)
        return arrivals

    def _approaches_in_group(self, group: pd.DataFrame) -> list[MartaArrival]:
        """One (train, station, destination) group → one MartaArrival per approach."""
        next_arrs = group["next_arr"].to_numpy()
        waiting = group["waiting_seconds"].to_numpy()
        if len(next_arrs) == 0:
            return []
        # An approach boundary is where consecutive next_arr values diverge by more
        # than the split gap. That captures both "train cleared this stop and the
        # prediction moved to the next round trip" and "train was unscheduled for
        # a while and reappeared."
        starts = [0]
        for i in range(1, len(next_arrs)):
            if (
                abs(int(next_arrs[i]) - int(next_arrs[i - 1]))
                > self.approach_split_gap_s
            ):
                starts.append(i)
        ends = starts[1:] + [len(next_arrs)]

        out: list[MartaArrival] = []
        for s, e in zip(starts, ends):
            seg_wait = waiting[s:e]
            min_offset = int(seg_wait.argmin())
            min_wait = int(seg_wait[min_offset])
            if min_wait > self.max_wait_for_arrival_s:
                continue
            row = group.iloc[s + min_offset]
            out.append(
                MartaArrival(
                    train_id=row["train_id"],
                    station=row["station"],
                    line=row["line"],
                    destination=row["destination"],
                    direction=row["direction"],
                    arrival_ts=int(row["next_arr"]),
                    last_waiting_seconds=min_wait,
                )
            )
        return out

    @cached_property
    def trips(self) -> list["MartaTrip"]:
        """Segment a train's arrivals into directional trips by destination changes."""
        by_train: dict[str, list[MartaArrival]] = defaultdict(list)
        for a in self.arrivals:
            by_train[a.train_id].append(a)
        trips: list[MartaTrip] = []
        for train_id, train_arrivals in by_train.items():
            train_arrivals.sort(key=lambda a: a.arrival_ts)
            current: list[MartaArrival] = []
            current_dest: str | None = None
            trip_idx = 0
            for a in train_arrivals:
                if a.destination != current_dest and current:
                    trips.append(
                        MartaTrip(train_id, trip_idx, current_dest, tuple(current))
                    )
                    trip_idx += 1
                    current = []
                current.append(a)
                current_dest = a.destination
            if current:
                trips.append(
                    MartaTrip(train_id, trip_idx, current_dest, tuple(current))
                )
        return trips


@dataclass(frozen=True, slots=True)
class MartaTrip:
    """A directional run: a contiguous sequence of one train's arrivals to the same destination."""

    train_id: str
    trip_index: int  # 0, 1, 2, ... within this train's day
    destination: str
    arrivals: tuple[MartaArrival, ...]

    @property
    def trip_id(self) -> str:
        return f"{self.train_id}-{self.trip_index:03d}-{_slug(self.destination)}"


def export_marta_events_csv(
    day: MartaDay,
    local_tz: ZoneInfo,
    base_dir: Path | str = DEFAULT_CURATED_DIR,
) -> tuple[int, int]:
    """Write per-(line, dir, station, service_date) events.csv files.

    Returns (rows_written, files_written). MARTA Metrorail rides on its own
    static GTFS (mdb-368). Schedule enrichment is intentionally not wired up
    yet — MARTA's realtime trip_ids are synthesized here and won't match the
    GTFS trip_ids, so the direct (trip_id, stop_id) lookup that powers
    enrichment for GTFS-RT feeds won't fire.
    """
    base = Path(base_dir)
    buckets: dict[tuple[str, int, str, dt.date], list[dict]] = defaultdict(list)

    for trip in day.trips:
        for seq, arrival in enumerate(trip.arrivals, start=1):
            direction_id = arrival.direction_id
            if direction_id is None:
                continue
            service_date = _service_date(arrival.arrival_ts, local_tz)
            event = {
                "service_date": service_date.isoformat(),
                "route_id": arrival.line,
                "trip_id": trip.trip_id,
                "direction_id": direction_id,
                "stop_id": _slug(arrival.station),
                "stop_sequence": seq,
                "vehicle_id": "0",
                "vehicle_label": arrival.train_id,
                "event_type": "ARR",
                "event_time": _fmt_local(arrival.arrival_ts, local_tz),
                "scheduled_headway": "",
                "scheduled_tt": "",
                "vehicle_consist": arrival.train_id,
                "occupancy_status": "",
                "occupancy_percentage": "",
            }
            key = (
                event["route_id"],
                event["direction_id"],
                event["stop_id"],
                service_date,
            )
            buckets[key].append(event)

    rows_written = 0
    files_written = 0
    for (route_id, direction_id, stop_id, service_date), events in buckets.items():
        events.sort(key=lambda e: e["event_time"])
        path = (
            base
            / "events"
            / f"feed={day.feed}"
            / "daily-rapid-data"  # MARTA Metrorail is metro/rapid transit
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


def _slug(s: str) -> str:
    """Make a string safe for a file/directory name."""
    return s.upper().replace(" ", "_").replace("/", "_").replace(".", "")


def _service_date(unix_ts: int, local_tz: ZoneInfo) -> dt.date:
    return dt.datetime.fromtimestamp(unix_ts, tz=local_tz).date()


def _fmt_local(unix_ts: int, local_tz: ZoneInfo) -> str:
    local = dt.datetime.fromtimestamp(unix_ts, tz=local_tz)
    return local.isoformat(sep=" ", timespec="seconds")


def _coerce_date(d: dt.date | str) -> dt.date:
    if isinstance(d, dt.date):
        return d
    return dt.date.fromisoformat(d)
