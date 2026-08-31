"use client";

import Link from "next/link";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { MetricBarChart } from "@/components/MetricBarChart";
import { api } from "@/lib/apiClient";
import { topByRoute } from "@/lib/rankings";
import { useApiData } from "@/lib/useApiData";
import { useSection } from "@/lib/useSection";
import type { RouteDayRow, StopDayRow } from "@/lib/types";

interface RouteAgg {
  route_id: string;
  headway_p50_s: number;
  dwell_p50_s: number;
  dwell_p90_s: number;
  visit_count: number;
}

const MAX_CHART_ROUTES = 15;
const MIN_VISITS_FOR_RANKING = 10;
const MAX_STOPS_PER_ROUTE = 5;

function mean(values: number[]): number {
  return values.reduce((a, b) => a + b, 0) / values.length;
}

function aggregateRoutes(rows: RouteDayRow[]): RouteAgg[] {
  const byRoute = new Map<string, RouteDayRow[]>();
  for (const row of rows) {
    const list = byRoute.get(row.route_id) ?? [];
    list.push(row);
    byRoute.set(row.route_id, list);
  }
  return [...byRoute.entries()]
    .map(([route_id, group]) => ({
      route_id,
      headway_p50_s: Math.round(mean(group.map((r) => r.headway_p50_s ?? 0))),
      dwell_p50_s: Math.round(mean(group.map((r) => r.dwell_p50_s ?? 0))),
      dwell_p90_s: Math.round(mean(group.map((r) => r.dwell_p90_s ?? 0))),
      visit_count: group.reduce((sum, r) => sum + r.visit_count, 0),
    }))
    .sort((a, b) => a.route_id.localeCompare(b.route_id));
}

function busiestRoutes(routeAgg: RouteAgg[]): RouteAgg[] {
  return [...routeAgg]
    .sort((a, b) => b.visit_count - a.visit_count)
    .slice(0, MAX_CHART_ROUTES)
    .sort((a, b) => a.route_id.localeCompare(b.route_id));
}

export default function HeadwaysDwellsPage() {
  const scope = useSection();
  const { section } = scope;
  const line = useLine();
  const { start, end } = useDateRange();
  const routeFilter = scope.apiRouteFilter(line);

  const { data, error, loading } = useApiData<[RouteDayRow[], StopDayRow[]]>(
    `${scope.key}|${line}|${start}|${end}`,
    () =>
      Promise.all([
        api.routeDay(scope.agencyId, {
          start_date: start,
          end_date: end,
          route_id: routeFilter,
        }),
        api.stopDay(scope.agencyId, {
          start_date: start,
          end_date: end,
          route_id: routeFilter,
        }),
      ]),
    scope.ready,
  );
  // `routeFilter` may have declined to filter server-side for a large section.
  const routeRows = data?.[0].filter((r) => scope.includes(r.route_id)) ?? null;
  const stopRows = data?.[1].filter((r) => scope.includes(r.route_id)) ?? null;

  const routeAgg = routeRows ? aggregateRoutes(routeRows) : null;
  const chartRoutes = routeAgg ? busiestRoutes(routeAgg) : null;
  const mostVariableStops = stopRows
    ? topByRoute(
        [...stopRows]
          .filter((r) => r.headway_cov !== null && r.visit_count >= MIN_VISITS_FOR_RANKING)
          .sort((a, b) => (b.headway_cov ?? 0) - (a.headway_cov ?? 0)),
        { routeId: (r) => r.route_id, limit: 25, perRouteCap: MAX_STOPS_PER_ROUTE },
      )
    : null;

  return (
    <>
      <div className="filter-bar">
        {scope.routes && scope.routes.length > 1 && <LinePicker routes={scope.routes} allowAll />}
        <DateRangePicker />
      </div>
      <FilterContext scope={section.label} line={line || undefined} when={`${start} – ${end}`} />
      <main>
        {error && <ErrorState what="data">{error}</ErrorState>}
        {!error && (
          <>
            <div className="card">
              <h2>
                Headway (p50), average by route, {start} – {end}
              </h2>
              {loading && <LoadingState />}
              {routeAgg && routeAgg.length === 0 && (
                <EmptyState>No data for this range.</EmptyState>
              )}
              {chartRoutes && chartRoutes.length > 0 && (
                <>
                  {routeAgg && routeAgg.length > MAX_CHART_ROUTES && (
                    <p className="card-hint">
                      Showing the {MAX_CHART_ROUTES} busiest of {routeAgg.length} routes by visit
                      count.
                    </p>
                  )}
                  <MetricBarChart
                    data={chartRoutes}
                    categoryKey="route_id"
                    series={[
                      {
                        dataKey: "headway_p50_s",
                        label: "Headway p50 (s)",
                        color: "var(--series-1)",
                      },
                    ]}
                  />
                </>
              )}
            </div>

            <div className="card">
              <h2>
                Dwell time, average by route, {start} – {end}
              </h2>
              {chartRoutes && chartRoutes.length > 0 && (
                <MetricBarChart
                  data={chartRoutes}
                  categoryKey="route_id"
                  series={[
                    { dataKey: "dwell_p50_s", label: "Dwell p50 (s)", color: "var(--series-1)" },
                    { dataKey: "dwell_p90_s", label: "Dwell p90 (s)", color: "var(--series-2)" },
                  ]}
                />
              )}
            </div>

            <div className="card">
              <h2>Most variable stops</h2>
              <p className="card-hint">
                Highest headway coefficient of variation over the selected range (top 25, min{" "}
                {MIN_VISITS_FOR_RANKING} visits, max {MAX_STOPS_PER_ROUTE} per route).
              </p>
              {loading && <LoadingState />}
              {mostVariableStops && mostVariableStops.length === 0 && (
                <EmptyState>No stop data for this range.</EmptyState>
              )}
              {mostVariableStops && mostVariableStops.length > 0 && (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Stop</th>
                        <th>Route</th>
                        <th>Service date</th>
                        <th>Headway CoV</th>
                        <th>Headway p50 (s)</th>
                        <th>Headway p90 (s)</th>
                        <th>Dwell p50 (s)</th>
                        <th>Dwell p90 (s)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {mostVariableStops.map((r, i) => (
                        <tr key={`${r.stop_id}-${r.service_date}-${i}`}>
                          <td>{r.stop_id}</td>
                          <td>
                            <Link
                              href={`/agency/${scope.agencyId}/${section.slug}/route?line=${r.route_id}&start=${start}&end=${end}`}
                            >
                              {r.route_id}
                            </Link>
                          </td>
                          <td>{r.service_date}</td>
                          <td>{r.headway_cov?.toFixed(2) ?? "—"}</td>
                          <td>{r.headway_p50_s ?? "—"}</td>
                          <td>{r.headway_p90_s ?? "—"}</td>
                          <td>{r.dwell_p50_s ?? "—"}</td>
                          <td>{r.dwell_p90_s ?? "—"}</td>
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
