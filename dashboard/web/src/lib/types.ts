export interface AgencySummary {
  agency_id: string;
  name: string;
  timezone: string;
  continent: string | null;
  region: string | null;
  /** Every mode the agency operates. Absent on API builds older than the
   * mode-tag migration -- use agencyTypeTags(), which falls back to `type`. */
  types?: string[];
  /** @deprecated first entry of `types`; kept for older API builds. */
  type: string | null;
  accent_color: string | null;
  tagline: string | null;
  logo: string | null;
}

export interface AgencyMetricsSummary {
  agency_id: string;
  name: string;
  on_time_pct: number | null;
  matched_count: number;
  avg_speed_mph: number | null;
  total_delay_minutes: number;
  alert_count: number;
  delay_alert_count: number;
}

export interface RouteRow {
  route_id: string;
  route_short_name: string | null;
  route_long_name: string | null;
  mode: string;
}

export interface StopRow {
  stop_id: string;
  stop_code: string | null;
  stop_name: string | null;
  stop_lat: number | null;
  stop_lon: number | null;
}

export interface DirectionRow {
  route_id: string;
  direction_id: number;
  direction: string | null;
  direction_destination: string | null;
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

export interface SegmentDayRow {
  feed: string;
  route_id: string;
  direction_id: number | null;
  from_stop_id: string;
  to_stop_id: string;
  service_date: string;
  sample_count: number;
  speed_p50_mph: number | null;
  speed_p90_mph: number | null;
  speed_mean_mph: number | null;
  transit_p50_s: number | null;
  distance_m: number;
}

export interface RouteShapePoint {
  route_id: string;
  direction_id: number;
  shape_id: string;
  point_sequence: number;
  lat: number;
  lon: number;
  dist_m: number;
}

export interface RouteShapeStop {
  route_id: string;
  direction_id: number;
  shape_id: string;
  stop_id: string;
  dist_m: number;
}

export interface RouteShapeResponse {
  points: RouteShapePoint[];
  stops: RouteShapeStop[];
}

export interface SegmentFeatureProperties {
  from_stop_id: string;
  to_stop_id: string;
  direction_id: number;
  bucket: number;
  avg_speed_mph: number;
  sample_count: number;
  from_name: string;
  to_name: string;
  direction_label: string;
  is_interpolated: boolean;
}

export interface StopFeatureProperties {
  stop_id: string;
  name: string;
}

export interface SpeedBucketLegendEntry {
  bucket: number;
  label: string;
  min_mph: number;
  max_mph: number;
}

export interface SegmentSpeedMapResponse {
  segments: GeoJSON.FeatureCollection<GeoJSON.LineString, SegmentFeatureProperties>;
  stops: GeoJSON.FeatureCollection<GeoJSON.Point, StopFeatureProperties>;
  legend: SpeedBucketLegendEntry[];
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

export interface TripRun {
  service_date: string;
  trip_id: string;
  vehicle_id: string | null;
  direction_id: number | null;
  departure_unix: number;
  arrival_unix: number;
  travel_time_s: number;
  headway_s: number | null;
}

export interface TripDay {
  service_date: string;
  trip_count: number;
  travel_time_p10_s: number | null;
  travel_time_p50_s: number | null;
  travel_time_p90_s: number | null;
  headway_p50_s: number | null;
  headway_p90_s: number | null;
}

export interface TripMetrics {
  from_stop_id: string;
  to_stop_id: string;
  route_id: string;
  direction_id: number | null;
  runs: TripRun[];
  days: TripDay[];
}
