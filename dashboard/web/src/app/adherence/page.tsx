"use client";

import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { DayPicker, useDay } from "@/components/DayPicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { NoAgencySelected } from "@/components/NoAgencySelected";
import { StatTile } from "@/components/StatTile";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import type { Adherence, RouteRow } from "@/lib/types";

const MAX_ROWS = 500;

function formatDelay(seconds: number | null): string {
  if (seconds === null) return "—";
  return `${seconds > 0 ? "+" : ""}${seconds}s`;
}

export default function AdherencePage() {
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");
  const line = useLine();
  const day = useDay();

  const { data: routesData } = useApiData<RouteRow[]>(`routes|${agency}`, Boolean(agency), () =>
    api.routes(agency!),
  );

  const {
    data: rows,
    error,
    loading,
  } = useApiData<Adherence[]>(`adherence|${agency}|${line}|${day}`, Boolean(agency), () =>
    api.adherence(agency!, {
      start_date: day,
      end_date: day,
      route_id: line ? [line] : undefined,
      limit: MAX_ROWS,
    }),
  );

  const summary = rows?.reduce(
    (acc, r) => {
      acc.total += 1;
      if (r.on_time) acc.onTime += 1;
      else if ((r.arrival_delay_s ?? 0) > 0) acc.late += 1;
      else acc.early += 1;
      return acc;
    },
    { total: 0, onTime: 0, late: 0, early: 0 },
  );

  return (
    <>
      <div className="filter-bar">
        <AgencyPicker />
        {agency && routesData && routesData.length > 0 && (
          <LinePicker routes={routesData} allowAll />
        )}
        <DayPicker />
      </div>
      {agency && <FilterContext agency={agency} line={line || undefined} when={day} />}
      <main>
        {!agency && <NoAgencySelected />}
        {agency && error && <ErrorState what="adherence data">{error}</ErrorState>}
        {agency && !error && loading && <LoadingState />}
        {summary && summary.total > 0 && (
          <div className="card">
            <h2>Trip adherence, {day}</h2>
            <div className="stat-row">
              <StatTile label="Events" value={summary.total.toLocaleString()} />
              <StatTile label="On-time" value={summary.onTime.toLocaleString()} />
              <StatTile label="Late" value={summary.late.toLocaleString()} />
              <StatTile label="Early" value={summary.early.toLocaleString()} />
            </div>
            {rows && rows.length >= MAX_ROWS && (
              <p className="card-hint">
                Showing the first {MAX_ROWS} events for this day — pick a route above to narrow the
                results.
              </p>
            )}
          </div>
        )}
        {rows && rows.length === 0 && (
          <EmptyState>No adherence events recorded for {day}.</EmptyState>
        )}
        {rows && rows.length > 0 && (
          <div className="card">
            <h2>Stop-level events</h2>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Route</th>
                    <th>Trip</th>
                    <th>Stop</th>
                    <th>Seq</th>
                    <th>Status</th>
                    <th>Arr delay</th>
                    <th>Dep delay</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => (
                    <tr key={`${r.trip_id}-${r.stop_id}-${r.stop_sequence}-${i}`}>
                      <td>{r.route_id}</td>
                      <td>{r.trip_id}</td>
                      <td>{r.stop_id}</td>
                      <td>{r.stop_sequence}</td>
                      <td>{r.status}</td>
                      <td>{formatDelay(r.arrival_delay_s)}</td>
                      <td>{formatDelay(r.departure_delay_s)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </main>
    </>
  );
}
