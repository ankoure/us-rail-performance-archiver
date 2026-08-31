"use client";

import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { StatTile } from "@/components/StatTile";
import { StopPairPicker } from "@/components/StopPairPicker";
import { TimeSeriesChart } from "@/components/TimeSeriesChart";
import { api } from "@/lib/apiClient";
import { formatDuration } from "@/lib/durations";
import type { TripMetrics } from "@/lib/types";
import { useApiData } from "@/lib/useApiData";
import { useSection } from "@/lib/useSection";
import { useRouteStops, useStopPair } from "@/lib/useStopPair";

const TRAVEL_SERIES = [
  { dataKey: "travel_time_p10_s", label: "Fastest 10%", color: "var(--series-3)" },
  { dataKey: "travel_time_p50_s", label: "Median", color: "var(--series-1)" },
  { dataKey: "travel_time_p90_s", label: "Slowest 10%", color: "var(--series-2)" },
];

const HEADWAY_SERIES = [
  { dataKey: "headway_p50_s", label: "Median headway", color: "var(--series-1)" },
  { dataKey: "headway_p90_s", label: "90th percentile", color: "var(--series-2)" },
];

export default function MultiDayTripsPage() {
  const scope = useSection();
  const { section } = scope;
  const line = useLine();
  const { start, end } = useDateRange();
  const pair = useStopPair();

  const stops = useRouteStops(scope.agencyId, line, pair.direction);
  const ready = Boolean(line && pair.from && pair.to);

  const { data, error, loading } = useApiData<TripMetrics>(
    `trips-multi|${scope.agencyId}|${line}|${pair.direction}|${pair.from}|${pair.to}|${start}|${end}`,
    () =>
      api.tripMetrics(scope.agencyId, {
        route_id: line,
        from_stop_id: pair.from,
        to_stop_id: pair.to,
        start_date: start,
        end_date: end,
        direction_id: pair.direction,
        aggregate: true,
      }),
    ready,
  );

  const days = data?.days ?? null;
  const label = (id: string) => stops?.find((s) => s.stop_id === id)?.label ?? id;
  const totalTrips = days?.reduce((n, d) => n + d.trip_count, 0) ?? 0;
  const medians = days?.flatMap((d) => (d.travel_time_p50_s === null ? [] : [d.travel_time_p50_s]));
  const best = medians?.length ? Math.min(...medians) : null;
  const worst = medians?.length ? Math.max(...medians) : null;

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
        {line && stops && stops.length > 0 && !ready && (
          <EmptyState>Pick a start and end stop to compare days.</EmptyState>
        )}
        {ready && error && <ErrorState what="trip data">{error}</ErrorState>}
        {ready && !error && (
          <>
            <div className="card">
              <h2>
                {label(pair.from)} → {label(pair.to)}, {start} – {end}
              </h2>
              {loading && <LoadingState />}
              {days && days.length === 0 && (
                <EmptyState>
                  No trips ran the whole way between these stops in this range.
                </EmptyState>
              )}
              {days && days.length > 0 && (
                <>
                  <div className="stat-row">
                    <StatTile label="Trips" value={totalTrips.toLocaleString()} />
                    <StatTile label="Best day (median)" value={formatDuration(best)} />
                    <StatTile label="Worst day (median)" value={formatDuration(worst)} />
                  </div>
                  <TimeSeriesChart
                    data={days}
                    dateKey="service_date"
                    series={TRAVEL_SERIES}
                    valueFormatter={formatDuration}
                  />
                </>
              )}
            </div>

            {days && days.length > 0 && (
              <div className="card">
                <h2>Headway at {label(pair.from)}</h2>
                <p className="card-hint">
                  Gap between vehicles arriving at the start of the trip — how long a rider waits.
                </p>
                <TimeSeriesChart
                  data={days}
                  dateKey="service_date"
                  series={HEADWAY_SERIES}
                  valueFormatter={formatDuration}
                />
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}
