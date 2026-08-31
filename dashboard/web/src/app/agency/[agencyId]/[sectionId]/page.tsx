"use client";

import Link from "next/link";
import { METRIC_PAGES } from "@/lib/metricPages";
import { useSection } from "@/lib/useSection";

export default function SectionOverviewPage() {
  const { agencyId, section, routes } = useSection();
  const metrics = METRIC_PAGES.filter((s) => s.slug !== "");
  const empty = routes !== null && routes.length === 0;

  return (
    <main>
      <div className="card">
        <h2>{section.label}</h2>
        {empty ? (
          <p className="empty-state">{section.note ?? "No routes in this section."}</p>
        ) : (
          <>
            <p className="card-hint">
              {routes ? `${routes.length} routes.` : ""} Every view below is limited to this
              section.
            </p>
            <div className="agency-grid">
              {metrics.map(({ slug, label, blurb }) => (
                <Link
                  key={slug}
                  href={`/agency/${agencyId}/${section.slug}/${slug}`}
                  className="agency-card"
                >
                  <span className="agency-card-name">{label}</span>
                  <span className="agency-card-tagline">{blurb}</span>
                </Link>
              ))}
            </div>
          </>
        )}
      </div>
    </main>
  );
}
