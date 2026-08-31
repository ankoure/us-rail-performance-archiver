"use client";

import { useMemo } from "react";
import { useRouter, usePathname, useSearchParams } from "next/navigation";
import { api } from "./apiClient";
import type { RouteShapeResponse, StopRow } from "./types";
import { useApiData } from "./useApiData";

export interface OrderedStop {
  stop_id: string;
  label: string;
}

/**
 * The stops of one route in travel order, for a from/to picker.
 *
 * Order comes from the route_shape_stops mart's `dist_m` (metres along the
 * canonical shape), which is the only source that knows the real sequence;
 * /stops is an unordered agency-wide manifest and only supplies names.
 */
export function useRouteStops(agencyId: string, routeId: string, directionId: number) {
  const { data: shape } = useApiData<RouteShapeResponse>(
    `route-shape|${agencyId}|${routeId}`,
    () => api.routeShape(agencyId, routeId),
    Boolean(routeId),
  );
  const { data: stops } = useApiData<StopRow[]>(`stops|${agencyId}`, () => api.stops(agencyId));

  return useMemo(() => {
    if (!shape) return null;
    const names = new Map((stops ?? []).map((s) => [s.stop_id, s.stop_name]));
    const seen = new Set<string>();
    return shape.stops
      .filter((s) => s.direction_id === directionId)
      .sort((a, b) => a.dist_m - b.dist_m)
      .filter((s) => (seen.has(s.stop_id) ? false : (seen.add(s.stop_id), true)))
      .map((s) => ({ stop_id: s.stop_id, label: names.get(s.stop_id) ?? s.stop_id }));
  }, [shape, stops, directionId]);
}

export interface StopPair {
  from: string;
  to: string;
  direction: number;
  set(next: { from?: string; to?: string; direction?: number }): void;
  swap(): void;
}

/** from/to/direction held in the query string, so a chosen pair is linkable. */
export function useStopPair(): StopPair {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const from = searchParams.get("from") ?? "";
  const to = searchParams.get("to") ?? "";
  const direction = Number(searchParams.get("direction") ?? "0");

  function write(next: Record<string, string>) {
    const params = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(next)) {
      if (value) params.set(key, value);
      else params.delete(key);
    }
    router.push(`${pathname}?${params.toString()}`);
  }

  return {
    from,
    to,
    direction,
    set(next) {
      const patch: Record<string, string> = {};
      if (next.from !== undefined) patch.from = next.from;
      if (next.to !== undefined) patch.to = next.to;
      if (next.direction !== undefined) {
        patch.direction = String(next.direction);
        // Stop ids are direction-specific on some feeds and the ordering
        // certainly is, so a direction change invalidates the chosen pair.
        patch.from = "";
        patch.to = "";
      }
      write(patch);
    },
    swap() {
      write({ from: to, to: from });
    },
  };
}
