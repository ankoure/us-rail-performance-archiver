import pyarrow as pa
import pyarrow.parquet as pq

from analysis.vehicle_day import Vehicle, VehicleDay, Visit, merge_close_visits


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


class TestVehicleDwellsPositionBased:
    """Feeds like septa-rail / metra / uta publish stop_id but never set
    current_status. Dwell detection falls back to same-stop_id runs."""

    def test_consecutive_same_stop_pings_become_one_visit(self):
        rows = [
            {"vehicle_timestamp": 100, "current_status": None, "stop_id": "A"},
            {"vehicle_timestamp": 115, "current_status": None, "stop_id": "A"},
            {"vehicle_timestamp": 130, "current_status": None, "stop_id": "A"},
            {"vehicle_timestamp": 145, "current_status": None, "stop_id": "B"},
            {"vehicle_timestamp": 160, "current_status": None, "stop_id": "B"},
        ]
        v = Vehicle("V1", rows, merge_gap_seconds=0)
        assert len(v.dwells) == 2
        assert v.dwells[0].stop_id == "A"
        # Position-based runs are IN_TRANSIT_TO segments, not real dwells: the
        # run's last ping (not first) approximates arrival, and there's no
        # separately observable departure.
        assert v.dwells[0].arrival_ts == 130
        assert v.dwells[0].departure_ts == 130
        assert v.dwells[0].ping_count == 3
        assert v.dwells[1].stop_id == "B"
        assert v.dwells[1].arrival_ts == 160
        assert v.dwells[1].departure_ts == 160

    def test_null_stop_id_breaks_the_run(self):
        rows = [
            {"vehicle_timestamp": 100, "current_status": None, "stop_id": "A"},
            {"vehicle_timestamp": 115, "current_status": None, "stop_id": None},
            {"vehicle_timestamp": 130, "current_status": None, "stop_id": "A"},
        ]
        v = Vehicle("V1", rows, merge_gap_seconds=0)
        assert len(v.dwells) == 2
        assert all(d.stop_id == "A" for d in v.dwells)

    def test_returns_to_status_mode_when_any_ping_is_stopped_at(self):
        # Even one STOPPED_AT ping selects status-based mode, so the same-stop_id
        # IN_TRANSIT_TO pings around it are ignored.
        rows = [
            {
                "vehicle_timestamp": 100,
                "current_status": "IN_TRANSIT_TO",
                "stop_id": "A",
            },
            {"vehicle_timestamp": 115, "current_status": "STOPPED_AT", "stop_id": "A"},
            {
                "vehicle_timestamp": 130,
                "current_status": "IN_TRANSIT_TO",
                "stop_id": "A",
            },
        ]
        v = Vehicle("V1", rows, merge_gap_seconds=0)
        # Only the single STOPPED_AT ping becomes a visit.
        assert len(v.dwells) == 1
        assert v.dwells[0].ping_count == 1
        assert v.dwells[0].arrival_ts == 115


class TestVehicleDwellsNeverStoppedAt:
    """Feeds like Sofia / Vilnius / Fuenlabrada set current_status on every
    ping and still never emit STOPPED_AT. Selecting the algorithm on the
    field's presence sent them down the status-based path, where nothing ever
    matched, and they produced zero visits every day."""

    def _rows(self, status: str = "IN_TRANSIT_TO"):
        return [
            {"vehicle_timestamp": 100, "current_status": status, "stop_id": "A"},
            {"vehicle_timestamp": 115, "current_status": status, "stop_id": "A"},
            {"vehicle_timestamp": 130, "current_status": status, "stop_id": "B"},
        ]

    def test_always_in_transit_to_falls_back_to_position_based(self):
        v = Vehicle("V1", self._rows(), merge_gap_seconds=0)
        assert len(v.dwells) == 2
        # Position-based semantics: the run's last ping is the arrival estimate
        # and there is no separately observable departure.
        assert v.dwells[0].stop_id == "A"
        assert v.dwells[0].arrival_ts == 115
        assert v.dwells[0].departure_ts == 115
        assert v.dwells[1].stop_id == "B"

    def test_incoming_at_also_falls_back(self):
        # INCOMING_AT is likewise an approach, not a dwell (Fuenlabrada mixes
        # it with IN_TRANSIT_TO and never sends STOPPED_AT).
        v = Vehicle("V1", self._rows(status="INCOMING_AT"), merge_gap_seconds=0)
        assert len(v.dwells) == 2

    def test_feed_level_flag_keeps_a_quiet_vehicle_in_status_mode(self):
        # A vehicle can go a whole day without a STOPPED_AT ping on a feed that
        # publishes them. Inferring per-vehicle would mix approach-based visits
        # into a dwell-based feed, so the feed's answer wins.
        v = Vehicle(
            "V1",
            self._rows(),
            merge_gap_seconds=0,
            feed_publishes_stopped_at=True,
        )
        assert v.dwells == []

    def test_feed_level_flag_forces_position_mode(self):
        rows = [
            {"vehicle_timestamp": 100, "current_status": "STOPPED_AT", "stop_id": "A"},
        ]
        v = Vehicle(
            "V1", rows, merge_gap_seconds=0, feed_publishes_stopped_at=False
        )
        # Position-based ignores the status entirely: one same-stop run.
        assert len(v.dwells) == 1
        assert v.dwells[0].arrival_ts == 100


class TestVehicleDayPublishesStoppedAt:
    """The algorithm choice is made once per (feed, day), not per vehicle."""

    def _write(self, tmp_path, rows):
        part = (
            tmp_path
            / "vehicles"
            / "feed=test-vehicles"
            / "year=2026"
            / "month=8"
            / "day=31"
        )
        part.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist(rows), part / "data.parquet")
        return VehicleDay("test-vehicles", "2026-08-31", base_dir=tmp_path)

    def _row(self, vid, ts, status, stop):
        return {
            "vehicle.vehicle.id": vid,
            "vehicle.timestamp": ts,
            "vehicle.current_status": status,
            "vehicle.stop_id": stop,
        }

    def test_false_when_no_vehicle_ever_stops(self, tmp_path):
        day = self._write(
            tmp_path,
            [
                self._row("V1", 100, "IN_TRANSIT_TO", "A"),
                self._row("V1", 115, "IN_TRANSIT_TO", "A"),
                self._row("V2", 120, "INCOMING_AT", "B"),
            ],
        )
        assert day.publishes_stopped_at is False
        # Every vehicle falls back to position-based, so the day yields visits
        # instead of silently producing none.
        assert [v.stop_id for v in day.stops] != []

    def test_one_vehicles_stopped_at_holds_the_whole_partition(self, tmp_path):
        day = self._write(
            tmp_path,
            [
                self._row("V1", 100, "STOPPED_AT", "A"),
                self._row("V2", 120, "IN_TRANSIT_TO", "B"),
                self._row("V2", 135, "IN_TRANSIT_TO", "B"),
            ],
        )
        assert day.publishes_stopped_at is True
        by_id = {v.vehicle_id: v for v in day.vehicles}
        assert len(by_id["V1"].dwells) == 1
        # V2 never stopped; it must not switch to approach-based visits.
        assert by_id["V2"].dwells == []

    def test_vehicle_lookup_uses_the_same_answer(self, tmp_path):
        day = self._write(
            tmp_path,
            [
                self._row("V1", 100, "STOPPED_AT", "A"),
                self._row("V2", 120, "IN_TRANSIT_TO", "B"),
            ],
        )
        assert day.vehicle("V2").dwells == []

