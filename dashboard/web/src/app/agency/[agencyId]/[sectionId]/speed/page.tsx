"use client";

import { useMemo } from "react";
import Link from "next/link";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { TimeSeriesChart, type ChartAnnotation } from "@/components/TimeSeriesChart";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import { useSection } from "@/lib/useSection";
import { aggregateSegmentSpeeds, type SegmentSpeedAgg } from "@/lib/segments";
import type { DirectionRow, LineDelaysSummary, SegmentDayRow, StopRow } from "@/lib/types";

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

function buildSlowestSegments(rows: SegmentDayRow[]): SegmentSpeedAgg[] {
  return aggregateSegmentSpeeds(rows)
    .filter((s) => s.sample_count >= MIN_SAMPLES_FOR_SLOWEST)
    .sort((a, b) => a.avg_speed_mph - b.avg_speed_mph)
    .slice(0, MAX_SLOWEST_SEGMENTS);
}

export default function SpeedPage() {
  const scope = useSection();
  const { section } = scope;
  const line = useLine();
  const { start, end } = useDateRange();

  const enabled = scope.ready && Boolean(line);

  // Routes/names/directions come from the latest static schedule, not tied to
  // the selected date range — fetched once per agency, not per line or date
  // change.
  const { data: stopsData } = useApiData<StopRow[]>(`stops|${scope.agencyId}`, () =>
    api.stops(scope.agencyId),
  );
  const { data: directionsData } = useApiData<DirectionRow[]>(`directions|${scope.agencyId}`, () =>
    api.directions(scope.agencyId),
  );

  const { data, error, loading } = useApiData<SegmentDayRow[]>(
    `${scope.key}|${line}|${start}|${end}`,
    () => api.segmentDay(scope.agencyId, { start_date: start, end_date: end, route_id: [line] }),
    enabled,
  );

  // Best-effort: delay-day markers on the speed/travel-time charts below. Not
  // gated on `error` from the segment fetch — a failure here just means no
  // markers render, not a page-level error.
  const { data: delayRows } = useApiData<LineDelaysSummary[]>(
    `delays|${scope.key}|${start}|${end}`,
    () => api.lineDelaysRange(scope.agencyId, { start_date: start, end_date: end }),
  );
  const delayAnnotations: ChartAnnotation[] = (delayRows ?? [])
    .filter((r) => r.total_delay_minutes > 0 && r.service_date)
    .map((r) => ({ date: r.service_date!, label: `${r.total_delay_minutes}m delay` }));

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
    // Most non-MBTA feeds omit directions.txt, so this is a plain "Direction N"
    // fallback, not agency-specific guesswork — see analysis/static_gtfs.py's
    // directions.txt handling for why the real data can't always be there.
    return directionLabelByRoute.get(`${routeId}|${directionId}`) ?? `Direction ${directionId}`;
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
        {scope.routes && scope.routes.length > 0 && <LinePicker routes={scope.routes} />}
        <DateRangePicker />
      </div>
      <FilterContext scope={section.label} line={line || undefined} when={`${start} – ${end}`} />
      <main>
        {!scope.routes && <LoadingState what="routes" />}
        {scope.routes && scope.routes.length === 0 && (
          <EmptyState>No routes found for this agency yet.</EmptyState>
        )}
        {scope.routes && scope.routes.length > 0 && !line && (
          <EmptyState>Pick a line above to load speed data.</EmptyState>
        )}
        {enabled && error && <ErrorState what="speed data">{error}</ErrorState>}
        {enabled && !error && (
          <>
            <p className="card-hint">
              <Link
                href={`/agency/${scope.agencyId}/${section.slug}/speed/map?line=${line}&start=${start}&end=${end}`}
              >
                View on map →
              </Link>
            </p>
            <div className="card">
              <h2>
                Average speed (p50), {line}, {start} – {end}
              </h2>
              <p className="card-hint">
                Straight-line distance divided into in-motion time, weighted by sample count per
                day. Distance is shape-following where GTFS shape geometry is available.
              </p>
              {loading && <LoadingState />}
              {speedSeries && speedSeries.points.length === 0 && (
                <EmptyState>No speed data for this range.</EmptyState>
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
                  annotations={delayAnnotations}
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
              {loading && <LoadingState />}
              {travelSeries && travelSeries.points.length === 0 && (
                <EmptyState>No travel time data for this range.</EmptyState>
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
                  annotations={delayAnnotations}
                />
              )}
            </div>

            <div className="card">
              <h2>Slowest segments</h2>
              <p className="card-hint">
                {`Lowest average speed over the selected range (top ${MAX_SLOWEST_SEGMENTS}, min ${MIN_SAMPLES_FOR_SLOWEST} samples). Station names are from the latest static-GTFS snapshot, not the schedule in effect on any particular day in this range.`}
              </p>
              {loading && <LoadingState />}
              {slowest && slowest.length === 0 && (
                <EmptyState>No segment data for this range.</EmptyState>
              )}
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
