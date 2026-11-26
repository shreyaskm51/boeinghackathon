import React from "react";
import { etaLabel, vesselLabel } from "../lib/format.js";

const TYPE_COLORS = { CARGO_VESSEL: "#f59e0b", FERRY: "#06b6d4", FISHING_BOAT: "#10b981" };
const ZONE_NAMES = {
  BERTH: "At berth",
  ANCHORAGE_QUEUE: "Anchorage",
  COASTAL_APPROACH: "Approaching",
  OFFSHORE_EEZ: "Offshore",
};

export default function ShipsPanel({ view, selectedVesselId, onSelectVessel }) {
  const { vessels, reroute, state } = view;
  if (state !== "current" && state !== "old") return null;

  return (
    <aside className="glass panel ships-panel" aria-label="Ships detected">
      <div className="panel-title">Ships detected · {vessels.length}</div>
      {reroute ? (
        <div className="reroute">
          Busy — consider {reroute.recommended_port_name} [{reroute.recommended_port_id}]
          {reroute.distance_km != null ? `, ~${Math.round(reroute.distance_km)} km` : ""}
        </div>
      ) : null}
      {vessels.length === 0 ? (
        <p className="muted empty">No ships detected in this pass</p>
      ) : (
        <ul className="ship-list">
          {vessels.map((v) => (
            <li key={v.vessel_id}>
              <button
                type="button"
                className={`ship-row${v.vessel_id === selectedVesselId ? " selected" : ""}`}
                onClick={() => onSelectVessel(v.vessel_id)}
              >
                <span className="ship-dot" style={{ background: TYPE_COLORS[v.vessel_type] ?? "#64748b" }} />
                <span className="ship-main">
                  <span className="ship-name">{vesselLabel(v)}</span>
                  <span className="ship-zone">{ZONE_NAMES[v.zone] ?? v.zone}</span>
                </span>
                <span className="ship-eta">{etaLabel(v)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
