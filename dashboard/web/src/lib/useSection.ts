"use client";

import { useMemo } from "react";
import { useParams } from "next/navigation";
import { api } from "./apiClient";
import { ALL_SECTION, findSection, sectionRoutes, type Section } from "./sections";
import type { RouteRow } from "./types";
import { useApiData } from "./useApiData";

/**
 * Longest route_id list we're willing to put in a query string. Beyond it a
 * section fetches agency-wide and narrows client-side instead — MBTA's bus
 * section alone is ~370 routes, which would be a multi-kilobyte URL.
 */
const MAX_ROUTE_ID_FILTER = 50;

export interface SectionScope {
  agencyId: string;
  section: Section;
  /** Routes in this section; null until the agency's route list has loaded. */
  routes: RouteRow[] | null;
  /**
   * False while a non-"all" section is still waiting on its route list. Metric
   * fetches gate on this so they aren't issued once unfiltered and then again
   * filtered.
   */
  ready: boolean;
  /** `route_id` filter to hand the API, or undefined to fetch agency-wide. */
  apiRouteFilter(line: string): string[] | undefined;
  /** Whether a row belongs to this section. Always applied, since
   * `apiRouteFilter` may have declined to filter server-side. */
  includes(routeId: string): boolean;
  /** Stable cache-key fragment for the current scope. */
  key: string;
}

export function useSection(): SectionScope {
  const { agencyId, sectionId } = useParams<{ agencyId: string; sectionId: string }>();

  const { data: allRoutes } = useApiData<RouteRow[]>(`routes|${agencyId}`, () =>
    api.routes(agencyId),
  );

  return useMemo(() => {
    const section = findSection(agencyId, sectionId, allRoutes);
    const isAll = section.slug === ALL_SECTION.slug;
    const routes = allRoutes ? sectionRoutes(section, allRoutes) : null;
    const ids = routes ? new Set(routes.map((r) => r.route_id)) : null;

    return {
      agencyId,
      section,
      routes,
      ready: isAll || ids !== null,
      apiRouteFilter(line: string) {
        if (line) return [line];
        if (isAll || !ids || ids.size > MAX_ROUTE_ID_FILTER) return undefined;
        return [...ids];
      },
      includes(routeId: string) {
        return isAll || ids === null || ids.has(routeId);
      },
      key: `${agencyId}|${sectionId}|${ids ? ids.size : "?"}`,
    };
  }, [agencyId, sectionId, allRoutes]);
}
