import agencyModes from "./agencyModes.json";
import {
  canonicalMode,
  OTHER_ROUTE_MODE_LABEL,
  ROUTE_MODE_ORDER,
  routeModeLabel,
} from "./routeModes";
import type { RouteRow } from "./types";

export interface Section {
  slug: string;
  label: string;
  /** Explicit route membership. Takes precedence over `modes`. */
  routeIds?: string[];
  /** Membership by RouteRow.mode. */
  modes?: string[];
  /** Shown in place of the generic empty state when the section has no routes. */
  note?: string;
}

/** Every agency has this one: the unfiltered, agency-wide view. */
export const ALL_SECTION: Section = { slug: "all", label: "All routes" };

/**
 * Hand-curated section lists, for agencies whose riders think in named lines
 * rather than GTFS modes. Agencies absent here get one section per mode their
 * routes actually use (see `autoSections`).
 *
 * Kept as TypeScript rather than YAML — the pattern used by
 * dashboard/api/*_metadata.yaml — because sections are a purely front-end
 * concern that both `generateStaticParams` and the browser bundle have to
 * read, and a static export leaves no server to parse YAML at request time.
 */
export const CURATED_SECTIONS: Record<string, Section[]> = {
  MBTA: [
    { slug: "red-line", label: "Red Line", routeIds: ["Red"] },
    { slug: "orange-line", label: "Orange Line", routeIds: ["Orange"] },
    { slug: "blue-line", label: "Blue Line", routeIds: ["Blue"] },
    {
      slug: "green-line",
      label: "Green Line",
      routeIds: ["Green-B", "Green-C", "Green-D", "Green-E"],
    },
    { slug: "mattapan-line", label: "Mattapan Line", routeIds: ["Mattapan"] },
    { slug: "buses", label: "Buses", modes: ["bus"] },
    { slug: "commuter-rail", label: "Commuter Rail", modes: ["commuter_rail"] },
    {
      slug: "the-ride",
      label: "The RIDE",
      routeIds: [],
      note: "The RIDE is paratransit and MBTA publishes no GTFS-realtime feed for it, so there is nothing archived to report on.",
    },
    { slug: "ferry", label: "Ferry", modes: ["ferry_other"] },
  ],
};

function modeSlug(mode: string): string {
  return mode.replace(/_/g, "-");
}

function modeSection(mode: string): Section {
  return { slug: modeSlug(mode), label: routeModeLabel(mode), modes: [mode] };
}

/** One section per mode, ordered by the shared mode taxonomy. */
export function autoSections(modes: string[]): Section[] {
  const known = ROUTE_MODE_ORDER.filter((m) => modes.includes(m));
  const unknown = modes.filter((m) => !ROUTE_MODE_ORDER.includes(m)).sort();
  return [...known, ...unknown].map(modeSection);
}

/**
 * The sections for an agency, always led by ALL_SECTION. `routes` is optional:
 * pass it to derive an uncurated agency's sections from live data, or omit it
 * to fall back to the build-time manifest (which is what generateStaticParams
 * uses, and what a page renders before its routes have loaded).
 */
export function sectionsFor(agencyId: string, routes?: RouteRow[] | null): Section[] {
  const curated = CURATED_SECTIONS[agencyId];
  if (curated) return [ALL_SECTION, ...curated];

  const modes = routes
    ? [...new Set(routes.map((r) => canonicalMode(r.mode)).filter((m): m is string => Boolean(m)))]
    : ((agencyModes as Record<string, string[]>)[agencyId] ?? []);
  return [ALL_SECTION, ...autoSections(modes)];
}

export function findSection(agencyId: string, slug: string, routes?: RouteRow[] | null): Section {
  const match = sectionsFor(agencyId, routes).find((s) => s.slug === slug);
  // An unknown slug can only be reached by hand-editing the URL; treat it as
  // an empty section rather than crashing the page.
  return match ?? { slug, label: OTHER_ROUTE_MODE_LABEL, routeIds: [] };
}

/** The subset of `routes` belonging to `section`. */
export function sectionRoutes(section: Section, routes: RouteRow[]): RouteRow[] {
  if (section.slug === ALL_SECTION.slug) return routes;
  if (section.routeIds) {
    const ids = new Set(section.routeIds);
    return routes.filter((r) => ids.has(r.route_id));
  }
  if (section.modes) {
    const modes = new Set(section.modes);
    return routes.filter((r) => modes.has(canonicalMode(r.mode) ?? ""));
  }
  return [];
}

/**
 * Build-time section slugs for one agency. Uncurated agencies come from the
 * generated manifest, so the export only contains sections that exist rather
 * than every mode crossed with every agency.
 */
export function sectionSlugs(agencyId: string): string[] {
  return sectionsFor(agencyId).map((s) => s.slug);
}
