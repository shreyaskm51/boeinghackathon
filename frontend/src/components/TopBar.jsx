import React from "react";
import { daysAgoLabel, formatPassTimeIST } from "../lib/format.js";

export default function TopBar({ ports, portId, onChangePort, view, refreshing, onRefresh }) {
  const observed = view?.real?.status === "OK" ? view.real.scene_datetime : null;
  return (
    <header className="glass topbar">
      <div className="wordmark">EdgePort</div>
      <select className="port-picker" value={portId} onChange={(e) => onChangePort(e.target.value)} aria-label="Port">
        {ports.map((p) => (
          <option key={p.port_id} value={p.port_id}>
            {p.port_name} · {p.state}
          </option>
        ))}
      </select>

      <div className="topbar-pass">
        {view?.state === "old" ? <span className="badge-old">Old satellite pass: {view.ageDays} days ago</span> : null}
        {observed ? (
          <span className="pass-text">
            Sentinel-1 pass {formatPassTimeIST(observed)} · {daysAgoLabel(view.ageDays ?? 0)}
          </span>
        ) : (
          <span className="pass-text muted">No satellite pass shown</span>
        )}
        <button
          type="button"
          className={`icon-btn${refreshing ? " spinning" : ""}`}
          onClick={onRefresh}
          disabled={refreshing}
          title="Fetch the newest satellite pass for this port (~15 s)"
          aria-label="Fetch the newest satellite pass"
        >
          ⟳
        </button>
      </div>
    </header>
  );
}
