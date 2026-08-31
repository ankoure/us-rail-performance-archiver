"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { METRIC_PAGES, metricHref } from "@/lib/metricPages";
import { findSection } from "@/lib/sections";

const DAY_SLUGS = new Set(["alerts", "line-delays", "adherence"]);
const RANGE_SLUGS = new Set([
  "otp",
  "headways-dwells",
  "speed",
  "speed/map",
  "route",
  "trips",
  "trips/multi",
]);

/** The `/agency/<id>/<section>/` remainder of a pathname, e.g. "speed/map". */
function metricSlug(pathname: string): string {
  return pathname.split("/").slice(4).join("/").replace(/\/$/, "");
}

function slugKind(slug: string): "day" | "range" | null {
  if (DAY_SLUGS.has(slug)) return "day";
  if (RANGE_SLUGS.has(slug)) return "range";
  return null;
}

/**
 * Forwards the current filter state (date range/day, line, ...) to `target`,
 * translating between the day-picker and range-picker vocabularies so
 * switching tabs doesn't silently drop the selected date(s). Agency and
 * section are no longer in the query string — they live in the path.
 */
function targetHref(fromSlug: string, toSlug: string, href: string, current: URLSearchParams) {
  const next = new URLSearchParams(current.toString());
  const fromKind = slugKind(fromSlug);
  const toKind = slugKind(toSlug);

  if (fromKind === "day" && toKind === "range") {
    const day = next.get("day");
    if (day) {
      next.set("start", day);
      next.set("end", day);
    }
    next.delete("day");
  } else if (fromKind === "range" && toKind === "day") {
    const end = next.get("end");
    if (end) next.set("day", end);
    next.delete("start");
    next.delete("end");
  }

  const qs = next.toString();
  return qs ? `${href}?${qs}` : href;
}

export function SectionNav({ agencyId, sectionId }: { agencyId: string; sectionId: string }) {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const currentSlug = metricSlug(pathname);
  // Resolved without live routes: the label comes from the curated list or the
  // build-time mode manifest, both of which are available synchronously.
  const section = findSection(agencyId, sectionId);

  return (
    <nav className="navbar section-nav">
      <span className="section-crumb">
        <Link href={`/agency/${agencyId}`}>Sections</Link>
        <span className="section-crumb-sep">›</span>
        {section.label}
      </span>
      {METRIC_PAGES.map(({ slug, label }) => {
        const href = metricHref(agencyId, sectionId, slug);
        const isActive =
          currentSlug === slug || (slug !== "" && currentSlug.startsWith(`${slug}/`));
        return (
          <Link
            key={slug || "overview"}
            href={targetHref(currentSlug, slug, href, searchParams)}
            className={`navbar-link${isActive ? " active" : ""}`}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
