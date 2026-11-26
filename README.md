# EdgePort — Onboard Satellite AI for Regional Port & Coastal Vessel Intelligence

Hackathon MVP (Track A + Track B). Full spec: [`EdgePort_PRD.pdf`](EdgePort_PRD.pdf).
**Two-person split:** [`TEAM_SPLIT.md`](TEAM_SPLIT.md).

- **Orbital Edge Engine (`onboard_ai/`)** — simulates a LEO SAR payload: land mask → CA-CFAR →
  ONNX classifier → WGS84 georeferencing → deletes the raster → < 200 KB JSON packet
  covering 60 Indian regional and island ports (the PRD's original 3 KB budget was raised by team
  decision so hundreds of vessels are listed; it is still a 99.99 % bandwidth saving).
- **Ground Station & Dashboard (`ground_station/`, `frontend/`)** — reads the packet, flags dark
  vessels against AIS, computes port utilization / ETAs / ₹ Cr trade, sends 160-char SMS alerts;
  React operator dashboard served by `ground_station/api_server.py`.

## Setup
```bash
git clone <repo-url>
cd edgeport
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cd frontend && npm ci && npm run build && cd ..   # React dashboard (Node 18+)
```
On Windows, if `import onnxruntime` fails with "filename or extension is too long", create the
venv at a short path such as `C:\venvs\edgeport`.

## Run
```bash
python -m onboard_ai.real_sentinel1  # fetch REAL Sentinel-1 scenes for all ports (~5 min, free, no account)
python -m ground_station.api_server  # dashboard + API at http://127.0.0.1:8000
python -m onboard_ai.edge_pipeline   # optional: run a pass from the CLI (writes outputs/)
pytest                               # tests
```
For frontend development with hot reload, run the API server and `cd frontend && npm run dev`
(Vite on http://localhost:5173 proxies `/api` to port 8000).

The dashboard is **one port's own screen**: pick a port at the top (your choice is
remembered) and its map fills the screen — a light MapLibre map (OpenFreeMap Positron) with the
berth and anchorage areas, the ships detected in the newest real Sentinel-1 pass, and frosted
panels for port status, the ship list (click a ship to fly to it) and the port master SMS.

- **Real radar** toggle lays the actual Sentinel-1 image over the map.
- **⟳** fetches the port's newest Sentinel-1 pass (~15 s); ports not fetched yet show a button.
- Passes older than **30 days** are marked "Old satellite pass"; ports with no recent scene
  (Kavaratti) say so instead of showing zeros.
- Real scenes come from Copernicus Sentinel-1 via Microsoft Planetary Computer; no AIS is
  matched to them yet, so no vessel is flagged dark.

## Repo layout
```
contracts/        telemetry_schema.json + sample_telemetry.json  (shared contract)
data/             ports_config.json (shared), ais_ground_truth.json (Person B), sample_passes/
onboard_ai/       Person A — SAR simulator, CFAR+ONNX detector, georeferencer, edge pipeline
ground_station/   Person B — analytics engine, AIS connector, SMS alerts, API server
frontend/         React + MapLibre single-port dashboard (Vite)
outputs/          generated telemetry (git-ignored)
docs/             per-person task briefs with ready-to-paste Antigravity prompts
```
