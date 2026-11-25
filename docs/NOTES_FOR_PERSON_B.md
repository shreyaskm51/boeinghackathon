# Integration notes for Person B (from Person A)

Your `main` (React dashboard, ground station, 60 ports) is merged into `feature/onboard-ai`,
and the whole system was run end to end in a browser: single-port, X-band and full 60-port
passes all work through your server and dashboard. To finish integration I edited some of
your files — please review:

| File | Change |
|---|---|
| `ground_station/ais_connector.py` | `fetch_ais_feed` also loads `outputs/simulated_ais.json` (AIS for the simulated vessels, written by every simulated pass; ~3–20 % deliberately missing). Before this, ~95 % of simulated ships showed as dark; now ~8 % (91 of 1,071 on a 60-port sweep). Pass `include_simulated=False` to turn it off. |
| `ground_station/api_server.py` | `/api/simulate` returns HTTP 400 with a message for bad input (`ValueError`) or an unreadable upload (`OSError`) — it used to drop the connection. One pass at a time (lock). Optional `sensor_mode` (`C-BAND` / `X-BAND`); the response adds `xband_raw_preview_url`, `xband_overlay_preview_url` and `stats`. |
| `tests/test_ground_analytics.py` | Autouse fixture sends `latest_telemetry.json` / `simulated_ais.json` to a temp folder, so tests no longer depend on or overwrite `outputs/`. The `GET /` check only runs once `frontend/dist` is built. New tests: bad input -> 400, few dark vessels on a simulated pass, X-band previews. |
| `frontend/src/components/SimulatorPanel.jsx` | Defaults to a single port (the 60-port sweep takes ~30 s and says so), adds a C-band / X-band selector, shows the X-band spotlight images, accepts `.npy`, labels say < 200 KB. |
| `frontend/src/App.jsx` | Header says < 200 KB; simulation errors show the server's message. |

## Still to agree on
- **Packet budget:** 200 KB. A 60-port sweep detects ~1,100 vessels; ~740 fit, chosen by
  priority (every port keeps some; all vessels stay in each port's `counts`). Raising the
  limit to ~300 KB would list them all.
- **Licence:** the detector's classifier is trained on OpenSARShip (CC-BY-NC-SA) — please keep
  this credit visible in the app: *"Contains modified Copernicus Sentinel data, processed and
  annotated by the OpenSARShip (OSSDD) dataset authors."*
- **Workflow:** please push to a feature branch and open a PR instead of pushing to `main`.

My FastAPI server (`api/`) was removed — your `ground_station/api_server.py` is the one API.
The old `contract/16-ports-200kb` branch is superseded by your 60-port list.
