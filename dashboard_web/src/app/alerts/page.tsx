"use client";

import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { DayPicker, useDay } from "@/components/DayPicker";
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

  const { data, error } = useApiData<AlertRow[]>(`${agency}|${day}`, Boolean(agency), () =>
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
      <main>
        {!agency && <NoAgencySelected />}
        {agency && error && <p className="error-state">Failed to load alerts: {error}</p>}
        {agency && !error && !rows && <p className="empty-state">Loading…</p>}
        {agency && !error && rows && rows.length === 0 && (
          <p className="empty-state">No alerts recorded for {day}.</p>
        )}
        {allRows && allRows.length > MAX_ALERTS && (
          <p className="card-hint">
            Showing the {MAX_ALERTS} most severe/recent of {allRows.length} alerts.
          </p>
        )}
        {rows?.map((row) => (
          <AlertCard key={row.alert_id} row={row} />
        ))}
      </main>
    </>
  );
}
