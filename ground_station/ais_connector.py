"""AIS ground-truth loader and optional live AISStream.io WebSocket connector."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AIS_PATH = REPO_ROOT / "data" / "ais_ground_truth.json"
# AIS reports for the vessels of the latest simulated orbital pass, written by
# onboard_ai.edge_pipeline.run_pass (same record format; dark vessels are simply absent).
SIMULATED_AIS_PATH = REPO_ROOT / "outputs" / "simulated_ais.json"
DEFAULT_PORTS_PATH = REPO_ROOT / "data" / "ports_config.json"
AISSTREAM_HOST = "stream.aisstream.io"
AISSTREAM_PORT = 443
AISSTREAM_PATH = "/v0/stream"


def _map_ais_ship_type(ship_type_code: Any) -> str:
    """Map numeric AIS ship type code or string to EdgePort vessel_type enum."""
    if isinstance(ship_type_code, str):
        upper = ship_type_code.strip().upper()
        if upper in {"FISHING_BOAT", "FERRY", "CARGO_VESSEL"}:
            return upper
        if "FISH" in upper or "TRAWL" in upper:
            return "FISHING_BOAT"
        if "PASSENGER" in upper or "FERRY" in upper:
            return "FERRY"
        return "CARGO_VESSEL"

    try:
        code = int(ship_type_code)
    except (TypeError, ValueError):
        return "CARGO_VESSEL"

    if code == 30:
        return "FISHING_BOAT"
    if 60 <= code <= 69:
        return "FERRY"
    return "CARGO_VESSEL"


def normalize_ais_record(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize local or live AIS record into a standard dictionary."""
    return {
        "mmsi": str(raw.get("mmsi", "")).strip(),
        "name": str(raw.get("name", "UNKNOWN VESSEL")).strip() or "UNKNOWN VESSEL",
        "type": _map_ais_ship_type(raw.get("type", raw.get("vessel_type", "CARGO_VESSEL"))),
        "port_id": raw.get("port_id"),
        "lat": float(raw["lat"]),
        "lon": float(raw["lon"]),
        "sog_knots": float(raw.get("sog_knots", 0.0)),
        "timestamp": str(raw.get("timestamp", "")),
    }


def load_local_ais(ais_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Load simulated local AIS ground-truth records from JSON."""
    path = Path(ais_path) if ais_path else DEFAULT_AIS_PATH
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    records = payload.get("records", payload if isinstance(payload, list) else [])
    return [normalize_ais_record(rec) for rec in records if "lat" in rec and "lon" in rec]


def _load_port_bounding_boxes(ports_path: str | Path | None = None) -> list[list[list[float]]]:
    """Build AISStream BoundingBoxes list from ports_config.json."""
    path = Path(ports_path) if ports_path else DEFAULT_PORTS_PATH
    with path.open("r", encoding="utf-8") as fh:
        config = json.load(fh)
    boxes: list[list[list[float]]] = []
    for port in config.get("ports", []):
        bbox = port.get("eez_corridor_bbox") or port.get("anchorage_bbox")
        if bbox and len(bbox) == 4:
            min_lon, min_lat, max_lon, max_lat = bbox
            boxes.append([[float(min_lat), float(min_lon)], [float(max_lat), float(max_lon)]])
    return boxes or [[[6.0, 68.0], [23.0, 90.0]]]


def _recv_exact(sock: ssl.SSLSocket, num_bytes: int) -> bytes:
    """Receive an exact byte count from a TLS socket."""
    chunks: list[bytes] = []
    remaining = num_bytes
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise EOFError("WebSocket connection closed by peer.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_ws_text(sock: ssl.SSLSocket, message: str) -> None:
    """Send a masked RFC 6455 client-to-server text frame."""
    payload = message.encode("utf-8")
    mask_key = os.urandom(4)
    length = len(payload)
    header = bytearray([0x81])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask_key)
    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _read_ws_frame(sock: ssl.SSLSocket) -> tuple[int, bytes]:
    """Read a single RFC 6455 frame from the server."""
    b1, b2 = _recv_exact(sock, 2)
    opcode = b1 & 0x0F
    is_masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    mask_key = _recv_exact(sock, 4) if is_masked else b""
    payload = _recv_exact(sock, length) if length else b""
    if is_masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _parse_aisstream_message(raw_msg: str) -> dict[str, Any] | None:
    """Parse an AISStream.io JSON message into a normalized AIS record."""
    data = json.loads(raw_msg)
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    meta = data.get("MetaData") or {}
    pos = (data.get("Message") or {}).get("PositionReport") or {}
    lat = pos.get("Latitude", meta.get("latitude"))
    lon = pos.get("Longitude", meta.get("longitude"))
    mmsi = meta.get("MMSI") or pos.get("UserID")
    if lat is None or lon is None or not mmsi:
        return None
    return normalize_ais_record(
        {
            "mmsi": mmsi,
            "name": meta.get("ShipName", f"MMSI-{mmsi}"),
            "type": meta.get("ShipType", "CARGO_VESSEL"),
            "lat": lat,
            "lon": lon,
            "sog_knots": pos.get("Sog", 0.0),
            "timestamp": meta.get("time_utc", ""),
        }
    )


def fetch_live_aisstream(
    api_key: str,
    bounding_boxes: list[list[list[float]]],
    timeout_s: float = 4.0,
    max_records: int = 50,
) -> list[dict[str, Any]]:
    """Connect to AISStream.io over TLS WebSocket using Python stdlib only."""
    ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1(
            (ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
    ).decode("ascii")

    request = (
        f"GET {AISSTREAM_PATH} HTTP/1.1\r\n"
        f"Host: {AISSTREAM_HOST}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")

    ssl_ctx = ssl.create_default_context()
    records_by_mmsi: dict[str, dict[str, Any]] = {}
    deadline = time.monotonic() + timeout_s

    with socket.create_connection((AISSTREAM_HOST, AISSTREAM_PORT), timeout=timeout_s) as raw_sock:
        with ssl_ctx.wrap_socket(raw_sock, server_hostname=AISSTREAM_HOST) as tls_sock:
            tls_sock.settimeout(timeout_s)
            tls_sock.sendall(request)

            response_buf = b""
            while b"\r\n\r\n" not in response_buf:
                chunk = tls_sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Incomplete WebSocket handshake response.")
                response_buf += chunk

            header_text = response_buf.split(b"\r\n\r\n", 1)[0].decode("latin-1")
            if "101" not in header_text.splitlines()[0] or expected_accept not in header_text:
                raise ConnectionError("WebSocket upgrade rejected by AISStream.io.")

            subscribe_msg = json.dumps(
                {
                    "APIKey": api_key,
                    "BoundingBoxes": bounding_boxes,
                    "FilterMessageTypes": ["PositionReport"],
                }
            )
            _send_ws_text(tls_sock, subscribe_msg)

            while time.monotonic() < deadline and len(records_by_mmsi) < max_records:
                remaining = max(0.2, deadline - time.monotonic())
                tls_sock.settimeout(remaining)
                try:
                    opcode, payload = _read_ws_frame(tls_sock)
                except (socket.timeout, TimeoutError, EOFError):
                    break
                if opcode == 0x8:  # Close frame
                    break
                if opcode == 0x9:  # Ping -> Pong
                    continue
                if opcode == 0x1:
                    parsed = _parse_aisstream_message(payload.decode("utf-8", errors="ignore"))
                    if parsed:
                        records_by_mmsi[parsed["mmsi"]] = parsed

    return list(records_by_mmsi.values())


def fetch_ais_feed(
    use_live: bool = True,
    timeout_s: float = 4.0,
    ais_path: str | Path | None = None,
    ports_path: str | Path | None = None,
    include_simulated: bool = True,
) -> dict[str, Any]:
    """Load local AIS records and optionally enrich with live AISStream.io data.

    With ``include_simulated`` (default), AIS reports for the latest simulated orbital pass
    (``outputs/simulated_ais.json``) are added, so simulated vessels that broadcast AIS are
    matched and only the deliberately silent ones are flagged as dark.
    """
    local_records = load_local_ais(ais_path=ais_path)
    if include_simulated and SIMULATED_AIS_PATH.exists():
        try:
            known = {rec["mmsi"] for rec in local_records}
            local_records += [rec for rec in load_local_ais(ais_path=SIMULATED_AIS_PATH)
                              if rec["mmsi"] not in known]
        except (OSError, ValueError, KeyError):
            pass  # unreadable simulator output -> fall back to the ground-truth file only
    api_key = os.environ.get("AISSTREAM_API_KEY", "").strip()

    if not use_live or not api_key:
        reason = (
            "AISSTREAM_API_KEY not set; using local simulated AIS feed."
            if not api_key
            else "Live AIS disabled; using local simulated AIS feed."
        )
        return {
            "records": local_records,
            "source": "local_simulated",
            "live_enabled": False,
            "live_count": 0,
            "status_message": reason,
        }

    try:
        boxes = _load_port_bounding_boxes(ports_path=ports_path)
        live_records = fetch_live_aisstream(api_key=api_key, bounding_boxes=boxes, timeout_s=timeout_s)
        merged: dict[str, dict[str, Any]] = {rec["mmsi"]: rec for rec in local_records}
        for rec in live_records:
            merged[rec["mmsi"]] = rec
        return {
            "records": list(merged.values()),
            "source": "aisstream_live+local" if live_records else "local_simulated",
            "live_enabled": True,
            "live_count": len(live_records),
            "status_message": (
                f"Fetched {len(live_records)} live AIS reports from AISStream.io."
                if live_records
                else "Connected to AISStream.io (0 reports in window); using local AIS fallback."
            ),
        }
    except Exception as exc:  # noqa: BLE001 — graceful offline fallback required by PRD
        return {
            "records": local_records,
            "source": "local_simulated",
            "live_enabled": False,
            "live_count": 0,
            "status_message": f"AISStream.io skipped ({exc}); using local simulated AIS feed.",
        }
