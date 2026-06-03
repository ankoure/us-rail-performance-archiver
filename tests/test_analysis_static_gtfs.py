"""Tests for analysis/static_gtfs.py.

Builds tiny in-memory GTFS zips covering exactly the cases each test exercises.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from analysis.static_gtfs import StaticGtfs, _categorize_route_type, _hms_to_seconds


def build_gtfs_zip(
    tmp_path: Path,
    calendar: str | None = None,
    calendar_dates: str | None = None,
    trips: str | None = None,
    stop_times: str | None = None,
    routes: str | None = None,
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
        if routes is not None:
            z.writestr("routes.txt", routes)
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
        # T2 at stop B: scheduled tt = 5 min = 300s; headway from T1 = 10 min = 600s
        events = [
            {
                "trip_id": "T2",
                "stop_id": "B",
                "scheduled_headway": "",
                "scheduled_tt": "",
            }
        ]
        gtfs_with_schedule.enrich_events(events, dt.date(2026, 5, 20))
        assert events[0]["scheduled_tt"] == 300
        assert events[0]["scheduled_headway"] == 600

    def test_early_arrival_still_populates_from_trip_id(self, gtfs_with_schedule):
        # Vehicle ran ahead of schedule — asof on event time would miss, but
        # (trip_id, stop_id) lookup is independent of actual arrival time.
        events = [
            {
                "trip_id": "T1",
                "stop_id": "B",
                "scheduled_headway": "",
                "scheduled_tt": "",
            }
        ]
        gtfs_with_schedule.enrich_events(events, dt.date(2026, 5, 20))
        # T1 is the first trip at B, so headway is undefined (empty);
        # tt is 5 min from T1's start.
        assert events[0]["scheduled_tt"] == 300
        assert events[0]["scheduled_headway"] == ""

    def test_unknown_trip_id_leaves_fields_empty(self, gtfs_with_schedule):
        events = [
            {
                "trip_id": "T_NOT_IN_GTFS",
                "stop_id": "B",
                "scheduled_headway": "",
                "scheduled_tt": "",
            }
        ]
        gtfs_with_schedule.enrich_events(events, dt.date(2026, 5, 20))
        assert events[0]["scheduled_headway"] == ""
        assert events[0]["scheduled_tt"] == ""

    def test_empty_input_is_a_noop(self, gtfs_with_schedule):
        # Just shouldn't raise
        gtfs_with_schedule.enrich_events([], dt.date(2026, 5, 20))


class TestTripDirections:
    """trip_directions powers direction_id backfill for realtime feeds that
    publish trip_id but omit direction_id (NYCT subway, TriMet, etc.)."""

    def test_basic_mapping(self, tmp_path):
        trips = (
            "trip_id,route_id,service_id,direction_id\n"
            "T1,R,WEEKDAY,0\n"
            "T2,R,WEEKDAY,1\n"
            "T3,R,WEEKDAY,0\n"
        )
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path, trips=trips))
        assert gtfs.trip_directions == {
            "T1": ("R", 0),
            "T2": ("R", 1),
            "T3": ("R", 0),
        }

    def test_missing_direction_id_column_yields_none(self, tmp_path):
        # GTFS spec allows direction_id to be absent entirely.
        trips = "trip_id,route_id,service_id\nT1,R,WEEKDAY\n"
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path, trips=trips))
        assert gtfs.trip_directions == {"T1": ("R", None)}

    def test_blank_direction_id_yields_none(self, tmp_path):
        # Per-row blank direction_id stays None, not 0.
        trips = (
            "trip_id,route_id,service_id,direction_id\nT1,R,WEEKDAY,0\nT2,R,WEEKDAY,\n"
        )
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path, trips=trips))
        assert gtfs.trip_directions == {
            "T1": ("R", 0),
            "T2": ("R", None),
        }


class TestCategorizeRouteType:
    @pytest.mark.parametrize(
        "rt, expected",
        [
            (0, "rapid"),
            (1, "rapid"),
            (5, "rapid"),
            (12, "rapid"),
            (2, "cr"),
            (3, "bus"),
            (11, "bus"),
            (100, "cr"),
            (117, "cr"),
            (300, "cr"),
            (307, "cr"),
            (200, "bus"),
            (700, "bus"),
            (716, "bus"),
            (800, "bus"),
            (400, "rapid"),
            (405, "rapid"),
            (900, "rapid"),
            (906, "rapid"),
            (4, "other"),  # ferry
            (1300, "other"),  # aerial lift
            (None, "other"),
        ],
    )
    def test_known_route_types(self, rt, expected):
        assert _categorize_route_type(rt) == expected


class TestRouteModes:
    def test_mixed_modes_in_one_feed(self, tmp_path):
        # An agency like Metro Transit MN ships rail (light rail) + bus in the
        # same GTFS — route_modes must classify each route independently.
        routes = "route_id,route_type\nLRT_BLUE,0\nBUS_5,3\nCR_NORTHSTAR,2\nFERRY_X,4\n"
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path, routes=routes))
        assert gtfs.route_modes == {
            "LRT_BLUE": "rapid",
            "BUS_5": "bus",
            "CR_NORTHSTAR": "cr",
            "FERRY_X": "other",
        }

    def test_missing_routes_file_yields_empty(self, tmp_path):
        # Some agency snapshots in the wild ship without routes.txt; the lookup
        # should just degrade to "no info", not raise.
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path))
        assert gtfs.route_modes == {}

    def test_blank_route_type_falls_through_to_other(self, tmp_path):
        routes = "route_id,route_type\nR_OK,1\nR_BLANK,\n"
        gtfs = StaticGtfs(build_gtfs_zip(tmp_path, routes=routes))
        assert gtfs.route_modes == {"R_OK": "rapid", "R_BLANK": "other"}


# pandas import used by TestScheduledStops.test_scheduled_headway_is_per_route_dir_stop
import pandas as pd  # noqa: E402
