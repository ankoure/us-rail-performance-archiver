"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { RouteRow } from "@/lib/types";
import {
  canonicalMode,
  OTHER_ROUTE_MODE_LABEL,
  ROUTE_MODE_LABELS,
  ROUTE_MODE_ORDER,
} from "@/lib/routeModes";

export function useLine(): string {
  const searchParams = useSearchParams();
  return searchParams.get("line") ?? "";
}

function lineLabel(route: RouteRow): string {
  return route.route_short_name ?? route.route_id ?? route.route_long_name;
}

function dedupeByLabel(routes: RouteRow[]): RouteRow[] {
  const seen = new Set<string>();
  return [...routes]
    .sort((a, b) => lineLabel(a).localeCompare(lineLabel(b)))
    .filter((r) => {
      const label = lineLabel(r);
      if (seen.has(label)) return false;
      seen.add(label);
      return true;
    });
}

function groupByMode(routes: RouteRow[]): { label: string; routes: RouteRow[] }[] {
  const byMode = new Map<string, RouteRow[]>();
  for (const r of routes) {
    const mode = canonicalMode(r.mode);
    const key = mode && ROUTE_MODE_LABELS[mode] ? mode : OTHER_ROUTE_MODE_LABEL;
    if (!byMode.has(key)) byMode.set(key, []);
    byMode.get(key)!.push(r);
  }
  return [...ROUTE_MODE_ORDER, OTHER_ROUTE_MODE_LABEL]
    .filter((key) => byMode.has(key))
    .map((key) => ({
      label: key === OTHER_ROUTE_MODE_LABEL ? OTHER_ROUTE_MODE_LABEL : ROUTE_MODE_LABELS[key],
      routes: byMode.get(key)!,
    }));
}

export function LinePicker({
  routes,
  allowAll = false,
}: {
  routes: RouteRow[];
  allowAll?: boolean;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const line = useLine();

  function onChange(nextLine: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (nextLine) {
      params.set("line", nextLine);
    } else {
      params.delete("line");
    }
    router.push(`${pathname}?${params.toString()}`);
  }

  const groups = groupByMode(dedupeByLabel(routes));

  return (
    <div className="field">
      <label htmlFor="line-picker">Line</label>
      <select id="line-picker" value={line} onChange={(e) => onChange(e.target.value)}>
        <option value="">{allowAll ? "All routes" : "Select a line…"}</option>
        {groups.map((group) => (
          <optgroup key={group.label} label={group.label}>
            {group.routes.map((r) => (
              <option key={r.route_id} value={r.route_id}>
                {lineLabel(r)}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}
