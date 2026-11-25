"""EdgePort HTTP API for the React dashboard.

React runs in the browser and cannot import Python, so this FastAPI app exposes the onboard
pipeline over HTTP::

    python -m api.server            # http://127.0.0.1:8000   (interactive docs at /docs)

Endpoints (all JSON unless noted):

    GET  /api/health                         service + model status
    GET  /api/ports                          the monitored ports (data/ports_config.json)
    GET  /api/telemetry/latest               latest packet (runs a default sweep the first time)
    POST /api/pass                           run an orbital pass (multipart form, optional upload)
    GET  /api/passes/{pass_id}/images/{name} PNG preview / alert chip of a recent pass

Ground-station analytics hook
-----------------------------
If ``ground_station/analytics_engine.py`` defines ``enrich_packet(packet: dict) -> dict``, every
packet returned by the API also carries ``"ground_station": enrich_packet(packet)`` (e.g. AIS
dark-vessel matching, utilization, SMS alerts). Without it that field is ``null`` and React
gets the raw onboard packet only.

Environment variables: ``EDGEPORT_API_HOST`` (default 127.0.0.1), ``EDGEPORT_API_PORT``
(8000), ``EDGEPORT_CORS_ORIGINS`` (comma-separated; default the usual Vite / CRA dev servers).
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image

from onboard_ai.cfar_onnx_detector import get_classifier
from onboard_ai.edge_pipeline import DEFAULT_OUTPUT, ROOT, run_pass
from onboard_ai.georeferencer import load_ports_config
from onboard_ai.sar_simulator import PASS_TIMES_IST

log = logging.getLogger("edgeport.api")

LATEST_PATH = ROOT / DEFAULT_OUTPUT
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_CACHED_PASSES = 20
PREVIEW_NAMES = ("raw_preview", "overlay_preview", "xband_raw_preview", "xband_overlay_preview")
DEFAULT_CORS = ("http://localhost:5173,http://127.0.0.1:5173,"   # Vite
                "http://localhost:3000,http://127.0.0.1:3000")   # Create React App / Next.js
ALL_PORTS = {"", "ALL", "all", "*"}


def _load_enrich_hook() -> Callable[[dict], Any] | None:
    try:
        from ground_station.analytics_engine import enrich_packet  # type: ignore[attr-defined]
    except Exception:  # module missing, or it does not provide the hook (yet)
        return None
    return enrich_packet if callable(enrich_packet) else None


def _png(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class PassCache:
    """Images of the most recent passes, kept in memory and served by URL."""

    def __init__(self, maxlen: int = MAX_CACHED_PASSES):
        self._items: OrderedDict[str, dict[str, bytes]] = OrderedDict()
        self._lock = threading.Lock()
        self._maxlen = maxlen

    def put(self, images: dict[str, bytes]) -> str:
        pass_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._items[pass_id] = images
            while len(self._items) > self._maxlen:
                self._items.popitem(last=False)
        return pass_id

    def get(self, pass_id: str, name: str) -> bytes | None:
        with self._lock:
            return self._items.get(pass_id, {}).get(name)


def create_app() -> FastAPI:
    app = FastAPI(
        title="EdgePort API",
        version="1.0",
        description="Onboard satellite AI for regional port & coastal vessel intelligence. "
                    "Packet format: contracts/telemetry_schema.json.",
    )
    origins = [o.strip() for o in os.environ.get("EDGEPORT_CORS_ORIGINS", DEFAULT_CORS).split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["*"])

    cache = PassCache()
    run_lock = threading.Lock()      # one orbital pass at a time (shared output file, CPU-bound)
    enrich = _load_enrich_hook()

    def respond(packet: dict, **extra: Any) -> dict:
        return {**extra, "telemetry": packet, "ground_station": enrich(packet) if enrich else None}

    def run_and_cache(**kwargs: Any) -> dict:
        with run_lock:
            result = run_pass(**kwargs)
        images = {n: _png(result[n]) for n in PREVIEW_NAMES if result.get(n) is not None}
        chip_names = []
        for i, (chip, path) in enumerate(zip(result["alert_chips"], result["stats"]["alert_chip_paths"])):
            name = f"chip_{i:02d}"
            images[name] = chip
            chip_names.append((name, Path(path).stem))
        pass_id = cache.put(images)
        base = f"/api/passes/{pass_id}/images"
        stats = {k: v for k, v in result["stats"].items() if k not in ("output_path", "alert_chip_paths")}
        return respond(
            result["telemetry"],
            pass_id=pass_id,
            stats=stats,
            images={n: f"{base}/{n}.png" for n in PREVIEW_NAMES if n in images},
            alert_chips=[{"url": f"{base}/{n}.png", "label": label} for n, label in chip_names],
        )

    @app.get("/api/health")
    def health() -> dict:
        clf = get_classifier()
        return {
            "status": "ok",
            "classifier": clf.path.name,
            "ports": len(load_ports_config()["ports"]),
            "ground_station_hook": enrich is not None,
            "latest_packet_available": LATEST_PATH.exists(),
        }

    @app.get("/api/ports")
    def ports() -> dict:
        return {"ports": load_ports_config()["ports"], "pass_times_ist": list(PASS_TIMES_IST),
                "sea_states": ["CALM", "MODERATE", "MONSOON_ROUGH"], "sensor_modes": ["C-BAND", "X-BAND"]}

    @app.get("/api/telemetry/latest")
    def latest() -> dict:
        if not LATEST_PATH.exists():
            log.info("No telemetry yet - running a default constellation sweep")
            return run_and_cache(port_id=None)
        packet = json.loads(LATEST_PATH.read_text(encoding="utf-8"))
        return respond(packet, pass_id=None, generated_at=time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(LATEST_PATH.stat().st_mtime)))

    @app.post("/api/pass")
    def orbital_pass(
        port_id: str = Form("", description="A port_id from /api/ports, or empty / ALL for a full sweep"),
        sea_state: str = Form("MODERATE", description="CALM | MODERATE | MONSOON_ROUGH (UI labels accepted)"),
        pass_time_ist: str = Form("18:00", description="06:00 | 12:00 | 18:00 | 23:30"),
        sensor_mode: str = Form("C-BAND", description="C-BAND | X-BAND"),
        seed: int | None = Form(None, description="Optional RNG seed for a reproducible scene"),
        image: UploadFile | None = File(None, description="Optional .tif / .png / .npy SAR scene"),
    ) -> dict:
        upload = None
        if image is not None and image.filename:
            upload = image.file.read(MAX_UPLOAD_BYTES + 1)
            if len(upload) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Upload larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")
            if not upload:
                upload = None
        try:
            return run_and_cache(port_id=None if port_id.strip() in ALL_PORTS else port_id.strip(),
                                 sea_state=sea_state, pass_time_ist=pass_time_ist,
                                 uploaded_image=upload, seed=seed, sensor_mode=sensor_mode)
        except ValueError as exc:  # unknown port / sea state / pass time / sensor mode
            raise HTTPException(400, str(exc)) from exc
        except (OSError, SyntaxError) as exc:  # unreadable upload
            raise HTTPException(400, f"Could not read the uploaded image: {exc}") from exc

    @app.get("/api/passes/{pass_id}/images/{name}.png",
             response_class=Response, responses={200: {"content": {"image/png": {}}}})
    def pass_image(pass_id: str, name: str) -> Response:
        data = cache.get(pass_id, name)
        if data is None:
            raise HTTPException(404, "Image not found (only the most recent passes are kept)")
        return Response(content=data, media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    return app


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger().handlers[0].addFilter(lambda r: r.name != "root")
    host = os.environ.get("EDGEPORT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("EDGEPORT_API_PORT", "8000"))
    get_classifier()  # load / generate the ONNX model before the first request
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
