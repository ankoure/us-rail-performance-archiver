"""Tests for analysis/geo.py."""

from __future__ import annotations

import math

from analysis.geo import (
    cumulative_arc_length_m,
    haversine_m,
    project_point_to_polyline,
)

# Three collinear points running east along ~40N (Manhattan latitude), evenly
# spaced by construction so the middle point sits at the midpoint distance.
LINE = [
    (40.0, -74.00),
    (40.0, -73.99),
    (40.0, -73.98),
]


class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_m(40.0, -74.0, 40.0, -74.0) == 0.0

    def test_known_distance(self):
        # Times Square to 34th St Penn is ~1.25 km.
        d = haversine_m(40.7580, -73.9855, 40.7506, -73.9971)
        assert 1000 < d < 1600

    def test_symmetric(self):
        a = haversine_m(40.7580, -73.9855, 40.7506, -73.9971)
        b = haversine_m(40.7506, -73.9971, 40.7580, -73.9855)
        assert math.isclose(a, b)


class TestCumulativeArcLength:
    def test_empty(self):
        assert cumulative_arc_length_m([]) == []

    def test_single_point(self):
        assert cumulative_arc_length_m([(40.0, -74.0)]) == [0.0]

    def test_starts_at_zero_and_monotonic(self):
        cumulative = cumulative_arc_length_m(LINE)
        assert cumulative[0] == 0.0
        assert cumulative[1] > cumulative[0]
        assert cumulative[2] > cumulative[1]

    def test_matches_sum_of_consecutive_haversines(self):
        cumulative = cumulative_arc_length_m(LINE)
        expected_total = haversine_m(*LINE[0], *LINE[1]) + haversine_m(
            *LINE[1], *LINE[2]
        )
        assert math.isclose(cumulative[-1], expected_total, rel_tol=1e-9)


class TestProjectPointToPolyline:
    def test_fewer_than_two_points_returns_none(self):
        assert project_point_to_polyline(40.0, -74.0, [], []) is None
        assert project_point_to_polyline(40.0, -74.0, [(40.0, -74.0)], [0.0]) is None

    def test_point_on_first_vertex(self):
        cumulative = cumulative_arc_length_m(LINE)
        d = project_point_to_polyline(*LINE[0], LINE, cumulative)
        assert math.isclose(d, 0.0, abs_tol=1e-6)

    def test_point_on_last_vertex(self):
        cumulative = cumulative_arc_length_m(LINE)
        d = project_point_to_polyline(*LINE[-1], LINE, cumulative)
        assert math.isclose(d, cumulative[-1], rel_tol=1e-6)

    def test_point_on_middle_vertex(self):
        cumulative = cumulative_arc_length_m(LINE)
        d = project_point_to_polyline(*LINE[1], LINE, cumulative)
        assert math.isclose(d, cumulative[1], rel_tol=1e-6)

    def test_point_offset_perpendicular_projects_onto_line(self):
        # A touch north of the midpoint between LINE[0] and LINE[1] — the closest
        # point on the segment is still roughly its midpoint, not either endpoint.
        cumulative = cumulative_arc_length_m(LINE)
        mid_lon = (LINE[0][1] + LINE[1][1]) / 2
        d = project_point_to_polyline(40.0005, mid_lon, LINE, cumulative)
        seg_len = cumulative[1] - cumulative[0]
        assert 0.3 * seg_len < d < 0.7 * seg_len

    def test_point_beyond_start_clamps_to_first_vertex(self):
        cumulative = cumulative_arc_length_m(LINE)
        d = project_point_to_polyline(40.0, -74.02, LINE, cumulative)
        assert math.isclose(d, 0.0, abs_tol=1e-6)

    def test_point_beyond_end_clamps_to_last_vertex(self):
        cumulative = cumulative_arc_length_m(LINE)
        d = project_point_to_polyline(40.0, -73.96, LINE, cumulative)
        assert math.isclose(d, cumulative[-1], rel_tol=1e-6)

    def test_bent_polyline_distance_exceeds_straight_line(self):
        # An L-shaped shape: straight-line endpoint distance is the hypotenuse,
        # but the along-shape distance from start to end is the two legs summed.
        bend = [
            (40.00, -74.00),
            (40.00, -73.99),  # corner
            (40.01, -73.99),
        ]
        cumulative = cumulative_arc_length_m(bend)
        along_shape = cumulative[-1]
        straight_line = haversine_m(*bend[0], *bend[-1])
        assert along_shape > straight_line
