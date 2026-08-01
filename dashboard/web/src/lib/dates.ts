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
