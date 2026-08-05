"""Shared geometry helpers: great-circle distance and point-to-polyline projection.

`haversine_m` backs the straight-line distance used throughout the gold layer.
`cumulative_arc_length_m` and `project_point_to_polyline` back shape-following
distance: projecting a stop onto its trip's GTFS shape polyline instead of
assuming a straight line between stations. GTFS's own `shape_dist_traveled`
column is unreliable in practice (e.g. MBTA's shapes.txt carries the column
but every value is null), so distance along a shape is derived purely from
point geometry here rather than trusted from the source file.
"""

from __future__ import annotations

import math

EARTH_R_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_R_M * math.asin(math.sqrt(a))


def cumulative_arc_length_m(points: list[tuple[float, float]]) -> list[float]:
    """Cumulative haversine distance (metres) at each point of an ordered polyline.

    `cumulative[0]` is always 0.0. GTFS shapes are dense enough (points every
    few to a few dozen metres) that summing consecutive point-to-point
    haversines is an accurate proxy for true track length.
    """
    cumulative = [0.0] * len(points)
    for i in range(1, len(points)):
        lat1, lon1 = points[i - 1]
        lat2, lon2 = points[i]
        cumulative[i] = cumulative[i - 1] + haversine_m(lat1, lon1, lat2, lon2)
    return cumulative


def _to_local_xy(lat: float, lon: float, ref_lat: float) -> tuple[float, float]:
    """Equirectangular projection to metres, centred on `ref_lat`.

    Valid only over the short spans this is used for (one shape segment, at
    most a few hundred metres) — flat-earth error at that scale is negligible
    next to GTFS shape point spacing itself.
    """
    x = math.radians(lon) * math.cos(math.radians(ref_lat)) * EARTH_R_M
    y = math.radians(lat) * EARTH_R_M
    return x, y


def project_point_to_polyline(
    lat: float,
    lon: float,
    points: list[tuple[float, float]],
    cumulative: list[float],
) -> float | None:
    """Distance along the polyline of the point closest to (lat, lon).

    Projects onto every consecutive segment (clamped to the segment's span)
    and returns the cumulative distance at the closest projection. `points`
    and `cumulative` must correspond 1:1 (see `cumulative_arc_length_m`).
    Returns None when there are fewer than 2 points to project onto.
    """
    if len(points) < 2:
        return None
    ref_lat = points[0][0]
    px, py = _to_local_xy(lat, lon, ref_lat)
    best_dist_sq = math.inf
    best_along = 0.0
    ax, ay = _to_local_xy(points[0][0], points[0][1], ref_lat)
    for i in range(len(points) - 1):
        bx, by = _to_local_xy(points[i + 1][0], points[i + 1][1], ref_lat)
        dx, dy = bx - ax, by - ay
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq == 0:
            t = 0.0
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
            t = max(0.0, min(1.0, t))
        proj_x, proj_y = ax + t * dx, ay + t * dy
        dist_sq = (px - proj_x) ** 2 + (py - proj_y) ** 2
        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_along = cumulative[i] + t * math.sqrt(seg_len_sq)
        ax, ay = bx, by
    return best_along
