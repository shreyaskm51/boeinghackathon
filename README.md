# EdgePort — Onboard Satellite AI for Regional Port & Coastal Vessel Intelligence

Hackathon MVP (Track A + Track B). Full spec: [`EdgePort_PRD.pdf`](EdgePort_PRD.pdf).
**Two-person split:** [`TEAM_SPLIT.md`](TEAM_SPLIT.md).

- **Orbital Edge Engine (`onboard_ai/`)** — simulates a LEO SAR payload: land mask → CA-CFAR →
  ONNX classifier → WGS84 georeferencing → deletes the raster → < 200 KB JSON packet
  covering 60 Indian regional and island ports (the PRD's original 3 KB budget was raised by team
  decision so hundreds of vessels are listed; it is still a 99.99 % bandwidth saving).
- **Ground Station & Dashboard (`ground_station/`, `app.py`)** — reads the packet, flags dark
  vessels against AIS, computes port utilization / ETAs / ₹ Cr trade, sends 160-char SMS alerts.

## Setup
```bash
git clone <repo-url>
cd edgeport
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Run
```bash
python -m onboard_ai.edge_pipeline   # writes outputs/latest_telemetry.json
streamlit run app.py                 # dashboard
pytest                               # tests
```

## Repo layout
```
contracts/        telemetry_schema.json + sample_telemetry.json  (shared contract)
data/             ports_config.json (shared), ais_ground_truth.json (Person B), sample_passes/
onboard_ai/       Person A — SAR simulator, CFAR+ONNX detector, georeferencer, edge pipeline
ground_station/   Person B — analytics engine, SMS alert formatter
app.py            Person B — Streamlit dashboard
outputs/          generated telemetry (git-ignored)
docs/             per-person task briefs with ready-to-paste Antigravity prompts
```
