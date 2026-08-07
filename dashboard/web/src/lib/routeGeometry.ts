import type { RouteShapePoint, RouteShapeStop, StopRow } from "./types";

export interface LonLat {
  lon: number;
  lat: number;
}

/** shape_id -> that shape's points, sorted by point_sequence. A route+direction
 * commonly has more than one shape (a branching line has one per branch — see
 * analysis/static_gtfs.py's route_direction_shapes), so points must stay
 * grouped by shape, never flattened across shapes (their point_sequence
 * numbering only makes sense within one shape). */
export function groupPointsByShape(points: RouteShapePoint[]): Map<string, RouteShapePoint[]> {
  const byShape = new Map<string, RouteShapePoint[]>();
  for (const p of points) {
    const arr = byShape.get(p.shape_id) ?? [];
    arr.push(p);
    byShape.set(p.shape_id, arr);
  }
  for (const arr of byShape.values()) arr.sort((a, b) => a.point_sequence - b.point_sequence);
  return byShape;
}

export function groupStopOffsetsByShape(stops: RouteShapeStop[]): Map<string, Map<string, number>> {
  const byShape = new Map<string, Map<string, number>>();
  for (const s of stops) {
    const m = byShape.get(s.shape_id) ?? new Map<string, number>();
    m.set(s.stop_id, s.dist_m);
    byShape.set(s.shape_id, m);
  }
  return byShape;
}

/** direction_id -> its shape_ids, most-common first — the backend
 * (StaticGtfs.route_direction_shapes) already orders shapes that way and the
 * API response preserves array order, so first-occurrence order here mirrors
 * that priority without needing an explicit rank field. */
export function shapesByDirection(points: RouteShapePoint[]): Map<number, string[]> {
  const byDirection = new Map<number, string[]>();
  for (const p of points) {
    const arr = byDirection.get(p.direction_id) ?? [];
    if (!arr.includes(p.shape_id)) arr.push(p.shape_id);
    byDirection.set(p.direction_id, arr);
  }
  return byDirection;
}

export function buildStopCoords(stops: StopRow[]): Map<string, LonLat> {
  const m = new Map<string, LonLat>();
  for (const s of stops) {
    if (s.stop_lat !== null && s.stop_lon !== null) {
      m.set(s.stop_id, { lat: s.stop_lat, lon: s.stop_lon });
    }
  }
  return m;
}

export interface SlicedGeometry {
  coordinates: [number, number][];
  /** True when no shape covered both stops and this fell back to a straight
   * line — callers should treat a low-sample-count fallback with suspicion
   * (see the function doc): it's the signature of a poller-gap artifact, not
   * a real uncovered branch. */
  isFallback: boolean;
}

/**
 * Slice the polyline between two stops for one direction, trying each of
 * that direction's shapes in priority order until one has offsets for both
 * stops (a branching line needs this — a trunk stop before the split is on
 * every branch's shape with a different offset on each, while a
 * branch-only stop is only on that branch's shape; two stops on different
 * branches with no shared shape have no valid slice at all).
 *
 * Once a shape is chosen, brackets the [from, to] offset range with the
 * nearest polyline point either side, so a very short segment (no interior
 * shape point between two close stops) still yields a real line rather than
 * a degenerate one-point slice.
 *
 * Falls back to a straight two-point line between the stops' own lat/lon
 * when no shape covers both — see analysis/static_gtfs.py's
 * `route_direction_stop_offsets`, the same "degrade, don't error" contract
 * `analysis/segment_speed.py`'s shape-distance fallback already uses. In
 * practice a fallback with very few samples is usually not a genuinely
 * uncovered branch but a poller-gap artifact — two Visits far apart on the
 * same trip recorded as one "segment" spanning several real stations (see
 * `speed/page.tsx`'s MIN_SAMPLES_FOR_TRAVEL_TIME_SUM comment for the same
 * phenomenon) — so callers should gate low-sample fallback segments rather
 * than draw the long, geographically nonsensical chord.
 *
 * Returns null only when neither geometry source has both stops.
 */
export function sliceSegmentGeometry(
  candidateShapeIds: string[] | undefined,
  pointsByShape: Map<string, RouteShapePoint[]>,
  stopOffsetsByShape: Map<string, Map<string, number>>,
  fromStopId: string,
  toStopId: string,
  stopCoords: Map<string, LonLat>,
): SlicedGeometry | null {
  for (const shapeId of candidateShapeIds ?? []) {
    const offsets = stopOffsetsByShape.get(shapeId);
    const points = pointsByShape.get(shapeId);
    const fromDist = offsets?.get(fromStopId);
    const toDist = offsets?.get(toStopId);
    if (!points || points.length < 2 || fromDist === undefined || toDist === undefined) continue;

    const lo = Math.min(fromDist, toDist);
    const hi = Math.max(fromDist, toDist);

    let loIdx = 0;
    for (let i = 0; i < points.length; i++) {
      if (points[i].dist_m <= lo) loIdx = i;
      else break;
    }
    let hiIdx = points.length - 1;
    for (let i = points.length - 1; i >= 0; i--) {
      if (points[i].dist_m >= hi) hiIdx = i;
      else break;
    }
    if (hiIdx <= loIdx) hiIdx = Math.min(loIdx + 1, points.length - 1);

    if (hiIdx > loIdx) {
      return {
        coordinates: points.slice(loIdx, hiIdx + 1).map((p): [number, number] => [p.lon, p.lat]),
        isFallback: false,
      };
    }
  }

  const a = stopCoords.get(fromStopId);
  const b = stopCoords.get(toStopId);
  if (!a || !b) return null;
  return {
    coordinates: [
      [a.lon, a.lat],
      [b.lon, b.lat],
    ],
    isFallback: true,
  };
}
