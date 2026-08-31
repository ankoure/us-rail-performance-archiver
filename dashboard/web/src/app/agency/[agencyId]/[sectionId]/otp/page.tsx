"use client";

import Link from "next/link";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { OtpStackedBarChart, type OtpBarDatum } from "@/components/OtpStackedBarChart";
import { StatTile, type StatDelta } from "@/components/StatTile";
import { api } from "@/lib/apiClient";
import { previousPeriod } from "@/lib/dates";
import { topByRoute } from "@/lib/rankings";
import { useApiData } from "@/lib/useApiData";
import { useSection } from "@/lib/useSection";
import type { RouteDayOtp, StopDayOtp } from "@/lib/types";

function pctDelta(current: number, prev: number): StatDelta {
  const diff = current - prev;
  const direction: StatDelta["direction"] = diff > 0.05 ? "up" : diff < -0.05 ? "down" : "flat";
  return { direction, text: `${diff >= 0 ? "+" : ""}${diff.toFixed(1)} pts vs. previous period` };
}

const MIN_MATCHED_FOR_RANKING = 10;
const MAX_STOPS_PER_ROUTE = 5;

function aggregateByRoute(rows: RouteDayOtp[]): OtpBarDatum[] {
  const byRoute = new Map<string, OtpBarDatum>();
  for (const row of rows) {
    const existing = byRoute.get(row.route_id) ?? {
      route_id: row.route_id,
      on_time_count: 0,
      early_count: 0,
      late_count: 0,
    };
    existing.on_time_count += row.on_time_count;
    existing.early_count += row.early_count;
    existing.late_count += row.late_count;
    byRoute.set(row.route_id, existing);
  }
  return [...byRoute.values()].sort((a, b) => a.route_id.localeCompare(b.route_id));
}

export default function OtpPage() {
  const scope = useSection();
  const { section } = scope;
  const line = useLine();
  const { start, end } = useDateRange();
  const routeFilter = scope.apiRouteFilter(line);

  const { data, error, loading } = useApiData<[RouteDayOtp[], StopDayOtp[]]>(
    `${scope.key}|${line}|${start}|${end}`,
    () =>
      Promise.all([
        api.routeDayOtp(scope.agencyId, {
          start_date: start,
          end_date: end,
          route_id: routeFilter,
        }),
        api.stopDayOtp(scope.agencyId, {
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

  const routeAgg = routeRows ? aggregateByRoute(routeRows) : null;
  const MAX_CHART_ROUTES = 15;
  const chartRoutes = routeAgg
    ? [...routeAgg]
        .sort(
          (a, b) =>
            b.on_time_count +
            b.early_count +
            b.late_count -
            (a.on_time_count + a.early_count + a.late_count),
        )
        .slice(0, MAX_CHART_ROUTES)
    : null;
  const overall = routeRows?.reduce(
    (acc, r) => ({
      matched: acc.matched + r.matched_count,
      onTime: acc.onTime + r.on_time_count,
    }),
    { matched: 0, onTime: 0 },
  );

  const prevRange = previousPeriod(start, end);
  const { data: prevRouteRows } = useApiData<RouteDayOtp[]>(
    `prev-otp|${scope.key}|${line}|${prevRange.start}|${prevRange.end}`,
    () =>
      api.routeDayOtp(scope.agencyId, {
        start_date: prevRange.start,
        end_date: prevRange.end,
        route_id: routeFilter,
      }),
    scope.ready,
  );
  const prevOverall = prevRouteRows
    ?.filter((r) => scope.includes(r.route_id))
    .reduce(
      (acc, r) => ({
        matched: acc.matched + r.matched_count,
        onTime: acc.onTime + r.on_time_count,
      }),
      { matched: 0, onTime: 0 },
    );
  const otpDelta =
    overall && overall.matched > 0 && prevOverall && prevOverall.matched > 0
      ? pctDelta(
          (overall.onTime / overall.matched) * 100,
          (prevOverall.onTime / prevOverall.matched) * 100,
        )
      : undefined;

  const worstStops = stopRows
    ? topByRoute(
        [...stopRows]
          .filter((r) => r.on_time_pct !== null && r.matched_count >= MIN_MATCHED_FOR_RANKING)
          .sort((a, b) => (a.on_time_pct ?? 0) - (b.on_time_pct ?? 0)),
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
        {error && <ErrorState what="OTP data">{error}</ErrorState>}
        {!error && (
          <>
            <div className="card">
              <h2>
                On-time performance, {start} – {end}
              </h2>
              {overall && overall.matched > 0 && (
                <div className="stat-row">
                  <StatTile
                    label="Overall on-time %"
                    value={`${((overall.onTime / overall.matched) * 100).toFixed(1)}%`}
                    delta={otpDelta}
                  />
                  <StatTile label="Matched events" value={overall.matched.toLocaleString()} />
                  <StatTile label="Routes" value={String(routeAgg?.length ?? 0)} />
                </div>
              )}
              {loading && <LoadingState />}
              {routeAgg && routeAgg.length === 0 && (
                <EmptyState>No OTP data for this range.</EmptyState>
              )}
              {chartRoutes && chartRoutes.length > 0 && (
                <>
                  {routeAgg && routeAgg.length > MAX_CHART_ROUTES && (
                    <p className="card-hint">
                      Showing the {MAX_CHART_ROUTES} busiest of {routeAgg.length} routes by matched
                      events.
                    </p>
                  )}
                  <OtpStackedBarChart data={chartRoutes} />
                </>
              )}
            </div>

            <div className="card">
              <h2>Worst-performing stops</h2>
              <p className="card-hint">
                Lowest on-time % over the selected range (top 25, min {MIN_MATCHED_FOR_RANKING}{" "}
                matched events, max {MAX_STOPS_PER_ROUTE} per route).
              </p>
              {loading && <LoadingState />}
              {worstStops && worstStops.length === 0 && (
                <EmptyState>No stop OTP data for this range.</EmptyState>
              )}
              {worstStops && worstStops.length > 0 && (
                <div className="table-scroll">
                  <table>
                    <thead>
                      <tr>
                        <th>Stop</th>
                        <th>Route</th>
                        <th>Service date</th>
                        <th>On-time %</th>
                        <th>Matched</th>
                        <th>Arr delay p50 (s)</th>
                        <th>Arr delay p90 (s)</th>
                      </tr>
                    </thead>
                    <tbody>
                      {worstStops.map((r, i) => (
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
                          <td>{r.on_time_pct !== null ? `${r.on_time_pct.toFixed(1)}%` : "—"}</td>
                          <td>{r.matched_count}</td>
                          <td>{r.arr_delay_p50_s ?? "—"}</td>
                          <td>{r.arr_delay_p90_s ?? "—"}</td>
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
