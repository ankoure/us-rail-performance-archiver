function toISODate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Data lags behind real time, so "today" for defaults means yesterday UTC. */
export function defaultDay(): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - 1);
  return toISODate(d);
}

export function defaultRange(days: number): { start: string; end: string } {
  const end = new Date();
  end.setUTCDate(end.getUTCDate() - 1);
  const start = new Date(end);
  start.setUTCDate(start.getUTCDate() - (days - 1));
  return { start: toISODate(start), end: toISODate(end) };
}

export function dayBefore(day: string): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - 1);
  return toISODate(d);
}

/** The immediately-preceding period of equal length, for week-over-week-style comparisons. */
export function previousPeriod(start: string, end: string): { start: string; end: string } {
  const startDate = new Date(`${start}T00:00:00Z`);
  const endDate = new Date(`${end}T00:00:00Z`);
  const lengthDays = Math.round((endDate.getTime() - startDate.getTime()) / 86_400_000) + 1;
  const prevEnd = new Date(startDate);
  prevEnd.setUTCDate(prevEnd.getUTCDate() - 1);
  const prevStart = new Date(prevEnd);
  prevStart.setUTCDate(prevStart.getUTCDate() - (lengthDays - 1));
  return { start: toISODate(prevStart), end: toISODate(prevEnd) };
}

/**
 * Widest end - start span (in days) the API accepts for its parquet-backed
 * endpoints; mirrors MAX_RANGE_DAYS in dashboard/api/dependencies.py.
 */
export const MAX_RANGE_DAYS = 366;

/**
 * Tighter limit for endpoints that scan raw events or one alert snapshot per
 * day (trip_metrics, line_delays); mirrors MAX_EVENTS_RANGE_DAYS in
 * dashboard/api/dependencies.py.
 */
export const MAX_EVENTS_RANGE_DAYS = 62;

export function addDays(day: string, n: number): string {
  const d = new Date(`${day}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return toISODate(d);
}

/**
 * Pull a (possibly hand-edited) range back inside what the API accepts:
 * a reversed range collapses to the end day, and a too-wide one keeps its end
 * and moves its start forward to maxDays before it.
 */
export function clampRange(
  start: string,
  end: string,
  maxDays: number,
): { start: string; end: string } {
  if (start > end) return { start: end, end };
  const earliest = addDays(end, -maxDays);
  return { start: start < earliest ? earliest : start, end };
}
