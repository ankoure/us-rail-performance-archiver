"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { AlertCard } from "@/components/AlertCard";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { NoAgencySelected } from "@/components/NoAgencySelected";
import { StatTile, type StatDelta } from "@/components/StatTile";
import { TimeSeriesChart, type ChartAnnotation } from "@/components/TimeSeriesChart";
import { api } from "@/lib/apiClient";
import { previousPeriod } from "@/lib/dates";
import { useApiData } from "@/lib/useApiData";
import type {
  AlertRow,
  LineDelaysSummary,
  RouteDayOtp,
  RouteDayRow,
  RouteRow,
  SegmentDayRow,
} from "@/lib/types";

function mean(values: number[]): number | null {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
}

function pctDelta(current: number, prev: number): StatDelta {
  const diff = current - prev;
  const direction: StatDelta["direction"] = diff > 0.05 ? "up" : diff < -0.05 ? "down" : "flat";
  return { direction, text: `${diff >= 0 ? "+" : ""}${diff.toFixed(1)} pts vs. previous period` };
}

interface DailyOtp {
  service_date: string;
  on_time_pct: number | null;
}

function dailyOtp(rows: RouteDayOtp[]): DailyOtp[] {
  const byDate = new Map<string, { matched: number; onTime: number }>();
  for (const r of rows) {
    const bucket = byDate.get(r.service_date) ?? { matched: 0, onTime: 0 };
    bucket.matched += r.matched_count;
    bucket.onTime += r.on_time_count;
    byDate.set(r.service_date, bucket);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([service_date, { matched, onTime }]) => ({
      service_date,
      on_time_pct: matched > 0 ? (onTime / matched) * 100 : null,
    }));
}

interface DailyHeadwayDwell {
  service_date: string;
  headway_p50_s: number | null;
  dwell_p50_s: number | null;
}

function dailyHeadwayDwell(rows: RouteDayRow[]): DailyHeadwayDwell[] {
  const byDate = new Map<string, RouteDayRow[]>();
  for (const r of rows) {
    const list = byDate.get(r.service_date) ?? [];
    list.push(r);
    byDate.set(r.service_date, list);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([service_date, group]) => ({
      service_date,
      headway_p50_s: mean(group.map((r) => r.headway_p50_s).filter((v): v is number => v !== null)),
      dwell_p50_s: mean(group.map((r) => r.dwell_p50_s).filter((v): v is number => v !== null)),
    }));
}

interface DailySpeed {
  service_date: string;
  speed_p50_mph: number | null;
}

function dailySpeed(rows: SegmentDayRow[]): DailySpeed[] {
  const byDate = new Map<string, { total: number; weight: number }>();
  for (const r of rows) {
    if (r.speed_p50_mph === null) continue;
    const bucket = byDate.get(r.service_date) ?? { total: 0, weight: 0 };
    bucket.total += r.speed_p50_mph * r.sample_count;
    bucket.weight += r.sample_count;
    byDate.set(r.service_date, bucket);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([service_date, { total, weight }]) => ({
      service_date,
      speed_p50_mph: weight > 0 ? total / weight : null,
    }));
}

export default function RoutePage() {
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");
  const line = useLine();
  const { start, end } = useDateRange();

  const enabled = Boolean(agency) && Boolean(line);

  const { data: routesData } = useApiData<RouteRow[]>(`routes|${agency}`, Boolean(agency), () =>
    api.routes(agency!),
  );
  const {
    data: otpRows,
    error: otpError,
    loading: otpLoading,
  } = useApiData<RouteDayOtp[]>(`route-otp|${agency}|${line}|${start}|${end}`, enabled, () =>
    api.routeDayOtp(agency!, { start_date: start, end_date: end, route_id: [line] }),
  );

  const { data: hdRows, loading: hdLoading } = useApiData<RouteDayRow[]>(
    `route-hd|${agency}|${line}|${start}|${end}`,
    enabled,
    () => api.routeDay(agency!, { start_date: start, end_date: end, route_id: [line] }),
  );

  const { data: speedRows, loading: speedLoading } = useApiData<SegmentDayRow[]>(
    `route-speed|${agency}|${line}|${start}|${end}`,
    enabled,
    () => api.segmentDay(agency!, { start_date: start, end_date: end, route_id: [line] }),
  );

  // The raw per-alert list is still day-only server-side; only the
  // delay-minutes summary below got a ranged endpoint (see B3), so this
  // covers the end of the selected range rather than the full range.
  const { data: alertRows, loading: alertsLoading } = useApiData<AlertRow[]>(
    `route-alerts|${agency}|${line}|${end}`,
    enabled,
    () => api.alerts(agency!, end),
  );

  const { data: delayRows } = useApiData<LineDelaysSummary[]>(
    `route-delays|${agency}|${start}|${end}`,
    enabled,
    () => api.lineDelaysRange(agency!, { start_date: start, end_date: end }),
  );
  const delayAnnotations: ChartAnnotation[] = (delayRows ?? [])
    .filter((r) => r.total_delay_minutes > 0 && r.service_date)
    .map((r) => ({ date: r.service_date!, label: `${r.total_delay_minutes}m delay` }));

  const otpSeries = otpRows ? dailyOtp(otpRows) : null;
  const hdSeries = hdRows ? dailyHeadwayDwell(hdRows) : null;
  const speedSeries = speedRows ? dailySpeed(speedRows) : null;
  const routeAlerts = alertRows
    ? alertRows.filter((row) => row.alert.informed_entity.some((e) => e.route_id === line))
    : null;

  const overallOtp = otpRows?.reduce(
    (acc, r) => ({ matched: acc.matched + r.matched_count, onTime: acc.onTime + r.on_time_count }),
    { matched: 0, onTime: 0 },
  );

  const prevRange = previousPeriod(start, end);
  const { data: prevOtpRows } = useApiData<RouteDayOtp[]>(
    `prev-route-otp|${agency}|${line}|${prevRange.start}|${prevRange.end}`,
    enabled,
    () =>
      api.routeDayOtp(agency!, {
        start_date: prevRange.start,
        end_date: prevRange.end,
        route_id: [line],
      }),
  );
  const prevOverallOtp = prevOtpRows?.reduce(
    (acc, r) => ({ matched: acc.matched + r.matched_count, onTime: acc.onTime + r.on_time_count }),
    { matched: 0, onTime: 0 },
  );
  const otpDelta =
    overallOtp && overallOtp.matched > 0 && prevOverallOtp && prevOverallOtp.matched > 0
      ? pctDelta(
          (overallOtp.onTime / overallOtp.matched) * 100,
          (prevOverallOtp.onTime / prevOverallOtp.matched) * 100,
        )
      : undefined;

  const avgHeadway = hdSeries
    ? mean(hdSeries.map((d) => d.headway_p50_s).filter((v): v is number => v !== null))
    : null;
  const avgDwell = hdSeries
    ? mean(hdSeries.map((d) => d.dwell_p50_s).filter((v): v is number => v !== null))
    : null;
  const avgSpeed = speedSeries
    ? mean(speedSeries.map((d) => d.speed_p50_mph).filter((v): v is number => v !== null))
    : null;

  return (
    <>
      <div className="filter-bar">
        <AgencyPicker />
        {agency && routesData && routesData.length > 0 && <LinePicker routes={routesData} />}
        <DateRangePicker />
      </div>
      {agency && (
        <FilterContext agency={agency} line={line || undefined} when={`${start} – ${end}`} />
      )}
      <main>
        {!agency && <NoAgencySelected />}
        {agency && routesData && routesData.length > 0 && !line && (
          <EmptyState>Pick a route above to load route health.</EmptyState>
        )}
        {enabled && otpError && <ErrorState what="route health data">{otpError}</ErrorState>}
        {enabled && !otpError && (
          <>
            <div className="card">
              <h2>
                Route health, {line}, {start} – {end}
              </h2>
              <div className="stat-row">
                <StatTile
                  label="On-time %"
                  value={
                    overallOtp && overallOtp.matched > 0
                      ? `${((overallOtp.onTime / overallOtp.matched) * 100).toFixed(1)}%`
                      : "—"
                  }
                  delta={otpDelta}
                />
                <StatTile
                  label="Headway p50"
                  value={avgHeadway !== null ? `${Math.round(avgHeadway)}s` : "—"}
                />
                <StatTile
                  label="Dwell p50"
                  value={avgDwell !== null ? `${Math.round(avgDwell)}s` : "—"}
                />
                <StatTile
                  label="Avg speed"
                  value={avgSpeed !== null ? `${avgSpeed.toFixed(1)} mph` : "—"}
                />
              </div>
            </div>

            <div className="card">
              <h2>On-time % by day</h2>
              {otpLoading && <LoadingState />}
              {otpSeries && otpSeries.length === 0 && (
                <EmptyState>No OTP data for this range.</EmptyState>
              )}
              {otpSeries && otpSeries.length > 0 && (
                <TimeSeriesChart
                  data={otpSeries}
                  dateKey="service_date"
                  series={[
                    { dataKey: "on_time_pct", label: "On-time %", color: "var(--series-1)" },
                  ]}
                  valueFormatter={(v) => `${v.toFixed(0)}%`}
                  annotations={delayAnnotations}
                />
              )}
            </div>

            <div className="card">
              <h2>Headway &amp; dwell (p50) by day</h2>
              {hdLoading && <LoadingState />}
              {hdSeries && hdSeries.length === 0 && (
                <EmptyState>No headway/dwell data for this range.</EmptyState>
              )}
              {hdSeries && hdSeries.length > 0 && (
                <TimeSeriesChart
                  data={hdSeries}
                  dateKey="service_date"
                  series={[
                    {
                      dataKey: "headway_p50_s",
                      label: "Headway p50 (s)",
                      color: "var(--series-1)",
                    },
                    { dataKey: "dwell_p50_s", label: "Dwell p50 (s)", color: "var(--series-2)" },
                  ]}
                />
              )}
            </div>

            <div className="card">
              <h2>Average speed (p50) by day</h2>
              {speedLoading && <LoadingState />}
              {speedSeries && speedSeries.length === 0 && (
                <EmptyState>No speed data for this range.</EmptyState>
              )}
              {speedSeries && speedSeries.length > 0 && (
                <TimeSeriesChart
                  data={speedSeries}
                  dateKey="service_date"
                  series={[
                    {
                      dataKey: "speed_p50_mph",
                      label: "Speed p50 (mph)",
                      color: "var(--series-3)",
                    },
                  ]}
                  valueFormatter={(v) => `${v.toFixed(0)} mph`}
                  annotations={delayAnnotations}
                />
              )}
              <p className="card-hint">
                <Link href={`/speed?agency=${agency}&line=${line}&start=${start}&end=${end}`}>
                  View full speed detail →
                </Link>
              </p>
            </div>

            <div className="card">
              <h2>Alerts on {end}</h2>
              <p className="card-hint">
                Alert history isn&apos;t range-queryable yet, so this shows the end date of the
                selected range only.
              </p>
              {alertsLoading && <LoadingState />}
              {routeAlerts && routeAlerts.length === 0 && (
                <EmptyState>No alerts recorded for this route on {end}.</EmptyState>
              )}
              {routeAlerts?.map((row) => (
                <AlertCard key={row.alert_id} row={row} agency={agency ?? undefined} day={end} />
              ))}
            </div>
          </>
        )}
      </main>
    </>
  );
}
