"use client";

import { AgencyMultiPicker, useAgencies } from "@/components/AgencyMultiPicker";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { MetricBarChart } from "@/components/MetricBarChart";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import type { AgencyMetricsSummary } from "@/lib/types";

const MAX_COMPARE_AGENCIES = 6;

export default function ComparePage() {
  const agencies = useAgencies();
  const { start, end } = useDateRange();

  const withinLimit = agencies.length > 0 && agencies.length <= MAX_COMPARE_AGENCIES;

  const { data, error, loading } = useApiData<AgencyMetricsSummary[]>(
    `compare|${agencies.join(",")}|${start}|${end}`,
    () => api.agenciesSummary({ agency: agencies, start_date: start, end_date: end }),
    withinLimit,
  );

  const rows = data
    ? [...data].sort((a, b) => (b.on_time_pct ?? -1) - (a.on_time_pct ?? -1))
    : null;
  const speedRows = rows?.filter((r) => r.avg_speed_mph !== null) ?? null;

  return (
    <>
      <div className="filter-bar">
        <AgencyMultiPicker />
        <DateRangePicker />
      </div>
      <main>
        {agencies.length === 0 && (
          <EmptyState>Pick two or more agencies above to compare.</EmptyState>
        )}
        {agencies.length > MAX_COMPARE_AGENCIES && (
          <EmptyState>Pick at most {MAX_COMPARE_AGENCIES} agencies at a time.</EmptyState>
        )}
        {withinLimit && error && <ErrorState what="comparison data">{error}</ErrorState>}
        {withinLimit && !error && loading && <LoadingState />}
        {withinLimit && !error && rows && rows.length > 0 && (
          <>
            <div className="card">
              <h2>
                On-time % by agency, {start} – {end}
              </h2>
              <MetricBarChart
                data={rows.map((r) => ({ name: r.name, on_time_pct: r.on_time_pct ?? 0 }))}
                categoryKey="name"
                series={[{ dataKey: "on_time_pct", label: "On-time %", color: "var(--series-1)" }]}
              />
            </div>

            {speedRows && speedRows.length > 0 && (
              <div className="card">
                <h2>Average speed (p50, mph) by agency</h2>
                <p className="card-hint">Only agencies with speed data for this range are shown.</p>
                <MetricBarChart
                  data={speedRows.map((r) => ({
                    name: r.name,
                    avg_speed_mph: r.avg_speed_mph as number,
                  }))}
                  categoryKey="name"
                  series={[
                    {
                      dataKey: "avg_speed_mph",
                      label: "Avg speed (mph)",
                      color: "var(--series-3)",
                    },
                  ]}
                />
              </div>
            )}

            <div className="card">
              <h2>Total delay minutes by agency</h2>
              <MetricBarChart
                data={rows.map((r) => ({
                  name: r.name,
                  total_delay_minutes: r.total_delay_minutes,
                }))}
                categoryKey="name"
                series={[
                  {
                    dataKey: "total_delay_minutes",
                    label: "Delay minutes",
                    color: "var(--series-2)",
                  },
                ]}
              />
            </div>

            <div className="card">
              <h2>Details</h2>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>Agency</th>
                      <th>On-time %</th>
                      <th>Matched events</th>
                      <th>Avg speed (mph)</th>
                      <th>Delay minutes</th>
                      <th>Alerts</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.agency_id}>
                        <td>{r.name}</td>
                        <td>{r.on_time_pct !== null ? `${r.on_time_pct.toFixed(1)}%` : "—"}</td>
                        <td>{r.matched_count.toLocaleString()}</td>
                        <td>{r.avg_speed_mph !== null ? r.avg_speed_mph.toFixed(1) : "—"}</td>
                        <td>{r.total_delay_minutes.toLocaleString()}</td>
                        <td>{r.alert_count.toLocaleString()}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </>
        )}
        {withinLimit && !error && rows && rows.length === 0 && (
          <EmptyState>No data for the selected agencies and range.</EmptyState>
        )}
      </main>
    </>
  );
}
