"use client";

import { useParams } from "next/navigation";

/**
 * The agency owning the current page, read from the `/agency/[agencyId]/…`
 * path segment. Only valid inside that segment — every page there is agency-
 * scoped by construction, so unlike the old `?agency=` query param this never
 * returns null and pages don't need a "no agency selected" state.
 */
export function useAgencyId(): string {
  return useParams<{ agencyId: string }>().agencyId;
}
