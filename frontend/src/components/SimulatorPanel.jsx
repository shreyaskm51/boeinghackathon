import React, { useState } from "react";

export default function SimulatorPanel({
  ports,
  activePacket,
  simulation,
  isSimulating,
  onRunSimulation,
}) {
  const [simPort, setSimPort] = useState("ALL");
  const [seaState, setSeaState] = useState("MODERATE");
  const [passTimeIst, setPassTimeIst] = useState("18:00");
  const [uploadedImageB64, setUploadedImageB64] = useState(null);
  const [uploadedName, setUploadedName] = useState("");

  const handleFileChange = (event) => {
    const file = event.target.files?.[0];
    if (!file) {
      setUploadedImageB64(null);
      setUploadedName("");
      return;
    }
    setUploadedName(file.name);
    const reader = new FileReader();
    reader.onload = () => {
      setUploadedImageB64(typeof reader.result === "string" ? reader.result : null);
    };
    reader.readAsDataURL(file);
  };

  const handleSubmit = (event) => {
    event.preventDefault();
    onRunSimulation({
      port_id: simPort === "ALL" ? null : simPort,
      sea_state: seaState,
      pass_time_ist: passTimeIst,
      uploaded_image_b64: uploadedImageB64,
    });
  };

  return (
    <section className="panel" aria-labelledby="sim-panel-heading">
      <div className="panel-header">
        <h2 id="sim-panel-heading">
          3. Onboard AI Live Simulator (Raw SAR → CFAR/ONNX → &lt; 3 KB Telemetry)
        </h2>
        {uploadedName ? <span className="muted">Uploaded SAR: {uploadedName}</span> : null}
      </div>

      <form className="sim-controls" onSubmit={handleSubmit}>
        <div className="form-field">
          <label htmlFor="sim-port-select">Target Port / Swath</label>
          <select
            id="sim-port-select"
            value={simPort}
            onChange={(e) => setSimPort(e.target.value)}
          >
            <option value="ALL">All {ports.length} Ports (Constellation Sweep)</option>
            {ports.map((p) => (
              <option key={p.port_id} value={p.port_id}>
                {p.port_name} [{p.port_id}] ({p.state})
              </option>
            ))}
          </select>
        </div>

        <div className="form-field">
          <label htmlFor="sim-sea-select">Weather / Sea State</label>
          <select
            id="sim-sea-select"
            value={seaState}
            onChange={(e) => setSeaState(e.target.value)}
          >
            <option value="CALM">CALM</option>
            <option value="MODERATE">MODERATE</option>
            <option value="MONSOON_ROUGH">MONSOON_ROUGH</option>
          </select>
        </div>

        <div className="form-field">
          <label htmlFor="sim-pass-select">LEO Pass (IST)</label>
          <select
            id="sim-pass-select"
            value={passTimeIst}
            onChange={(e) => setPassTimeIst(e.target.value)}
          >
            <option value="06:00">06:00 IST</option>
            <option value="12:00">12:00 IST</option>
            <option value="18:00">18:00 IST</option>
            <option value="23:30">23:30 IST</option>
          </select>
        </div>

        <div className="form-field">
          <label htmlFor="sim-file-upload">Optional Sentinel-1 SAR (.tif / .png)</label>
          <input
            id="sim-file-upload"
            type="file"
            accept=".tif,.tiff,.png"
            onChange={handleFileChange}
          />
        </div>

        <button type="submit" className="btn btn-primary" disabled={isSimulating}>
          {isSimulating ? "Running Orbital Pass..." : "🛰️ Run Orbital Pass Simulation"}
        </button>
      </form>

      {simulation ? (
        <>
          {simulation.fallback_mode ? (
            <div className="banner-info">
              Displaying ground-station preview generated from the &lt; 3 KB telemetry contract
              (Person A&apos;s <code>onboard_ai/edge_pipeline.py</code> will plug in automatically once merged).
            </div>
          ) : null}

          <div className="sim-results-grid">
            <div className="sar-card">
              <h3>Raw SAR Backscatter Scene</h3>
              <img
                className="sar-preview-img"
                src={simulation.raw_preview_url}
                alt="Raw SAR backscatter scene preview"
              />
            </div>

            <div className="sar-card">
              <h3>Onboard CFAR + ONNX Overlay</h3>
              <img
                className="sar-preview-img"
                src={simulation.overlay_preview_url}
                alt="Onboard detected vessel bounding box overlay"
              />
            </div>

            <div className="sar-card">
              <h3>Downlinked Telemetry Packet (&lt; 3 KB)</h3>
              <pre className="json-viewer">{JSON.stringify(activePacket, null, 2)}</pre>
            </div>
          </div>

          {simulation.alert_chip_urls?.length > 0 ? (
            <div style={{ marginBlockStart: "0.9rem" }}>
              <strong style={{ fontSize: "0.84rem" }}>
                64×64 Alert Chips (Critical Congestion / Offshore Dark Vessel)
              </strong>
              <div className="chips-row">
                {simulation.alert_chip_urls.map((url, idx) => (
                  <img
                    key={`chip-${idx}`}
                    src={url}
                    alt={`Vessel alert chip ${idx + 1}`}
                    className="chip-img"
                  />
                ))}
              </div>
            </div>
          ) : null}
        </>
      ) : (
        <details>
          <summary style={{ cursor: "pointer", fontSize: "0.86rem", color: "#94a3b8" }}>
            Inspect Active Downlinked JSON Telemetry Packet (&lt; 3 KB)
          </summary>
          <pre className="json-viewer" style={{ marginBlockStart: "0.6rem" }}>
            {JSON.stringify(activePacket, null, 2)}
          </pre>
        </details>
      )}
    </section>
  );
}
