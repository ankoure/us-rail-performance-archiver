"use client";

import { DayPicker, useDay } from "@/components/DayPicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { AlertCard } from "@/components/AlertCard";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import { useSection } from "@/lib/useSection";
import type { AlertRow } from "@/lib/types";

const MAX_ALERTS = 50;
const SEVERITY_RANK: Record<string, number> = { SEVERE: 0, WARNING: 1, INFO: 2 };

function severityRank(row: AlertRow): number {
  return SEVERITY_RANK[row.alert.severity_level ?? ""] ?? 3;
}

export default function AlertsPage() {
  const scope = useSection();
  const { section } = scope;
  const day = useDay();

  // The alerts endpoint has no route filter, so a section narrows the feed
  // client-side to alerts naming at least one of its routes. Alerts that name
  // no route at all (agency-wide notices) stay visible in every section.
  const { data, error, loading } = useApiData<AlertRow[]>(
    `${scope.key}|${day}`,
    () => api.alerts(scope.agencyId, day),
    scope.ready,
  );
  const allRows = data
    ? data
        .filter((row) => {
          const routes = row.alert.informed_entity
            .map((e) => e.route_id)
            .filter((r): r is string => Boolean(r));
          return routes.length === 0 || routes.some((r) => scope.includes(r));
        })
        .sort((a, b) => severityRank(a) - severityRank(b) || b.last_seen - a.last_seen)
    : null;
  const rows = allRows?.slice(0, MAX_ALERTS) ?? null;

  return (
    <>
      <div className="filter-bar">
        <DayPicker />
      </div>
      <FilterContext scope={section.label} when={day} />
      <main>
        {error && <ErrorState what="alerts">{error}</ErrorState>}
        {!error && loading && <LoadingState />}
        {!error && rows && rows.length === 0 && (
          <EmptyState>No alerts recorded for {day}.</EmptyState>
        )}
        {allRows && allRows.length > MAX_ALERTS && (
          <p className="card-hint">
            Showing the {MAX_ALERTS} most severe/recent of {allRows.length} alerts.
          </p>
        )}
        {rows?.map((row) => (
          <AlertCard
            key={row.alert_id}
            row={row}
            agency={scope.agencyId}
            section={section.slug}
            day={day}
          />
        ))}
      </main>
    </>
  );
}
