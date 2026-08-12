"use client";

import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { DayPicker, useDay } from "@/components/DayPicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { NoAgencySelected } from "@/components/NoAgencySelected";
import { AlertCard } from "@/components/AlertCard";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import type { AlertRow } from "@/lib/types";

const MAX_ALERTS = 50;
const SEVERITY_RANK: Record<string, number> = { SEVERE: 0, WARNING: 1, INFO: 2 };

function severityRank(row: AlertRow): number {
  return SEVERITY_RANK[row.alert.severity_level ?? ""] ?? 3;
}

export default function AlertsPage() {
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");
  const day = useDay();

  const { data, error, loading } = useApiData<AlertRow[]>(`${agency}|${day}`, Boolean(agency), () =>
    api.alerts(agency!, day),
  );
  const allRows = data
    ? [...data].sort((a, b) => severityRank(a) - severityRank(b) || b.last_seen - a.last_seen)
    : null;
  const rows = allRows?.slice(0, MAX_ALERTS) ?? null;

  return (
    <>
      <div className="filter-bar">
        <AgencyPicker />
        <DayPicker />
      </div>
      {agency && <FilterContext agency={agency} when={day} />}
      <main>
        {!agency && <NoAgencySelected />}
        {agency && error && <ErrorState what="alerts">{error}</ErrorState>}
        {agency && !error && loading && <LoadingState />}
        {agency && !error && rows && rows.length === 0 && (
          <EmptyState>No alerts recorded for {day}.</EmptyState>
        )}
        {allRows && allRows.length > MAX_ALERTS && (
          <p className="card-hint">
            Showing the {MAX_ALERTS} most severe/recent of {allRows.length} alerts.
          </p>
        )}
        {rows?.map((row) => (
          <AlertCard key={row.alert_id} row={row} agency={agency ?? undefined} day={day} />
        ))}
      </main>
    </>
  );
}
