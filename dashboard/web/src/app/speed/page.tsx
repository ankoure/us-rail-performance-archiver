"use client";

import { useMemo } from "react";
import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { LinePicker, useLine } from "@/components/LinePicker";
import { NoAgencySelected } from "@/components/NoAgencySelected";
import { TimeSeriesChart } from "@/components/TimeSeriesChart";
import { api } from "@/lib/apiClient";
import { directionLabel as fallbackDirectionLabel } from "@/lib/mbtaRailLines";
import { useApiData } from "@/lib/useApiData";
import type { DirectionRow, SegmentDayRow, StopRow } from "@/lib/types";

const MBTA_AGENCY_ID = "MBTA";
const MIN_SAMPLES_FOR_SLOWEST = 5;
const MAX_SLOWEST_SEGMENTS = 20;
const METERS_PER_MILE = 1609.344;

// A poller gap between two Visits gets recorded as one long "segment" spanning
// several real stations, not a data error compute_segment_speeds can detect —
// it just looks like a rare, slow inter-stop hop. A weighted average dilutes a
// handful of these among hundreds of genuine same-pair observations, but a sum
// has no such protection: on MBTA Red Line data, one contaminated day's sum
// came out to 1900+ minutes for a line that runs end to end in about an hour,
// entirely from single-observation segments spanning multiple real stops.
// Genuine adjacent-station segments occur on nearly every trip; requiring a
// much higher sample count for anything going into a sum (vs. a mean) is what
// separates the two.
const MIN_SAMPLES_FOR_TRAVEL_TIME_SUM = 10;

interface DayPoint {
  service_date: string;
  [seriesKey: string]: string | number | null;
}

interface SegmentAgg {
  from_stop_id: string;
  to_stop_id: string;
  direction_id: number | null;
  distance_m: number;
  sample_count: number;
  weighted_speed_sum: number;
}

function seriesKeyFor(directionId: number | null): string {
  return `dir${directionId ?? "null"}`;
}

/** One row per service_date, with a `dir{N}` column per direction present in the data. */
function buildDailySeries(
  rows: SegmentDayRow[],
  valueOf: (r: SegmentDayRow) => number | null,
  aggregate: "sum" | "weighted_mean",
  minSampleCount = 1,
): { points: DayPoint[]; directionIds: number[] } {
  const byDate = new Map<string, Map<string, { total: number; weight: number }>>();
  const directionIds = new Set<number>();

  for (const row of rows) {
    if (row.sample_count < minSampleCount) continue;
    const value = valueOf(row);
    if (value === null) continue;
    if (row.direction_id !== null) directionIds.add(row.direction_id);
    const key = seriesKeyFor(row.direction_id);
    const perDate = byDate.get(row.service_date) ?? new Map();
    const bucket = perDate.get(key) ?? { total: 0, weight: 0 };
    if (aggregate === "sum") {
      bucket.total += value;
      bucket.weight = 1;
    } else {
      bucket.total += value * row.sample_count;
      bucket.weight += row.sample_count;
    }
    perDate.set(key, bucket);
    byDate.set(row.service_date, perDate);
  }

  const points: DayPoint[] = [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([service_date, perDate]) => {
      const point: DayPoint = { service_date };
      for (const [key, { total, weight }] of perDate) {
        point[key] = weight > 0 ? total / weight : null;
      }
      return point;
    });

  return { points, directionIds: [...directionIds].sort() };
}

function buildSlowestSegments(rows: SegmentDayRow[]): (SegmentAgg & { avg_speed_mph: number })[] {
  const byKey = new Map<string, SegmentAgg>();
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
  return [...byKey.values()]
    .filter((s) => s.sample_count >= MIN_SAMPLES_FOR_SLOWEST)
    .map((s) => ({ ...s, avg_speed_mph: s.weighted_speed_sum / s.sample_count }))
    .sort((a, b) => a.avg_speed_mph - b.avg_speed_mph)
    .slice(0, MAX_SLOWEST_SEGMENTS);
}

export default function SpeedPage() {
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");
  const line = useLine();
  const { start, end } = useDateRange();

  const isMbta = agency === MBTA_AGENCY_ID;
  const enabled = isMbta && Boolean(line);

  const { data, error } = useApiData<SegmentDayRow[]>(
    `${agency}|${line}|${start}|${end}`,
    enabled,
    () => api.segmentDay(agency!, { start_date: start, end_date: end, route_id: [line] }),
  );

  // Names/directions come from the latest static-GTFS snapshot, not tied to the
  // selected date range — fetched once per agency, not per line or date change.
  const { data: stopsData } = useApiData<StopRow[]>(`stops|${agency}`, isMbta, () => api.stops(agency!));
  const { data: directionsData } = useApiData<DirectionRow[]>(
    `directions|${agency}`,
    isMbta,
    () => api.directions(agency!),
  );

  const stopNameById = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of stopsData ?? []) {
      if (s.stop_name) m.set(s.stop_id, s.stop_name);
    }
    return m;
  }, [stopsData]);

  const directionLabelByRoute = useMemo(() => {
    const m = new Map<string, string>();
    for (const d of directionsData ?? []) {
      if (d.direction && d.direction_destination) {
        m.set(`${d.route_id}|${d.direction_id}`, `${d.direction} to ${d.direction_destination}`);
      }
    }
    return m;
  }, [directionsData]);

  function resolveDirectionLabel(routeId: string, directionId: number): string {
    return directionLabelByRoute.get(`${routeId}|${directionId}`) ?? fallbackDirectionLabel(routeId, directionId);
  }

  function resolveStopName(stopId: string): string {
    return stopNameById.get(stopId) ?? stopId;
  }

  const speedSeries = data ? buildDailySeries(data, (r) => r.speed_p50_mph, "weighted_mean") : null;
  const travelSeries = data
    ? buildDailySeries(
        data,
        (r) => (r.transit_p50_s !== null ? r.transit_p50_s / 60 : null),
        "sum",
        MIN_SAMPLES_FOR_TRAVEL_TIME_SUM,
      )
    : null;
  const slowest = data ? buildSlowestSegments(data) : null;

  return (
    <>
      <div className="filter-bar">
        <AgencyPicker />
        {isMbta && <LinePicker />}
        <DateRangePicker />
      </div>
      <main>
        {!agency && <NoAgencySelected />}
        {agency && !isMbta && (
          <p className="empty-state">
            Speed &amp; Travel Time is available for MBTA rail lines only right now.
          </p>
        )}
        {isMbta && !line && <p className="empty-state">Pick a line above to load speed data.</p>}
        {enabled && error && <p className="error-state">Failed to load speed data: {error}</p>}
        {enabled && !error && (
          <>
            <div className="card">
              <h2>
                Average speed (p50), {line}, {start} – {end}
              </h2>
              <p className="card-hint">
                Straight-line distance divided into in-motion time, weighted by sample count per day. Distance is
                shape-following where GTFS shape geometry is available.
              </p>
              {!speedSeries && <p className="empty-state">Loading…</p>}
              {speedSeries && speedSeries.points.length === 0 && (
                <p className="empty-state">No speed data for this range.</p>
              )}
              {speedSeries && speedSeries.points.length > 0 && (
                <TimeSeriesChart
                  data={speedSeries.points}
                  dateKey="service_date"
                  series={speedSeries.directionIds.map((d, i) => ({
                    dataKey: seriesKeyFor(d),
                    label: resolveDirectionLabel(line, d),
                    color: i === 0 ? "var(--series-1)" : "var(--series-2)",
                  }))}
                  valueFormatter={(v) => `${v.toFixed(0)} mph`}
                />
              )}
            </div>

            <div className="card">
              <h2>
                End-to-end travel time, {line}, {start} – {end}
              </h2>
              <p className="card-hint">
                {`Sum of in-motion time across every observed inter-stop segment for the day (min ${MIN_SAMPLES_FOR_TRAVEL_TIME_SUM} samples per segment, to exclude rare vehicle-polling gaps that would otherwise look like one long hop) — an approximation of a full one-way run, not a single trip's actual duration.`}
              </p>
              {!travelSeries && <p className="empty-state">Loading…</p>}
              {travelSeries && travelSeries.points.length === 0 && (
                <p className="empty-state">No travel time data for this range.</p>
              )}
              {travelSeries && travelSeries.points.length > 0 && (
                <TimeSeriesChart
                  data={travelSeries.points}
                  dateKey="service_date"
                  series={travelSeries.directionIds.map((d, i) => ({
                    dataKey: seriesKeyFor(d),
                    label: resolveDirectionLabel(line, d),
                    color: i === 0 ? "var(--series-1)" : "var(--series-2)",
                  }))}
                  valueFormatter={(v) => `${v.toFixed(0)} min`}
                />
              )}
            </div>

            <div className="card">
              <h2>Slowest segments</h2>
              <p className="card-hint">
                {`Lowest average speed over the selected range (top ${MAX_SLOWEST_SEGMENTS}, min ${MIN_SAMPLES_FOR_SLOWEST} samples). Station names are from the latest static-GTFS snapshot, not the schedule in effect on any particular day in this range.`}
              </p>
              {!slowest && <p className="empty-state">Loading…</p>}
              {slowest && slowest.length === 0 && <p className="empty-state">No segment data for this range.</p>}
              {slowest && slowest.length > 0 && (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>From</th>
                        <th>To</th>
                        <th>Direction</th>
                        <th>Distance (mi)</th>
                        <th>Avg speed (mph)</th>
                        <th>Samples</th>
                      </tr>
                    </thead>
                    <tbody>
                      {slowest.map((s, i) => (
                        <tr key={`${s.from_stop_id}-${s.to_stop_id}-${s.direction_id}-${i}`}>
                          <td>{resolveStopName(s.from_stop_id)}</td>
                          <td>{resolveStopName(s.to_stop_id)}</td>
                          <td>{resolveDirectionLabel(line, s.direction_id ?? -1)}</td>
                          <td>{(s.distance_m / METERS_PER_MILE).toFixed(2)}</td>
                          <td>{s.avg_speed_mph.toFixed(1)}</td>
                          <td>{s.sample_count}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </>
        )}
      </main>
    </>
  );
}
