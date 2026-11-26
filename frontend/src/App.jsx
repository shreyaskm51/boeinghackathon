import React, { useCallback, useEffect, useMemo, useState } from "react";
import PortMap from "./components/PortMap.jsx";
import PortStatsPanel from "./components/PortStatsPanel.jsx";
import ShipsPanel from "./components/ShipsPanel.jsx";
import SmsPanel from "./components/SmsPanel.jsx";
import StatePanel from "./components/StatePanel.jsx";
import Toast from "./components/Toast.jsx";
import TopBar from "./components/TopBar.jsx";
import { pickInitialPortId, selectPortView } from "./lib/portState.js";

const STORAGE_KEY = "edgeport.port";
// Keeps the harbour visible between the floating panels. On narrow screens the side panels
// would leave no room for the map, so the camera ignores them there.
const WIDE_PADDING = { top: 90, left: 270, right: 310, bottom: 160 };
const NARROW_PADDING = { top: 80, left: 24, right: 24, bottom: 150 };
const WIDE_MIN_PX = 1100;

function readStoredPort() {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function useMapPadding() {
  const [wide, setWide] = useState(() => window.innerWidth >= WIDE_MIN_PX);
  useEffect(() => {
    const onResize = () => setWide(window.innerWidth >= WIDE_MIN_PX);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return wide ? WIDE_PADDING : NARROW_PADDING;
}

export default function App() {
  const [data, setData] = useState(null);
  const [loadError, setLoadError] = useState("");
  const [portId, setPortId] = useState("");
  const [selectedVesselId, setSelectedVesselId] = useState(null);
  const [radarOn, setRadarOn] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [toast, setToast] = useState("");
  const padding = useMapPadding();

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

  // Fetch the newest real Sentinel-1 pass for the current port (~15 s). On failure the
  // previous data stays on screen and the reason is shown in a toast.
  const refreshPort = async () => {
    setRefreshing(true);
    setToast("");
    try {
      const res = await fetch("/api/real/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ port_id: portId }),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json.error || `Refresh failed (${res.status})`);
      setData(json);
      if (json.refreshed_port && json.refreshed_port.status !== "OK") {
        setToast(json.refreshed_port.error || "No usable satellite pass for this port.");
      }
    } catch (err) {
      setToast(err instanceof Error && err.message !== "Failed to fetch" ? err.message : "Refresh failed: EdgePort server unreachable.");
    } finally {
      setRefreshing(false);
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
        padding={padding}
      />
      <TopBar
        ports={data.ports_config.ports}
        portId={portId}
        onChangePort={changePort}
        view={view}
        refreshing={refreshing}
        onRefresh={refreshPort}
      />
      {view ? (
        <>
          <PortStatsPanel view={view} />
          <ShipsPanel view={view} selectedVesselId={selectedVesselId} onSelectVessel={setSelectedVesselId} />
          <StatePanel view={view} refreshing={refreshing} onFetch={refreshPort} />
          {view.state !== "not_fetched" ? <SmsPanel alert={view.alert} /> : null}
        </>
      ) : null}
      {data.real_data?.attribution ? <div className="data-credit">{data.real_data.attribution}</div> : null}
      <Toast message={toast} onClose={() => setToast("")} />
    </div>
  );
}
