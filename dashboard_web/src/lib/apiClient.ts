import type {
  AgencySummary,
  StopDayRow,
  RouteDayRow,
  StopDayOtp,
  RouteDayOtp,
  Adherence,
  AlertRow,
  LineDelaysSummary,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type QueryValue = string | string[] | undefined;

async function getJSON<T>(path: string, params: object = {}): Promise<T> {
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params as Record<string, QueryValue>)) {
    if (value === undefined) continue;
    if (Array.isArray(value)) {
      for (const item of value) qs.append(key, item);
    } else {
      qs.append(key, value);
    }
  }
  const query = qs.toString();
  const res = await fetch(`${API_BASE}${path}${query ? `?${query}` : ""}`);
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

export interface DateRangeFilters {
  start_date: string;
  end_date: string;
  route_id?: string[];
  stop_id?: string[];
}

export const api = {
  agencies: () => getJSON<AgencySummary[]>("/agencies"),

  stopDay: (agency: string, filters: DateRangeFilters) =>
    getJSON<StopDayRow[]>(`/agencies/${agency}/stop_day`, filters),

  routeDay: (agency: string, filters: Omit<DateRangeFilters, "stop_id">) =>
    getJSON<RouteDayRow[]>(`/agencies/${agency}/route_day`, filters),

  stopDayOtp: (agency: string, filters: DateRangeFilters) =>
    getJSON<StopDayOtp[]>(`/agencies/${agency}/stop_day_otp`, filters),

  routeDayOtp: (agency: string, filters: Omit<DateRangeFilters, "stop_id">) =>
    getJSON<RouteDayOtp[]>(`/agencies/${agency}/route_day_otp`, filters),

  adherence: (agency: string, filters: DateRangeFilters & { trip_id?: string[]; limit?: number }) =>
    getJSON<Adherence[]>(`/agencies/${agency}/adherence`, {
      ...filters,
      limit: filters.limit?.toString(),
    }),

  alerts: (agency: string, day: string) => getJSON<AlertRow[]>(`/agencies/${agency}/alerts/${day}`),

  lineDelays: (agency: string, day: string) =>
    getJSON<LineDelaysSummary>(`/agencies/${agency}/line_delays/${day}`),
};
