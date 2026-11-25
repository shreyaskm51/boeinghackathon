# Integration notes for Person B (from Person A)

I merged your `main` (React dashboard, ground station, 60 ports) into `feature/onboard-ai` and
ran everything together: your server now drives the real onboard pipeline for all 60 ports,
including the 4 island ports and full sweeps. Here is what's left on your side.

## 1. Read the simulated AIS feed — otherwise almost every ship shows as "dark"
Your AIS file only knows the sample packet's vessels, so every ship the simulator creates is
unmatched: 16 of 19 at Karwar, 711 of 742 on a full sweep.

Each simulated pass now writes **`outputs/simulated_ais.json`** (same format as
`data/ais_ground_truth.json`; also returned as `run_pass(...)["simulated_ais"]`). About
3–20 % of vessels are deliberately missing, so a few genuine dark vessels remain. Load it
alongside your file, e.g. in `fetch_ais_feed`:

```python
sim_path = REPO_ROOT / "outputs" / "simulated_ais.json"
if sim_path.exists():
    local_records += load_local_ais(ais_path=sim_path)
```

Measured with your own `analyze_telemetry`: dark vessels drop from 16→2 (Karwar),
22→5 (Okha), 27→1 (Kakinada), 10→2 (Port Blair).

## 2. `api_server.py` drops the connection on bad input
`run_simulation` only catches `KeyError`; `run_pass` raises `ValueError` for an unknown port,
sea state or pass time (e.g. `"pass_time_ist": "09:15"`), so the request gets no response at
all. Catch `ValueError` too and return HTTP 400.

## 3. `test_api_server_endpoints_and_simulation_smoke` fails on a fresh clone
- It requests `/` and expects the built React app, but `frontend/dist/` is git-ignored, so it
  returns 404 until someone runs `npm run build`. Skip that check when `dist/` is missing, or
  build in the test setup.
- It reads the shared `outputs/latest_telemetry.json`. With the real pipeline merged, the
  simulate call overwrites that file, so results depend on what ran before. Point the test at
  a temp file (monkeypatch `DEFAULT_LATEST_TELEMETRY_PATH`).

## 4. Packet budget is now 200 KB (schema on `main` still says 3 KB)
With 60 ports the port summaries alone are ~20 KB, so `main`'s schema rejects every full
sweep. `feature/onboard-ai` keeps your `^IN-[A-Z0-9]{3,5}$` port-ID pattern and sets
`downlinked_json_size_kb < 200`. A full sweep detects ~1,100 vessels; ~740 fit — lowest
priority vessels (mostly fishing boats) are left out of `vessels` but are still in each port's
`counts`.

## 5. Things to design around
- **Timing:** a single port takes ~0.5–1 s; a full 60-port sweep ~26 s (X-band ~2.5x longer).
  Default the simulator button to one port and show a spinner for "ALL".
- **X-band:** `run_pass(..., sensor_mode="X-BAND")` adds a 3 m spotlight that finds far more
  small fishing boats; it also returns `xband_raw_preview` / `xband_overlay_preview`.
- **Model:** detection uses a classifier trained on real Sentinel-1 data (OpenSARShip), which
  is CC-BY-NC-SA — please keep the attribution in the app's about/footer:
  *"Contains modified Copernicus Sentinel data, processed and annotated by the OpenSARShip
  (OSSDD) dataset authors."*
- **Workflow:** please push to `feature/ground-dashboard` and open a PR instead of pushing to
  `main` directly, so we can both review before merging.

## Removed on my side
My FastAPI server (`api/`) is gone — your `ground_station/api_server.py` is the one API. The
old `contract/16-ports-200kb` branch is superseded by your 60-port list.
