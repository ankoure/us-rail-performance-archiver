# dashboard/api/services/segment_speed_map.py

from datetime import date

import pyarrow.compute as pc

from api.services import data
from api.services.agencies import Agency

# Below this many combined samples across the date range, a segment is
# "insufficient data" (bucket -1) rather than colored by its speed. Mirrors
# the map's old client-side MIN_SAMPLES.
MIN_SAMPLES = 5

SPEED_BUCKET_LABELS = ["Critical", "Serious", "Warning", "Good"]

_SegmentKey = tuple[str, str, int]


def _segment_key(from_stop_id: str, to_stop_id: str, direction_id: int) -> _SegmentKey:
    return (from_stop_id, to_stop_id, direction_id)


def _filter_to_route(table, route_id: str):
    if "route_id" not in table.column_names:
        return table
    return table.filter(pc.field("route_id") == route_id)


def _aggregate_segment_speeds(rows: list[dict]) -> list[dict]:
    """Weighted-mean p50 speed per (from_stop_id, to_stop_id, direction_id)
    across every row supplied (e.g. every day in a date range), weighted by
    sample_count. Port of dashboard/web/src/lib/segments.ts's
    aggregateSegmentSpeeds."""
    acc: dict[_SegmentKey, dict] = {}
    for row in rows:
        if row["speed_p50_mph"] is None or row["direction_id"] is None:
            continue
        key = _segment_key(row["from_stop_id"], row["to_stop_id"], row["direction_id"])
        entry = acc.setdefault(
            key,
            {
                "from_stop_id": row["from_stop_id"],
                "to_stop_id": row["to_stop_id"],
                "direction_id": row["direction_id"],
                "sample_count": 0,
                "weighted_speed_sum": 0.0,
            },
        )
        entry["sample_count"] += row["sample_count"]
        entry["weighted_speed_sum"] += row["speed_p50_mph"] * row["sample_count"]

    return [
        {
            "from_stop_id": e["from_stop_id"],
            "to_stop_id": e["to_stop_id"],
            "direction_id": e["direction_id"],
            "sample_count": e["sample_count"],
            "avg_speed_mph": e["weighted_speed_sum"] / e["sample_count"],
        }
        for e in acc.values()
    ]


def _assign_speed_buckets(
    segments: list[dict],
) -> tuple[dict[_SegmentKey, int], list[dict]]:
    """Rank-based quartile split: the slowest quarter of segments (by count,
    not by sample weight) is bucket 0 (critical), the fastest quarter is
    bucket 3 (good) -- relative to what's in `segments`, not a fixed mph
    scale, since e.g. commuter rail and subway have very different top
    speeds. Port of lib/segments.ts's assignSpeedBuckets."""
    sorted_segments = sorted(segments, key=lambda s: s["avg_speed_mph"])
    n = len(sorted_segments)
    bucket_by_key: dict[_SegmentKey, int] = {}
    speeds_by_bucket: list[list[float]] = [[], [], [], []]

    for i, s in enumerate(sorted_segments):
        frac = i / (n - 1) if n > 1 else 0.5
        bucket = min(3, int(frac * 4))
        key = _segment_key(s["from_stop_id"], s["to_stop_id"], s["direction_id"])
        bucket_by_key[key] = bucket
        speeds_by_bucket[bucket].append(s["avg_speed_mph"])

    legend = [
        {
            "bucket": bucket,
            "label": SPEED_BUCKET_LABELS[bucket],
            "min_mph": min(speeds),
            "max_mph": max(speeds),
        }
        for bucket, speeds in enumerate(speeds_by_bucket)
        if speeds
    ]
    return bucket_by_key, legend


def _group_points_by_shape(points: list[dict]) -> dict[str, list[dict]]:
    by_shape: dict[str, list[dict]] = {}
    for p in points:
        by_shape.setdefault(p["shape_id"], []).append(p)
    for pts in by_shape.values():
        pts.sort(key=lambda p: p["point_sequence"])
    return by_shape


def _group_stop_offsets_by_shape(stops: list[dict]) -> dict[str, dict[str, float]]:
    by_shape: dict[str, dict[str, float]] = {}
    for s in stops:
        by_shape.setdefault(s["shape_id"], {})[s["stop_id"]] = s["dist_m"]
    return by_shape


def _shapes_by_direction(points: list[dict]) -> dict[int, list[str]]:
    """direction_id -> its shape_ids, first-occurrence order -- mirrors
    analysis.static_gtfs.StaticGtfs.route_direction_shapes's priority, which
    the route_shapes mart's row order already preserves."""
    by_direction: dict[int, list[str]] = {}
    for p in points:
        shapes = by_direction.setdefault(p["direction_id"], [])
        if p["shape_id"] not in shapes:
            shapes.append(p["shape_id"])
    return by_direction


def _find_covering_shape(
    candidate_shape_ids: list[str],
    points_by_shape: dict[str, list[dict]],
    stop_offsets_by_shape: dict[str, dict[str, float]],
    from_stop_id: str,
    to_stop_id: str,
) -> tuple[str, list[dict], dict[str, float]] | None:
    """First of a direction's shapes (priority order) with offsets for both
    stops -- a stop pair spanning two branches with no shared shape returns
    None. See [[route_direction_stop_offsets]]'s doc for why a single shape
    covering both is required rather than mixing offsets across shapes."""
    for shape_id in candidate_shape_ids:
        offsets = stop_offsets_by_shape.get(shape_id)
        points = points_by_shape.get(shape_id)
        if not points or len(points) < 2 or not offsets:
            continue
        if from_stop_id in offsets and to_stop_id in offsets:
            return shape_id, points, offsets
    return None


def _bracket_geometry(
    points: list[dict], lo: float, hi: float
) -> list[tuple[float, float]]:
    """Slice `points` (one shape's polyline, sorted by dist_m) down to the
    stretch bracketing [lo, hi] with the nearest polyline point on either
    side. `points` has at least 2 entries by construction (checked by
    callers), so this always returns at least 2 coordinates."""
    lo_idx = 0
    for i, p in enumerate(points):
        if p["dist_m"] <= lo:
            lo_idx = i
        else:
            break
    hi_idx = len(points) - 1
    for i in range(len(points) - 1, -1, -1):
        if points[i]["dist_m"] >= hi:
            hi_idx = i
        else:
            break
    if hi_idx <= lo_idx:
        hi_idx = min(lo_idx + 1, len(points) - 1)
    return [(p["lon"], p["lat"]) for p in points[lo_idx : hi_idx + 1]]


def _slice_segment_hops(
    candidate_shape_ids: list[str],
    points_by_shape: dict[str, list[dict]],
    stop_offsets_by_shape: dict[str, dict[str, float]],
    from_stop_id: str,
    to_stop_id: str,
    stop_coords: dict[str, tuple[float, float]],
) -> list[dict] | None:
    """Slice the polyline between two stops for one direction into one hop
    per real intervening stop, so a poller-gap segment (e.g. the vehicle
    stream skipped a stop's ping and segment_speed paired two non-adjacent
    visits -- see analysis/segment_speed.py) renders as several
    station-to-station hops instead of one line spanning several real
    stations. Tries each of the direction's shapes in priority order until
    one has offsets for both stops (see [[_find_covering_shape]]), then
    brackets every real stop projected onto that same shape between the two
    offsets and emits one hop per consecutive pair, each carrying the full
    segment's aggregate speed (there's no finer-grained timing to split it
    by -- this is a display-only interpolation, not new measured data).
    Falls back to a single straight two-point hop between the stops' own
    lat/lon when no shape covers both (e.g. a stop pair spanning two
    branches with no shared shape) -- this is the same
    likely-poller-gap-artifact case the caller already down-weights via
    MIN_SAMPLES. Returns None only when neither geometry source has both
    stops. Historical note: this supersedes the single-hop
    dashboard/web/src/lib/routeGeometry.ts's sliceSegmentGeometry port --
    see that function's doc for the underlying branching-shape rationale."""
    covering = _find_covering_shape(
        candidate_shape_ids,
        points_by_shape,
        stop_offsets_by_shape,
        from_stop_id,
        to_stop_id,
    )
    if covering is not None:
        _shape_id, points, offsets = covering
        from_dist = offsets[from_stop_id]
        to_dist = offsets[to_stop_id]
        lo, hi = sorted((from_dist, to_dist))
        forward = from_dist <= to_dist
        intermediates = sorted(
            (
                (stop_id, dist)
                for stop_id, dist in offsets.items()
                if lo < dist < hi and stop_id not in (from_stop_id, to_stop_id)
            ),
            key=lambda item: item[1],
            reverse=not forward,
        )
        hop_stop_ids = [
            from_stop_id,
            *(stop_id for stop_id, _ in intermediates),
            to_stop_id,
        ]
        is_interpolated = len(hop_stop_ids) > 2
        hops = []
        for a_id, b_id in zip(hop_stop_ids, hop_stop_ids[1:]):
            hop_lo, hop_hi = sorted((offsets[a_id], offsets[b_id]))
            hops.append(
                {
                    "from_stop_id": a_id,
                    "to_stop_id": b_id,
                    "coordinates": _bracket_geometry(points, hop_lo, hop_hi),
                    "is_fallback": False,
                    "is_interpolated": is_interpolated,
                }
            )
        return hops

    a = stop_coords.get(from_stop_id)
    b = stop_coords.get(to_stop_id)
    if a is None or b is None:
        return None
    return [
        {
            "from_stop_id": from_stop_id,
            "to_stop_id": to_stop_id,
            "coordinates": [a, b],
            "is_fallback": True,
            "is_interpolated": False,
        }
    ]


def build_segment_speed_map(
    agency: Agency, route_id: str, start_date: date, end_date: date
) -> dict:
    """Ready-to-render payload for the segment speed map: per-segment
    LineString features sliced from the route's static-GTFS shape and
    colored by a relative (this-route, this-range) speed bucket, plus the
    route's stops as Point features and the bucket legend. Combines what
    used to be three separate fetches (segment_day, route_shape, stops) plus
    client-side aggregation/slicing/bucketing into one call -- see
    SegmentSpeedMap.tsx."""
    feeds = agency.feed_names

    segment_rows = data.read_kind(
        "segment_day",
        agency.feed_names,
        start_date,
        end_date,
        filters={"route_id": [route_id]},
    ).to_pylist()
    aggregated = _aggregate_segment_speeds(segment_rows)
    qualifying = [s for s in aggregated if s["sample_count"] >= MIN_SAMPLES]
    bucket_by_key, legend = _assign_speed_buckets(qualifying)

    shape_points = _filter_to_route(
        data.read_latest_version_mart("route_shapes", feeds), route_id
    ).to_pylist()
    shape_stops = _filter_to_route(
        data.read_latest_version_mart("route_shape_stops", feeds), route_id
    ).to_pylist()
    points_by_shape = _group_points_by_shape(shape_points)
    stop_offsets_by_shape = _group_stop_offsets_by_shape(shape_stops)
    shape_ids_by_direction = _shapes_by_direction(shape_points)

    stop_rows = data.read_latest_version_mart("gtfs_stops", feeds).to_pylist()
    stop_name_by_id = {s["stop_id"]: s["stop_name"] or s["stop_id"] for s in stop_rows}
    stop_coords: dict[str, tuple[float, float]] = {
        s["stop_id"]: (s["stop_lon"], s["stop_lat"])
        for s in stop_rows
        if s["stop_lat"] is not None and s["stop_lon"] is not None
    }

    direction_rows = _filter_to_route(
        data.read_latest_version_mart("gtfs_directions", feeds), route_id
    ).to_pylist()
    direction_label_by_id: dict[int, str] = {
        d["direction_id"]: f"{d['direction']} to {d['direction_destination']}"
        for d in direction_rows
        if d["direction"] and d["direction_destination"]
    }

    segment_features = []
    for s in aggregated:
        direction_id = s["direction_id"]
        hops = _slice_segment_hops(
            shape_ids_by_direction.get(direction_id, []),
            points_by_shape,
            stop_offsets_by_shape,
            s["from_stop_id"],
            s["to_stop_id"],
            stop_coords,
        )
        if hops is None:
            continue
        bucket = bucket_by_key.get(
            _segment_key(s["from_stop_id"], s["to_stop_id"], direction_id), -1
        )
        for hop in hops:
            # A low-sample fallback (no shape covers both stops) is almost
            # always a poller-gap artifact -- one rare "segment" spanning
            # several real stations -- not a genuinely uncovered branch; see
            # _slice_segment_hops's doc.
            if hop["is_fallback"] and s["sample_count"] < MIN_SAMPLES:
                continue
            segment_features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": hop["coordinates"],
                    },
                    "properties": {
                        "from_stop_id": hop["from_stop_id"],
                        "to_stop_id": hop["to_stop_id"],
                        "direction_id": direction_id,
                        "bucket": bucket,
                        "avg_speed_mph": s["avg_speed_mph"],
                        "sample_count": s["sample_count"],
                        "from_name": stop_name_by_id.get(
                            hop["from_stop_id"], hop["from_stop_id"]
                        ),
                        "to_name": stop_name_by_id.get(
                            hop["to_stop_id"], hop["to_stop_id"]
                        ),
                        "direction_label": direction_label_by_id.get(
                            direction_id, f"Direction {direction_id}"
                        ),
                        "is_interpolated": hop["is_interpolated"],
                    },
                }
            )

    route_stop_ids = {s["stop_id"] for s in shape_stops}
    stop_features = []
    for stop_id in route_stop_ids:
        coords = stop_coords.get(stop_id)
        if coords is None:
            continue
        stop_features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": list(coords)},
                "properties": {
                    "stop_id": stop_id,
                    "name": stop_name_by_id.get(stop_id, stop_id),
                },
            }
        )

    return {
        "segments": {"type": "FeatureCollection", "features": segment_features},
        "stops": {"type": "FeatureCollection", "features": stop_features},
        "legend": legend,
    }
