"""Tests for analysis/adherence.py (gold tier v2: on-time performance).

`compute_adherence` is pure — it takes a hand-built `schedules` lookup, so most
tests need no GTFS files at all. `schedule_index` is exercised against a tiny
in-memory GTFS zip, mirroring tests/test_analysis_static_gtfs.py.
"""

from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa

from analysis.adherence import (
    ADHERENCE_SCHEMA,
    ROUTE_DAY_OTP_SCHEMA,
    STOP_DAY_OTP_SCHEMA,
    SchedRow,
    compute_adherence,
    schedule_index,
)
from analysis.static_gtfs import StaticGtfs
from analysis.vehicle_day import Visit

NY = ZoneInfo("America/New_York")

# Local noon in New York (well clear of the midnight boundary); 43200 s after
# local midnight, so a scheduled time of 43200 lands exactly on NOON.
NOON = 1_716_220_800  # 2024-05-20 12:00:00-04:00
DAY = dt.date(2024, 5, 20)
NOON_SECONDS = 43_200


def _visit(
    stop_id: str = "S1",
    arrival: int = NOON,
    departure: int | None = None,
    trip_id: str = "T1",
    **kwargs,
) -> Visit:
    defaults = dict(vehicle_id="V1", route_id="R1", direction_id=0, stop_sequence=5)
    defaults.update(kwargs)
    return Visit(
        stop_id=stop_id,
        arrival_ts=arrival,
        departure_ts=departure if departure is not None else arrival,
        ping_count=1,
        trip_id=trip_id,
        **defaults,
    )


def _sched(
    arrival_seconds: int = NOON_SECONDS,
    departure_seconds: int | None = None,
    route_id: str | None = "R1",
    direction_id: int | None = 0,
    stop_sequence: int | None = 5,
) -> SchedRow:
    return SchedRow(
        arrival_seconds=arrival_seconds,
        departure_seconds=arrival_seconds
        if departure_seconds is None
        else departure_seconds,
        route_id=route_id,
        direction_id=direction_id,
        stop_sequence=stop_sequence,
    )


def _only(rows: list[dict], **match) -> dict:
    hits = [r for r in rows if all(r[k] == v for k, v in match.items())]
    assert len(hits) == 1, f"expected 1 row for {match}, got {len(hits)}"
    return hits[0]


class TestClassification:
    def test_on_time_zero_delay(self):
        sched = {DAY: {("T1", "S1"): _sched()}}
        fact, _, _ = compute_adherence([_visit(arrival=NOON)], sched, "f", NY)
        row = fact[0]
        assert row["arrival_delay_s"] == 0
        assert row["status"] == "on_time"
        assert row["on_time"] is True

    def test_early_and_late(self):
        sched = {DAY: {("T1", "S1"): _sched()}}
        early, _, _ = compute_adherence([_visit(arrival=NOON - 120)], sched, "f", NY)
        late, _, _ = compute_adherence([_visit(arrival=NOON + 600)], sched, "f", NY)
        assert early[0]["arrival_delay_s"] == -120 and early[0]["status"] == "early"
        assert late[0]["arrival_delay_s"] == 600 and late[0]["status"] == "late"

    def test_window_boundaries_are_inclusive_on_time(self):
        # default window [-60, +300]: exactly -60 / +300 are still on_time.
        sched = {DAY: {("T1", "S1"): _sched()}}
        for delta, status in [
            (-60, "on_time"),
            (-61, "early"),
            (300, "on_time"),
            (301, "late"),
        ]:
            fact, _, _ = compute_adherence(
                [_visit(arrival=NOON + delta)], sched, "f", NY
            )
            assert fact[0]["status"] == status, (delta, fact[0]["status"])

    def test_custom_thresholds(self):
        sched = {DAY: {("T1", "S1"): _sched()}}
        # 360s late is "late" by default but on_time with a +6min window.
        fact, _, _ = compute_adherence(
            [_visit(arrival=NOON + 360)], sched, "f", NY, late_threshold_s=360
        )
        assert fact[0]["status"] == "on_time"


class TestMatching:
    def test_no_trip_id_dropped(self):
        sched = {DAY: {("T1", "S1"): _sched()}}
        fact, stop, route = compute_adherence([_visit(trip_id=None)], sched, "f", NY)
        assert fact == [] and stop == [] and route == []

    def test_no_schedule_match_dropped(self):
        sched = {DAY: {("OTHER", "S1"): _sched()}}
        fact, _, _ = compute_adherence([_visit(trip_id="T1")], sched, "f", NY)
        assert fact == []

    def test_route_and_direction_taken_from_schedule(self):
        # Visit has no route/direction (NYCT-style); the schedule supplies them.
        sched = {
            DAY: {("T1", "S1"): _sched(route_id="RED", direction_id=1, stop_sequence=9)}
        }
        visit = _visit(route_id=None, direction_id=None, stop_sequence=None)
        fact, _, _ = compute_adherence([visit], sched, "f", NY)
        assert fact[0]["route_id"] == "RED"
        assert fact[0]["direction_id"] == 1
        assert fact[0]["stop_sequence"] == 9

    def test_route_mode_populated(self):
        sched = {DAY: {("T1", "S1"): _sched()}}
        fact, _, _ = compute_adherence(
            [_visit()], sched, "f", NY, route_modes={"R1": "rapid"}
        )
        assert fact[0]["route_mode"] == "rapid"

    def test_empty_vehicle_id_becomes_null(self):
        # trip_updates-derived visits carry vehicle_id "".
        sched = {DAY: {("T1", "S1"): _sched()}}
        fact, _, _ = compute_adherence([_visit(vehicle_id="")], sched, "f", NY)
        assert fact[0]["vehicle_id"] is None

    def test_owl_trip_matches_prior_service_day(self):
        # A vehicle arriving 01:10 local on the 21st belongs to the 20th's
        # service day (scheduled as 25:10 = 90600s). The prior-day schedule
        # entry resolves it and stamps service_date = the 20th.
        owl_arrival = int(dt.datetime(2024, 5, 21, 1, 10, tzinfo=NY).timestamp())
        sched = {DAY: {("OWL", "S1"): _sched(arrival_seconds=90_600)}}
        fact, _, _ = compute_adherence(
            [_visit(trip_id="OWL", arrival=owl_arrival)], sched, "f", NY
        )
        assert len(fact) == 1
        assert fact[0]["arrival_delay_s"] == 0
        assert fact[0]["service_date"] == "2024-05-20"

    def test_ambiguous_trip_id_picks_nearest_service_day(self):
        # The same (trip_id, stop) is scheduled on BOTH days as a post-midnight
        # continuation. A vehicle arriving 00:05 on the 20th must anchor to the
        # 19th's occurrence (24:05 -> ~00:05, ~0 delay), not the 20th's (24:22 ->
        # 00:22 on the 21st, a spurious −24 h). Nearest |delay| wins.
        arrival = int(dt.datetime(2024, 5, 20, 0, 5, tzinfo=NY).timestamp())
        sched = {
            DAY: {("OWL", "S1"): _sched(arrival_seconds=87_720)},  # 24:22 on the 20th
            dt.date(2024, 5, 19): {
                ("OWL", "S1"): _sched(arrival_seconds=86_700)
            },  # 24:05 on the 19th
        }
        fact, _, _ = compute_adherence(
            [_visit(trip_id="OWL", arrival=arrival)], sched, "f", NY
        )
        assert len(fact) == 1
        assert fact[0]["service_date"] == "2024-05-19"
        assert abs(fact[0]["arrival_delay_s"]) <= 1

    def test_departure_fallback_when_no_scheduled_arrival(self):
        # arrival_seconds == -1 (absent) -> classify on departure delay instead.
        sched = {
            DAY: {
                ("T1", "S1"): _sched(arrival_seconds=-1, departure_seconds=NOON_SECONDS)
            }
        }
        fact, _, _ = compute_adherence(
            [_visit(arrival=NOON + 1000, departure=NOON)], sched, "f", NY
        )
        assert fact[0]["arrival_delay_s"] is None
        assert fact[0]["scheduled_arrival_unix"] is None
        assert fact[0]["departure_delay_s"] == 0
        assert fact[0]["status"] == "on_time"


class TestAggregates:
    def _mixed(self) -> list[Visit]:
        # Same (route, dir, stop, date): one on_time, one early, two late.
        return [
            _visit(trip_id="T1", arrival=NOON),  # on_time (0)
            _visit(trip_id="T2", arrival=NOON - 200),  # early (-200)
            _visit(trip_id="T3", arrival=NOON + 400),  # late (+400)
            _visit(trip_id="T4", arrival=NOON + 600),  # late (+600)
        ]

    def _sched_all(self) -> dict:
        return {DAY: {(t, "S1"): _sched() for t in ("T1", "T2", "T3", "T4")}}

    def test_stop_day_counts_and_pct(self):
        _, stop, _ = compute_adherence(self._mixed(), self._sched_all(), "f", NY)
        row = _only(stop, stop_id="S1")
        assert row["matched_count"] == 4
        assert row["on_time_count"] == 1
        assert row["early_count"] == 1
        assert row["late_count"] == 2
        assert row["on_time_pct"] == 0.25
        # arrival delays [0, -200, 400, 600] -> p50 = 200
        assert row["arr_delay_p50_s"] == 200
        assert row["arr_delay_mean_s"] == 200.0

    def test_route_day_distinct_stops(self):
        visits = self._mixed() + [_visit(trip_id="T5", stop_id="S2", arrival=NOON)]
        sched = self._sched_all()
        sched[DAY][("T5", "S2")] = _sched()
        _, _, route = compute_adherence(visits, sched, "f", NY)
        row = _only(route, route_id="R1")
        assert row["matched_count"] == 5
        assert row["distinct_stop_count"] == 2

    def test_null_direction_bucketed_separately(self):
        # direction_id ends up null only when neither schedule nor visit supplies
        # it; such visits get their own bucket rather than being dropped.
        sched = {
            DAY: {
                ("T1", "S1"): _sched(direction_id=0),
                ("T2", "S1"): _sched(direction_id=None),
            }
        }
        visits = [
            _visit(trip_id="T1", direction_id=0),
            _visit(trip_id="T2", direction_id=None),
        ]
        _, stop, _ = compute_adherence(visits, sched, "f", NY)
        assert {r["direction_id"] for r in stop} == {0, None}


class TestSchemaRoundTrip:
    def test_all_three_marts_cast(self):
        visits = [
            _visit(trip_id="T1", arrival=NOON),
            _visit(trip_id="T2", stop_id="S2", arrival=NOON + 400, direction_id=None),
        ]
        sched = {
            DAY: {
                ("T1", "S1"): _sched(),
                ("T2", "S2"): _sched(direction_id=None),
            }
        }
        fact, stop, route = compute_adherence(
            visits, sched, "f", NY, route_modes={"R1": "rapid"}
        )
        assert (
            pa.Table.from_pylist(fact, schema=ADHERENCE_SCHEMA).schema
            == ADHERENCE_SCHEMA
        )
        assert (
            pa.Table.from_pylist(stop, schema=STOP_DAY_OTP_SCHEMA).schema
            == STOP_DAY_OTP_SCHEMA
        )
        assert (
            pa.Table.from_pylist(route, schema=ROUTE_DAY_OTP_SCHEMA).schema
            == ROUTE_DAY_OTP_SCHEMA
        )


def _build_gtfs_zip(tmp_path: Path, **tables: str) -> Path:
    """Minimal GTFS zip from {table_name: csv_text} (e.g. trips=..., stop_times=...)."""
    zip_path = tmp_path / "feed.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for name, text in tables.items():
            z.writestr(f"{name}.txt", text)
    return zip_path


class TestScheduleIndex:
    def _gtfs(self, tmp_path: Path) -> StaticGtfs:
        calendar = (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WD,1,1,1,1,1,0,0,20260501,20260531\n"
        )
        trips = "route_id,service_id,trip_id,direction_id\nR,WD,T1,0\nR,WD,T2,1\n"
        stop_times = (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T1,06:00:00,06:00:30,A,1\n"
            "T1,06:05:00,06:05:30,B,2\n"
            "T2,06:10:00,06:10:30,A,1\n"
        )
        return StaticGtfs(
            _build_gtfs_zip(
                tmp_path, calendar=calendar, trips=trips, stop_times=stop_times
            )
        )

    def test_builds_keyed_lookup(self, tmp_path):
        index = schedule_index(self._gtfs(tmp_path), dt.date(2026, 5, 20))
        assert index[("T1", "A")] == SchedRow(21_600, 21_630, "R", 0, 1)
        assert index[("T1", "B")].arrival_seconds == 6 * 3600 + 5 * 60
        assert index[("T2", "A")].direction_id == 1

    def test_inactive_date_is_empty(self, tmp_path):
        # 2026-05-23 is a Saturday — outside the WD service window.
        assert schedule_index(self._gtfs(tmp_path), dt.date(2026, 5, 23)) == {}
