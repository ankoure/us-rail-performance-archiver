"use client";

import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { NoAgencySelected } from "@/components/NoAgencySelected";
import { OtpStackedBarChart, type OtpBarDatum } from "@/components/OtpStackedBarChart";
import { StatTile } from "@/components/StatTile";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import type { RouteDayOtp, StopDayOtp } from "@/lib/types";

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
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");
  const { start, end } = useDateRange();

  const { data, error } = useApiData<[RouteDayOtp[], StopDayOtp[]]>(
    `${agency}|${start}|${end}`,
    Boolean(agency),
    () =>
      Promise.all([
        api.routeDayOtp(agency!, { start_date: start, end_date: end }),
        api.stopDayOtp(agency!, { start_date: start, end_date: end }),
      ]),
  );
  const routeRows = data?.[0] ?? null;
  const stopRows = data?.[1] ?? null;

  const routeAgg = routeRows ? aggregateByRoute(routeRows) : null;
  const MAX_CHART_ROUTES = 15;
  const chartRoutes = routeAgg
    ? [...routeAgg]
        .sort(
          (a, b) =>
            b.on_time_count + b.early_count + b.late_count - (a.on_time_count + a.early_count + a.late_count),
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
  const worstStops = stopRows
    ? [...stopRows]
        .filter((r) => r.on_time_pct !== null)
        .sort((a, b) => (a.on_time_pct ?? 0) - (b.on_time_pct ?? 0))
        .slice(0, 25)
    : null;

  return (
    <>
      <div className="filter-bar">
        <AgencyPicker />
        <DateRangePicker />
      </div>
      <main>
        {!agency && <NoAgencySelected />}
        {agency && error && <p className="error-state">Failed to load OTP data: {error}</p>}
        {agency && !error && (
          <>
            <div className="card">
              <h2>On-time performance, {start} – {end}</h2>
              {overall && overall.matched > 0 && (
                <div className="stat-row">
                  <StatTile
                    label="Overall on-time %"
                    value={`${((overall.onTime / overall.matched) * 100).toFixed(1)}%`}
                  />
                  <StatTile label="Matched events" value={overall.matched.toLocaleString()} />
                  <StatTile label="Routes" value={String(routeAgg?.length ?? 0)} />
                </div>
              )}
              {!routeAgg && <p className="empty-state">Loading…</p>}
              {routeAgg && routeAgg.length === 0 && <p className="empty-state">No OTP data for this range.</p>}
              {chartRoutes && chartRoutes.length > 0 && (
                <>
                  {routeAgg && routeAgg.length > MAX_CHART_ROUTES && (
                    <p className="card-hint">
                      Showing the {MAX_CHART_ROUTES} busiest of {routeAgg.length} routes by matched events.
                    </p>
                  )}
                  <OtpStackedBarChart data={chartRoutes} />
                </>
              )}
            </div>

            <div className="card">
              <h2>Worst-performing stops</h2>
              <p className="card-hint">Lowest on-time % over the selected range (top 25).</p>
              {!worstStops && <p className="empty-state">Loading…</p>}
              {worstStops && worstStops.length === 0 && <p className="empty-state">No stop OTP data for this range.</p>}
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
                          <td>{r.route_id}</td>
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
