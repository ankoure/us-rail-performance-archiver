"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { METRIC_PAGES } from "@/lib/metricPages";
import { api } from "@/lib/apiClient";
import { ALL_SECTION, sectionRoutes, sectionsFor } from "@/lib/sections";
import type { RouteRow } from "@/lib/types";
import { useAgencyId } from "@/lib/useAgencyId";

export default function AgencyOverviewPage() {
  const agency = useAgencyId();
  const [routes, setRoutes] = useState<RouteRow[] | null>(null);

  useEffect(() => {
    api
      .routes(agency)
      .then(setRoutes)
      .catch(() => setRoutes([]));
  }, [agency]);

  // Rendered from the build-time manifest first, then refined once the live
  // route list arrives — so the section list is right even for an agency
  // whose routes landed after the last build.
  const sections = sectionsFor(agency, routes);
  const metrics = METRIC_PAGES.filter((s) => s.slug !== "");

  // An agency with nothing but "All routes" has no meaningful choice to
  // offer; skip the one-item picker and show its metrics directly.
  if (sections.length === 1) {
    return (
      <main>
        <div className="card">
          <h2>Metrics</h2>
          <div className="agency-grid">
            {metrics.map(({ slug, label, blurb }) => (
              <Link
                key={slug}
                href={`/agency/${agency}/${ALL_SECTION.slug}/${slug}`}
                className="agency-card"
              >
                <span className="agency-card-name">{label}</span>
                <span className="agency-card-tagline">{blurb}</span>
              </Link>
            ))}
          </div>
        </div>
      </main>
    );
  }

  return (
    <main>
      <div className="card">
        <h2>Sections</h2>
        <p className="card-hint">
          Pick a part of the system to see its metrics. To look at a different agency, go back to
          the agency browser.
        </p>
        <div className="agency-grid">
          {sections.map((s) => (
            <Link key={s.slug} href={`/agency/${agency}/${s.slug}`} className="agency-card">
              <span className="agency-card-name">{s.label}</span>
              {routes && (
                <span className="agency-card-tagline">
                  {`${sectionRoutes(s, routes).length} routes`}
                </span>
              )}
            </Link>
          ))}
        </div>
      </div>
    </main>
  );
}
