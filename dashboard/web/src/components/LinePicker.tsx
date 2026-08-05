"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { MBTA_RAIL_LINES } from "@/lib/mbtaRailLines";

export function useLine(): string {
  const searchParams = useSearchParams();
  return searchParams.get("line") ?? "";
}

export function LinePicker() {
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
        <option value="">Select a line…</option>
        {MBTA_RAIL_LINES.map((l) => (
          <option key={l.routeId} value={l.routeId}>
            {l.label}
          </option>
        ))}
      </select>
    </div>
  );
}
