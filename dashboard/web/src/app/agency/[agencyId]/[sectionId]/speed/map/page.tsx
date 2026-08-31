"use client";

import dynamic from "next/dynamic";
import Link from "next/link";
import { DateRangePicker, useDateRange } from "@/components/DateRangePicker";
import { EmptyState, ErrorState, LoadingState } from "@/components/DataState";
import { FilterContext } from "@/components/FilterContext";
import { LinePicker, useLine } from "@/components/LinePicker";
import { api } from "@/lib/apiClient";
import { useApiData } from "@/lib/useApiData";
import { useSection } from "@/lib/useSection";
import type { SegmentSpeedMapResponse } from "@/lib/types";

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

export default function SpeedMapPage() {
  const scope = useSection();
  const { section } = scope;
  const line = useLine();
  const { start, end } = useDateRange();

  const enabled = scope.ready && Boolean(line);

  const {
    data: mapData,
    error,
    loading,
  } = useApiData<SegmentSpeedMapResponse>(
    `segment-speed-map|${scope.key}|${line}|${start}|${end}`,
    () =>
      api.segmentSpeedMap(scope.agencyId, { route_id: line!, start_date: start, end_date: end }),
    enabled,
  );

  const ready = Boolean(mapData);
  const hasGeometry =
    Boolean(mapData) &&
    (mapData!.segments.features.length > 0 || mapData!.stops.features.length > 0);

  return (
    <>
      <div className="filter-bar">
        {scope.routes && scope.routes.length > 0 && <LinePicker routes={scope.routes} />}
        <DateRangePicker />
      </div>
      <FilterContext scope={section.label} line={line || undefined} when={`${start} – ${end}`} />
      <main>
        {!scope.routes && <LoadingState what="routes" />}
        {scope.routes && scope.routes.length === 0 && (
          <EmptyState>No routes found for this agency yet.</EmptyState>
        )}
        {scope.routes && scope.routes.length > 0 && !line && (
          <EmptyState>Pick a line above to load the map.</EmptyState>
        )}
        {enabled && error && <ErrorState what="map data">{error}</ErrorState>}
        {enabled && !error && (
          <div className="card">
            <h2>
              Segment speeds on map, {line}, {start} – {end}
            </h2>
            <p className="card-hint">
              <Link
                href={`/agency/${scope.agencyId}/${section.slug}/speed?line=${line}&start=${start}&end=${end}`}
              >
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
            {ready && hasGeometry && (
              <p className="card-hint">
                A small number of segments are excluded from this map because the feed reported data
                that couldn&apos;t be reliably matched to a track direction.
              </p>
            )}
          </div>
        )}
      </main>
    </>
  );
}
