"use client";

import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { StatTile } from "@/components/StatTile";
import { StopPairPicker } from "@/components/StopPairPicker";
import { TripRunScatter } from "@/components/TripRunScatter";
import { api } from "@/lib/apiClient";
import { formatDuration } from "@/lib/durations";
import type { TripMetrics } from "@/lib/types";
import { useApiData } from "@/lib/useApiData";
import { useSection } from "@/lib/useSection";
import { useRouteStops, useStopPair } from "@/lib/useStopPair";

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
}

export default function TripsPage() {
  const scope = useSection();
  const { section } = scope;
  const line = useLine();
  const { start, end } = useDateRange();
  const pair = useStopPair();

  const stops = useRouteStops(scope.agencyId, line, pair.direction);
  const ready = Boolean(line && pair.from && pair.to);

  const { data, error, loading } = useApiData<TripMetrics>(
    `trips|${scope.agencyId}|${line}|${pair.direction}|${pair.from}|${pair.to}|${start}|${end}`,
    () =>
      api.tripMetrics(scope.agencyId, {
        route_id: line,
        from_stop_id: pair.from,
        to_stop_id: pair.to,
        start_date: start,
        end_date: end,
        direction_id: pair.direction,
      }),
    ready,
  );

  const runs = data?.runs ?? null;
  const travel = runs?.map((r) => r.travel_time_s) ?? [];
  const waits = runs?.flatMap((r) => (r.headway_s === null ? [] : [r.headway_s])) ?? [];
  const label = (id: string) => stops?.find((s) => s.stop_id === id)?.label ?? id;

  return (
    <>
      <div className="filter-bar">
        {scope.routes && scope.routes.length > 0 && <LinePicker routes={scope.routes} />}
        <StopPairPicker stops={stops} pair={pair} directions={[0, 1]} />
        <DateRangePicker />
      </div>
      <FilterContext scope={section.label} line={line || undefined} when={`${start} – ${end}`} />
      <main>
        {!line && <EmptyState>Pick a line above to choose a pair of stops.</EmptyState>}
        {line && !stops && <LoadingState what="stops" />}
        {line && stops && stops.length === 0 && (
          <EmptyState>No stop ordering is available for this route yet.</EmptyState>
        )}
        {line && stops && stops.length > 0 && !ready && (
          <EmptyState>Pick a start and end stop to see every trip between them.</EmptyState>
        )}
        {ready && error && <ErrorState what="trip data">{error}</ErrorState>}
        {ready && !error && (
          <div className="card">
            <h2>
              {label(pair.from)} → {label(pair.to)}
            </h2>
            <p className="card-hint">
              Each point is one vehicle&rsquo;s run, placed at the time it left {label(pair.from)}.
            </p>
            {loading && <LoadingState />}
            {runs && runs.length === 0 && (
              <EmptyState>
                No trips ran the whole way from {label(pair.from)} to {label(pair.to)} in this
                range. If these stops are served in the other direction, try Swap.
              </EmptyState>
            )}
            {runs && runs.length > 0 && (
              <>
                <div className="stat-row">
                  <StatTile label="Trips" value={runs.length.toLocaleString()} />
                  <StatTile label="Median travel time" value={formatDuration(median(travel))} />
                  <StatTile label="Median headway" value={formatDuration(median(waits))} />
                </div>
                <TripRunScatter runs={runs} />
              </>
            )}
          </div>
        )}
      </main>
    </>
  );
}
