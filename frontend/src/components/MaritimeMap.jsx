import React, { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";

const VESSEL_STYLES = {
  FISHING_BOAT: { color: "#10B981", label: "Fishing Boat (AIS Verified)" },
  FERRY: { color: "#06B6D4", label: "Ferry (AIS Verified)" },
  CARGO_VESSEL: { color: "#F59E0B", label: "Cargo Ship (AIS Verified)" },
  DARK_VESSEL: { color: "#EF4444", label: "Unmatched Dark Vessel / Alert" },
};

// Simplified India + island silhouettes for zero-dependency offline SVG map mode
const INDIA_COAST_POLYGONS = [
  [
    [68.4, 23.6], [69.0, 22.8], [69.6, 22.9], [68.9, 22.2], [70.0, 21.0],
    [72.2, 21.7], [72.6, 21.1], [72.8, 19.1], [73.2, 17.3], [73.8, 15.5],
    [74.3, 14.5], [74.8, 13.0], [75.8, 11.2], [76.5, 9.0], [77.5, 8.08],
    [78.2, 8.8], [79.3, 9.3], [79.9, 10.8], [79.8, 11.9], [80.3, 13.3],
    [80.1, 14.3], [80.7, 15.9], [81.2, 16.2], [82.3, 17.0], [83.3, 17.7],
    [85.0, 19.3], [86.9, 20.8], [87.1, 21.5], [88.3, 22.1], [89.1, 21.7],
    [88.0, 24.0], [72.0, 24.0], [68.4, 23.6],
  ],
  [[92.5, 13.5], [93.1, 12.6], [92.8, 11.5], [92.6, 10.5], [92.5, 13.5]],
  [[93.6, 8.1], [94.0, 7.0], [93.7, 6.7], [93.5, 7.8], [93.6, 8.1]],
];

// Real Sentinel-1 data has no same-time AIS yet: such vessels are "not checked", not dark.
function isDark(vessel) {
  return !vessel.ais_matched && vessel.alert_flag !== "AIS_NOT_CHECKED";
}

function getVesselStyle(vessel) {
  if (isDark(vessel)) {
    return VESSEL_STYLES.DARK_VESSEL;
  }
  return VESSEL_STYLES[vessel.vessel_type] || VESSEL_STYLES.CARGO_VESSEL;
}

function bboxToLeafletBounds(bbox) {
  const [minLon, minLat, maxLon, maxLat] = bbox;
  return [
    [minLat, minLon],
    [maxLat, maxLon],
  ];
}

function formatVesselPopup(vessel) {
  const aisText = vessel.ais_matched
    ? `VERIFIED (MMSI ${vessel.matched_ais_mmsi} — ${vessel.matched_ais_name})`
    : vessel.alert_flag === "AIS_NOT_CHECKED"
      ? "AIS not checked (real Sentinel-1 detection)"
      : `ALERT: ${vessel.alert_flag || "DARK_VESSEL"}`;
  const eta = vessel.eta_minutes_effective ?? vessel.eta_minutes;
  return `
    <div style="font-size:12px;line-height:1.45;color:#0f172a">
      <strong>${vessel.vessel_id}</strong> (${vessel.vessel_type})<br/>
      Port: ${vessel.port_id} | Zone: ${vessel.zone}<br/>
      Length: ${vessel.estimated_length_m} m | Conf: ${Math.round(vessel.confidence * 100)}%<br/>
      Offshore: ${vessel.distance_from_shore_km} km | ETA: ${eta != null ? `${eta} min` : "N/A"}<br/>
      AIS: ${aisText}
    </div>
  `;
}

function OfflineVectorMap({ ports, portsSummaryById, vessels, selectedPortId, coastalKm }) {
  const [hoveredText, setHoveredText] = useState(
    "Hover any port or vessel marker to inspect telemetry (100% offline vector map)"
  );

  const viewBounds = useMemo(() => {
    if (selectedPortId && selectedPortId !== "ALL") {
      const target = ports.find((p) => p.port_id === selectedPortId);
      if (target) {
        return {
          minLon: target.lon - 1.4,
          maxLon: target.lon + 1.4,
          minLat: target.lat - 1.2,
          maxLat: target.lat + 1.2,
        };
      }
    }
    return { minLon: 66.5, maxLon: 95.5, minLat: 5.5, maxLat: 24.0 };
  }, [ports, selectedPortId]);

  const width = 920;
  const height = 460;
  const project = (lon, lat) => {
    const x = ((lon - viewBounds.minLon) / (viewBounds.maxLon - viewBounds.minLon)) * width;
    const y = height - ((lat - viewBounds.minLat) / (viewBounds.maxLat - viewBounds.minLat)) * height;
    return [x, y];
  };

  const visiblePorts =
    selectedPortId && selectedPortId !== "ALL"
      ? ports.filter((p) => p.port_id === selectedPortId)
      : ports;

  const visibleVessels =
    selectedPortId && selectedPortId !== "ALL"
      ? vessels.filter((v) => v.port_id === selectedPortId)
      : vessels;

  const degPerKm = 1 / 111.32;

  return (
    <div style={{ position: "relative", inlineSize: "100%", blockSize: "100%" }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="xMidYMid meet"
        style={{ display: "block", inlineSize: "100%", blockSize: "100%", background: "#081224" }}
        role="img"
        aria-label="Offline maritime map of Indian non-major ports and vessels"
      >
        {/* Graticule */}
        {[70, 75, 80, 85, 90].map((lon) => {
          const [x] = project(lon, viewBounds.minLat);
          return (
            <line
              key={`lon-${lon}`}
              x1={x}
              y1={0}
              x2={x}
              y2={height}
              stroke="rgba(148,163,184,0.12)"
              strokeDasharray="3 4"
            />
          );
        })}
        {[8, 12, 16, 20].map((lat) => {
          const [, y] = project(viewBounds.minLon, lat);
          return (
            <line
              key={`lat-${lat}`}
              x1={0}
              y1={y}
              x2={width}
              y2={y}
              stroke="rgba(148,163,184,0.12)"
              strokeDasharray="3 4"
            />
          );
        })}

        {/* India landmass */}
        {INDIA_COAST_POLYGONS.map((poly, idx) => {
          const d =
            poly
              .map(([lon, lat], pIdx) => {
                const [x, y] = project(lon, lat);
                return `${pIdx === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
              })
              .join(" ") + " Z";
          return (
            <path
              key={`coast-${idx}`}
              d={d}
              fill="#1a2942"
              stroke="#475569"
              strokeWidth="1.5"
            />
          );
        })}

        {/* Active swath zones + ports */}
        {visiblePorts.map((port) => {
          const [px, py] = project(port.lon, port.lat);
          const summary = portsSummaryById[port.port_id];
          const isActive = Boolean(summary) || (selectedPortId && selectedPortId !== "ALL");

          if (!isActive) {
            return (
              <circle
                key={port.port_id}
                cx={px}
                cy={py}
                r={3.8}
                fill="#94a3b8"
                stroke="#0f172a"
                strokeWidth="1"
                style={{ cursor: "pointer" }}
                onMouseEnter={() =>
                  setHoveredText(
                    `${port.port_name} [${port.port_id}] · ${port.state} · Awaiting Next LEO Pass`
                  )
                }
              />
            );
          }

          const [rx] = project(port.lon + coastalKm * degPerKm, port.lat);
          const radiusPx = Math.max(12, Math.abs(rx - px));
          const [bMinX, bMaxY] = project(port.berth_bbox[0], port.berth_bbox[1]);
          const [bMaxX, bMinY] = project(port.berth_bbox[2], port.berth_bbox[3]);
          const [aMinX, aMaxY] = project(port.anchorage_bbox[0], port.anchorage_bbox[1]);
          const [aMaxX, aMinY] = project(port.anchorage_bbox[2], port.anchorage_bbox[3]);
          const utilPct = Math.round((summary?.utilization_index ?? 0) * 100);

          return (
            <g key={port.port_id}>
              <circle
                cx={px}
                cy={py}
                r={radiusPx}
                fill="none"
                stroke="#38bdf8"
                strokeWidth="1.3"
                strokeDasharray="4 3"
              />
              <rect
                x={Math.min(aMinX, aMaxX)}
                y={Math.min(aMinY, aMaxY)}
                width={Math.max(4, Math.abs(aMaxX - aMinX))}
                height={Math.max(4, Math.abs(aMaxY - aMinY))}
                fill="rgba(245, 158, 11, 0.16)"
                stroke="#F59E0B"
                strokeWidth="1.2"
              />
              <rect
                x={Math.min(bMinX, bMaxX)}
                y={Math.min(bMinY, bMaxY)}
                width={Math.max(4, Math.abs(bMaxX - bMinX))}
                height={Math.max(4, Math.abs(bMaxY - bMinY))}
                fill="rgba(16, 185, 129, 0.25)"
                stroke="#10B981"
                strokeWidth="1.4"
              />
              <circle
                cx={px}
                cy={py}
                r={6}
                fill="#38bdf8"
                stroke="#0f172a"
                strokeWidth="1.5"
                style={{ cursor: "pointer" }}
                onMouseEnter={() =>
                  setHoveredText(
                    `${port.port_name} [${port.port_id}] · Util ${utilPct}% · Dark Vessels: ${summary?.dark_vessels_detected ?? 0}`
                  )
                }
              />
              <text x={px + 8} y={py - 6} fontSize="11" fontWeight="700" fill="#e2e8f0">
                {port.port_id}
              </text>
            </g>
          );
        })}

        {/* Vessels */}
        {visibleVessels.map((vessel) => {
          const [vx, vy] = project(vessel.longitude, vessel.latitude);
          const style = getVesselStyle(vessel);
          const eta = vessel.eta_minutes_effective ?? vessel.eta_minutes;
          return (
            <circle
              key={vessel.vessel_id}
              cx={vx}
              cy={vy}
              r={isDark(vessel) ? 8.5 : 6.5}
              fill={style.color}
              stroke="#09111f"
              strokeWidth="1.8"
              style={{ cursor: "pointer" }}
              onMouseEnter={() =>
                setHoveredText(
                  `${vessel.vessel_id} (${vessel.vessel_type}) · ${vessel.port_id} · ${vessel.zone} · ${vessel.estimated_length_m}m · ETA ${eta ?? "N/A"}m · ${
                    vessel.ais_matched ? `AIS ${vessel.matched_ais_mmsi}` : isDark(vessel) ? "DARK VESSEL" : "AIS not checked"
                  }`
                )
              }
            />
          );
        })}
      </svg>

      <div
        style={{
          position: "absolute",
          insetBlockEnd: "10px",
          insetInlineStart: "10px",
          insetInlineEnd: "10px",
          background: "rgba(9, 17, 31, 0.88)",
          border: "1px solid #243554",
          borderRadius: "6px",
          padding: "0.45rem 0.7rem",
          fontSize: "0.8rem",
          color: "#e2e8f0",
        }}
      >
        {hoveredText}
      </div>
    </div>
  );
}

export default function MaritimeMap({
  ports,
  portsSummary,
  vessels,
  selectedPortId,
  coastalKm = 50,
  offlineMode,
}) {
  const mapContainerRef = useRef(null);
  const leafletMapRef = useRef(null);
  const [tileErrorFallback, setTileErrorFallback] = useState(false);

  const portsSummaryById = useMemo(() => {
    const map = {};
    for (const p of portsSummary || []) {
      map[p.port_id] = p;
    }
    return map;
  }, [portsSummary]);

  const useOffline = offlineMode || tileErrorFallback;

  useEffect(() => {
    if (useOffline || !mapContainerRef.current) {
      if (leafletMapRef.current) {
        leafletMapRef.current.remove();
        leafletMapRef.current = null;
      }
      return;
    }

    const visiblePorts =
      selectedPortId && selectedPortId !== "ALL"
        ? ports.filter((p) => p.port_id === selectedPortId)
        : ports;

    const center =
      selectedPortId && selectedPortId !== "ALL" && visiblePorts.length > 0
        ? [visiblePorts[0].lat, visiblePorts[0].lon]
        : [15.5, 80.5];
    const zoom = selectedPortId && selectedPortId !== "ALL" ? 11 : 5;

    if (!leafletMapRef.current) {
      leafletMapRef.current = L.map(mapContainerRef.current, {
        center,
        zoom,
        zoomControl: true,
      });
    } else {
      leafletMapRef.current.setView(center, zoom);
    }

    const map = leafletMapRef.current;
    map.eachLayer((layer) => {
      map.removeLayer(layer);
    });

    const tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 18,
    });
    tiles.on("tileerror", () => {
      setTileErrorFallback(true);
    });
    tiles.addTo(map);

    for (const port of visiblePorts) {
      const summary = portsSummaryById[port.port_id];
      const isActive = Boolean(summary) || (selectedPortId && selectedPortId !== "ALL");

      if (!isActive) {
        L.circleMarker([port.lat, port.lon], {
          radius: 4.5,
          color: "#475569",
          weight: 1,
          fillColor: "#94a3b8",
          fillOpacity: 0.85,
        })
          .bindTooltip(`${port.port_name} [${port.port_id}] · ${port.state} · Awaiting Next LEO Pass`)
          .addTo(map);
        continue;
      }

      const utilPct = Math.round((summary?.utilization_index ?? 0) * 100);
      const darkCnt = summary?.dark_vessels_detected ?? 0;

      L.circle([port.lat, port.lon], {
        radius: coastalKm * 1000,
        color: "#38bdf8",
        weight: 1.5,
        dashArray: "6, 4",
        fill: false,
      })
        .bindTooltip(`${port.port_name} — ${coastalKm} km Coastal Approach Boundary`)
        .addTo(map);

      L.rectangle(bboxToLeafletBounds(port.berth_bbox), {
        color: "#10B981",
        weight: 2,
        fillColor: "#10B981",
        fillOpacity: 0.22,
      })
        .bindTooltip(`${port.port_id} Berth Zone (${utilPct}% occupied)`)
        .addTo(map);

      L.rectangle(bboxToLeafletBounds(port.anchorage_bbox), {
        color: "#F59E0B",
        weight: 2,
        dashArray: "4, 3",
        fillColor: "#F59E0B",
        fillOpacity: 0.14,
      })
        .bindTooltip(`${port.port_id} Anchorage Queue Zone`)
        .addTo(map);

      L.circleMarker([port.lat, port.lon], {
        radius: 7,
        color: "#ffffff",
        weight: 2,
        fillColor: "#1d4ed8",
        fillOpacity: 1,
      })
        .bindTooltip(`${port.port_name} [${port.port_id}] — Util ${utilPct}% | Dark: ${darkCnt}`)
        .addTo(map);
    }

    const visibleVessels =
      selectedPortId && selectedPortId !== "ALL"
        ? vessels.filter((v) => v.port_id === selectedPortId)
        : vessels;

    for (const vessel of visibleVessels) {
      const style = getVesselStyle(vessel);
      L.circleMarker([vessel.latitude, vessel.longitude], {
        radius: isDark(vessel) ? 9 : 7,
        color: isDark(vessel) ? "#111827" : style.color,
        weight: 2,
        fillColor: style.color,
        fillOpacity: 0.94,
      })
        .bindPopup(formatVesselPopup(vessel))
        .bindTooltip(`${vessel.vessel_id} · ${style.label}`)
        .addTo(map);
    }
  }, [useOffline, ports, portsSummaryById, vessels, selectedPortId, coastalKm]);

  useEffect(() => {
    return () => {
      if (leafletMapRef.current) {
        leafletMapRef.current.remove();
        leafletMapRef.current = null;
      }
    };
  }, []);

  return (
    <div className="map-frame">
      {useOffline ? (
        <OfflineVectorMap
          ports={ports}
          portsSummaryById={portsSummaryById}
          vessels={vessels}
          selectedPortId={selectedPortId}
          coastalKm={coastalKm}
        />
      ) : (
        <div ref={mapContainerRef} style={{ inlineSize: "100%", blockSize: "100%" }} />
      )}
    </div>
  );
}
