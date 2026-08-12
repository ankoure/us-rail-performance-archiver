"use client";

import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { DayPicker, useDay } from "@/components/DayPicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { NoAgencySelected } from "@/components/NoAgencySelected";
import { MetricBarChart } from "@/components/MetricBarChart";
import { StatTile, type StatDelta } from "@/components/StatTile";
import { api } from "@/lib/apiClient";
import { dayBefore } from "@/lib/dates";
import { useApiData } from "@/lib/useApiData";

function minutesDelta(current: number, prev: number): StatDelta {
  const diff = current - prev;
  const direction: StatDelta["direction"] = diff > 0 ? "up" : diff < 0 ? "down" : "flat";
  return {
    direction,
    text: `${diff >= 0 ? "+" : ""}${diff.toLocaleString()} min vs. previous day`,
  };
}

export default function LineDelaysPage() {
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");
  const day = useDay();

  const {
    data: summary,
    error,
    loading,
  } = useApiData(`${agency}|${day}`, Boolean(agency), () => api.lineDelays(agency!, day));

  const prevDay = dayBefore(day);
  const { data: prevSummary } = useApiData(
    `prev-line-delays|${agency}|${prevDay}`,
    Boolean(agency),
    () => api.lineDelays(agency!, prevDay),
  );
  const delayDelta =
    summary && prevSummary
      ? minutesDelta(summary.total_delay_minutes, prevSummary.total_delay_minutes)
      : undefined;

  const minutesByType = summary
    ? Object.entries(summary.delay_by_type)
        .map(([type, minutes]) => ({ type, minutes }))
        .sort((a, b) => b.minutes - a.minutes)
    : null;
  const countByType = summary
    ? Object.entries(summary.count_by_type)
        .map(([type, count]) => ({ type, count }))
        .sort((a, b) => b.count - a.count)
    : null;

  return (
    <>
      <div className="filter-bar">
        <AgencyPicker />
        <DayPicker />
      </div>
      {agency && <FilterContext agency={agency} when={day} />}
      <main>
        {!agency && <NoAgencySelected />}
        {agency && error && <ErrorState what="line delays">{error}</ErrorState>}
        {agency && !error && loading && <LoadingState />}
        {summary && (
          <div className="card">
            <h2>Line delays, {summary.service_date ?? day}</h2>
            <div className="stat-row">
              <StatTile label="Total alerts" value={String(summary.alert_count)} />
              <StatTile label="Delay alerts" value={String(summary.delay_alert_count)} />
              <StatTile
                label="Total delay (min)"
                value={summary.total_delay_minutes.toLocaleString()}
                delta={delayDelta}
              />
              <StatTile label="Feeds" value={String(summary.feeds.length)} />
            </div>
          </div>
        )}

        {minutesByType && minutesByType.length > 0 && (
          <div className="card">
            <h2>Delay minutes by type</h2>
            <MetricBarChart
              data={minutesByType}
              categoryKey="type"
              series={[{ dataKey: "minutes", label: "Minutes", color: "var(--series-1)" }]}
            />
          </div>
        )}

        {countByType && countByType.length > 0 && (
          <div className="card">
            <h2>Alert count by type</h2>
            <MetricBarChart
              data={countByType}
              categoryKey="type"
              series={[{ dataKey: "count", label: "Alerts", color: "var(--series-2)" }]}
            />
          </div>
        )}

        {summary && minutesByType?.length === 0 && countByType?.length === 0 && (
          <EmptyState>No delay alerts recorded for {day}.</EmptyState>
        )}
      </main>
    </>
  );
}
