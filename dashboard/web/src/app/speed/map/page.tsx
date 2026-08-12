"use client";

import { useMemo } from "react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AgencyPicker } from "@/components/AgencyPicker";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { NoAgencySelected } from "@/components/NoAgencySelected";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import type { RouteRow, SegmentSpeedMapResponse } from "@/lib/types";

// maplibre-gl touches window/canvas at import time — must never be part of
// the server-rendered bundle for this statically-exported app. See
// node_modules/next/dist/docs/01-app/02-guides/lazy-loading.md.
const SegmentSpeedMap = dynamic(
  () => import("@/components/SegmentSpeedMap").then((m) => m.SegmentSpeedMap),
  {
    ssr: false,
    loading: () => <LoadingState what="map" />,
  },
);

const RAIL_MODES = new Set(["rapid", "cr"]);

export default function SpeedMapPage() {
  const searchParams = useSearchParams();
  const agency = searchParams.get("agency");
  const line = useLine();
  const { start, end } = useDateRange();

  const enabled = Boolean(agency) && Boolean(line);

  const { data: routesData } = useApiData<RouteRow[]>(`routes|${agency}`, Boolean(agency), () =>
    api.routes(agency!),
  );

  const railRoutes = useMemo(
    () => (routesData ?? []).filter((r) => RAIL_MODES.has(r.mode)),
    [routesData],
  );

  const {
    data: mapData,
    error,
    loading,
  } = useApiData<SegmentSpeedMapResponse>(
    `segment-speed-map|${agency}|${line}|${start}|${end}`,
    enabled,
    () => api.segmentSpeedMap(agency!, { route_id: line!, start_date: start, end_date: end }),
  );

  const ready = Boolean(mapData);
  const hasGeometry =
    Boolean(mapData) &&
    (mapData!.segments.features.length > 0 || mapData!.stops.features.length > 0);

  return (
    <>
      <div className="filter-bar">
        <AgencyPicker railOnly />
        {agency && railRoutes.length > 0 && <LinePicker routes={railRoutes} />}
        <DateRangePicker />
      </div>
      {agency && (
        <FilterContext agency={agency} line={line || undefined} when={`${start} – ${end}`} />
      )}
      <main>
        {!agency && <NoAgencySelected />}
        {agency && !routesData && <LoadingState what="routes" />}
        {agency && routesData && railRoutes.length === 0 && (
          <EmptyState>No rail routes found for this agency yet.</EmptyState>
        )}
        {agency && railRoutes.length > 0 && !line && (
          <EmptyState>Pick a line above to load the map.</EmptyState>
        )}
        {enabled && error && <ErrorState what="map data">{error}</ErrorState>}
        {enabled && !error && (
          <div className="card">
            <h2>
              Segment speeds on map, {line}, {start} – {end}
            </h2>
            <p className="card-hint">
              <Link href={`/speed?agency=${agency}&line=${line}&start=${start}&end=${end}`}>
                ← View as chart
              </Link>
            </p>
            <p className="card-hint">
              Each segment is colored by its average speed (p50) relative to every other segment on
              this line over the selected range — not a fixed mph scale, since rapid transit and
              commuter rail run at very different speeds. Segments follow the actual track geometry
              from the latest static-GTFS snapshot; hover a segment for exact figures.
            </p>
            {loading && <LoadingState />}
            {ready && !hasGeometry && (
              <EmptyState>No shape geometry published for this route yet.</EmptyState>
            )}
            {ready && hasGeometry && <SegmentSpeedMap data={mapData!} />}
          </div>
        )}
      </main>
    </>
  );
}
