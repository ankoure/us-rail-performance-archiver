"""Tests for analysis/segment_speed.py.

`compute_segment_speeds` is pure — it takes hand-built visits and stop coords, so
no GTFS files or parquet are needed.
"""

from __future__ import annotations

import math
from zoneinfo import ZoneInfo


from analysis.segment_speed import (
    SEGMENT_DAY_SCHEMA,
    SEGMENT_SPEED_SCHEMA,
    _haversine_m,
    compute_segment_speeds,
)
from analysis.vehicle_day import Visit

NY = ZoneInfo("America/New_York")

# 2024-05-20 12:00:00-04:00
NOON = 1_716_220_800

# Two stops roughly 1 km apart in Manhattan (Times Sq / 34th St Penn area).
COORDS = {
    "S1": (40.7580, -73.9855),  # Times Square
    "S2": (40.7506, -73.9971),  # 34th St Penn Station (~1.25 km)
    "S3": (40.7484, -74.0048),  # 23rd St area
}


def _visit(
    stop_id: str,
    arrival: int,
    departure: int | None = None,
    *,
    trip_id: str = "T1",
    route_id: str = "R1",
    direction_id: int = 0,
    stop_sequence: int | None = None,
    vehicle_id: str = "V1",
) -> Visit:
    return Visit(
        vehicle_id=vehicle_id,
        stop_id=stop_id,
        arrival_ts=arrival,
        departure_ts=departure if departure is not None else arrival,
        ping_count=1,
        route_id=route_id,
        trip_id=trip_id,
        direction_id=direction_id,
        stop_sequence=stop_sequence,
    )


class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine_m(40.0, -74.0, 40.0, -74.0) == 0.0

    def test_known_distance(self):
        # Times Square to 34th St Penn is ~1.25 km.
        d = _haversine_m(*COORDS["S1"], *COORDS["S2"])
        assert 1000 < d < 1600

    def test_symmetric(self):
        a, b = (
            _haversine_m(*COORDS["S1"], *COORDS["S2"]),
            _haversine_m(*COORDS["S2"], *COORDS["S1"]),
        )
        assert math.isclose(a, b)


class TestComputeSegmentSpeeds:
    def _run(self, visits, coords=None, **kw):
        return compute_segment_speeds(
            visits, coords if coords is not None else COORDS, "feed", NY, **kw
        )

    def test_basic_segment(self):
        # 600 s in transit between S1 and S2
        v1 = _visit("S1", NOON, NOON + 30)
        v2 = _visit("S2", NOON + 630)
        fact, seg = self._run([v1, v2])
        assert len(fact) == 1
        row = fact[0]
        assert row["from_stop_id"] == "S1"
        assert row["to_stop_id"] == "S2"
        assert row["transit_s"] == 600
        assert row["distance_m"] > 0
        assert row["speed_mph"] > 0

    def test_two_segments(self):
        # Trip with three stops → two segments.
        v1 = _visit("S1", NOON, NOON + 30)
        v2 = _visit("S2", NOON + 630, NOON + 660)
        v3 = _visit("S3", NOON + 960)
        fact, seg = self._run([v1, v2, v3])
        assert len(fact) == 2
        assert len(seg) == 2

    def test_segment_day_aggregates(self):
        # Two observations of the same segment (different trips).
        v1a = _visit("S1", NOON, NOON + 30, trip_id="T1")
        v2a = _visit("S2", NOON + 630, trip_id="T1")
        v1b = _visit("S1", NOON + 3600, NOON + 3630, trip_id="T2")
        v2b = _visit("S2", NOON + 4230, trip_id="T2")
        fact, seg = self._run([v1a, v2a, v1b, v2b])
        assert len(fact) == 2
        assert len(seg) == 1
        row = seg[0]
        assert row["sample_count"] == 2
        assert row["speed_p50_mph"] is not None
        assert row["speed_mean_mph"] is not None

    def test_drops_missing_coords(self):
        coords = {"S1": COORDS["S1"]}  # S2 missing
        v1 = _visit("S1", NOON, NOON + 30)
        v2 = _visit("S2", NOON + 630)
        fact, _ = self._run([v1, v2], coords=coords)
        assert fact == []

    def test_drops_non_positive_transit(self):
        # arrival_ts of second stop <= departure_ts of first → invalid.
        v1 = _visit("S1", NOON, NOON + 60)
        v2 = _visit("S2", NOON + 50)  # arrives before V1 departed
        fact, _ = self._run([v1, v2])
        assert fact == []

    def test_drops_transit_above_max(self):
        # 7200 s transit > default max of 3600 → artifact, must be dropped.
        v1 = _visit("S1", NOON, NOON)
        v2 = _visit("S2", NOON + 7200)
        fact, _ = self._run([v1, v2])
        assert fact == []

    def test_respects_custom_max_transit(self):
        # 600 s transit is normally kept; a 500 s cap drops it.
        v1 = _visit("S1", NOON, NOON + 30)
        v2 = _visit("S2", NOON + 630)
        fact_normal, _ = self._run([v1, v2])
        fact_tight, _ = self._run([v1, v2], max_transit_s=500)
        assert len(fact_normal) == 1
        assert fact_tight == []

    def test_drops_speed_above_max(self):
        # Teleportation: S1 to S2 in 1 second → thousands of mph.
        v1 = _visit("S1", NOON, NOON)
        v2 = _visit("S2", NOON + 1)
        fact, _ = self._run([v1, v2])
        assert fact == []

    def test_respects_custom_max_speed(self):
        # At 600 s transit, speed is ~5–10 mph; raising the cap to 1 mph would drop it.
        v1 = _visit("S1", NOON, NOON + 30)
        v2 = _visit("S2", NOON + 630)
        fact_normal, _ = self._run([v1, v2])
        fact_tight, _ = self._run([v1, v2], max_speed_mph=1.0)
        assert len(fact_normal) == 1
        assert fact_tight == []

    def test_drops_visit_without_route_id(self):
        v1 = _visit("S1", NOON, NOON + 30, route_id=None)
        v2 = _visit("S2", NOON + 630, route_id=None)
        # Manually set route_id=None via dataclass rebuild
        v1 = Visit(
            vehicle_id="V1",
            stop_id="S1",
            arrival_ts=NOON,
            departure_ts=NOON + 30,
            ping_count=1,
            route_id=None,
            trip_id="T1",
            direction_id=0,
        )
        v2 = Visit(
            vehicle_id="V1",
            stop_id="S2",
            arrival_ts=NOON + 630,
            departure_ts=NOON + 630,
            ping_count=1,
            route_id=None,
            trip_id="T1",
            direction_id=0,
        )
        fact, _ = self._run([v1, v2])
        assert fact == []

    def test_trips_are_independent(self):
        # Visits from two different trips should not be paired across trip boundaries.
        v_t1 = _visit("S1", NOON, NOON + 30, trip_id="T1")
        v_t2 = _visit("S2", NOON + 630, trip_id="T2")
        fact, _ = self._run([v_t1, v_t2])
        assert fact == []

    def test_schema_compatible_output(self):
        import pyarrow as pa

        v1 = _visit("S1", NOON, NOON + 30)
        v2 = _visit("S2", NOON + 630, stop_sequence=2)
        fact, seg = self._run([v1, v2])
        # Should cast to schema without error.
        pa.Table.from_pylist(fact, schema=SEGMENT_SPEED_SCHEMA)
        pa.Table.from_pylist(seg, schema=SEGMENT_DAY_SCHEMA)

    def test_stop_sequence_propagated(self):
        v1 = _visit("S1", NOON, NOON + 30, stop_sequence=1)
        v2 = _visit("S2", NOON + 630, stop_sequence=2)
        fact, _ = self._run([v1, v2])
        assert fact[0]["from_stop_sequence"] == 1
        assert fact[0]["to_stop_sequence"] == 2

    def test_segment_day_distance_constant(self):
        # distance_m in segment_day should be the GTFS-derived haversine, stable
        # across repeated observations of the same segment.
        v1a = _visit("S1", NOON, NOON + 30, trip_id="T1")
        v2a = _visit("S2", NOON + 630, trip_id="T1")
        v1b = _visit("S1", NOON + 3600, NOON + 3630, trip_id="T2")
        v2b = _visit("S2", NOON + 4230, trip_id="T2")
        _, seg = self._run([v1a, v2a, v1b, v2b])
        expected_d = _haversine_m(*COORDS["S1"], *COORDS["S2"])
        assert math.isclose(seg[0]["distance_m"], expected_d, rel_tol=1e-9)
