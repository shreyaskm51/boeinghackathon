# Person A — Onboard Edge AI (Track B)

**Branch:** `feature/onboard-ai`  **You own:** `onboard_ai/**`, `tests/test_onboard_*.py`
**Do not edit:** `ground_station/**`, `app.py`, `contracts/**` (contracts only via agreed PR)

## Paste this into Antigravity

> Read `EdgePort_PRD.pdf` (sections 2, 3, 4.2, 5, 6), `TEAM_SPLIT.md`, `AGENTS.md`, and
> `contracts/telemetry_schema.json`. Implement PRD Steps 3 and 4 only — the `onboard_ai/`
> package — fully, with no TODOs or placeholders:
>
> 1. `onboard_ai/sar_simulator.py`: load a user-uploaded Sentinel-1 `.tif` (rasterio optional,
>    wrapped in try/except ImportError) or `.png`; otherwise synthesize a 2048×2048 uint16 SAR
>    backscatter array with Gamma speckle for sea states CALM / MODERATE / MONSOON_ROUGH, a
>    binary land mask, and injected FISHING_BOAT (10–15 m), FERRY (20–45 m), CARGO_VESSEL
>    (50–180 m) targets for pass times 06:00 / 12:00 / 18:00 / 23:30 IST. Deterministic with a seed.
> 2. `onboard_ai/cfar_onnx_detector.py`: apply land mask, vectorized 2D Cell-Averaging CFAR
>    (`threshold = k × local_sea_mean`, use `scipy.ndimage.uniform_filter` with guard cells),
>    connected components via `scipy.ndimage.label`, then classify each blob with an ONNX
>    Runtime classifier. On first run, train a small scikit-learn model on synthetic features
>    and export `onboard_ai/models/vessel_classifier.onnx` via skl2onnx (int8-quantize if easy).
> 3. `onboard_ai/georeferencer.py`: pixel centroid → WGS84 lat/lon using the port's bbox from
>    `data/ports_config.json`; compute `estimated_length_m`, `estimated_tonnage_mt`,
>    `distance_from_shore_km`, `eta_minutes`; tag zone BERTH / ANCHORAGE_QUEUE /
>    COASTAL_APPROACH (0–50 km) / OFFSHORE_EEZ (50–370 km).
> 4. `onboard_ai/edge_pipeline.py`: implement `run_pass(...)` with the exact signature in
>    `TEAM_SPLIT.md`. Crop ROI → detect → georeference → build packet → `del raw_raster` →
>    write `outputs/latest_telemetry.json`. Log raw MB vs JSON KB and bandwidth saved %.
>    Generate 64×64 PNG chips only for CRITICAL_CONGESTION or DARK_VESSEL_OFFSHORE.
>    Add `if __name__ == "__main__":` so `python -m onboard_ai.edge_pipeline` writes a packet.
> 5. `tests/test_onboard_pipeline.py` (pytest): packet validates against the schema, is < 200 KB,
>    bandwidth_saved_pct > 99, all 16 ports present on a full sweep.
>
> Keep the packet under 200 KB (contract v2; measured minified with `separators=(",", ":")`).
> Every detected vessel normally fits; if a sweep ever exceeds the budget, keep all port
> summaries and drop the lowest-priority vessels first. Round lat/lon to 4 dp and other
> floats to 1–2 dp. Set `ais_matched: false` and `dark_vessels_detected: 0` — the ground
> station fills those in.

## Done when
- `python -m onboard_ai.edge_pipeline` writes a valid packet < 200 KB.
- `pytest tests/test_onboard_pipeline.py` passes.
- PR from `feature/onboard-ai` → `main` opened.
