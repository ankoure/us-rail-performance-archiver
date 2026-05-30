"""Tests for analysis/marta_day.py — synthetic MARTA prediction frames."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo
from unittest.mock import PropertyMock, patch

import pandas as pd
import pytest

from analysis.marta_day import (
    DIRECTION_MAP,
    MartaArrival,
    MartaDay,
    MartaTrip,
    export_marta_events_csv,
)

EASTERN = ZoneInfo("America/New_York")


def make_day(rows: list[dict], **kwargs) -> MartaDay:
    """Build a MartaDay backed by a synthetic dataframe (no file I/O)."""
    day = MartaDay("marta-traindata", dt.date(2026, 5, 12), **kwargs)
    df = pd.DataFrame(rows)
    # _df is a cached_property; populate its slot directly.
    object.__setattr__(day, "_df", df)
    # Mark the cached_property as already-computed so it doesn't try to load.
    day.__dict__["_df"] = df
    return day


def ping(
    feed_timestamp: int,
    train_id: str,
    station: str,
    waiting_seconds: int,
    next_arr: int | None = None,
    line: str = "GOLD",
    destination: str = "Airport",
    direction: str = "S",
) -> dict:
    return {
        "feed_timestamp": feed_timestamp,
        "train_id": train_id,
        "station": station,
        "waiting_seconds": waiting_seconds,
        "next_arr": next_arr if next_arr is not None else feed_timestamp + waiting_seconds,
        "line": line,
        "destination": destination,
        "direction": direction,
        "event_time": feed_timestamp,
    }


class TestArrivals:
    def test_picks_min_waiting_seconds_per_train_station_dest(self):
        # Train approaches AIRPORT: 196 → 165 → 136 → 101 → 81 → 6
        rows = [
            ping(1000, "308", "AIRPORT", 196),
            ping(1029, "308", "AIRPORT", 165),
            ping(1059, "308", "AIRPORT", 136),
            ping(1093, "308", "AIRPORT", 101),
            ping(1120, "308", "AIRPORT", 81),
            ping(1179, "308", "AIRPORT", 6, next_arr=1180),
        ]
        day = make_day(rows)
        assert len(day.arrivals) == 1
        arr = day.arrivals[0]
        assert arr.train_id == "308"
        assert arr.station == "AIRPORT"
        assert arr.arrival_ts == 1180
        assert arr.last_waiting_seconds == 6

    def test_drops_empty_train_id(self):
        rows = [
            ping(1000, "", "AIRPORT", 60),
            ping(1100, "308", "AIRPORT", 5, next_arr=1105),
        ]
        day = make_day(rows)
        # Only the row with a real train_id should produce an arrival
        assert len(day.arrivals) == 1
        assert day.arrivals[0].train_id == "308"

    def test_drops_arrivals_above_max_wait_threshold(self):
        # min waiting_seconds is 200 → above default 120 cutoff → dropped
        rows = [
            ping(1000, "308", "AIRPORT", 300),
            ping(1100, "308", "AIRPORT", 200),
        ]
        day = make_day(rows, max_wait_for_arrival_s=120)
        assert day.arrivals == []

    def test_keeps_arrivals_at_relaxed_threshold(self):
        rows = [
            ping(1000, "308", "AIRPORT", 300),
            ping(1100, "308", "AIRPORT", 200),
        ]
        day = make_day(rows, max_wait_for_arrival_s=300)
        assert len(day.arrivals) == 1

    def test_separates_arrivals_by_destination(self):
        # Same train visits AIRPORT once heading south, then later heading north (different dest)
        rows = [
            ping(1000, "308", "AIRPORT", 60, destination="Airport"),
            ping(1100, "308", "AIRPORT", 5, next_arr=1105, destination="Airport"),
            ping(1200, "308", "AIRPORT", 60, destination="North Springs"),
            ping(1290, "308", "AIRPORT", 5, next_arr=1295, destination="North Springs"),
        ]
        day = make_day(rows)
        assert len(day.arrivals) == 2
        dests = {a.destination for a in day.arrivals}
        assert dests == {"Airport", "North Springs"}

    def test_splits_approaches_when_next_arr_jumps(self):
        # Train approaches AIRPORT (Airport-bound), arrives at ~1180.
        # Then 30 minutes later approaches AIRPORT again on the next round trip.
        rows = [
            ping(1000, "308", "AIRPORT", 196, next_arr=1196),
            ping(1100, "308", "AIRPORT", 96, next_arr=1196),
            ping(1180, "308", "AIRPORT", 6, next_arr=1186),
            # 30 min later, another approach — different next_arr
            ping(2900, "308", "AIRPORT", 90, next_arr=2990),
            ping(2980, "308", "AIRPORT", 5, next_arr=2985),
        ]
        day = make_day(rows)
        assert len(day.arrivals) == 2
        assert day.arrivals[0].arrival_ts == 1186
        assert day.arrivals[1].arrival_ts == 2985

    def test_arrivals_sorted_by_time(self):
        rows = [
            ping(2000, "B", "S2", 5, next_arr=2005),
            ping(1000, "A", "S1", 5, next_arr=1005),
            ping(1500, "C", "S3", 5, next_arr=1505),
        ]
        day = make_day(rows)
        timestamps = [a.arrival_ts for a in day.arrivals]
        assert timestamps == sorted(timestamps)


class TestTrips:
    def test_segments_on_destination_change(self):
        # Train 308: SB to Airport, then NB to North Springs, then SB to Airport again.
        # The third trip's MIDTOWN ping (with next_arr far from the first MIDTOWN approach's
        # next_arr) should split into its own approach, giving us 3 trips total.
        rows = [
            ping(1000, "308", "MIDTOWN", 5, next_arr=1005, destination="Airport"),
            ping(1200, "308", "AIRPORT", 5, next_arr=1205, destination="Airport"),
            ping(1400, "308", "MIDTOWN", 5, next_arr=1405, destination="North Springs"),
            ping(1600, "308", "NORTH_SPRINGS", 5, next_arr=1605, destination="North Springs"),
            ping(3000, "308", "MIDTOWN", 5, next_arr=3005, destination="Airport"),
        ]
        day = make_day(rows)
        trips = day.trips
        assert len(trips) == 3
        assert trips[0].destination == "Airport"
        assert len(trips[0].arrivals) == 2
        assert trips[1].destination == "North Springs"
        assert trips[2].destination == "Airport"
        assert [t.trip_index for t in trips] == [0, 1, 2]

    def test_trip_id_format(self):
        trip = MartaTrip(
            train_id="308",
            trip_index=1,
            destination="North Springs",
            arrivals=(),
        )
        assert trip.trip_id == "308-001-NORTH_SPRINGS"


class TestDirectionMapping:
    @pytest.mark.parametrize("d,expected", [("N", 0), ("S", 1), ("E", 0), ("W", 1)])
    def test_maps_cardinal_to_int(self, d, expected):
        a = MartaArrival(
            train_id="T", station="S", line="RED", destination="D",
            direction=d, arrival_ts=0, last_waiting_seconds=0,
        )
        assert a.direction_id == expected

    def test_unknown_direction_returns_none(self):
        a = MartaArrival(
            train_id="T", station="S", line="RED", destination="D",
            direction="?", arrival_ts=0, last_waiting_seconds=0,
        )
        assert a.direction_id is None


class TestExportMartaEventsCsv:
    def test_writes_one_row_per_arrival(self, tmp_path):
        rows = [
            ping(1100, "308", "AIRPORT", 5, next_arr=1105),
            ping(1300, "308", "COLLEGE PARK", 5, next_arr=1305),
        ]
        day = make_day(rows)
        n_rows, n_files = export_marta_events_csv(day, EASTERN, base_dir=tmp_path)
        assert n_rows == 2
        # AIRPORT and COLLEGE PARK = two different stops = two files
        assert n_files == 2

    def test_skips_unknown_direction(self, tmp_path):
        rows = [
            ping(1100, "308", "AIRPORT", 5, next_arr=1105, direction="?"),
        ]
        day = make_day(rows)
        n_rows, n_files = export_marta_events_csv(day, EASTERN, base_dir=tmp_path)
        assert n_rows == 0
        assert n_files == 0

    def test_uses_daily_rapid_data_layout(self, tmp_path):
        rows = [ping(1100, "308", "AIRPORT", 5, next_arr=1105, line="GOLD", direction="S")]
        day = make_day(rows)
        export_marta_events_csv(day, EASTERN, base_dir=tmp_path)
        # Path should be: events/feed=marta-traindata/daily-rapid-data/GOLD-1-AIRPORT/Year=.../Month=.../Day=.../events.csv
        csvs = list(tmp_path.rglob("events.csv"))
        assert len(csvs) == 1
        parts = csvs[0].relative_to(tmp_path).parts
        assert parts[0] == "events"
        assert parts[1] == "feed=marta-traindata"
        assert parts[2] == "daily-rapid-data"
        assert parts[3] == "GOLD-1-AIRPORT"
