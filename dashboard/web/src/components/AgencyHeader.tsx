"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import { api } from "@/lib/apiClient";
import type { AgencySummary } from "@/lib/types";

/**
 * Identity bar for every `/agency/<id>/…` page: the way back to the agency
 * browser plus the agency's own name, and — when curated in
 * dashboard/api/agency_metadata.yaml — its accent colour, logo and tagline.
 *
 * There is deliberately no agency picker here. Switching agencies means going
 * back to the home page, which keeps every agency's URLs unambiguous.
 */
export function AgencyHeader({ agencyId }: { agencyId: string }) {
  const [agency, setAgency] = useState<AgencySummary | null>(null);

  useEffect(() => {
    api
      .agencies()
      .then((rows) => setAgency(rows.find((a) => a.agency_id === agencyId) ?? null))
      .catch(() => setAgency(null));
  }, [agencyId]);

  const style = agency?.accent_color
    ? ({ "--agency-accent": agency.accent_color } as CSSProperties)
    : undefined;

  return (
    <div className="agency-banner" style={style}>
      <Link href="/" className="agency-banner-back">
        ← All agencies
      </Link>
      {agency?.logo && <img src={agency.logo} alt="" className="agency-banner-logo" />}
      <Link href={`/agency/${agencyId}`} className="agency-banner-name">
        {agency?.name ?? agencyId}
      </Link>
      {agency?.tagline && <span className="tagline">{agency.tagline}</span>}
    </div>
  );
}
