import React from "react";
import { approachingCount } from "../lib/format.js";

const CONGESTION = {
  NORMAL: ["Normal", "chip-ok"],
  MODERATE: ["Moderate", "chip-info"],
  HIGH_CONGESTION: ["High congestion", "chip-warn"],
  CRITICAL_CONGESTION: ["Critical congestion", "chip-danger"],
};

export default function PortStatsPanel({ view }) {
  const { summary, port, state, ageDays } = view;
  if (!summary || !port || (state !== "current" && state !== "old")) return null;
  const counts = summary.counts ?? {};
  const occupied = counts.berth_occupied ?? 0;
  const max = port.max_berths || 1;
  const [label, chip] = CONGESTION[summary.congestion_status] ?? [summary.congestion_status, "chip-info"];

  return (
    <aside className={`glass panel stats-panel${state === "old" ? " stale" : ""}`} aria-label="Port status">
      {state === "old" ? <div className="badge-old block">Old satellite pass: {ageDays} days ago</div> : null}
      <div className={`chip ${chip}`}>{label}</div>

      <div className="stat">
        <div className="stat-label">Berths occupied</div>
        <div className="stat-value">
          {occupied} <span className="stat-of">/ {port.max_berths}</span>
        </div>
        <div className="bar">
          <div className="bar-fill" style={{ inlineSize: `${Math.min(100, (occupied / max) * 100)}%` }} />
        </div>
      </div>

      <div className="stat">
        <div className="stat-label">Waiting at anchorage</div>
        <div className="stat-value amber">{counts.anchorage_waiting ?? 0}</div>
      </div>

      <div className="stat">
        <div className="stat-label">Approaching (0–50 km)</div>
        <div className="stat-value">{approachingCount(counts)}</div>
      </div>

      {counts.offshore_eez_vessels > 0 ? (
        <div className="stat">
          <div className="stat-label">Offshore (EEZ)</div>
          <div className="stat-value">{counts.offshore_eez_vessels}</div>
        </div>
      ) : null}

      <div className="stat">
        <div className="stat-label">Est. cargo value</div>
        <div className="stat-value small">₹{summary.estimated_trade_value_inr_cr} Cr</div>
      </div>
    </aside>
  );
}
