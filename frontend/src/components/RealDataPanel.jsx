import React, { useEffect, useMemo, useState } from "react";

const fmtDate = (iso) => (iso ? iso.slice(0, 16).replace("T", " ") + " UTC" : "—");

function daysAgo(iso) {
  if (!iso) return null;
  const d = (Date.now() - Date.parse(iso)) / 86_400_000;
  return d < 1 ? "today" : `${Math.round(d)} days ago`;
}

export default function RealDataPanel({ ports, realData, focusPortId, onRefreshPort, onRefreshAll, busyPort }) {
  const portsInfo = realData?.ports || {};
  const job = realData?.job || {};
  const [viewPort, setViewPort] = useState("");

  const observed = useMemo(
    () => Object.entries(portsInfo).filter(([, v]) => v.status === "OK"),
    [portsInfo]
  );
  const missing = ports.filter((p) => portsInfo[p.port_id]?.status !== "OK");

  // follow the sidebar's focus port; otherwise show the most recently observed port
  useEffect(() => {
    if (focusPortId && focusPortId !== "ALL") setViewPort(focusPortId);
  }, [focusPortId]);
  const fallback = observed.sort((a, b) => (b[1].scene_datetime || "").localeCompare(a[1].scene_datetime || ""))[0]?.[0];
  const shownId = viewPort || fallback || ports[0]?.port_id || "";
  const shown = portsInfo[shownId];
  const shownPort = ports.find((p) => p.port_id === shownId);
  const cacheBust = shown?.fetched_utc ? `?t=${encodeURIComponent(shown.fetched_utc)}` : "";

  return (
    <section className="panel" aria-labelledby="real-panel-heading">
      <div className="panel-header">
        <h2 id="real-panel-heading">Real Sentinel-1 Observations (Copernicus)</h2>
        <span className="muted">
          {observed.length} / {ports.length} ports observed · AIS not checked for real scenes
        </span>
      </div>

      <div className="sim-controls">
        <div className="form-field">
          <label htmlFor="real-port-select">Port</label>
          <select id="real-port-select" value={shownId} onChange={(e) => setViewPort(e.target.value)}>
            {ports.map((p) => {
              const info = portsInfo[p.port_id];
              const tag = info?.status === "OK" ? info.scene_datetime.slice(0, 10) : info ? info.status : "not fetched";
              return (
                <option key={p.port_id} value={p.port_id}>
                  {p.port_name} [{p.port_id}] — {tag}
                </option>
              );
            })}
          </select>
        </div>
        <button
          type="button"
          className="btn btn-primary"
          disabled={Boolean(busyPort) || job.running}
          onClick={() => onRefreshPort(shownId)}
        >
          {busyPort === shownId ? "Fetching real scene…" : "Fetch latest real scene (~15 s)"}
        </button>
        <button type="button" className="btn" disabled={job.running || Boolean(busyPort)} onClick={onRefreshAll}>
          {job.running ? `Fetching all ports… ${job.done}/${job.total}` : `Fetch all ${ports.length} ports (~5 min)`}
        </button>
      </div>

      {shown?.status === "OK" ? (
        <>
          <div className="muted" style={{ marginBlock: "0.6rem" }}>
            <strong>{shownPort?.port_name}</strong> · scene {fmtDate(shown.scene_datetime)} ({daysAgo(shown.scene_datetime)}) ·{" "}
            {shown.platform} · sea state {shown.sea_state} · {shown.vessels_detected} vessels detected
          </div>
          <div className="sim-results-grid">
            <div className="sar-card">
              <h3>Real Sentinel-1 VV backscatter</h3>
              <img className="sar-preview-img" src={`${shown.raw_preview_url}${cacheBust}`} alt={`Real Sentinel-1 radar over ${shownId}`} />
            </div>
            <div className="sar-card">
              <h3>Onboard CFAR + ONNX detections</h3>
              <img
                className="sar-preview-img"
                src={`${shown.overlay_preview_url}${cacheBust}`}
                alt={`Detected vessels on real Sentinel-1 radar over ${shownId}`}
              />
            </div>
          </div>
        </>
      ) : (
        <div className="banner-info" style={{ marginBlockStart: "0.6rem" }}>
          {shown
            ? `${shownPort?.port_name}: ${shown.error || shown.status}`
            : `${shownPort?.port_name || shownId}: no real scene fetched yet — use "Fetch latest real scene".`}
        </div>
      )}

      {job.errors?.length ? (
        <details style={{ marginBlockStart: "0.6rem" }}>
          <summary className="muted">{job.errors.length} port(s) without a usable scene in the last refresh</summary>
          <ul className="muted">
            {job.errors.map((e, i) => (
              <li key={`${e.port_id}-${i}`}>
                {e.port_id || "refresh"}: {e.error || e.status}
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {missing.length && !job.running ? (
        <div className="muted" style={{ marginBlockStart: "0.5rem" }}>
          Not observed: {missing.slice(0, 12).map((p) => p.port_id).join(", ")}
          {missing.length > 12 ? ` and ${missing.length - 12} more` : ""}
        </div>
      ) : null}

      <div className="muted" style={{ marginBlockStart: "0.6rem", fontSize: "0.75rem" }}>
        {realData?.attribution}
      </div>
    </section>
  );
}
