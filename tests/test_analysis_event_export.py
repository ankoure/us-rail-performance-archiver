from analysis.event_export import _backfill_visit
from analysis.vehicle_day import Visit


def _visit(
    *,
    route_id: str | None = "R",
    direction_id: int | None = 0,
    trip_id: str | None = "T1",
) -> Visit:
    return Visit(
        vehicle_id="V1",
        stop_id="S1",
        arrival_ts=100,
        departure_ts=120,
        ping_count=1,
        route_id=route_id,
        trip_id=trip_id,
        direction_id=direction_id,
        stop_sequence=5,
    )


class TestBackfillVisit:
    def test_passthrough_when_already_complete(self):
        v = _visit(route_id="R", direction_id=0)
        assert _backfill_visit(v, {}) is v

    def test_fills_missing_direction_from_lookup(self):
        v = _visit(route_id="R", direction_id=None, trip_id="T1")
        result = _backfill_visit(v, {"T1": ("R", 1)})
        assert result is not None
        assert result.direction_id == 1
        assert result.route_id == "R"

    def test_fills_missing_route_from_lookup(self):
        v = _visit(route_id=None, direction_id=0, trip_id="T1")
        result = _backfill_visit(v, {"T1": ("R", 0)})
        assert result is not None
        assert result.route_id == "R"

    def test_fills_both_when_both_missing(self):
        v = _visit(route_id=None, direction_id=None, trip_id="T1")
        result = _backfill_visit(v, {"T1": ("R", 1)})
        assert result is not None
        assert result.route_id == "R"
        assert result.direction_id == 1

    def test_drops_visit_when_trip_id_not_in_lookup(self):
        v = _visit(direction_id=None, trip_id="T_UNKNOWN")
        assert _backfill_visit(v, {"T_OTHER": ("R", 0)}) is None

    def test_drops_visit_when_trip_id_is_none_and_direction_is_none(self):
        v = _visit(direction_id=None, trip_id=None)
        assert _backfill_visit(v, {}) is None

    def test_drops_visit_when_lookup_also_lacks_direction(self):
        # The trip is in trips.txt but its direction_id column was blank.
        v = _visit(direction_id=None, trip_id="T1")
        assert _backfill_visit(v, {"T1": ("R", None)}) is None

    def test_realtime_route_id_wins_over_lookup(self):
        # A populated realtime route_id should not be overwritten by trips.txt
        # — the realtime value is what the agency reported for that ping.
        v = _visit(route_id="RT_ROUTE", direction_id=None, trip_id="T1")
        result = _backfill_visit(v, {"T1": ("STATIC_ROUTE", 1)})
        assert result is not None
        assert result.route_id == "RT_ROUTE"
        assert result.direction_id == 1
