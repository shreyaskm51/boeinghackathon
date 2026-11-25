# `onboard_ai/` — Orbital Edge Engine (Track B)

```bash
python -m onboard_ai.edge_pipeline                       # full sweep, all 4 ports -> outputs/latest_telemetry.json
python -m onboard_ai.edge_pipeline --port IN-KAK --sea-state MONSOON_ROUGH --pass-time 06:00
python -m onboard_ai.edge_pipeline --port IN-KRW --upload my_scene.png
python -m onboard_ai.edge_pipeline --sensor-mode X-BAND              # adds 3 m X-band spotlights
pytest tests/test_onboard_pipeline.py
```
First run trains the classifier and writes `onboard_ai/models/vessel_classifier.onnx` (~10 s).

## For the dashboard (`app.py`)
```python
from onboard_ai.edge_pipeline import run_pass

result = run_pass(port_id=None, sea_state="Monsoon Rough Sea", pass_time_ist="18:00",
                  uploaded_image=uploaded_file.getvalue() if uploaded_file else None,
                  sensor_mode="X-BAND")   # optional keyword; default "C-BAND"
result["telemetry"]              # dict, exactly contracts/telemetry_schema.json, also written to outputs/
result["raw_preview"]            # uint8 HxW  (<= 512 px) C-band coastal SAR, dB-scaled -> st.image(...)
result["overlay_preview"]        # uint8 HxWx3 detections: green fishing, cyan ferry, gold cargo, red alert
result["xband_raw_preview"]      # same for the 3 m X-band spotlight (None in C-band mode)
result["xband_overlay_preview"]  # same for the 3 m X-band spotlight (None in C-band mode)
result["alert_chips"]            # list[PNG bytes], 64x64, only for CRITICAL_CONGESTION / DARK_VESSEL_OFFSHORE
result["stats"]                  # raw MB, JSON bytes, bandwidth saved %, inference ms, counts, chip paths
```
`sea_state` accepts `CALM` / `MODERATE` / `MONSOON_ROUGH` or UI labels such as `"Clear Calm"`.
`sensor_mode` is an optional keyword added after the contract was agreed; existing calls are
unaffected. No caching needed on the dashboard side: the ONNX session is cached per process.

## Sensor modes
| Mode | What is imaged | Use it for |
|---|---|---|
| `C-BAND` (default) | 10 m coastal ROI (20 x 20 km) + three 10 m offshore EEZ chips | All-weather counts of ferries and cargo, anchorage queues, offshore traffic |
| `X-BAND` | everything above **plus** a 3 m X-band spotlight (9.2 x 9.2 km) over the harbour approach | Small fishing boats near the harbour. Inside the spotlight its detections replace the C-band ones, so nothing is double-counted |

## Pipeline
| Stage | File | What it does |
|---|---|---|
| A | `sar_simulator.py` | 2048² uint16 C-band coastal ROI (10 m, ~8.4 MB) + 3 offshore EEZ chips, and optionally a 3072² X-band spotlight (3 m) imaging the same vessels; Gamma speckle + wave texture per sea state; land with cranes/breakwater; vessels by port profile x pass time. Upload loader for `.tif` (rasterio optional) / `.png` / `.npy`. |
| B | `cfar_onnx_detector.py` | Cached land mask -> vectorized CA-CFAR (310 m guard / 710 m training windows, background estimated on a ~40 m grid, Pfa 1e-6, looks estimated from the scene) -> `ndimage.label` -> 9 blob features -> int8-quantized ONNX MLP trained at 10 m and 3 m (fishing / ferry / cargo / clutter) -> physical size gate. |
| C | `georeferencer.py` | Pixel -> WGS84, length, tonnage, cross-shore distance, zone (BERTH / ANCHORAGE_QUEUE / COASTAL_APPROACH / OFFSHORE_EEZ), ETA. |
| D | `edge_pipeline.py` | Port summaries, priority-ordered vessel list trimmed to the 3 KB budget, raster purge, alert chips, previews. |

## Measured on the simulator
C-band, 12 port passes per sea state (4 ports x 3 seeds):

| Sea state | Cargo | Ferry | Fishing | False alarms / pass |
|---|---|---|---|---|
| Calm | 100 % | 97 % | 99 % | 0 |
| Moderate | 100 % | 100 % | 54 % | 0.5 |
| Monsoon rough | 100 % | 71 % | 0 % | 2.4 (wave crests read as ferries) |

X-band vs C-band on the same vessels inside the spotlight (12 passes per sea state):

| Sea state | Fishing C-band -> X-band | Ferry C-band -> X-band |
|---|---|---|
| Calm | 14/14 -> 13/14 | 7/7 -> 6/7 |
| Moderate | **3/11 -> 9/11** | 8/9 -> 9/9 |
| Monsoon rough | 0/5 -> 0/5 | **6/9 -> 9/9** |

Whole-port effect, moderate sea, all 4 ports: coastal fishing boats counted rises from 33 to 45
(of 62 true). 10–15 m boats are 1–2 pixel targets at 10 m C-band resolution and drop out in
rough seas (PRD: C-band is reliable for vessels >= 25–30 m). X-band resolves them in moderate
seas, but in a monsoon sea neither band can separate them from wave clutter.

**Speed** (laptop CPU, detection + classification): C-band ~90 ms per port
(75 ms coastal ROI + 15 ms for three offshore chips); X-band mode ~205 ms per port.
A 4-port C-band sweep reports ~360 ms of onboard inference.

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
