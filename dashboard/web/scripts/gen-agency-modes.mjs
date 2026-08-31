/**
 * Regenerates src/lib/agencyModes.json — the set of route modes each agency
 * actually has data for, keyed by agency_id.
 *
 * Sections are a path segment (/agency/<id>/<section>/…), and the site is a
 * static export, so every section has to be enumerated at build time. Without
 * this manifest the build would have to prerender all six possible mode
 * sections for all ~200 agencies (~1,200 sections) when only ~110 really
 * exist — most agencies have one mode, and two thirds have no route data at
 * all. Committing the answer keeps `next build` off the network, the same
 * reason src/lib/agencyParams.ts reads config/feeds.yaml directly.
 *
 * Re-run whenever an agency's routes first land in the gold mart:
 *   npm run gen:modes
 *   API_BASE_URL=http://localhost:8000 npm run gen:modes
 */
import { writeFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const API = process.env.API_BASE_URL ?? "https://api.transit.andrewkoure.com";
const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "lib", "agencyModes.json");
const CONCURRENCY = 12;
const RAW_MODE_ALIASES = { bus: "bus_rta", cr: "commuter_rail", other: "ferry_other" };

const agencies = await (await fetch(`${API}/agencies`)).json();
const queue = [...agencies];
const modes = {};
const failed = [];

async function worker() {
  for (let a = queue.pop(); a; a = queue.pop()) {
    try {
      const res = await fetch(`${API}/agencies/${encodeURIComponent(a.agency_id)}/routes`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      // Canonicalised the same way the browser does (see canonicalMode in
      // src/lib/routeModes.ts) so the manifest matches the section slugs the
      // dashboard derives at runtime, whichever API version answered.
      const found = [
        ...new Set(
          (await res.json()).map((r) => RAW_MODE_ALIASES[r.mode] ?? r.mode).filter(Boolean),
        ),
      ].sort();
      // Agencies with no routes yet are omitted entirely rather than stored
      // as [], so the file stays a list of what exists.
      if (found.length) modes[a.agency_id] = found;
    } catch (e) {
      failed.push(`${a.agency_id}: ${e.message}`);
    }
  }
}
await Promise.all(Array.from({ length: CONCURRENCY }, worker));

if (failed.length) {
  console.error(`Refusing to write a partial manifest — ${failed.length} agencies failed:`);
  for (const f of failed.slice(0, 10)) console.error(`  ${f}`);
  process.exit(1);
}

const sorted = Object.fromEntries(
  Object.keys(modes)
    .sort()
    .map((k) => [k, modes[k]]),
);
writeFileSync(OUT, `${JSON.stringify(sorted, null, 2)}\n`);
const total = Object.values(sorted).reduce((n, m) => n + m.length, 0);
console.log(
  `wrote ${OUT}: ${Object.keys(sorted).length}/${agencies.length} agencies with routes, ${total} mode sections`,
);
