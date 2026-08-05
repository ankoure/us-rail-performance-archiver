/**
 * MBTA rapid-transit route_ids and direction labels, for the Speed dashboard's
 * flagship-system v1 (see docs/design plan: rail scope is MBTA-only for now,
 * expand to other onboarded rail feeds and eventually bus once this proves out).
 *
 * There's no dashboard API for route_type or human direction labels yet (the
 * gtfs_directions mart exists in pipeline/gtfs.py but isn't deployed/exposed),
 * so both lists are hardcoded here rather than fetched. MBTA's rail
 * direction_id convention is stable and well-documented, but this is a
 * hand-maintained convenience label, not derived from data — swap it for a
 * gtfs_directions-backed lookup once that mart has run in prod.
 */

export interface RailLine {
  routeId: string;
  label: string;
}

export const MBTA_RAIL_LINES: RailLine[] = [
  { routeId: "Red", label: "Red Line" },
  { routeId: "Mattapan", label: "Mattapan Line" },
  { routeId: "Orange", label: "Orange Line" },
  { routeId: "Blue", label: "Blue Line" },
  { routeId: "Green-B", label: "Green Line B" },
  { routeId: "Green-C", label: "Green Line C" },
  { routeId: "Green-D", label: "Green Line D" },
  { routeId: "Green-E", label: "Green Line E" },
];

// direction_id -> label, per MBTA route_id. Falls back to "Direction 0/1"
// for any route_id not listed here.
const DIRECTION_LABELS: Record<string, [string, string]> = {
  Red: ["Southbound", "Northbound"],
  Mattapan: ["Southbound", "Northbound"],
  Orange: ["Southbound", "Northbound"],
  Blue: ["Westbound", "Eastbound"],
  "Green-B": ["Westbound", "Eastbound"],
  "Green-C": ["Westbound", "Eastbound"],
  "Green-D": ["Westbound", "Eastbound"],
  "Green-E": ["Westbound", "Eastbound"],
};

export function directionLabel(routeId: string, directionId: number): string {
  const labels = DIRECTION_LABELS[routeId];
  if (labels && (directionId === 0 || directionId === 1)) {
    return labels[directionId];
  }
  return `Direction ${directionId}`;
}
