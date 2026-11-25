# Person B — Ground Station & Dashboard (Track A)

**Branch:** `feature/ground-dashboard`  **You own:** `ground_station/**`, `app.py`,
`data/ais_ground_truth.json`, `tests/test_ground_*.py`
**Do not edit:** `onboard_ai/**`, `contracts/**` (contracts only via agreed PR)

## Paste this into Antigravity

> Read `EdgePort_PRD.pdf` (sections 2, 3, 4.1, 4.3, 5.2, 6), `TEAM_SPLIT.md`, `AGENTS.md`, and
> `contracts/telemetry_schema.json`. Implement PRD Steps 2 (AIS part), 5 and 6 only, fully, with
> no TODOs or placeholders. Develop against `contracts/sample_telemetry.json`; the onboard
> pipeline is being built in parallel by my teammate.
>
> 1. `data/ais_ground_truth.json`: simulated AIS records (MMSI, name, type, lat, lon, timestamp)
>    near the 4 ports in `data/ports_config.json` — deliberately leave a few vessels from the
>    sample telemetry unmatched so dark vessels show up. Add `ground_station/ais_connector.py`
>    with an optional AISStream.io websocket fetch (API key from env var `AISSTREAM_API_KEY`,
>    gracefully skipped if missing).
> 2. `ground_station/analytics_engine.py`: load a telemetry packet; cross-match each vessel
>    against AIS (nearest record within ~1 km) and set `ais_matched`; recompute
>    `dark_vessels_detected` per port; Port Utilization Index (berth_occupied / max_berths),
>    congestion status (> 75% = HIGH_CONGESTION, > 90% = CRITICAL_CONGESTION), ETA queue,
>    reroute suggestions when utilization > 80%, and trade value in ₹ Crores from tonnage.
> 3. `ground_station/alert_formatter.py`: one plain-text SMS/WhatsApp alert per port per pass,
>    **always ≤ 160 characters** (truncate gracefully), in the PRD format:
>    `EDGEPORT ALERT [IN-KRW]: 5/6 Berths Full, 4 Cargo in Queue, 2 Ferries ETA 28m, ...`
> 4. `app.py` (Streamlit): the 4 panels from PRD 4.3 — KPI strip; Folium map with berth /
>    anchorage / 50 km boundary and colour-coded markers (Green fishing, Cyan ferry, Gold cargo,
>    Red dark vessel) plus an offline Plotly fallback when tiles can't load; Onboard AI
>    Simulator panel (port select, sea state, .tif/.png upload, "Run Orbital Pass Simulation"
>    button showing Raw SAR | Detected Overlay | JSON side by side); Trade nowcasting + SMS
>    alert center with copy-ready alerts. The simulator button calls
>    `onboard_ai.edge_pipeline.run_pass(...)` (signature in `TEAM_SPLIT.md`); wrap the import in
>    try/except and fall back to the sample JSON if it's not merged yet.
> 5. `tests/test_ground_analytics.py` (pytest): every alert ≤ 160 chars, dark vessels detected
>    from sample data, utilization math correct.

## Done when
- `streamlit run app.py` launches with zero errors using only `contracts/sample_telemetry.json`.
- `pytest tests/test_ground_analytics.py` passes.
- PR from `feature/ground-dashboard` → `main` opened.
