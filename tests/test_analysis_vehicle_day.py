from analysis.vehicle_day import Vehicle, Visit, merge_close_visits


def _visit(
    stop_id: str, arrival: int, departure: int, ping_count: int = 1, **kwargs
) -> Visit:
    defaults = dict(
        vehicle_id="V1", route_id="R1", trip_id="T1", direction_id=0, stop_sequence=5
    )
    defaults.update(kwargs)
    return Visit(
        stop_id=stop_id,
        arrival_ts=arrival,
        departure_ts=departure,
        ping_count=ping_count,
        **defaults,
    )


class TestMergeCloseVisits:
    def test_no_op_when_gap_is_zero(self):
        v1 = _visit("A", 100, 110)
        v2 = _visit("A", 130, 140)
        assert merge_close_visits([v1, v2], gap_seconds=0) == [v1, v2]

    def test_passthrough_for_zero_or_one(self):
        assert merge_close_visits([], 60) == []
        v = _visit("A", 100, 110)
        assert merge_close_visits([v], 60) == [v]

    def test_merges_same_stop_within_gap(self):
        v1 = _visit("A", 100, 110, ping_count=2)
        v2 = _visit("A", 130, 145, ping_count=3)  # gap = 130 - 110 = 20s
        result = merge_close_visits([v1, v2], gap_seconds=30)
        assert len(result) == 1
        merged = result[0]
        assert merged.arrival_ts == 100
        assert merged.departure_ts == 145
        assert merged.ping_count == 5

    def test_does_not_merge_different_stops(self):
        v1 = _visit("A", 100, 110)
        v2 = _visit("B", 115, 125)  # close in time but different stop
        assert merge_close_visits([v1, v2], gap_seconds=60) == [v1, v2]

    def test_does_not_merge_when_gap_exceeds_threshold(self):
        v1 = _visit("A", 100, 110)
        v2 = _visit("A", 200, 210)  # gap 90s
        assert merge_close_visits([v1, v2], gap_seconds=60) == [v1, v2]

    def test_merges_chain_of_three(self):
        v1 = _visit("A", 100, 110)
        v2 = _visit("A", 130, 140)  # gap 20s from v1
        v3 = _visit("A", 160, 170)  # gap 20s from v2
        result = merge_close_visits([v1, v2, v3], gap_seconds=30)
        assert len(result) == 1
        assert result[0].arrival_ts == 100
        assert result[0].departure_ts == 170

    def test_merged_visit_keeps_first_visits_identity(self):
        v1 = _visit("A", 100, 110, route_id="R1", trip_id="T_first", stop_sequence=5)
        v2 = _visit("A", 130, 140, route_id="R1", trip_id="T_second", stop_sequence=99)
        merged = merge_close_visits([v1, v2], gap_seconds=60)[0]
        # Identity fields come from the first visit, not the second
        assert merged.trip_id == "T_first"
        assert merged.stop_sequence == 5

    def test_boundary_gap_equal_to_threshold_merges(self):
        v1 = _visit("A", 100, 110)
        v2 = _visit("A", 170, 180)  # gap exactly 60s
        result = merge_close_visits([v1, v2], gap_seconds=60)
        assert len(result) == 1


class TestVehicleDwells:
    def test_simple_dwell_with_no_flicker(self):
        rows = [
            {
                "vehicle_timestamp": 100,
                "current_status": "IN_TRANSIT_TO",
                "stop_id": "A",
            },
            {"vehicle_timestamp": 115, "current_status": "STOPPED_AT", "stop_id": "A"},
            {"vehicle_timestamp": 130, "current_status": "STOPPED_AT", "stop_id": "A"},
            {
                "vehicle_timestamp": 145,
                "current_status": "IN_TRANSIT_TO",
                "stop_id": "B",
            },
        ]
        v = Vehicle("V1", rows)
        assert len(v.dwells) == 1
        assert v.dwells[0].arrival_ts == 115
        assert v.dwells[0].departure_ts == 130
        assert v.dwells[0].ping_count == 2

    def test_flicker_collapses_with_default_gap(self):
        # STOPPED_AT → IN_TRANSIT_TO at same stop → STOPPED_AT at same stop
        rows = [
            {"vehicle_timestamp": 100, "current_status": "STOPPED_AT", "stop_id": "A"},
            {"vehicle_timestamp": 115, "current_status": "STOPPED_AT", "stop_id": "A"},
            {
                "vehicle_timestamp": 130,
                "current_status": "IN_TRANSIT_TO",
                "stop_id": "A",
            },
            {"vehicle_timestamp": 145, "current_status": "STOPPED_AT", "stop_id": "A"},
            {"vehicle_timestamp": 160, "current_status": "STOPPED_AT", "stop_id": "A"},
        ]
        v = Vehicle("V1", rows)  # default merge_gap_seconds=60
        assert len(v.dwells) == 1
        assert v.dwells[0].arrival_ts == 100
        assert v.dwells[0].departure_ts == 160

    def test_flicker_not_collapsed_when_gap_disabled(self):
        rows = [
            {"vehicle_timestamp": 100, "current_status": "STOPPED_AT", "stop_id": "A"},
            {
                "vehicle_timestamp": 115,
                "current_status": "IN_TRANSIT_TO",
                "stop_id": "A",
            },
            {"vehicle_timestamp": 130, "current_status": "STOPPED_AT", "stop_id": "A"},
        ]
        v = Vehicle("V1", rows, merge_gap_seconds=0)
        assert len(v.dwells) == 2

    def test_long_layover_at_same_stop_not_merged(self):
        # Two real visits to the same terminal, separated by 5 min — different trips
        rows = [
            {
                "vehicle_timestamp": 100,
                "current_status": "STOPPED_AT",
                "stop_id": "TERMINAL",
                "trip_id": "T1",
            },
            {
                "vehicle_timestamp": 115,
                "current_status": "IN_TRANSIT_TO",
                "stop_id": "X",
            },
            {
                "vehicle_timestamp": 415,
                "current_status": "STOPPED_AT",
                "stop_id": "TERMINAL",
                "trip_id": "T2",
            },
        ]
        v = Vehicle("V1", rows)  # default 60s gap
        # 300s gap exceeds threshold, kept separate
        assert len(v.dwells) == 2
