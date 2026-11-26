import React from "react";

export default function StatePanel({ view, refreshing, onFetch }) {
  if (view.state === "no_scene") {
    return (
      <section className="glass panel state-panel" aria-live="polite">
        <div className="state-title">No recent satellite pass</div>
        <p className="muted">{view.real?.error ?? "No usable Sentinel-1 scene covers this port."}</p>
      </section>
    );
  }
  if (view.state === "not_fetched") {
    return (
      <section className="glass panel state-panel" aria-live="polite">
        <div className="state-title">Not fetched yet</div>
        <p className="muted">
          {view.hasAnyRealData
            ? "This port's latest Sentinel-1 pass hasn't been downloaded yet."
            : "No satellite data has been downloaded yet."}
        </p>
        <button type="button" className="btn-primary" onClick={onFetch} disabled={refreshing}>
          {refreshing ? "Fetching satellite pass…" : "Fetch this port (~15 s)"}
        </button>
      </section>
    );
  }
  return null;
}
