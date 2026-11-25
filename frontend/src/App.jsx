import React, { useCallback, useEffect, useMemo, useState } from "react";
import MaritimeMap from "./components/MaritimeMap.jsx";
import RealDataPanel from "./components/RealDataPanel.jsx";
import SimulatorPanel from "./components/SimulatorPanel.jsx";
import TradeNowcastChart from "./components/TradeNowcastChart.jsx";

const CONGESTION_COLORS = {
  NORMAL: "#10B981",
  MODERATE: "#38BDF8",
  HIGH_CONGESTION: "#F59E0B",
  CRITICAL_CONGESTION: "#EF4444",
};

export default function App() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [errorMsg, setErrorMsg] = useState("");
  const [selectedPortId, setSelectedPortId] = useState("ALL");
  const [useLiveAis, setUseLiveAis] = useState(false);
  const [offlineMapMode, setOfflineMapMode] = useState(false);
  const [isSimulating, setIsSimulating] = useState(false);
  const [copiedPortId, setCopiedPortId] = useState("");
  const [directorySearch, setDirectorySearch] = useState("");
  // "auto" lets the server choose (real Sentinel-1 data when it has some, else simulated)
  const [dataSource, setDataSource] = useState("auto");
  const [busyRealPort, setBusyRealPort] = useState("");

  const fetchDashboard = useCallback(async (liveAis, source = "auto") => {
    setLoading(true);
    setErrorMsg("");
    try {
      const sourceParam = source === "auto" ? "" : `&source=${source}`;
      const res = await fetch(`/api/dashboard?use_live_ais=${liveAis ? "1" : "0"}${sourceParam}`);
      if (!res.ok) {
        throw new Error(`API error (${res.status})`);
      }
      const json = await res.json();
      setData(json);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to load EdgePort API.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDashboard(useLiveAis, dataSource);
  }, [fetchDashboard, useLiveAis, dataSource]);

  const handleRefreshRealPort = async (portId) => {
    setBusyRealPort(portId);
    setErrorMsg("");
    try {
      const res = await fetch("/api/real/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ port_id: portId, use_live_ais: useLiveAis }),
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || `Real data refresh failed (${res.status})`);
      if (json.refreshed_port && json.refreshed_port.status !== "OK") {
        setErrorMsg(`${portId}: ${json.refreshed_port.error || json.refreshed_port.status}`);
      }
      setData(json);
      setDataSource("real");
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Real data refresh failed.");
    } finally {
      setBusyRealPort("");
    }
  };

  const handleRefreshAllReal = async () => {
    setErrorMsg("");
    try {
      const res = await fetch("/api/real/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ port_id: "ALL" }),
      });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || `Real data refresh failed (${res.status})`);
      setData((prev) => (prev ? { ...prev, real_data: { ...prev.real_data, job: json.job } } : prev));
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Real data refresh failed.");
    }
  };

  // poll the background "fetch all ports" job and reload the real view when it finishes
  const jobRunning = Boolean(data?.real_data?.job?.running);
  useEffect(() => {
    if (!jobRunning) return undefined;
    const timer = setInterval(async () => {
      try {
        const res = await fetch("/api/real/status");
        const status = await res.json();
        if (!status.job.running) {
          clearInterval(timer);
          setDataSource("real");
          fetchDashboard(useLiveAis, "real");
        } else {
          setData((prev) =>
            prev ? { ...prev, real_data: { ...prev.real_data, job: status.job, ports: status.ports } } : prev
          );
        }
      } catch {
        // transient network error: keep polling
      }
    }, 3000);
    return () => clearInterval(timer);
  }, [jobRunning, fetchDashboard, useLiveAis]);

  const handleReload = async () => {
    setLoading(true);
    setErrorMsg("");
    try {
      const res = await fetch("/api/reload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ use_live_ais: useLiveAis }),
      });
      if (!res.ok) {
        throw new Error(`Reload failed (${res.status})`);
      }
      const json = await res.json();
      setData(json);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Failed to reload telemetry.");
    } finally {
      setLoading(false);
    }
  };

  const handleRunSimulation = async (simParams) => {
    setIsSimulating(true);
    setErrorMsg("");
    try {
      const res = await fetch("/api/simulate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...simParams, use_live_ais: useLiveAis }),
      });
      if (!res.ok) {
        const detail = await res.json().catch(() => ({}));
        throw new Error(detail.error ? `Simulation failed: ${detail.error}` : `Simulation failed (${res.status})`);
      }
      const json = await res.json();
      setData(json);
      setDataSource("simulated");
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : "Simulation error.");
    } finally {
      setIsSimulating(false);
    }
  };

  const handleCopyAlert = async (portId, text) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedPortId(portId);
      setTimeout(() => setCopiedPortId(""), 1800);
    } catch {
      // Clipboard fallback ignored if blocked by browser permissions
    }
  };

  const handleDownloadSmsLog = () => {
    if (!data?.alerts_by_port) return;
    const lines = Object.values(data.alerts_by_port).join("\n");
    const blob = new Blob([lines], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "edgeport_sms_alerts.txt";
    link.click();
    URL.revokeObjectURL(url);
  };

  const allPorts = data?.ports_config?.ports || [];
  const visibleVessels = useMemo(() => {
    const list = data?.enriched_vessels || [];
    if (selectedPortId === "ALL") return list;
    return list.filter((v) => v.port_id === selectedPortId);
  }, [data, selectedPortId]);

  const filteredDirectory = useMemo(() => {
    const rows = data?.national_network || [];
    const q = directorySearch.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (r) =>
        r.port_id.toLowerCase().includes(q) ||
        r.port_name.toLowerCase().includes(q) ||
        r.state.toLowerCase().includes(q) ||
        r.coast.toLowerCase().includes(q)
    );
  }, [data, directorySearch]);

  if (loading && !data) {
    return (
      <div style={{ padding: "2rem", color: "#cbd5e1" }}>
        Loading EdgePort Ground Station telemetry...
      </div>
    );
  }

  if (errorMsg && !data) {
    return (
      <div style={{ padding: "2rem", color: "#fca5a5" }}>
        <h2>EdgePort Ground Station Unavailable</h2>
        <p>{errorMsg}</p>
        <button type="button" className="btn btn-primary" onClick={() => fetchDashboard(useLiveAis, dataSource)}>
          Retry Connection
        </button>
      </div>
    );
  }

  const kpis = data.kpis;
  const header = data.packet.telemetry_header;
  const portsSummary = data.packet.ports_summary || [];
  const alertsByPort = data.alerts_by_port || {};

  return (
    <div className="app-shell">
      <header className="top-banner">
        <div>
          <h1>🛰️ EdgePort — Onboard Satellite AI &amp; Regional Port Intelligence</h1>
          <p>
            Track A (Ground Station &amp; React Operator Console) · 0–370 km Indian EEZ · &lt; 200 KB
            Telemetry
          </p>
        </div>
        <div className="status-pill">
          <span>
            {data.onboard_pipeline_available
              ? "🟢 Onboard AI Connected"
              : "🟡 Sample Telemetry / Swath Mode"}
          </span>
          <span>·</span>
          <span>{kpis.total_network_ports} Indian Ports</span>
        </div>
      </header>

      <aside className="sidebar">
        <h2>Ground Station Controls</h2>

        <div className="form-field">
          <label htmlFor="data-source-select">Data Source</label>
          <select
            id="data-source-select"
            value={data.data_source || "simulated"}
            onChange={(e) => setDataSource(e.target.value)}
          >
            <option value="real" disabled={!data.real_data?.has_packet}>
              Real Sentinel-1 scenes{data.real_data?.has_packet ? "" : " (fetch first)"}
            </option>
            <option value="simulated">Simulated passes</option>
          </select>
        </div>

        <div className="form-field">
          <label htmlFor="focus-port-select">Focus Port (Map &amp; Vessels)</label>
          <select
            id="focus-port-select"
            value={selectedPortId}
            onChange={(e) => setSelectedPortId(e.target.value)}
          >
            <option value="ALL">All {allPorts.length} Ports (Active Swath + Network)</option>
            {allPorts.map((p) => (
              <option key={p.port_id} value={p.port_id}>
                {p.port_name} [{p.port_id}] ({p.state})
              </option>
            ))}
          </select>
        </div>

        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={useLiveAis}
            onChange={(e) => setUseLiveAis(e.target.checked)}
          />
          <span>Attempt Live AISStream.io Feed</span>
        </label>

        <label className="checkbox-row">
          <input
            type="checkbox"
            checked={offlineMapMode}
            onChange={(e) => setOfflineMapMode(e.target.checked)}
          />
          <span>Offline Map Mode (Vector SVG)</span>
        </label>

        <button type="button" className="btn" onClick={handleReload} disabled={loading}>
          {loading ? "Refreshing..." : "Reload Telemetry from Disk"}
        </button>

        <hr style={{ borderColor: "#243554", inlineSize: "100%" }} />
        <div className="muted">
          <div>Packet Source: {data.packet_source_label}</div>
          <div style={{ marginBlockStart: "0.35rem" }}>AIS: {data.ais_feed.status_message}</div>
        </div>
      </aside>

      <main className="main-content">
        {errorMsg ? <div className="banner-warn">{errorMsg}</div> : null}
        {data.data_source === "real" ? (
          <div className="banner-info">
            Showing <strong>real Sentinel-1 radar</strong> detections from {header.ports_observed} ports
            (scenes {header.oldest_scene_utc?.slice(0, 10)} to {header.pass_timestamp_utc?.slice(0, 10)}). AIS is
            not checked for real scenes, so no vessel is flagged dark.
          </div>
        ) : (
          <div className="banner-info">
            Showing <strong>simulated</strong> orbital passes (synthetic SAR scenes). Switch Data Source to see real
            Sentinel-1 detections.
          </div>
        )}

        {/* PANEL 1: KPI Telemetry Strip */}
        <section className="panel" aria-labelledby="kpi-heading">
          <div className="panel-header">
            <h2 id="kpi-heading">1. Orbital Telemetry &amp; Ground KPI Strip</h2>
            <span className="muted">
              Pass UTC: {header.pass_timestamp_utc} · Sat: {header.satellite_id}
            </span>
          </div>

          <div className="kpi-grid">
            <div className="kpi-card">
              <div className="kpi-label">Orbital Bandwidth Saved</div>
              <div className="kpi-value">{kpis.bandwidth_saved_pct.toFixed(2)}%</div>
              <div className="kpi-delta">
                {kpis.raw_scene_size_mb.toFixed(1)} MB → {kpis.downlinked_json_size_kb.toFixed(2)} KB
                JSON
              </div>
            </div>

            <div className="kpi-card">
              <div className="kpi-label">Onboard Inference Time</div>
              <div className="kpi-value">{kpis.onboard_inference_time_ms.toFixed(1)} ms</div>
              <div className="kpi-delta neutral">
                {header.sensor_mode.split("_")[0]} · {header.sea_state}
              </div>
            </div>

            <div className="kpi-card">
              <div className="kpi-label">Active Vessels (≤ 370 km EEZ)</div>
              <div className="kpi-value">{kpis.total_active_vessels}</div>
              <div className={`kpi-delta ${kpis.total_dark_vessels > 0 ? "alert" : ""}`}>
                {kpis.total_dark_vessels} Dark Vessel(s) flagged
              </div>
            </div>

            <div className="kpi-card">
              <div className="kpi-label">Port Congestion Alerts (&gt;75%)</div>
              <div className="kpi-value">
                {kpis.congested_ports_count} / {kpis.total_ports} Swath
              </div>
              <div className="kpi-delta neutral">
                Network: {kpis.total_network_ports} Indian Minor Ports
              </div>
            </div>

            <div className="kpi-card">
              <div className="kpi-label">Live Regional Trade Proxy</div>
              <div className="kpi-value">₹ {kpis.total_trade_value_inr_cr.toFixed(1)} Cr</div>
              <div className="kpi-delta">
                {Number(kpis.total_cargo_tonnage_mt).toLocaleString()} MT Est. Cargo
              </div>
            </div>
          </div>
        </section>

        {/* PANEL 2: Interactive Maritime Map */}
        <section className="panel" aria-labelledby="map-heading">
          <div className="panel-header">
            <h2 id="map-heading">
              2. Interactive Maritime Map (0–50 km Coastal Zones &amp; Dark Vessel Alerts)
            </h2>
          </div>

          <div className="legend-row">
            <span className="legend-item">
              <span className="legend-dot" style={{ background: "#10B981" }} /> Fishing Boat (AIS)
            </span>
            <span className="legend-item">
              <span className="legend-dot" style={{ background: "#06B6D4" }} /> Ferry (AIS)
            </span>
            <span className="legend-item">
              <span className="legend-dot" style={{ background: "#F59E0B" }} /> Cargo Ship (AIS)
            </span>
            <span className="legend-item">
              <span className="legend-dot" style={{ background: "#EF4444" }} /> Unmatched Dark Vessel
            </span>
            <span className="legend-item">
              <span className="legend-dot" style={{ background: "#94A3B8" }} /> Standby Port (Next LEO
              Pass)
            </span>
          </div>

          <MaritimeMap
            ports={allPorts}
            portsSummary={portsSummary}
            vessels={data.enriched_vessels}
            selectedPortId={selectedPortId}
            coastalKm={data.ports_config?.coastal_zone_km || 50}
            offlineMode={offlineMapMode}
          />

          {visibleVessels.length > 0 ? (
            <div
              tabIndex={0}
              role="region"
              aria-labelledby="vessel-table-caption"
              className="table-wrapper"
              style={{ marginBlockStart: "0.9rem" }}
            >
              <table className="data-table">
                <caption id="vessel-table-caption" className="muted" style={{ padding: "0.4rem 0.7rem", textAlign: "left" }}>
                  Priority Detected Vessels &amp; AIS Cross-Match
                </caption>
                <thead>
                  <tr>
                    <th>Vessel ID</th>
                    <th>Port</th>
                    <th>Type</th>
                    <th>Zone</th>
                    <th>Length (m)</th>
                    <th>Offshore (km)</th>
                    <th>ETA (min)</th>
                    <th>Conf</th>
                    <th>AIS Status</th>
                    <th>AIS Vessel / MMSI</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleVessels.map((v) => (
                    <tr key={v.vessel_id}>
                      <th>{v.vessel_id}</th>
                      <td>{v.port_id}</td>
                      <td>{v.vessel_type}</td>
                      <td>{v.zone}</td>
                      <td>{v.estimated_length_m}</td>
                      <td>{v.distance_from_shore_km}</td>
                      <td>{v.eta_minutes_effective ?? "—"}</td>
                      <td>{Math.round(v.confidence * 100)}%</td>
                      <td>
                        <span className={`badge ${v.ais_matched ? "badge-ok" : v.alert_flag === "AIS_NOT_CHECKED" ? "badge-wait" : "badge-dark"}`}>
                          {v.ais_matched ? "MATCHED" : v.alert_flag === "AIS_NOT_CHECKED" ? "AIS N/A" : v.alert_flag}
                        </span>
                      </td>
                      <td>
                        {v.ais_matched
                          ? `${v.matched_ais_name} (${v.matched_ais_mmsi})`
                          : v.alert_flag === "AIS_NOT_CHECKED"
                            ? "Not checked (real scene)"
                            : "No Transponder Match"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </section>

        {/* Real Sentinel-1 observations */}
        {data.real_data?.available ? (
          <RealDataPanel
            ports={allPorts}
            realData={data.real_data}
            focusPortId={selectedPortId}
            onRefreshPort={handleRefreshRealPort}
            onRefreshAll={handleRefreshAllReal}
            busyPort={busyRealPort}
          />
        ) : null}

        {/* PANEL 3: Onboard AI Live Simulator */}
        <SimulatorPanel
          ports={allPorts}
          activePacket={data.packet}
          simulation={data.simulation}
          isSimulating={isSimulating}
          onRunSimulation={handleRunSimulation}
        />

        {/* PANEL 4: Port Operations, Trade Nowcasting & SMS Alert Center */}
        <section className="panel" aria-labelledby="ops-heading">
          <div className="panel-header">
            <h2 id="ops-heading">
              4. Small-Port Operations, Trade Nowcasting &amp; SMS Alert Center
            </h2>
          </div>

          <div className="ops-split">
            <div>
              <h3 style={{ margin: "0 0 0.75rem 0", fontSize: "0.95rem" }}>
                Port Berth Utilization &amp; Automated Rerouting
              </h3>

              {portsSummary.map((port) => {
                const utilPct = Math.round(port.utilization_index * 100);
                const statusColor = CONGESTION_COLORS[port.congestion_status] || "#64748B";
                return (
                  <div key={port.port_id} className="port-meter">
                    <div className="port-meter-head">
                      <span>
                        <strong>
                          {port.port_name} ({port.port_id})
                        </strong>{" "}
                        ·{" "}
                        <span style={{ color: statusColor, fontWeight: 700 }}>
                          {port.congestion_status}
                        </span>
                      </span>
                      <span className="muted">
                        {port.counts.berth_occupied}/{port.max_berths} berths ·{" "}
                        {port.counts.anchorage_waiting} queued · {port.dark_vessels_detected} dark
                      </span>
                    </div>
                    <div className="progress-track">
                      <div
                        className="progress-fill"
                        style={{ width: `${Math.min(100, utilPct)}%`, backgroundColor: statusColor }}
                      />
                    </div>
                  </div>
                );
              })}

              {data.rerouting_suggestions?.length > 0 ? (
                data.rerouting_suggestions.map((s) => (
                  <div key={s.port_id} className="banner-warn">
                    ⚠️ <strong>Reroute Advisory (&gt;80% Load):</strong> {s.message}
                  </div>
                ))
              ) : (
                <div className="banner-info">
                  All monitored ports in current swath are operating below the 80% rerouting
                  threshold.
                </div>
              )}

              <h3 style={{ margin: "1rem 0 0.55rem 0", fontSize: "0.95rem" }}>
                Incoming Traffic &amp; ETA Queue
              </h3>
              {data.eta_queue?.length > 0 ? (
                <div
                  tabIndex={0}
                  role="region"
                  aria-label="Incoming vessel ETA queue"
                  className="table-wrapper"
                >
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Vessel</th>
                        <th>Port</th>
                        <th>Type</th>
                        <th>Zone</th>
                        <th>ETA (min)</th>
                        <th>AIS</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.eta_queue.map((v) => (
                        <tr key={`eta-${v.vessel_id}`}>
                          <th>{v.vessel_id}</th>
                          <td>{v.port_id}</td>
                          <td>{v.vessel_type}</td>
                          <td>{v.zone}</td>
                          <td>{v.eta_minutes_effective}</td>
                          <td>
                            <span className={`badge ${v.ais_matched ? "badge-ok" : v.alert_flag === "AIS_NOT_CHECKED" ? "badge-wait" : "badge-dark"}`}>
                              {v.ais_matched ? "Verified" : v.alert_flag === "AIS_NOT_CHECKED" ? "AIS N/A" : "DARK VESSEL"}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="muted">No inbound vessels currently queued outside berths.</p>
              )}
            </div>

            <div>
              <h3 style={{ margin: "0 0 0.75rem 0", fontSize: "0.95rem" }}>
                Multi-Pass Regional Trade Nowcasting (₹ Crores)
              </h3>
              <TradeNowcastChart rows={data.nowcast} />

              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "0.5rem",
                  margin: "1.1rem 0 0.6rem 0",
                }}
              >
                <h3 style={{ margin: 0, fontSize: "0.95rem" }}>
                  Port Master SMS / WhatsApp Alert Center (≤ 160 Chars)
                </h3>
                <button type="button" className="btn" onClick={handleDownloadSmsLog}>
                  Download Dispatch Log (.txt)
                </button>
              </div>

              {Object.entries(alertsByPort).map(([pid, alertText]) => (
                <div key={pid} className="sms-card">
                  <div className="sms-card-head">
                    <span>
                      <strong>{pid}</strong> ·{" "}
                      <span className="muted">{alertText.length} / 160 chars</span>
                    </span>
                    <button
                      type="button"
                      className="btn"
                      style={{ padding: "0.2rem 0.55rem", fontSize: "0.75rem" }}
                      onClick={() => handleCopyAlert(pid, alertText)}
                    >
                      {copiedPortId === pid ? "Copied ✓" : "Copy"}
                    </button>
                  </div>
                  <p className="sms-text">{alertText}</p>
                </div>
              ))}
            </div>
          </div>

          <details style={{ marginBlockStart: "1.15rem" }}>
            <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: "0.9rem" }}>
              All {data.national_network?.length || 0} Indian Non-Major Ports Directory &amp; LEO
              Swath Status
            </summary>

            <div style={{ marginBlock: "0.65rem", maxInlineSize: "340px" }}>
              <input
                type="search"
                placeholder="Filter by port ID, name, state, or coast..."
                value={directorySearch}
                onChange={(e) => setDirectorySearch(e.target.value)}
                aria-label="Filter Indian non-major ports directory"
              />
            </div>

            <div
              tabIndex={0}
              role="region"
              aria-label="Indian non-major ports directory"
              className="table-wrapper"
            >
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Port ID</th>
                    <th>Port Name</th>
                    <th>State / UT</th>
                    <th>Coast</th>
                    <th>Berths</th>
                    <th>Status</th>
                    <th>Utilization</th>
                    <th>Dark Vessels</th>
                    <th>Trade (₹ Cr)</th>
                    <th>Operational Profile</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredDirectory.map((r) => (
                    <tr key={r.port_id}>
                      <th>{r.port_id}</th>
                      <td>{r.port_name}</td>
                      <td>{r.state}</td>
                      <td>{r.coast}</td>
                      <td>{r.max_berths}</td>
                      <td>
                        <span
                          className={`badge ${
                            r.in_active_swath ? "badge-ok" : "badge-wait"
                          }`}
                        >
                          {r.congestion_status}
                        </span>
                      </td>
                      <td>
                        {r.utilization_index !== null
                          ? `${Math.round(r.utilization_index * 100)}%`
                          : "—"}
                      </td>
                      <td>{r.in_active_swath ? r.dark_vessels_detected : "—"}</td>
                      <td>{r.in_active_swath ? r.estimated_trade_value_inr_cr : "—"}</td>
                      <td>{r.operational_profile}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </section>
      </main>
    </div>
  );
}
