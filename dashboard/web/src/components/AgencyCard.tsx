"use client";

import Link from "next/link";
import type { CSSProperties } from "react";
import type { AgencySummary } from "@/lib/types";

export function AgencyCard({ agency }: { agency: AgencySummary }) {
  const style: CSSProperties | undefined = agency.accent_color
    ? { borderLeft: `3px solid ${agency.accent_color}` }
    : undefined;

  return (
    <Link href={`/agency/${agency.agency_id}`} className="agency-card" style={style}>
      <span className="agency-card-name">{agency.name}</span>
      {agency.region && <span className="agency-card-region">{agency.region}</span>}
      {agency.tagline && <span className="agency-card-tagline">{agency.tagline}</span>}
    </Link>
  );
}
