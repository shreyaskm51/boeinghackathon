"""Zero-dependency HTTP API server + static React host for EdgePort (Person B - Track A)."""

from __future__ import annotations

import base64
import copy
import io
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np
from PIL import Image, ImageDraw

from ground_station.ais_connector import fetch_ais_feed
from ground_station.alert_formatter import format_all_alerts
from ground_station.analytics_engine import (
    DEFAULT_LATEST_TELEMETRY_PATH,
    DEFAULT_SAMPLE_TELEMETRY_PATH,
    analyze_telemetry,
    build_multi_pass_nowcast,
    build_swath_telemetry_for_port,
    load_ports_config,
    load_telemetry_packet,
    summarize_national_port_network,
)

try:
    from onboard_ai.edge_pipeline import run_pass as onboard_run_pass

    ONBOARD_PIPELINE_AVAILABLE = True
except ImportError:
    onboard_run_pass = None
    ONBOARD_PIPELINE_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST_DIR = REPO_ROOT / "frontend" / "dist"

SEA_STATE_NOISE_SCALE = {
    "CALM": 18,
    "MODERATE": 34,
    "MONSOON_ROUGH": 56,
}

IST_TO_UTC = {
    "06:00": "2026-11-25T00:30:00Z",
    "12:00": "2026-11-25T06:30:00Z",
    "18:00": "2026-11-25T12:30:00Z",
    "23:30": "2026-11-25T18:00:00Z",
}

VESSEL_HEX_COLORS = {
    "FISHING_BOAT": "#10B981",
    "FERRY": "#06B6D4",
    "CARGO_VESSEL": "#F59E0B",
    "DARK_VESSEL": "#EF4444",
}


def _array_to_png_data_url(arr: np.ndarray) -> str:
    """Encode a 2D grayscale or 3D RGB NumPy array as a base64 PNG data URL."""
    clipped = np.clip(arr, 0, 255).astype(np.uint8)
    img = Image.fromarray(clipped)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _bytes_to_png_data_url(raw_bytes: bytes) -> str:
    """Encode raw PNG bytes as a base64 PNG data URL."""
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def generate_ground_preview_images(
    packet: dict[str, Any],
    sea_state: str,
    uploaded_bytes: bytes | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate deterministic SAR & detected-overlay previews when onboard_ai isn't merged yet."""
    size = 384
    raw_arr: np.ndarray | None = None
    if uploaded_bytes:
        try:
            img = Image.open(io.BytesIO(uploaded_bytes)).convert("L").resize((size, size))
            raw_arr = np.asarray(img, dtype=np.uint8).copy()
        except Exception:
            raw_arr = None

    if raw_arr is None:
        rng = np.random.default_rng(abs(hash((sea_state, len(packet.get("vessels", []))))) % (2**32))
        noise_scale = SEA_STATE_NOISE_SCALE.get(sea_state, 34)
        sea = rng.gamma(shape=3.0, scale=noise_scale / 3.0, size=(size, size))
        raw_arr = np.clip(sea, 0, 255).astype(np.uint8)
        x_idx = np.arange(size)[None, :]
        coast_curve = (58 + 14 * np.sin(np.linspace(0, 3.2 * np.pi, size)))[:, None]
        land_noise = np.clip(135 + rng.normal(0, 12, size=(size, size)), 90, 220).astype(np.uint8)
        raw_arr[x_idx < coast_curve] = land_noise[x_idx < coast_curve]

    vessels = packet.get("vessels", [])
    if uploaded_bytes is None:
        for idx, vessel in enumerate(vessels):
            vx = 110 + ((idx * 63) % 210)
            vy = 60 + ((idx * 79) % 260)
            half = 6 if vessel["vessel_type"] == "FISHING_BOAT" else (9 if vessel["vessel_type"] == "FERRY" else 13)
            raw_arr[max(0, vy - half // 2) : vy + half // 2, max(0, vx - half) : vx + half] = 248

    overlay_img = Image.fromarray(raw_arr).convert("RGB")
    draw = ImageDraw.Draw(overlay_img)
    for idx, vessel in enumerate(vessels):
        vx = 110 + ((idx * 63) % 210)
        vy = 60 + ((idx * 79) % 260)
        half = 8 if vessel["vessel_type"] == "FISHING_BOAT" else (11 if vessel["vessel_type"] == "FERRY" else 15)
        color = (
            VESSEL_HEX_COLORS["DARK_VESSEL"]
            if not vessel.get("ais_matched", False)
            else VESSEL_HEX_COLORS.get(vessel.get("vessel_type", "CARGO_VESSEL"), "#F59E0B")
        )
        draw.rectangle([vx - half, vy - half, vx + half, vy + half], outline=color, width=2)
        draw.text((vx - half, max(2, vy - half - 12)), vessel["vessel_id"], fill=color)

    return raw_arr, np.asarray(overlay_img, dtype=np.uint8)


class EdgePortState:
    """In-memory ground station state holding active telemetry packet and source label."""

    def __init__(self) -> None:
        packet, source_path = load_telemetry_packet()
        self.active_packet: dict[str, Any] = packet
        self.packet_source_label: str = source_path.name

    def reload_from_disk(self) -> None:
        packet, source_path = load_telemetry_packet()
        self.active_packet = packet
        self.packet_source_label = source_path.name

    def build_dashboard_payload(self, use_live_ais: bool = False) -> dict[str, Any]:
        ports_cfg = load_ports_config()
        ais_feed = fetch_ais_feed(use_live=use_live_ais)
        analyzed = analyze_telemetry(
            packet=self.active_packet,
            ais_records=ais_feed["records"],
            ports_config=ports_cfg,
        )
        alerts_by_port = format_all_alerts(analyzed)
        nowcast = build_multi_pass_nowcast(analyzed)
        national_network = summarize_national_port_network(analyzed)

        return {
            "packet": analyzed["packet"],
            "packet_source_label": self.packet_source_label,
            "onboard_pipeline_available": ONBOARD_PIPELINE_AVAILABLE,
            "kpis": analyzed["kpis"],
            "ais_feed": {
                "source": ais_feed["source"],
                "live_enabled": ais_feed["live_enabled"],
                "live_count": ais_feed["live_count"],
                "status_message": ais_feed["status_message"],
            },
            "ports_config": ports_cfg,
            "enriched_vessels": analyzed["enriched_vessels"],
            "eta_queue": analyzed["eta_queue"],
            "rerouting_suggestions": analyzed["rerouting_suggestions"],
            "alerts_by_port": alerts_by_port,
            "nowcast": nowcast,
            "national_network": national_network,
        }

    def run_simulation(
        self,
        port_id: str | None = None,
        sea_state: str = "MODERATE",
        pass_time_ist: str = "18:00",
        uploaded_image_b64: str | None = None,
        use_live_ais: bool = False,
    ) -> dict[str, Any]:
        uploaded_bytes: bytes | None = None
        if uploaded_image_b64:
            raw_b64 = uploaded_image_b64.split(",", 1)[-1]
            uploaded_bytes = base64.b64decode(raw_b64)

        fallback_mode = False
        if ONBOARD_PIPELINE_AVAILABLE and onboard_run_pass is not None:
            sim_result = onboard_run_pass(
                port_id=port_id,
                sea_state=sea_state,
                pass_time_ist=pass_time_ist,
                uploaded_image=uploaded_bytes,
                output_path=str(DEFAULT_LATEST_TELEMETRY_PATH),
            )
            self.active_packet = sim_result["telemetry"]
            self.packet_source_label = "onboard_ai.edge_pipeline.run_pass"
        else:
            fallback_mode = True
            pass_utc = IST_TO_UTC.get(pass_time_ist, "2026-11-25T18:00:00Z")
            if port_id is not None:
                sim_packet = build_swath_telemetry_for_port(
                    port_id=port_id,
                    sea_state=sea_state,
                    pass_timestamp_utc=pass_utc,
                )
            else:
                packet, _ = load_telemetry_packet(DEFAULT_SAMPLE_TELEMETRY_PATH, prefer_latest=False)
                sim_packet = copy.deepcopy(packet)
                sim_packet["telemetry_header"]["sea_state"] = sea_state
                sim_packet["telemetry_header"]["pass_timestamp_utc"] = pass_utc

            self.active_packet = sim_packet
            self.packet_source_label = "sample_telemetry.json (simulator fallback)"
            # Pre-run AIS matching before drawing overlay boxes so dark vessels draw red
            ais_feed = fetch_ais_feed(use_live=use_live_ais)
            pre_analyzed = analyze_telemetry(packet=sim_packet, ais_records=ais_feed["records"])
            raw_preview, overlay_preview = generate_ground_preview_images(
                pre_analyzed["packet"], sea_state, uploaded_bytes
            )
            sim_result = {
                "telemetry": sim_packet,
                "raw_preview": raw_preview,
                "overlay_preview": overlay_preview,
                "alert_chips": [],
            }

        payload = self.build_dashboard_payload(use_live_ais=use_live_ais)
        payload["simulation"] = {
            "fallback_mode": fallback_mode,
            "raw_preview_url": _array_to_png_data_url(sim_result["raw_preview"]),
            "overlay_preview_url": _array_to_png_data_url(sim_result["overlay_preview"]),
            "alert_chip_urls": [
                _bytes_to_png_data_url(chip) for chip in (sim_result.get("alert_chips") or [])
            ],
        }
        return payload


def make_handler(state: EdgePortState) -> type[BaseHTTPRequestHandler]:
    """Create a request handler bound to a shared EdgePortState instance."""

    class EdgePortRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # Keep server output quiet during tests and CLI usage

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send_json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, req_path: str) -> None:
            if not FRONTEND_DIST_DIR.exists():
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "React build not found. Run `npm run build` in frontend/ or use Vite dev server.",
                    },
                )
                return

            rel = req_path.lstrip("/") or "index.html"
            candidate = (FRONTEND_DIST_DIR / rel).resolve()
            if not str(candidate).startswith(str(FRONTEND_DIST_DIR.resolve())) or not candidate.is_file():
                candidate = FRONTEND_DIST_DIR / "index.html"

            mime, _ = mimetypes.guess_type(str(candidate))
            content = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "onboard_pipeline_available": ONBOARD_PIPELINE_AVAILABLE,
                    },
                )
                return

            if parsed.path == "/api/dashboard":
                params = parse_qs(parsed.query)
                use_live = params.get("use_live_ais", ["0"])[0] in ("1", "true", "True")
                payload = state.build_dashboard_payload(use_live_ais=use_live)
                self._send_json(HTTPStatus.OK, payload)
                return

            self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except json.JSONDecodeError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body."})
                return

            if parsed.path == "/api/reload":
                state.reload_from_disk()
                use_live = bool(body.get("use_live_ais", False))
                self._send_json(HTTPStatus.OK, state.build_dashboard_payload(use_live_ais=use_live))
                return

            if parsed.path == "/api/simulate":
                port_id = body.get("port_id")
                if port_id in ("", "ALL"):
                    port_id = None
                try:
                    payload = state.run_simulation(
                        port_id=port_id,
                        sea_state=str(body.get("sea_state", "MODERATE")),
                        pass_time_ist=str(body.get("pass_time_ist", "18:00")),
                        uploaded_image_b64=body.get("uploaded_image_b64"),
                        use_live_ais=bool(body.get("use_live_ais", False)),
                    )
                except KeyError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Unknown API route: {parsed.path}"})

    return EdgePortRequestHandler


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Start the EdgePort Ground Station HTTP API & React static server."""
    state = EdgePortState()
    server = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"EdgePort Ground Station API running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down EdgePort API server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
