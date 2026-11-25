# `onboard_ai/` — Orbital Edge Engine (Track B)

```bash
python -m onboard_ai.edge_pipeline                       # full sweep, all 4 ports -> outputs/latest_telemetry.json
python -m onboard_ai.edge_pipeline --port IN-KAK --sea-state MONSOON_ROUGH --pass-time 06:00
python -m onboard_ai.edge_pipeline --port IN-KRW --upload my_scene.png
pytest tests/test_onboard_pipeline.py
```
First run trains the classifier and writes `onboard_ai/models/vessel_classifier.onnx` (~5 s).

## For the dashboard (`app.py`)
```python
from onboard_ai.edge_pipeline import run_pass

result = run_pass(port_id=None, sea_state="Monsoon Rough Sea", pass_time_ist="18:00",
                  uploaded_image=uploaded_file.getvalue() if uploaded_file else None)
result["telemetry"]        # dict, exactly contracts/telemetry_schema.json, also written to outputs/
result["raw_preview"]      # uint8 HxW  (<= 512 px) raw SAR, dB-scaled  -> st.image(...)
result["overlay_preview"]  # uint8 HxWx3 detections: green fishing, cyan ferry, gold cargo, red alert
result["alert_chips"]      # list[PNG bytes], 64x64, only for CRITICAL_CONGESTION / DARK_VESSEL_OFFSHORE
result["stats"]            # raw MB, JSON bytes, bandwidth saved %, inference ms, counts, chip paths
```
`sea_state` accepts `CALM` / `MODERATE` / `MONSOON_ROUGH` or UI labels such as `"Clear Calm"`.
No caching needed on the dashboard side: the ONNX session is already cached per process.

## Pipeline
| Stage | File | What it does |
|---|---|---|
| A | `sar_simulator.py` | 2048² uint16 C-band coastal ROI (10 m, ~8.4 MB) + 3 offshore EEZ chips; Gamma speckle + wave texture per sea state; land with cranes/breakwater; vessels by port profile x pass time. Upload loader for `.tif` (rasterio optional) / `.png` / `.npy`. |
| B | `cfar_onnx_detector.py` | Land mask -> vectorized CA-CFAR (guard 31 px, training 71 px, Pfa 1e-6, looks estimated from the scene) -> `ndimage.label` -> 8 blob features -> int8-quantized ONNX MLP (fishing / ferry / cargo / clutter). |
| C | `georeferencer.py` | Pixel -> WGS84, length, tonnage, cross-shore distance, zone (BERTH / ANCHORAGE_QUEUE / COASTAL_APPROACH / OFFSHORE_EEZ), ETA. |
| D | `edge_pipeline.py` | Port summaries, priority-ordered vessel list trimmed to the 3 KB budget, raster purge, alert chips, previews. |

## Measured on the simulator (12 passes per sea state, 4 ports x 3 seeds)
| Sea state | Cargo | Ferry | Fishing | False alarms / pass |
|---|---|---|---|---|
| Calm | 100 % | 100 % | 99 % | ~0.3 |
| Moderate | 100 % | 100 % | ~59 % | ~0.5 |
| Monsoon rough | 100 % | ~74 % | 0 % | ~2 (wave crests read as ferries) |

Small fishing boats are 1–2 pixel targets at 10 m C-band resolution and drop out in rough seas,
matching the PRD's physics constraint (C-band is reliable for vessels >= 25–30 m).
Onboard inference is ~0.45 s per port on a laptop CPU.

## Known limits
- Only ~4 vessels fit in the 3 KB packet next to the 4 port summaries; full totals are in
  `ports_summary[].counts`. The list is prioritised: dark-vessel candidates, anchorage queue,
  approaching ferries, berths, then the rest.
- `ais_matched` is always `false` and `dark_vessels_detected` always `0` — the ground station
  owns AIS matching.
- Uploaded images without GeoTIFF georeferencing are assumed to be 10 m GSD centred on the
  selected port; their land mask is estimated from the image (Otsu + connectivity).
- `raw_scene_size_mb` reports the 142.4 MB full swath per port that the ROI is cropped from.
- **Windows:** if `import onnxruntime` fails with "filename or extension is too long", the venv
  path is too deep — create the venv somewhere short such as `C:\venvs\edgeport`.
