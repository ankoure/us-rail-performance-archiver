"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import type { RouteRow } from "@/lib/types";

export function useLine(): string {
  const searchParams = useSearchParams();
  return searchParams.get("line") ?? "";
}

function lineLabel(route: RouteRow): string {
  return route.route_short_name ?? route.route_id ?? route.route_long_name;
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

  return (
    <div className="field">
      <label htmlFor="line-picker">Line</label>
      <select id="line-picker" value={line} onChange={(e) => onChange(e.target.value)}>
        <option value="">{allowAll ? "All routes" : "Select a line…"}</option>
        {[...routes]
          .sort((a, b) => lineLabel(a).localeCompare(lineLabel(b)))
          .map((r) => (
            <option key={r.route_id} value={r.route_id}>
              {lineLabel(r)}
            </option>
          ))}
      </select>
    </div>
  );
}
