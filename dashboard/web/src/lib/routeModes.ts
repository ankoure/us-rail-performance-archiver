// Shared display labels for RouteRow.mode (see dashboard/api/services/route_metadata.py).
// "rapid" is the gold mart's own default -- it collapses subway and light
// rail because the persisted mart doesn't carry raw GTFS route_type, so it
// gets its own honestly-merged label until a manual override in
// dashboard/api/route_metadata.yaml assigns a specific route to
// subway_metro or light_rail_streetcar.

export const ROUTE_MODE_LABELS: Record<string, string> = {
  subway_metro: "Subway/Metro",
  light_rail_streetcar: "Light Rail/Streetcar",
  rapid: "Subway/Light Rail",
  commuter_rail: "Commuter Rail",
  bus: "Bus",
  ferry_other: "Ferry+Other",
};

export const ROUTE_MODE_ORDER = [
  "subway_metro",
  "light_rail_streetcar",
  "rapid",
  "commuter_rail",
  "bus",
  "ferry_other",
];

export const OTHER_ROUTE_MODE_LABEL = "Other";

// The gold mart's own raw mode strings, which the deployed API still returns
// directly. dashboard/api/services/route_metadata.py maps them onto the
// taxonomy above; mirroring that map here means the dashboard resolves the
// same modes -- and therefore the same section slugs -- whichever version of
// the API it is pointed at. Keep in sync with _DEFAULT_MODE_MAP there. The
// mart's "bus" needs no alias: it is spelled the same in both vocabularies.
// bus_rta is this taxonomy's own former spelling of that bucket, kept so a
// build deployed ahead of the renamed API still labels those routes.
const RAW_MODE_ALIASES: Record<string, string> = {
  cr: "commuter_rail",
  other: "ferry_other",
  bus_rta: "bus",
};

/** A route's mode in the taxonomy above, whatever spelling the API used. */
export function canonicalMode(mode: string | null | undefined): string | null {
  if (!mode) return null;
  return RAW_MODE_ALIASES[mode] ?? mode;
}

export function routeModeLabel(mode: string | null | undefined): string {
  const canonical = canonicalMode(mode);
  if (!canonical) return OTHER_ROUTE_MODE_LABEL;
  return ROUTE_MODE_LABELS[canonical] ?? canonical;
}
