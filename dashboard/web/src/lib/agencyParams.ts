import { readFileSync } from "node:fs";
import { join } from "node:path";

import { parse } from "yaml";

/**
 * Build-time source of truth for which `/agency/<id>/…` routes exist.
 *
 * The site is a static export (`output: "export"` in next.config.ts), so every
 * agency segment has to be enumerated at build time — `dynamicParams` is not
 * supported. We read config/feeds.yaml directly rather than calling the
 * dashboard API so a build never depends on the API being reachable; that file
 * is also what dashboard/api/services/agencies.py builds its /agencies
 * response from, so the two stay in sync by construction.
 *
 * Consequence: an agency added to feeds.yaml is only browsable after the next
 * `next build`.
 */
const FEEDS_CONFIG = join(process.cwd(), "..", "..", "config", "feeds.yaml");

interface FeedsConfig {
  agencies?: { agency_id: string }[];
}

export function agencyIds(): string[] {
  const raw = parse(readFileSync(FEEDS_CONFIG, "utf8")) as FeedsConfig;
  return (raw.agencies ?? []).map((a) => a.agency_id);
}

export function generateStaticParams(): { agencyId: string }[] {
  return agencyIds().map((agencyId) => ({ agencyId }));
}
