from zoneinfo import ZoneInfo

import pyarrow as pa

from analysis.metrics import (
    EVENTS_SCHEMA,
    ROUTE_DAY_SCHEMA,
    STOP_DAY_SCHEMA,
    _cov,
    _pct,
    compute_events,
    compute_marts,
)
from analysis.vehicle_day import Visit

NY = ZoneInfo("America/New_York")

# A weekday noon in New York, well clear of the local-midnight boundary.
NOON = 1_716_220_800  # 2024-05-20 12:00:00 UTC == 08:00 local


def _visit(
    stop_id: str = "S1",
    arrival: int = NOON,
    departure: int | None = None,
    **kwargs,
) -> Visit:
    defaults = dict(
        vehicle_id="V1", route_id="R1", trip_id="T1", direction_id=0, stop_sequence=5
    )
    defaults.update(kwargs)
    return Visit(
        stop_id=stop_id,
        arrival_ts=arrival,
        departure_ts=departure if departure is not None else arrival,
        ping_count=1,
        **defaults,
    )


def _stop_row(rows: list[dict], **match) -> dict:
    """The single stop-day row matching every key in `match`."""
    hits = [r for r in rows if all(r[k] == v for k, v in match.items())]
    assert len(hits) == 1, f"expected 1 row for {match}, got {len(hits)}"
    return hits[0]


class TestPercentileHelpers:
    def test_empty_is_none(self):
        assert _pct([], 50) is None

    def test_single_value_collapses(self):
        assert _pct([42], 50) == 42
        assert _pct([42], 90) == 42

    def test_known_list(self):
        # inclusive interpolation: p50 of [60,120,180,240] = 150, p90 = 222
        assert _pct([60, 120, 180, 240], 50) == 150
        assert round(_pct([60, 120, 180, 240], 90)) == 222

    def test_cov_regular_is_zero(self):
        assert _cov([300, 300, 300]) == 0.0

    def test_cov_irregular_is_positive(self):
        assert _cov([60, 600]) > 0

    def test_cov_needs_two_samples(self):
        assert _cov([300]) is None
        assert _cov([]) is None


class TestStopDayMart:
    def test_single_visit_has_no_headway(self):
        stop, _ = compute_marts([_visit()], "f", NY)
        row = _stop_row(stop, stop_id="S1")
        assert row["visit_count"] == 1
        assert row["trip_count"] == 1
        assert row["headway_p50_s"] is None
        assert row["headway_p90_s"] is None
        assert row["headway_mean_s"] is None
        assert row["headway_cov"] is None

    def test_headway_from_successive_arrivals(self):
        visits = [
            _visit(arrival=NOON, trip_id="T1"),
            _visit(arrival=NOON + 300, trip_id="T2"),
            _visit(arrival=NOON + 720, trip_id="T3"),  # +420
        ]
        stop, _ = compute_marts(visits, "f", NY)
        row = _stop_row(stop, stop_id="S1")
        assert row["visit_count"] == 3
        assert row["trip_count"] == 3
        # headways [300, 420] -> p50 = 360
        assert row["headway_p50_s"] == 360
        assert row["headway_mean_s"] == 360.0

    def test_distinct_vehicle_count(self):
        visits = [
            _visit(arrival=NOON, vehicle_id="A", trip_id="T1"),
            _visit(arrival=NOON + 300, vehicle_id="B", trip_id="T2"),
        ]
        stop, _ = compute_marts(visits, "f", NY)
        row = _stop_row(stop, stop_id="S1")
        assert row["distinct_vehicle_count"] == 2
        assert row["visit_count"] == 2

    def test_service_span(self):
        visits = [
            _visit(arrival=NOON, departure=NOON + 30),
            _visit(arrival=NOON + 600, departure=NOON + 650, trip_id="T2"),
        ]
        stop, _ = compute_marts(visits, "f", NY)
        row = _stop_row(stop, stop_id="S1")
        assert row["first_service_unix"] == NOON
        assert row["last_service_unix"] == NOON + 650
        assert row["service_span_s"] == 650

    def test_dwell_percentiles(self):
        visits = [
            _visit(arrival=NOON, departure=NOON + 10),
            _visit(arrival=NOON + 300, departure=NOON + 330, trip_id="T2"),  # dwell 30
        ]
        stop, _ = compute_marts(visits, "f", NY)
        row = _stop_row(stop, stop_id="S1")
        # dwells [10, 30] -> p50 = 20
        assert row["dwell_p50_s"] == 20

    def test_null_route_dropped(self):
        stop, route = compute_marts([_visit(route_id=None)], "f", NY)
        assert stop == []
        assert route == []

    def test_null_direction_kept_and_separate(self):
        visits = [
            _visit(arrival=NOON, direction_id=None, trip_id="T1"),
            _visit(arrival=NOON + 300, direction_id=0, trip_id="T2"),
        ]
        stop, _ = compute_marts(visits, "f", NY)
        dirs = {r["direction_id"] for r in stop}
        assert dirs == {None, 0}

    def test_service_date_splits_across_local_midnight(self):
        # 03:00 UTC on 2024-05-20 is 23:00 the prior day in New York (EDT).
        before_midnight = 1_716_174_000  # 2024-05-19 23:00 local
        after_midnight = before_midnight + 7200  # 2024-05-20 01:00 local
        visits = [
            _visit(arrival=before_midnight, trip_id="T1"),
            _visit(arrival=after_midnight, trip_id="T2"),
        ]
        stop, _ = compute_marts(visits, "f", NY)
        dates = {r["service_date"] for r in stop}
        assert dates == {"2024-05-19", "2024-05-20"}

    def test_trip_updates_shape_zero_dwell_excludes_empty_vehicle(self):
        # trip_updates-derived visits: arrival == departure, vehicle_id "".
        visits = [
            _visit(arrival=NOON, departure=NOON, vehicle_id="", trip_id="T1"),
            _visit(arrival=NOON + 300, departure=NOON + 300, vehicle_id="", trip_id="T2"),
        ]
        stop, _ = compute_marts(visits, "f", NY)
        row = _stop_row(stop, stop_id="S1")
        assert row["dwell_p50_s"] == 0
        assert row["distinct_vehicle_count"] == 0
        assert row["visit_count"] == 2


class TestRouteDayMart:
    def test_distinct_stop_count_and_median_of_medians(self):
        # Stop A medians 300, stop B medians 600 -> route headway = median(300,600)=450.
        visits = [
            _visit(stop_id="A", arrival=NOON, trip_id="T1"),
            _visit(stop_id="A", arrival=NOON + 300, trip_id="T2"),
            _visit(stop_id="A", arrival=NOON + 600, trip_id="T3"),
            _visit(stop_id="B", arrival=NOON, trip_id="T1"),
            _visit(stop_id="B", arrival=NOON + 600, trip_id="T2"),
            _visit(stop_id="B", arrival=NOON + 1200, trip_id="T3"),
        ]
        _, route = compute_marts(visits, "f", NY)
        assert len(route) == 1
        row = route[0]
        assert row["distinct_stop_count"] == 2
        assert row["trip_count"] == 3
        assert row["headway_p50_s"] == 450


class TestEventsMart:
    def test_each_visit_yields_arr_and_dep(self):
        rows = compute_events([_visit(arrival=NOON, departure=NOON + 30)], "f", NY)
        assert [r["event_type"] for r in rows] == ["ARR", "DEP"]
        assert [r["event_unix"] for r in rows] == [NOON, NOON + 30]
        assert {r["stop_id"] for r in rows} == {"S1"}

    def test_rows_sorted_by_event_unix(self):
        visits = [
            _visit(arrival=NOON + 600, departure=NOON + 630, trip_id="T2"),
            _visit(arrival=NOON, departure=NOON + 30, trip_id="T1"),
        ]
        rows = compute_events(visits, "f", NY)
        times = [r["event_unix"] for r in rows]
        assert times == sorted(times)

    def test_null_route_dropped(self):
        assert compute_events([_visit(route_id=None)], "f", NY) == []

    def test_null_direction_and_vehicle_preserved(self):
        rows = compute_events(
            [_visit(direction_id=None, vehicle_id="")], "f", NY
        )
        assert all(r["direction_id"] is None for r in rows)
        assert all(r["vehicle_id"] == "" for r in rows)

    def test_event_service_date_per_event(self):
        # A visit whose ARR is before local midnight and DEP after it files its
        # two events on different service dates.
        before = 1_716_174_000  # 2024-05-19 23:00 local
        rows = compute_events([_visit(arrival=before, departure=before + 7200)], "f", NY)
        dates = {r["event_type"]: r["service_date"] for r in rows}
        assert dates == {"ARR": "2024-05-19", "DEP": "2024-05-20"}

    def test_schema_round_trip(self):
        visits = [
            _visit(stop_id="A", arrival=NOON, trip_id="T1", direction_id=None),
            _visit(stop_id="B", arrival=NOON + 300, vehicle_id="", trip_id="T2"),
        ]
        rows = compute_events(visits, "f", NY)
        tbl = pa.Table.from_pylist(rows, schema=EVENTS_SCHEMA)
        assert tbl.num_rows == len(rows) == 4
        assert tbl.schema == EVENTS_SCHEMA


class TestSchemaRoundTrip:
    def test_rows_cast_into_declared_schemas(self):
        visits = [
            _visit(stop_id="A", arrival=NOON, trip_id="T1", direction_id=None),
            _visit(stop_id="A", arrival=NOON + 300, trip_id="T2", direction_id=None),
            _visit(stop_id="B", arrival=NOON, vehicle_id="X", trip_id="T1"),
        ]
        stop, route = compute_marts(visits, "f", NY)
        stop_tbl = pa.Table.from_pylist(stop, schema=STOP_DAY_SCHEMA)
        route_tbl = pa.Table.from_pylist(route, schema=ROUTE_DAY_SCHEMA)
        assert stop_tbl.num_rows == len(stop)
        assert route_tbl.num_rows == len(route)
        assert stop_tbl.schema == STOP_DAY_SCHEMA
