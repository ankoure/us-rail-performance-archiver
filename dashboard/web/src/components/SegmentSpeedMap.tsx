"use client";

import { useEffect, useMemo, useRef } from "react";
import { Map as MaplibreMap, NavigationControl, Popup, setWorkerUrl } from "maplibre-gl";
import type {
  ExpressionSpecification,
  LngLat,
  LngLatBoundsLike,
  MapLayerMouseEvent,
  StyleSpecification,
} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { SPEED_BUCKET_COLOR_VARS, INSUFFICIENT_DATA_COLOR_VAR } from "@/lib/segments";
import type { SegmentFeatureProperties, SegmentSpeedMapResponse, StopFeatureProperties } from "@/lib/types";

// maplibre-gl resolves its worker script from *its own* bundled module's
// `import.meta.url` by default (see maplibre-gl/build/readme.md's Workers
// section) -- under webpack/Turbopack that isn't a real http(s) URL, so the
// default resolution silently yields "" and `new Worker("")` fails. GeoJSON
// sources depend on that worker to tile/index their data, so every
// line/circle layer here would attach real data and valid paint properties
// yet render nothing, while the raster basemap (no worker needed) renders
// fine.
//
// Routing setWorkerUrl() through `new URL(..., import.meta.url)` (the usual
// bundler-asset fix) isn't enough here: the bundler emits the requested
// worker file but treats it as an opaque asset, so it doesn't also emit (or
// rewrite the reference to) "./maplibre-gl-shared.mjs", which the worker
// bundle itself imports via a bare relative specifier -- that fetch then
// 404s/aborts and the worker dies silently. Serving both files verbatim
// from public/maplibre/ (copied from node_modules/maplibre-gl/dist/ -- see
// that directory's README) sidesteps the bundler entirely, so the worker's
// own relative import resolves normally. Re-copy both files if maplibre-gl
// is upgraded.
setWorkerUrl("/maplibre/maplibre-gl-worker.mjs");

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

export interface SegmentSpeedMapProps {
  data: SegmentSpeedMapResponse;
}

export function SegmentSpeedMap({ data }: SegmentSpeedMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MaplibreMap | null>(null);
  const popupRef = useRef<Popup | null>(null);
  // Layer-delegated `map.on(event, layerId, ...)` listeners aren't tied to
  // the layer's lifetime — removing and re-adding a layer with the same id
  // (which render() does on every route/date change) leaves the old
  // listener registered, so a second render would double-fire. Layer ids
  // are stable per direction/stops, so each only needs attaching once ever.
  const attachedListenersRef = useRef<Set<string>>(new Set());

  const featureCollections = useMemo(() => {
    const byDirection = new Map<number, GeoJSON.FeatureCollection<GeoJSON.LineString, SegmentFeatureProperties>>();
    for (const feature of data.segments.features) {
      const directionId = feature.properties.direction_id;
      const fc = byDirection.get(directionId) ?? { type: "FeatureCollection", features: [] };
      fc.features.push(feature);
      byDirection.set(directionId, fc);
    }
    return byDirection;
  }, [data.segments]);

  const directionIds = useMemo(
    () => [...featureCollections.keys()].sort((a, b) => a - b),
    [featureCollections],
  );

  const bounds = useMemo((): LngLatBoundsLike | null => {
    let minLon = Infinity;
    let minLat = Infinity;
    let maxLon = -Infinity;
    let maxLat = -Infinity;
    function visit(lon: number, lat: number) {
      if (lon < minLon) minLon = lon;
      if (lon > maxLon) maxLon = lon;
      if (lat < minLat) minLat = lat;
      if (lat > maxLat) maxLat = lat;
    }
    for (const feature of data.segments.features) {
      for (const [lon, lat] of feature.geometry.coordinates) visit(lon, lat);
    }
    for (const feature of data.stops.features) {
      const [lon, lat] = feature.geometry.coordinates;
      visit(lon, lat);
    }
    if (minLon === Infinity) return null;
    return [
      [minLon, minLat],
      [maxLon, maxLat],
    ];
  }, [data.segments, data.stops]);

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
      const critical = resolveCssColor(SPEED_BUCKET_COLOR_VARS[0]);
      const serious = resolveCssColor(SPEED_BUCKET_COLOR_VARS[1]);
      const warning = resolveCssColor(SPEED_BUCKET_COLOR_VARS[2]);
      const good = resolveCssColor(SPEED_BUCKET_COLOR_VARS[3]);
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
        const sourceData = featureCollections.get(directionId) ?? { type: "FeatureCollection", features: [] };
        const offset = directionIds.length > 1 ? (i === 0 ? -2.5 : 2.5) : 0;

        map.addSource(sourceId, { type: "geojson", data: sourceData });
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
            const props = feature.properties as unknown as SegmentFeatureProperties;
            showPopup(map, popupRef, e.lngLat, props);
          });
          map.on("mouseleave", lineLayerId, () => {
            map.getCanvas().style.cursor = "";
            popupRef.current?.remove();
          });
          attachedListenersRef.current.add(lineLayerId);
        }
      });

      map.addSource(STOPS_SOURCE_ID, { type: "geojson", data: data.stops });
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
          const { name } = feature.properties as unknown as StopFeatureProperties;
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
  }, [featureCollections, directionIds, data.stops, bounds]);

  return (
    <div className="map-container">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      {data.legend.length > 0 && (
        <div className="map-legend legend" style={{ flexDirection: "column", gap: "6px" }}>
          {data.legend.map((entry) => (
            <div className="legend-item" key={entry.bucket}>
              <span className="legend-swatch" style={{ background: `var(${SPEED_BUCKET_COLOR_VARS[entry.bucket]})` }} />
              <span>
                {entry.label} ({entry.min_mph.toFixed(0)}–{entry.max_mph.toFixed(0)} mph)
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
  props: SegmentFeatureProperties,
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
