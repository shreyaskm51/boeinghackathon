# EdgePort — Two-Person Work Split

The PRD splits cleanly into two halves that only talk to each other through **one file**:
`outputs/latest_telemetry.json` (the < 3 KB orbital packet). Agree on that format (see
`contracts/`) and you can both build at the same time without blocking each other.

```
 PERSON A — Track B (Onboard AI)            PERSON B — Track A (Ground + Dashboard)
 ┌──────────────────────────────┐          ┌───────────────────────────────────────┐
 │ sar_simulator.py             │          │ ground_station/analytics_engine.py    │
 │ cfar_onnx_detector.py        │  writes  │ ground_station/alert_formatter.py     │
 │ georeferencer.py             │ ───────▶ │ app.py  (Streamlit dashboard)         │
 │ edge_pipeline.py             │  JSON    │ data/ais_ground_truth.json            │
 └──────────────────────────────┘          └───────────────────────────────────────┘
            outputs/latest_telemetry.json  (format = contracts/telemetry_schema.json)
```

## Person A — Onboard Edge AI (`onboard_ai/`)
Branch: `feature/onboard-ai` · Task brief: [`docs/PERSON_A_onboard_ai.md`](docs/PERSON_A_onboard_ai.md)

| File | PRD step |
|---|---|
| `onboard_ai/sar_simulator.py` | Step 3 — SAR scene synth + GeoTIFF/PNG loader |
| `onboard_ai/cfar_onnx_detector.py` | Step 4 — CA-CFAR + auto-generated ONNX classifier |
| `onboard_ai/georeferencer.py` | Step 4 — pixel → WGS84, length, zone tagging |
| `onboard_ai/edge_pipeline.py` | Step 4 — orchestrator, raster purge, writes telemetry JSON |
| `tests/test_onboard_*.py` | Packet < 3 KB, matches schema, > 99% bandwidth saved |

## Person B — Ground Station & Dashboard (`ground_station/`, `app.py`)
Branch: `feature/ground-dashboard` · Task brief: [`docs/PERSON_B_ground_dashboard.md`](docs/PERSON_B_ground_dashboard.md)

| File | PRD step |
|---|---|
| `data/ais_ground_truth.json` | Step 2 — simulated AIS feed (+ optional AISStream.io connector) |
| `ground_station/analytics_engine.py` | Step 5 — dark vessels, utilization, ETAs, ₹ Cr trade |
| `ground_station/alert_formatter.py` | Step 5 — ≤ 160-char SMS/WhatsApp alert |
| `app.py` | Step 6 — 4-panel Streamlit dashboard |
| `tests/test_ground_*.py` | SMS ≤ 160 chars, dark-vessel matching |

Person B develops against `contracts/sample_telemetry.json` until Person A's pipeline is
producing real packets — no waiting.

## Shared (already done in this repo — change only by agreement)
- `requirements.txt`, `data/ports_config.json`, `contracts/telemetry_schema.json`,
  `contracts/sample_telemetry.json`, `README.md`.
- **Contract change rule:** if you need a new/renamed field in the telemetry packet, update
  `contracts/telemetry_schema.json` + `contracts/sample_telemetry.json` in a small PR of its own
  and tell your partner before merging.

## Integration point (the one place you touch each other's code)
The "Run Orbital Pass Simulation" button in `app.py` (Person B) calls
`onboard_ai.edge_pipeline.run_pass(...)` (Person A). The signature is fixed now:

```python
def run_pass(
    port_id: str | None = None,          # None = full constellation sweep (all 4 ports)
    sea_state: str = "MODERATE",          # "CALM" | "MODERATE" | "MONSOON_ROUGH"
    pass_time_ist: str = "18:00",         # "06:00" | "12:00" | "18:00" | "23:30"
    uploaded_image: bytes | None = None,  # optional user .tif/.png
    output_path: str = "outputs/latest_telemetry.json",
) -> dict:
    """Returns {"telemetry": <packet dict>, "raw_preview": np.ndarray (downsampled),
                "overlay_preview": np.ndarray, "alert_chips": list[bytes]}"""
```
Until it exists, Person B should stub it with `try: from onboard_ai.edge_pipeline import run_pass
except ImportError:` and load the sample JSON.

## Who owns the AIS fields?
The satellite has no AIS. Person A writes `ais_matched: false` and `dark_vessels_detected: 0`
as placeholders; Person B's `analytics_engine.py` is the authority and recomputes both from
`data/ais_ground_truth.json`.

## Suggested timeline (12-hour hackathon)
| Hours | Person A | Person B |
|---|---|---|
| 0–1 | Both: clone, `pip install -r requirements.txt`, read PRD + this file | same |
| 1–5 | SAR simulator + CFAR detector | AIS data + analytics engine + SMS formatter |
| 5–8 | Georeferencer + edge pipeline → real JSON | Dashboard panels 1, 2, 4 on sample JSON |
| 8–9 | **Merge both branches into `main`** — wire the simulator panel to `run_pass` | same |
| 9–12 | Bug fixes, tuning, README, demo rehearsal | same |
