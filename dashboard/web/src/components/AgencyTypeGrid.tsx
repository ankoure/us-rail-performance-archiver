"use client";

import {
  AGENCY_TYPE_LABELS,
  AGENCY_TYPE_ORDER,
  agencyTypeTags,
  UNCLASSIFIED_LABEL,
} from "@/lib/agencyTypes";
import type { AgencySummary } from "@/lib/types";
import { AgencyCard } from "./AgencyCard";

export function AgencyTypeGrid({ agencies }: { agencies: AgencySummary[] }) {
  // Classification is a tag set, so an agency is listed once per mode it
  // operates -- MBTA shows up under Subway/Metro, Light Rail, Commuter Rail,
  // Bus and Ferry. Section counts therefore sum to more than the agency
  // count, which is the point: the sections answer "who runs a ferry here",
  // not "how many agencies are there".
  const byType = new Map<string, AgencySummary[]>();
  for (const a of agencies) {
    const tags = agencyTypeTags(a);
    for (const key of tags.length > 0 ? tags : [UNCLASSIFIED_LABEL]) {
      if (!byType.has(key)) byType.set(key, []);
      byType.get(key)!.push(a);
    }
  }

  // Tags outside the known taxonomy (a future bucket the deployed API knows
  // about and this build doesn't) still get a section, after the known ones.
  const extra = [...byType.keys()]
    .filter((key) => key !== UNCLASSIFIED_LABEL && !AGENCY_TYPE_ORDER.includes(key))
    .sort();
  const sections = [...AGENCY_TYPE_ORDER, ...extra, UNCLASSIFIED_LABEL].filter((key) =>
    byType.has(key),
  );

  if (sections.length === 0) {
    return <p className="empty-state">No agencies match this selection.</p>;
  }

  return (
    <>
      {sections.map((key) => {
        const rows = [...byType.get(key)!].sort((a, b) => a.name.localeCompare(b.name));
        const label =
          key === UNCLASSIFIED_LABEL ? UNCLASSIFIED_LABEL : (AGENCY_TYPE_LABELS[key] ?? key);
        return (
          <section key={key} className="agency-type-section">
            <h3>
              {label} <span className="card-hint">({rows.length})</span>
            </h3>
            <div className="agency-grid">
              {rows.map((a) => (
                <AgencyCard key={a.agency_id} agency={a} />
              ))}
            </div>
          </section>
        );
      })}
    </>
  );
}
