import React, { useCallback, useEffect, useMemo, useState } from "react";
import PortMap from "./components/PortMap.jsx";
import { pickInitialPortId, selectPortView } from "./lib/portState.js";

const STORAGE_KEY = "edgeport.port";
// keeps the harbour visible between the floating panels (top bar, left stats, right ships, bottom SMS)
const MAP_PADDING = { top: 90, left: 270, right: 310, bottom: 130 };

function readStoredPort() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export default function App() {
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [portId, setPortId] = useState("");
  const [selectedVesselId, setSelectedVesselId] = useState(null);
  const [radarOn, setRadarOn] = useState(false);

  const loadDashboard = useCallback(async () => {
    setLoadError("");
    try {
      const res = await fetch("/api/dashboard?source=real");
      if (!res.ok) throw new Error(`EdgePort API error (${res.status})`);
      const json = await res.json();
      setData(json);
      setPortId((current) => current || pickInitialPortId(json.ports_config.ports, readStoredPort()));
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Could not reach the EdgePort API.");
    }
  }, []);

  useEffect(() => {
    loadDashboard();
  }, [loadDashboard]);

  const changePort = (id) => {
    setPortId(id);
    setSelectedVesselId(null);
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // storage unavailable (private mode): the choice just isn't remembered
    }
  };

  const view = useMemo(() => (data && portId ? selectPortView(data, portId) : null), [data, portId]);
  const radar = view?.real?.status === "OK" ? { url: view.real.raw_preview_url, bounds: view.real.bounds } : null;

  if (!data) {
    return (
      <div className="splash">
        <div className="glass splash-card">
          <div className="wordmark">EdgePort</div>
          {loadError ? (
            <>
              <p>{loadError}</p>
              <button type="button" className="btn-primary" onClick={loadDashboard}>Try again</button>
            </>
          ) : (
            <p>Loading satellite observations…</p>
          )}
        </div>
      </div>
    );
  }

  const ports = data.ports_config.ports;
  return (
    <div className="app">
      <PortMap
        port={view?.port}
        vessels={view?.vessels ?? []}
        selectedVesselId={selectedVesselId}
        onSelectVessel={setSelectedVesselId}
        radar={radar}
        radarOn={radarOn}
        onToggleRadar={() => setRadarOn((on) => !on)}
        padding={MAP_PADDING}
      />
      <header className="glass topbar">
        <div className="wordmark">EdgePort</div>
        <select className="port-picker" value={portId} onChange={(e) => changePort(e.target.value)} aria-label="Port">
          {ports.map((p) => (
            <option key={p.port_id} value={p.port_id}>
              {p.port_name} · {p.state}
            </option>
          ))}
        </select>
      </header>
    </div>
  );
}
