/** The metric pages available under `/agency/<id>/<section>/`, in nav order. */
export const METRIC_PAGES = [
  { slug: "", label: "Overview", blurb: "Everything tracked for this section." },
  { slug: "otp", label: "OTP", blurb: "On-time performance by route and stop." },
  {
    slug: "headways-dwells",
    label: "Headways & Dwells",
    blurb: "Service frequency and time spent at stops.",
  },
  { slug: "speed", label: "Speed", blurb: "Segment speeds, with a map view." },
  { slug: "adherence", label: "Adherence", blurb: "Scheduled vs. actual trips for a single day." },
  { slug: "alerts", label: "Alerts", blurb: "Service alerts published by the agency." },
  { slug: "line-delays", label: "Line Delays", blurb: "Where delay concentrates across lines." },
  { slug: "trips", label: "Trips", blurb: "Every run between two stops on one day." },
  {
    slug: "trips/multi",
    label: "Multi-Day Trips",
    blurb: "How that same journey holds up across days.",
  },
] as const;

export function metricHref(agencyId: string, sectionId: string, slug: string): string {
  const base = `/agency/${agencyId}/${sectionId}`;
  return slug ? `${base}/${slug}` : base;
}
