"""Tests for analysis/static_gtfs.py.

Builds tiny in-memory GTFS zips covering exactly the cases each test exercises.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analysis.static_gtfs import StaticGtfs, _hms_to_seconds


def build_gtfs_zip(
    tmp_path: Path,
    calendar: str | None = None,
    calendar_dates: str | None = None,
    trips: str | None = None,
    stop_times: str | None = None,
) -> Path:
    """Write a minimal GTFS zip with whatever tables the caller cares to specify."""
    zip_path = tmp_path / "feed.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        if calendar is not None:
            z.writestr("calendar.txt", calendar)
        if calendar_dates is not None:
            z.writestr("calendar_dates.txt", calendar_dates)
        if trips is not None:
            z.writestr("trips.txt", trips)
        if stop_times is not None:
            z.writestr("stop_times.txt", stop_times)
    return zip_path


# Convenience constants used across tests
EASTERN = ZoneInfo("America/New_York")


class TestHmsToSeconds:
    def test_normal_time(self):
        assert _hms_to_seconds("06:30:45") == 6 * 3600 + 30 * 60 + 45

    def test_after_midnight_continuation(self):
        # GTFS allows hours >= 24 for next-day continuations
        assert _hms_to_seconds("25:30:00") == 25 * 3600 + 30 * 60

    def test_invalid_returns_sentinel(self):
        assert _hms_to_seconds("garbage") == -1
        assert _hms_to_seconds("") == -1
        assert _hms_to_seconds(None) == -1  # type: ignore[arg-type]


class TestActiveServiceIds:
    def test_weekday_window(self, tmp_path):
        # Service WEEKDAY runs Mon-Fri 2026-05-01 through 2026-05-31
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WEEKDAY,1,1,1,1,1,0,0,20260501,20260531\n"
            "WEEKEND,0,0,0,0,0,1,1,20260501,20260531\n"
        )
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path, calendar=calendar))
        # 2026-05-20 is a Wednesday
        assert gtfs.active_service_ids(dt.date(2026, 5, 20)) == {"WEEKDAY"}
        # 2026-05-23 is a Saturday
        assert gtfs.active_service_ids(dt.date(2026, 5, 23)) == {"WEEKEND"}
        # Outside the window
        assert gtfs.active_service_ids(dt.date(2026, 6, 1)) == set()

    def test_calendar_dates_addition(self, tmp_path):
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WEEKDAY,1,1,1,1,1,0,0,20260501,20260531\n"
        )
        # Add HOLIDAY service for one specific Saturday
        calendar_dates = "service_id,date,exception_type\nHOLIDAY,20260523,1\n"
        gtfs = StaticGtfs(
            build_gtfs_zip(tmp_path, calendar=calendar, calendar_dates=calendar_dates)
        )
        assert gtfs.active_service_ids(dt.date(2026, 5, 23)) == {"HOLIDAY"}

    def test_calendar_dates_removal(self, tmp_path):
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WEEKDAY,1,1,1,1,1,0,0,20260501,20260531\n"
        )
        # Remove weekday service for Memorial Day (Mon 2026-05-25)
        calendar_dates = "service_id,date,exception_type\nWEEKDAY,20260525,2\n"
        gtfs = StaticGtfs(
            build_gtfs_zip(tmp_path, calendar=calendar, calendar_dates=calendar_dates)
        )
        assert gtfs.active_service_ids(dt.date(2026, 5, 25)) == set()

    def test_no_calendar_dates_file(self, tmp_path):
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WEEKDAY,1,1,1,1,1,0,0,20260501,20260531\n"
        )
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path, calendar=calendar))
        assert gtfs.active_service_ids(dt.date(2026, 5, 20)) == {"WEEKDAY"}


class TestScheduledStops:
    @pytest.fixture
    def simple_gtfs(self, tmp_path) -> StaticGtfs:
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WD,1,1,1,1,1,0,0,20260501,20260531\n"
        )
        # Route R, direction 0. Three trips at this stop A, every 10 min.
        # Trip T1 visits stops A, B, C with travel times 0 / 5min / 12min from start.
        trips = (
            "route_id,service_id,trip_id,direction_id\n"
            "R,WD,T1,0\n"
            "R,WD,T2,0\n"
            "R,WD,T3,0\n"
        )
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,06:00:00,06:00:30,A,1\n"
            "T1,06:05:00,06:05:30,B,2\n"
            "T1,06:12:00,06:12:30,C,3\n"
            "T2,06:10:00,06:10:30,A,1\n"
            "T2,06:15:00,06:15:30,B,2\n"
            "T2,06:22:00,06:22:30,C,3\n"
            "T3,06:20:00,06:20:30,A,1\n"
            "T3,06:25:00,06:25:30,B,2\n"
            "T3,06:32:00,06:32:30,C,3\n"
        )
        return StaticGtfs(
            build_gtfs_zip(
                tmp_path, calendar=calendar, trips=trips, stop_times=stop_times
            )
        )

    def test_scheduled_tt_is_cumulative_from_trip_start(self, simple_gtfs):
        sched = simple_gtfs.scheduled_stops(dt.date(2026, 5, 20))
        t1 = sched[sched["trip_id"] == "T1"].sort_values("stop_sequence")
        assert list(t1["scheduled_tt"]) == [0, 5 * 60, 12 * 60]

    def test_scheduled_headway_is_per_route_dir_stop(self, simple_gtfs):
        sched = simple_gtfs.scheduled_stops(dt.date(2026, 5, 20))
        stop_a = sched[sched["stop_id"] == "A"].sort_values("arrival_seconds")
        # First trip has NaN headway, then 10 min, then 10 min
        headways = stop_a["scheduled_headway"].tolist()
        assert headways[0] is pd.NA
        assert headways[1:] == [10 * 60, 10 * 60]

    def test_returns_empty_on_inactive_date(self, simple_gtfs):
        # Saturday is outside the WD service
        assert simple_gtfs.scheduled_stops(dt.date(2026, 5, 23)).empty


class TestEnrichEvents:
    @pytest.fixture
    def gtfs_with_schedule(self, tmp_path) -> StaticGtfs:
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WD,1,1,1,1,1,0,0,20260501,20260531\n"
        )
        trips = "route_id,service_id,trip_id,direction_id\nR,WD,T1,0\nR,WD,T2,0\n"
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,06:00:00,06:00:30,A,1\n"
            "T1,06:05:00,06:05:30,B,2\n"
            "T2,06:10:00,06:10:30,A,1\n"
            "T2,06:15:00,06:15:30,B,2\n"
        )
        return StaticGtfs(
            build_gtfs_zip(
                tmp_path, calendar=calendar, trips=trips, stop_times=stop_times
            )
        )

    def test_populates_headway_and_tt(self, gtfs_with_schedule):
        # Real event at stop B at 06:16 local (a bit after T2's scheduled 06:15 arrival)
        events = [
            {
                "route_id": "R",
                "direction_id": 0,
                "stop_id": "B",
                "event_time": "2026-05-20 06:16:00-04:00",
                "scheduled_headway": "",
                "scheduled_tt": "",
            }
        ]
        gtfs_with_schedule.enrich_events(events, dt.date(2026, 5, 20), EASTERN)
        # Backward asof picks T2's scheduled arrival (06:15) at stop B
        # T2 at stop B: tt = 5 min = 300s; headway = T2 - T1 at stop B = 10 min = 600s
        assert events[0]["scheduled_tt"] == 300
        assert events[0]["scheduled_headway"] == 600

    def test_event_before_any_scheduled_arrival_gets_empty(self, gtfs_with_schedule):
        events = [
            {
                "route_id": "R",
                "direction_id": 0,
                "stop_id": "B",
                "event_time": "2026-05-20 05:00:00-04:00",  # before any scheduled
                "scheduled_headway": "",
                "scheduled_tt": "",
            }
        ]
        gtfs_with_schedule.enrich_events(events, dt.date(2026, 5, 20), EASTERN)
        assert events[0]["scheduled_headway"] == ""
        assert events[0]["scheduled_tt"] == ""

    def test_empty_input_is_a_noop(self, gtfs_with_schedule):
        # Just shouldn't raise
        gtfs_with_schedule.enrich_events([], dt.date(2026, 5, 20), EASTERN)


# pandas import used by TestScheduledStops.test_scheduled_headway_is_per_route_dir_stop
import pandas as pd  # noqa: E402
