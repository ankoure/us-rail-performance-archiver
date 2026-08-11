import type { SegmentDayRow } from "./types";

export interface SegmentSpeedAgg {
  from_stop_id: string;
  to_stop_id: string;
  direction_id: number | null;
  distance_m: number;
  sample_count: number;
  avg_speed_mph: number;
}

/** Weighted-mean p50 speed per (from_stop_id, to_stop_id, direction_id) across
 * every row supplied (e.g. every day in a date range), weighted by sample_count.
 * Shared by the slowest-segments table (speed/page.tsx) and the segment speed map. */
export function aggregateSegmentSpeeds(rows: SegmentDayRow[]): SegmentSpeedAgg[] {
  interface Acc {
    from_stop_id: string;
    to_stop_id: string;
    direction_id: number | null;
    distance_m: number;
    sample_count: number;
    weighted_speed_sum: number;
  }
  const byKey = new Map<string, Acc>();
  for (const row of rows) {
    if (row.speed_p50_mph === null) continue;
    const key = `${row.from_stop_id}|${row.to_stop_id}|${row.direction_id}`;
    const existing = byKey.get(key) ?? {
      from_stop_id: row.from_stop_id,
      to_stop_id: row.to_stop_id,
      direction_id: row.direction_id,
      distance_m: row.distance_m,
      sample_count: 0,
      weighted_speed_sum: 0,
    };
    existing.sample_count += row.sample_count;
    existing.weighted_speed_sum += row.speed_p50_mph * row.sample_count;
    byKey.set(key, existing);
  }
  return [...byKey.values()].map((s) => ({
    from_stop_id: s.from_stop_id,
    to_stop_id: s.to_stop_id,
    direction_id: s.direction_id,
    distance_m: s.distance_m,
    sample_count: s.sample_count,
    avg_speed_mph: s.weighted_speed_sum / s.sample_count,
  }));
}

// Reserved status ramp (see globals.css) repurposed as a slow→fast severity
// scale: a segment's relative standing among its peers *is* its performance
// state here, the same way the existing "Slowest segments" table already
// treats low speed as the notable/bad case — not a generic 4-way category.
// Index matches the `bucket` the segment_speed_map API assigns (0=critical
// .. 3=good); see dashboard/api/services/segment_speed_map.py.
export const SPEED_BUCKET_COLOR_VARS = [
  "--status-critical",
  "--status-serious",
  "--status-warning",
  "--status-good",
] as const;
export const INSUFFICIENT_DATA_COLOR_VAR = "--text-muted";
