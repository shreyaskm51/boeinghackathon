# `api/` — HTTP API for the React dashboard

React runs in the browser and can't import Python, so this small FastAPI server exposes the
onboard pipeline over HTTP.

```bash
pip install -r requirements.txt
python -m api.server                 # http://127.0.0.1:8000  — try it at http://127.0.0.1:8000/docs
```

In the React project, copy [`edgeport.ts`](edgeport.ts) to `src/api/edgeport.ts` (types + fetch
helpers) and point it at the server with `VITE_EDGEPORT_API=http://127.0.0.1:8000` in `.env`
(that is also the default).

```tsx
import { getPorts, getLatest, runPass, imageUrl } from "./api/edgeport";

const { ports } = await getPorts();                         // 16 ports with berth/anchorage boxes
const latest = await getLatest();                           // latest packet (map on first load)
const pass = await runPass({ portId: "IN-KAK", seaState: "MONSOON_ROUGH",
                             passTimeIst: "06:00", sensorMode: "X-BAND", image: file });
pass.telemetry.vessels;                                     // every detected vessel
<img src={imageUrl(pass.images!.overlay_preview!)} />       // detections overlay
pass.alert_chips!.map(c => <img key={c.url} src={imageUrl(c.url)} title={c.label} />);
```

## Endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | `{status, classifier, ports, ground_station_hook, latest_packet_available}` |
| GET | `/api/ports` | `{ports: [...], pass_times_ist, sea_states, sensor_modes}` — use this, never hard-code ports |
| GET | `/api/telemetry/latest` | `{telemetry, ground_station, generated_at}`; runs a default sweep the first time |
| POST | `/api/pass` | multipart form: `port_id` (empty = all 16), `sea_state`, `pass_time_ist`, `sensor_mode`, `seed`, `image` (optional .tif/.png/.npy, ≤ 50 MB). Returns `{pass_id, telemetry, ground_station, stats, images, alert_chips}` |
| GET | `/api/passes/{pass_id}/images/{name}.png` | PNG preview or alert chip (the last 20 passes are kept) |

`telemetry` is exactly `contracts/telemetry_schema.json`. Errors come back as
`{"detail": "..."}` with HTTP 400 (bad parameter or unreadable upload) or 413 (upload too large).

**Timing:** a single-port pass takes ~0.5 s (C-band) / ~1.5 s (X-band); a full 16-port sweep
~7 s / ~20 s. Show a spinner, and prefer single-port passes for the simulator button. Passes
run one at a time.

## Ground-station analytics hook
If `ground_station/analytics_engine.py` defines

```python
def enrich_packet(packet: dict) -> dict: ...   # AIS dark vessels, utilization, ETAs, SMS alerts
```

the API calls it on every packet and returns the result as `ground_station` (otherwise `null`).
Doing the analytics in TypeScript instead is also fine — then ignore that field.

## Settings
| Env var | Default |
|---|---|
| `EDGEPORT_API_HOST` | `127.0.0.1` (use `0.0.0.0` only to reach it from another device) |
| `EDGEPORT_API_PORT` | `8000` |
| `EDGEPORT_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000` |
