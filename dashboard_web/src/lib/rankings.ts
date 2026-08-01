/** Takes the first `limit` rows from an already-sorted list, capping how many can come from any one route. */
export function topByRoute<T>(
  rows: T[],
  { routeId, limit, perRouteCap }: { routeId: (row: T) => string; limit: number; perRouteCap: number },
): T[] {
  const result: T[] = [];
  const perRoute = new Map<string, number>();
  for (const row of rows) {
    const route = routeId(row);
    const count = perRoute.get(route) ?? 0;
    if (count >= perRouteCap) continue;
    result.push(row);
    perRoute.set(route, count + 1);
    if (result.length >= limit) break;
  }
  return result;
}
