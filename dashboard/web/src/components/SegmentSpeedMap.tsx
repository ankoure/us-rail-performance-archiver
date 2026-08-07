"use client";

import { useEffect, useMemo, useRef } from "react";
import { Map as MaplibreMap, NavigationControl, Popup } from "maplibre-gl";
import type {
  ExpressionSpecification,
  LngLat,
  LngLatBoundsLike,
  MapLayerMouseEvent,
  StyleSpecification,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { aggregateSegmentSpeeds, assignSpeedBuckets, segmentKey, INSUFFICIENT_DATA_COLOR_VAR } from "@/lib/segments";
import {
  buildStopCoords,
  groupPointsByShape,
  groupStopOffsetsByShape,
  shapesByDirection,
  sliceSegmentGeometry,
} from "@/lib/routeGeometry";
import type { RouteShapeResponse, SegmentDayRow, StopRow } from "@/lib/types";

// No API key, no signup — see the plan's basemap decision. Attribution is
// required by OSM's tile usage policy; maplibre surfaces it automatically
// from the source's `attribution` field via its built-in AttributionControl.
const OSM_STYLE: StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: [
        "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://b.tile.openstreetmap.org/{z}/{x}/{y}.png",
        "https://c.tile.openstreetmap.org/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors",
    },
  },
  layers: [{ id: "osm", type: "raster", source: "osm" }],
};

const MIN_SAMPLES = 5;
const SEGMENTS_SOURCE_PREFIX = "segment-speed";
const STOPS_SOURCE_ID = "segment-speed-stops";

function resolveCssColor(varName: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
}

interface SegmentFeatureProps {
  from_stop_id: string;
  to_stop_id: string;
  direction_id: number;
  bucket: number;
  avg_speed_mph: number;
  sample_count: number;
  from_name: string;
  to_name: string;
  direction_label: string;
}

export interface SegmentSpeedMapProps {
  routeShape: RouteShapeResponse;
  segments: SegmentDayRow[];
  stops: StopRow[];
  resolveDirectionLabel: (directionId: number) => string;
  resolveStopName: (stopId: string) => string;
}

export function SegmentSpeedMap({
  routeShape,
  segments,
  stops,
  resolveDirectionLabel,
  resolveStopName,
}: SegmentSpeedMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MaplibreMap | null>(null);
  const popupRef = useRef<Popup | null>(null);
  // Layer-delegated `map.on(event, layerId, ...)` listeners aren't tied to
  // the layer's lifetime — removing and re-adding a layer with the same id
  // (which render() does on every route/date change) leaves the old
  // listener registered, so a second render would double-fire. Layer ids
  // are stable per direction/stops, so each only needs attaching once ever.
  const attachedListenersRef = useRef<Set<string>>(new Set());

  const pointsByShape = useMemo(() => groupPointsByShape(routeShape.points), [routeShape]);
  const stopOffsetsByShape = useMemo(() => groupStopOffsetsByShape(routeShape.stops), [routeShape]);
  const shapeIdsByDirection = useMemo(() => shapesByDirection(routeShape.points), [routeShape]);
  const stopCoords = useMemo(() => buildStopCoords(stops), [stops]);
  const directionIds = useMemo(
    () => [...shapeIdsByDirection.keys()].sort((a, b) => a - b),
    [shapeIdsByDirection],
  );

  const aggregated = useMemo(
    () => aggregateSegmentSpeeds(segments).filter((s) => s.direction_id !== null),
    [segments],
  );
  const qualifying = useMemo(() => aggregated.filter((s) => s.sample_count >= MIN_SAMPLES), [aggregated]);
  const { bucketByKey, legend } = useMemo(() => assignSpeedBuckets(qualifying), [qualifying]);

  const featureCollections = useMemo(() => {
    const byDirection = new Map<number, GeoJSON.FeatureCollection<GeoJSON.LineString, SegmentFeatureProps>>();
    for (const s of aggregated) {
      const directionId = s.direction_id as number;
      const sliced = sliceSegmentGeometry(
        shapeIdsByDirection.get(directionId),
        pointsByShape,
        stopOffsetsByShape,
        s.from_stop_id,
        s.to_stop_id,
        stopCoords,
      );
      if (!sliced) continue;
      // A low-sample fallback (no shape covers both stops) is almost always
      // a poller-gap artifact — one rare "segment" spanning several real
      // stations — not a genuinely uncovered branch; drawing it as a long,
      // geographically nonsensical chord is worse than omitting it. See
      // sliceSegmentGeometry's doc.
      if (sliced.isFallback && s.sample_count < MIN_SAMPLES) continue;
      const bucket = bucketByKey.get(segmentKey(s)) ?? -1;
      const fc = byDirection.get(directionId) ?? { type: "FeatureCollection", features: [] };
      fc.features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: sliced.coordinates },
        properties: {
          from_stop_id: s.from_stop_id,
          to_stop_id: s.to_stop_id,
          direction_id: directionId,
          bucket,
          avg_speed_mph: s.avg_speed_mph,
          sample_count: s.sample_count,
          from_name: resolveStopName(s.from_stop_id),
          to_name: resolveStopName(s.to_stop_id),
          direction_label: resolveDirectionLabel(directionId),
        },
      });
      byDirection.set(directionId, fc);
    }
    return byDirection;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aggregated, shapeIdsByDirection, pointsByShape, stopOffsetsByShape, stopCoords, bucketByKey]);

  const bounds = useMemo((): LngLatBoundsLike | null => {
    let minLon = Infinity;
    let minLat = Infinity;
    let maxLon = -Infinity;
    let maxLat = -Infinity;
    for (const p of routeShape.points) {
      if (p.lon < minLon) minLon = p.lon;
      if (p.lon > maxLon) maxLon = p.lon;
      if (p.lat < minLat) minLat = p.lat;
      if (p.lat > maxLat) maxLat = p.lat;
    }
    if (minLon === Infinity) return null;
    return [
      [minLon, minLat],
      [maxLon, maxLat],
    ];
  }, [routeShape.points]);

  // Mount the map once.
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    const map = new MaplibreMap({
      container: containerRef.current,
      style: OSM_STYLE,
      bounds: bounds ?? undefined,
      fitBoundsOptions: { padding: 40, duration: 0 },
    });
    map.addControl(new NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;
    return () => {
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // (Re)build sources/layers whenever the route, direction set, or colored
  // data changes. Runs after 'load' the first time, immediately thereafter.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    function render(map: MaplibreMap) {
      const critical = resolveCssColor("--status-critical");
      const serious = resolveCssColor("--status-serious");
      const warning = resolveCssColor("--status-warning");
      const good = resolveCssColor("--status-good");
      const insufficient = resolveCssColor(INSUFFICIENT_DATA_COLOR_VAR);
      const surface = resolveCssColor("--surface");
      const lineColor: ExpressionSpecification = [
        "match",
        ["get", "bucket"],
        0,
        critical,
        1,
        serious,
        2,
        warning,
        3,
        good,
        insufficient,
      ];

      // Stale layers/sources from a previous route selection.
      for (const layerId of map.getStyle().layers ?? []) {
        if (layerId.id.startsWith(`${SEGMENTS_SOURCE_PREFIX}-`)) map.removeLayer(layerId.id);
      }
      for (const sourceId of Object.keys(map.getStyle().sources ?? {})) {
        if (sourceId.startsWith(`${SEGMENTS_SOURCE_PREFIX}-`)) map.removeSource(sourceId);
      }
      if (map.getLayer(`${STOPS_SOURCE_ID}-layer`)) map.removeLayer(`${STOPS_SOURCE_ID}-layer`);
      if (map.getSource(STOPS_SOURCE_ID)) map.removeSource(STOPS_SOURCE_ID);

      directionIds.forEach((directionId, i) => {
        const sourceId = `${SEGMENTS_SOURCE_PREFIX}-${directionId}`;
        const data = featureCollections.get(directionId) ?? { type: "FeatureCollection", features: [] };
        const offset = directionIds.length > 1 ? (i === 0 ? -2.5 : 2.5) : 0;

        map.addSource(sourceId, { type: "geojson", data });
        map.addLayer({
          id: `${sourceId}-casing`,
          type: "line",
          source: sourceId,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": surface, "line-width": 7, "line-offset": offset },
        });
        map.addLayer({
          id: `${sourceId}-line`,
          type: "line",
          source: sourceId,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": lineColor, "line-width": 4, "line-offset": offset },
        });

        const lineLayerId = `${sourceId}-line`;
        if (!attachedListenersRef.current.has(lineLayerId)) {
          map.on("mousemove", lineLayerId, (e: MapLayerMouseEvent) => {
            map.getCanvas().style.cursor = "pointer";
            const feature = e.features?.[0];
            if (!feature) return;
            const props = feature.properties as unknown as SegmentFeatureProps;
            showPopup(map, popupRef, e.lngLat, props);
          });
          map.on("mouseleave", lineLayerId, () => {
            map.getCanvas().style.cursor = "";
            popupRef.current?.remove();
          });
          attachedListenersRef.current.add(lineLayerId);
        }
      });

      const stopIds = new Set(routeShape.stops.map((s) => s.stop_id));
      const stopFeatures: GeoJSON.Feature<GeoJSON.Point, { stop_id: string; name: string }>[] = [];
      for (const stopId of stopIds) {
        const coords = stopCoords.get(stopId);
        if (!coords) continue;
        stopFeatures.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [coords.lon, coords.lat] },
          properties: { stop_id: stopId, name: resolveStopName(stopId) },
        });
      }
      map.addSource(STOPS_SOURCE_ID, {
        type: "geojson",
        data: { type: "FeatureCollection", features: stopFeatures },
      });
      map.addLayer({
        id: `${STOPS_SOURCE_ID}-layer`,
        type: "circle",
        source: STOPS_SOURCE_ID,
        paint: {
          "circle-radius": 4,
          "circle-color": surface,
          "circle-stroke-width": 2,
          "circle-stroke-color": resolveCssColor("--text-secondary"),
        },
      });
      const stopsLayerId = `${STOPS_SOURCE_ID}-layer`;
      if (!attachedListenersRef.current.has(stopsLayerId)) {
        map.on("mousemove", stopsLayerId, (e: MapLayerMouseEvent) => {
          map.getCanvas().style.cursor = "pointer";
          const feature = e.features?.[0];
          if (!feature) return;
          const name = (feature.properties as { name: string }).name;
          const node = document.createElement("div");
          node.className = "map-popup";
          const title = document.createElement("div");
          title.className = "map-popup-title";
          title.textContent = name;
          node.appendChild(title);
          popupRef.current?.remove();
          popupRef.current = new Popup({ closeButton: false, closeOnClick: false })
            .setLngLat(e.lngLat)
            .setDOMContent(node)
            .addTo(map);
        });
        map.on("mouseleave", stopsLayerId, () => {
          map.getCanvas().style.cursor = "";
          popupRef.current?.remove();
        });
        attachedListenersRef.current.add(stopsLayerId);
      }

      if (bounds) map.fitBounds(bounds, { padding: 40, duration: 300 });
    }

    if (map.isStyleLoaded()) render(map);
    else map.once("load", () => render(map));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [featureCollections, directionIds, bounds]);

  return (
    <div className="map-container">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      {legend.length > 0 && (
        <div className="map-legend legend" style={{ flexDirection: "column", gap: "6px" }}>
          {legend.map((entry) => (
            <div className="legend-item" key={entry.bucket}>
              <span className="legend-swatch" style={{ background: `var(${entry.colorVar})` }} />
              <span>
                {entry.label} ({entry.minMph.toFixed(0)}–{entry.maxMph.toFixed(0)} mph)
              </span>
            </div>
          ))}
          <div className="legend-item">
            <span className="legend-swatch" style={{ background: `var(${INSUFFICIENT_DATA_COLOR_VAR})` }} />
            <span>Insufficient data (&lt;{MIN_SAMPLES} samples)</span>
          </div>
        </div>
      )}
    </div>
  );
}

function showPopup(
  map: MaplibreMap,
  popupRef: React.MutableRefObject<Popup | null>,
  lngLat: LngLat,
  props: SegmentFeatureProps,
) {
  const node = document.createElement("div");
  node.className = "map-popup";

  const title = document.createElement("div");
  title.className = "map-popup-title";
  title.textContent = `${props.from_name} → ${props.to_name}`;
  node.appendChild(title);

  const direction = document.createElement("div");
  direction.className = "map-popup-hint";
  direction.textContent = props.direction_label;
  node.appendChild(direction);

  const speed = document.createElement("div");
  speed.className = "map-popup-value";
  speed.textContent =
    props.bucket === -1
      ? `${props.avg_speed_mph.toFixed(1)} mph (insufficient data)`
      : `${props.avg_speed_mph.toFixed(1)} mph avg`;
  node.appendChild(speed);

  const samples = document.createElement("div");
  samples.className = "map-popup-hint";
  samples.textContent = `${props.sample_count} samples`;
  node.appendChild(samples);

  popupRef.current?.remove();
  popupRef.current = new Popup({ closeButton: false, closeOnClick: false })
    .setLngLat(lngLat)
    .setDOMContent(node)
    .addTo(map);
}
