# `onboard_ai/` — Orbital Edge Engine (Track B)

```bash
python -m onboard_ai.edge_pipeline                       # full sweep, all 60 ports -> outputs/latest_telemetry.json
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
| B | `cfar_onnx_detector.py` | Cached land mask -> vectorized CA-CFAR (310 m guard / 710 m training windows, background estimated on a ~40 m grid, Pfa 1e-6, looks estimated from the scene) -> `ndimage.label` -> 9 blob features -> int8-quantized ONNX MLP trained on real OpenSARShip Sentinel-1 chips + simulator chips at 10 m and 3 m (fishing / ferry / cargo / clutter) -> physical size gate. |
| C | `georeferencer.py` | Pixel -> WGS84, length, tonnage, cross-shore distance, zone (BERTH / ANCHORAGE_QUEUE / COASTAL_APPROACH / OFFSHORE_EEZ), ETA. |
| D | `edge_pipeline.py` | Port summaries, priority-ordered vessel list (all vessels fit the 200 KB budget; lowest priority trimmed first if ever exceeded), raster purge, alert chips, previews. |

## How the classifier is trained
`get_classifier()` uses **`models/vessel_classifier_opensarship.onnx`** (committed, ~5 KB), trained by
`python -m onboard_ai.train_opensarship` on:

1. **Real Sentinel-1 ships from OpenSARShip** (Hugging Face `sylviaHoch/OpenSARShip-Ship-Detection-Dataset`,
   2 training shards = 238 real 10 m VV chips). Each chip runs through the exact onboard detector;
   blobs on an annotated ship become vessel rows, blobs elsewhere become real clutter rows
   (831 ship + 1,662 clutter rows, weighted x2). OpenSARShip has **no ship-type labels**, so the
   class is a size class from the rotated-box length (< 20 m fishing, 20–50 m ferry, >= 50 m
   cargo) — and all its annotated ships are cargo-sized (median 173 m).
2. **Simulator chips** at 10 m and 3 m for fishing boats, ferries and the X-band mode, which
   the real data does not cover.

If that file is missing, a simulator-only model is generated on first boot instead.
To retrain, download shards into `data/opensarship/` (see the module docstring) and re-run.
Scores are saved to `models/opensarship_eval.json`.

The real data also exposed a simulator flaw: simulated ships were ~2x too dim and lacked the
bright point reflectors of real steel hulls. `render_vessel` now adds superstructure
reflectors to ferries and cargo ships, calibrated against the real chips.

**License:** OpenSARShip annotations are CC-BY-NC-SA-4.0 and the imagery is modified
Copernicus Sentinel data, so the committed model is for **non-commercial use**, shared alike,
with attribution: *"Contains modified Copernicus Sentinel data, processed and annotated by
the OpenSARShip (OSSDD) dataset authors."*

## Measured on real Sentinel-1 data (held-out OpenSARShip test shard)
200 chips, 5,243 km², 1,068 annotated ships, never seen in training:

| Model | Ships found | False alarms | Precision | Size class |
|---|---|---|---|---|
| Simulator-only | 99.5 % | 1,514 (28.9 / 100 km²) | 49 % | 100 % |
| **OpenSARShip-trained (used)** | **96.4 %** | **533 (10.2 / 100 km²)** | **69 %** | 100 % |

Median length error vs the annotated boxes: ~23 m.

## Measured on the simulator
C-band, 12 port passes per sea state (original 4 ports x 3 seeds), OpenSARShip-trained model:

| Sea state | Cargo | Ferry | Fishing | False alarms / pass |
|---|---|---|---|---|
| Calm | 99 % | 100 % | 87 % | 0.1 |
| Moderate | 96 % | 97 % | 40 % | 0.25 |
| Monsoon rough | 95 % | 91 % | 1 % | 0.75 |

X-band vs C-band on the same vessels inside the spotlight (12 passes per sea state):

| Sea state | Fishing C-band -> X-band | Cargo C-band -> X-band | Ferry C-band -> X-band |
|---|---|---|---|
| Calm | 85/96 -> **96/96** | 69/69 -> 69/69 | 9/9 -> 9/9 |
| Moderate | 33/85 -> **64/85** | 65/70 -> **70/70** | 8/8 -> 8/8 |
| Monsoon rough | 0/49 -> 0/49 | 60/61 -> 61/61 | 11/12 -> 11/12 |

10–15 m boats are 1–2 pixel targets at 10 m C-band resolution; real radar is full of
one-pixel clutter that looks the same, so the real-data model is (correctly) cautious with
them. X-band resolves them in calm and moderate seas; in a monsoon sea neither band can
separate them from wave clutter.

**Speed** (laptop CPU, detection + classification): C-band ~90 ms per port
(75 ms coastal ROI + 15 ms for three offshore chips); X-band mode ~205 ms per port.

**Full 60-port sweep** (moderate sea, 18:00 IST, C-band): 8,544 MB raw SAR -> 199.8 KB packet
(99.99 % saved), 1,098 vessels detected / 742 in packet, onboard inference 5.2 s, ~26 s wall
time including scene simulation (X-band mode roughly 2.5x slower).

Most of the wall time is *generating* the synthetic scenes, which a real satellite would not
do. For a snappy dashboard button, run a single port (~0.5 s C-band, ~1.3 s X-band).

## Known limits
- 60 ports (Person B's list). 15 have hand-tuned traffic profiles; the rest get profiles derived
  from their berth count and description (fishing harbour, bulk port, island ferry hub...).
- ISLAND ports (Kavaratti, Minicoy, Port Blair, Campbell Bay) are modelled as a compact
  1.6 x 6 km island east of the harbour, quay on its western shore, sea all around.
- A full 60-port sweep detects ~1,100 vessels but only ~740 fit the 200 KB packet; the rest
  (lowest priority: offshore / coastal fishing) are dropped from `vessels`, never from `counts`.
- Each simulated pass writes `outputs/simulated_ais.json` (also `result["simulated_ais"]`):
  AIS reports for the simulated vessels in the `data/ais_ground_truth.json` format, with
  ~3–20 % of vessels (45 % of offshore cargo transits) deliberately missing = dark vessels.
- `ais_matched` is always `false` and `dark_vessels_detected` always `0` — the ground station
  owns AIS matching.
- Uploaded images without GeoTIFF georeferencing are assumed to be 10 m GSD centred on the
  selected port; their land mask is estimated from the image (Otsu + connectivity).
- `raw_scene_size_mb` reports the 142.4 MB full swath per port that the ROI is cropped from.
- **Windows:** if `import onnxruntime` fails with "filename or extension is too long", the venv
  path is too deep — create the venv somewhere short such as `C:\venvs\edgeport`.
