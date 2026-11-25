# Rules for AI coding agents (Antigravity, Claude Code, etc.)

This repo is built by **two people in parallel**. Read `TEAM_SPLIT.md` first.

1. **Stay in your lane.** Only edit the files your person owns (see `TEAM_SPLIT.md`). If a
   change is needed in the other half, stop and tell the human instead of editing it.
2. **The contract is fixed.** `contracts/telemetry_schema.json` defines the JSON packet that
   `onboard_ai/edge_pipeline.py` writes and `ground_station/` + `app.py` read. Don't rename,
   remove or retype fields. Don't edit anything in `contracts/` unless the human explicitly asks.
3. **`run_pass(...)` signature is fixed** — see `TEAM_SPLIT.md`.
4. Python 3.10+, dependencies only from `requirements.txt` (`rasterio` optional, wrapped in
   `try/except ImportError`). Ask before adding a dependency — it's a shared file.
5. No TODOs, stubs or placeholder code in finished modules (PRD requirement).
6. Paths are relative to the repo root; use `pathlib.Path(__file__).resolve().parents[...]`
   so things work from any working directory.
7. Generated files (`outputs/*.json`, `onboard_ai/models/*.onnx`, chips) are git-ignored and must
   be regenerated automatically on first run.
8. Work on your feature branch, commit small and often, and open a PR to `main`.
