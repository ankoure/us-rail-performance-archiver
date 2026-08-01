export interface AgencySummary {
  agency_id: string;
  name: string;
  timezone: string;
}

export interface StopDayRow {
  feed: string;
  route_id: string;
  direction_id: number | null;
  stop_id: string;
  service_date: string;
  visit_count: number;
  trip_count: number;
  distinct_vehicle_count: number;
  headway_p50_s: number | null;
  headway_p90_s: number | null;
  headway_mean_s: number | null;
  headway_cov: number | null;
  dwell_p50_s: number | null;
  dwell_p90_s: number | null;
  first_service_unix: number;
  last_service_unix: number;
  service_span_s: number;
}

export interface RouteDayRow {
  feed: string;
  route_id: string;
  direction_id: number | null;
  service_date: string;
  visit_count: number;
  trip_count: number;
  distinct_vehicle_count: number;
  distinct_stop_count: number;
  headway_p50_s: number | null;
  dwell_p50_s: number | null;
  dwell_p90_s: number | null;
  first_service_unix: number;
  last_service_unix: number;
  service_span_s: number;
}

export interface OtpAggFields {
  matched_count: number;
  on_time_count: number;
  early_count: number;
  late_count: number;
  on_time_pct: number | null;
  arr_delay_p50_s: number | null;
  arr_delay_p90_s: number | null;
  arr_delay_mean_s: number | null;
  dep_delay_p50_s: number | null;
}

export interface StopDayOtp extends OtpAggFields {
  feed: string;
  route_id: string;
  direction_id: number | null;
  stop_id: string;
  service_date: string;
}

export interface RouteDayOtp extends OtpAggFields {
  feed: string;
  route_id: string;
  direction_id: number | null;
  service_date: string;
  distinct_stop_count: number;
}

export interface Adherence {
  feed: string;
  route_id: string;
  direction_id: number | null;
  stop_id: string;
  stop_sequence: number;
  trip_id: string;
  vehicle_id: string;
  route_mode: string;
  service_date: string;
  arrival_unix: number | null;
  scheduled_arrival_unix: number | null;
  arrival_delay_s: number | null;
  departure_unix: number | null;
  scheduled_departure_unix: number | null;
  departure_delay_s: number | null;
  status: string;
  on_time: boolean;
}

export interface AlertTranslation {
  text: string;
  language: string;
}

export interface AlertTranslatedString {
  translation: AlertTranslation[];
}

export interface AlertActivePeriod {
  start: number | null;
  end: number | null;
}

export interface AlertInformedEntityTrip {
  trip_id: string | null;
  route_id: string | null;
  direction_id: number | null;
}

export interface AlertInformedEntity {
  agency_id: string | null;
  route_id: string | null;
  route_type: number | null;
  stop_id: string | null;
  direction_id: number | null;
  trip: AlertInformedEntityTrip | null;
}

export interface Alert {
  active_period: AlertActivePeriod[];
  informed_entity: AlertInformedEntity[];
  cause: string | null;
  effect: string | null;
  url: AlertTranslatedString | null;
  header_text: AlertTranslatedString | null;
  description_text: AlertTranslatedString | null;
  severity_level: string | null;
}

export interface AlertRow {
  alert_id: string;
  first_seen: number;
  last_seen: number;
  poll_count: number;
  alert: Alert;
}

export interface LineDelaysSummary {
  feeds: string[];
  service_date: string | null;
  alert_count: number;
  delay_alert_count: number;
  total_delay_minutes: number;
  delay_by_type: Record<string, number>;
  count_by_type: Record<string, number>;
}
