import React, { useEffect, useRef, useState } from "react";
import { Map as MapLibreMap, NavigationControl, Popup, setWorkerUrl } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
// MapLibre 6 loads its worker from a separate file that Vite does not bundle on its own:
// ship it as an asset and point MapLibre at it.
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { vesselLabel } from "../lib/format.js";

setWorkerUrl(workerUrl);

// Free, keyless light basemap; VITE_MAP_STYLE overrides it (used to test an unreachable style).
const STYLE_URL = import.meta.env.VITE_MAP_STYLE || "https://tiles.openfreemap.org/styles/positron";
const TYPE_COLORS = { CARGO_VESSEL: "#f59e0b", FERRY: "#06b6d4", FISHING_BOAT: "#10b981" };
const ZONE_COLORS = { ANCHORAGE: "#e08a00", BERTHS: "#1d6fd8" };
const LABEL_FONT = ["Noto Sans Bold"];
const EMPTY = { type: "FeatureCollection", features: [] };

function zonesGeoJSON(port) {
  if (!port) return EMPTY;
  const zones = [["ANCHORAGE", port.anchorage_bbox], ["BERTHS", port.berth_bbox]];
  const features = [];
  for (const [kind, [w, s, e, n]] of zones) {
    features.push({
      type: "Feature",
      properties: { kind, color: ZONE_COLORS[kind] },
      geometry: { type: "Polygon", coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]] },
    });
    features.push({
      type: "Feature",
      properties: { kind, color: ZONE_COLORS[kind], label: kind },
      geometry: { type: "Point", coordinates: [w, n] },
    });
  }
  return { type: "FeatureCollection", features };
}

function vesselsGeoJSON(vessels, selectedId) {
  return {
    type: "FeatureCollection",
    features: vessels.map((v) => ({
      type: "Feature",
      properties: {
        id: v.vessel_id,
        color: TYPE_COLORS[v.vessel_type] ?? "#64748b",
        selected: v.vessel_id === selectedId ? 1 : 0,
        label: `${v.vessel_id} · ${vesselLabel(v)}`,
      },
      geometry: { type: "Point", coordinates: [v.longitude, v.latitude] },
    })),
  };
}

/** Harbour + anchorage boxes, widened to include this port's detected ships. */
function portBounds(port, vessels) {
  const [a, b] = [port.anchorage_bbox, port.berth_bbox];
  let [w, s, e, n] = [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])];
  for (const v of vessels) {
    w = Math.min(w, v.longitude); e = Math.max(e, v.longitude);
    s = Math.min(s, v.latitude); n = Math.max(n, v.latitude);
  }
  return [[w, s], [e, n]];
}

function addLayers(map) {
  map.addSource("zones", { type: "geojson", data: EMPTY });
  map.addSource("vessels", { type: "geojson", data: EMPTY });
  const isArea = ["==", ["geometry-type"], "Polygon"];
  map.addLayer({ id: "zones-fill", type: "fill", source: "zones", filter: isArea,
    paint: { "fill-color": ["get", "color"], "fill-opacity": 0.08 } });
  map.addLayer({ id: "zones-line", type: "line", source: "zones", filter: isArea,
    paint: { "line-color": ["get", "color"], "line-width": 2, "line-dasharray": [2, 1.5] } });
  map.addLayer({ id: "zones-label", type: "symbol", source: "zones", filter: ["==", ["geometry-type"], "Point"],
    layout: { "text-field": ["get", "label"], "text-font": LABEL_FONT, "text-size": 11, "text-anchor": "bottom-left",
      "text-offset": [0, -0.3], "text-letter-spacing": 0.08 },
    paint: { "text-color": ["get", "color"], "text-halo-color": "#ffffff", "text-halo-width": 1.5 } });
  map.addLayer({ id: "vessels", type: "circle", source: "vessels",
    paint: {
      "circle-color": ["get", "color"],
      "circle-radius": ["case", ["==", ["get", "selected"], 1], 10, 6.5],
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": ["case", ["==", ["get", "selected"], 1], 3.5, 2],
    } });
}

/**
 * Full-screen MapLibre map of one port: berth/anchorage areas, detected vessels and an
 * optional georeferenced real radar layer.
 */
export default function PortMap({ port, vessels, selectedVesselId, onSelectVessel, radar, radarOn, onToggleRadar, padding }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const onSelectRef = useRef(onSelectVessel);
  const [ready, setReady] = useState(false);
  onSelectRef.current = onSelectVessel;

  // create the map once
  useEffect(() => {
    const map = new MapLibreMap({ container: containerRef.current, style: STYLE_URL, pitch: 20, maxPitch: 60 });
    mapRef.current = map;
    map.addControl(new NavigationControl({ visualizePitch: true }), "bottom-right");
    let errorLogged = false;
    map.on("error", (e) => {
      if (!errorLogged) console.warn("Map error (the dashboard keeps working):", e?.error?.message ?? e);
      errorLogged = true;
    });
    const hover = new Popup({ closeButton: false, closeOnClick: false, offset: 12, className: "vessel-popup" });
    map.on("load", () => {
      addLayers(map);
      map.on("click", "vessels", (e) => onSelectRef.current?.(e.features[0].properties.id));
      map.on("mouseenter", "vessels", (e) => {
        map.getCanvas().style.cursor = "pointer";
        hover.setLngLat(e.features[0].geometry.coordinates).setText(e.features[0].properties.label).addTo(map);
      });
      map.on("mouseleave", "vessels", () => {
        map.getCanvas().style.cursor = "";
        hover.remove();
      });
      setReady(true);
    });
    return () => {
      hover.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  // port changed: redraw zones and fit the camera to the harbour + anchorage
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map || !port) return;
    map.getSource("zones").setData(zonesGeoJSON(port));
    map.fitBounds(portBounds(port, vessels), { padding, duration: 900, maxZoom: 14 });
  }, [ready, port, padding]); // eslint-disable-line react-hooks/exhaustive-deps -- re-fit on port change, not on every ship update

  useEffect(() => {
    if (ready) mapRef.current?.getSource("vessels").setData(vesselsGeoJSON(vessels, selectedVesselId));
  }, [ready, vessels, selectedVesselId]);

  useEffect(() => {
    const map = mapRef.current;
    const v = vessels.find((x) => x.vessel_id === selectedVesselId);
    if (ready && map && v) map.flyTo({ center: [v.longitude, v.latitude], zoom: Math.max(map.getZoom(), 13.5), padding });
  }, [ready, selectedVesselId]); // eslint-disable-line react-hooks/exhaustive-deps

  // real radar image laid over the map at its scene bounds, under the zones
  useEffect(() => {
    const map = mapRef.current;
    if (!ready || !map) return;
    if (!radar?.bounds) {
      if (map.getLayer("radar")) map.removeLayer("radar");
      if (map.getSource("radar")) map.removeSource("radar");
      return;
    }
    const [w, s, e, n] = radar.bounds;
    const coordinates = [[w, n], [e, n], [e, s], [w, s]];
    const source = map.getSource("radar");
    if (source) source.updateImage({ url: radar.url, coordinates });
    else {
      map.addSource("radar", { type: "image", url: radar.url, coordinates });
      map.addLayer({ id: "radar", type: "raster", source: "radar", paint: { "raster-opacity": 0.7 } }, "zones-fill");
    }
    map.setLayoutProperty("radar", "visibility", radarOn ? "visible" : "none");
  }, [ready, radar?.url, radar?.bounds?.join(","), radarOn]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="map-root">
      <div ref={containerRef} className="map-canvas" />
      {radar?.bounds ? (
        <button type="button" className={`glass radar-toggle${radarOn ? " on" : ""}`} onClick={onToggleRadar}>
          <span className="radar-dot" /> Real radar {radarOn ? "on" : "off"}
        </button>
      ) : null}
    </div>
  );
}
