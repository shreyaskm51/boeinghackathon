"""Tests for the React-facing HTTP API (api/server.py)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import server
from onboard_ai.sar_simulator import simulate_pass, to_display_uint8

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts" / "telemetry_schema.json").read_text(encoding="utf-8"))
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


def _validate(packet):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(packet, SCHEMA)


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["classifier"].endswith(".onnx") and body["ports"] == 16


def test_ports_lists_all_ports_and_options(client):
    body = client.get("/api/ports").json()
    assert len(body["ports"]) == 16
    assert {"port_id", "lat", "lon", "berth_bbox", "anchorage_bbox"} <= set(body["ports"][0])
    assert body["sensor_modes"] == ["C-BAND", "X-BAND"] and "18:00" in body["pass_times_ist"]


def test_run_pass_returns_packet_and_fetchable_images(client):
    r = client.post("/api/pass", data={"port_id": "IN-KAK", "sea_state": "Clear Calm",
                                       "pass_time_ist": "12:00", "seed": "5"})
    assert r.status_code == 200, r.text
    body = r.json()
    _validate(body["telemetry"])
    assert [p["port_id"] for p in body["telemetry"]["ports_summary"]] == ["IN-KAK"]
    assert body["ground_station"] is None or isinstance(body["ground_station"], (dict, list))
    assert "output_path" not in body["stats"]              # no server paths leak to the browser
    assert set(body["images"]) == {"raw_preview", "overlay_preview"}
    for url in [*body["images"].values(), *(c["url"] for c in body["alert_chips"])]:
        img = client.get(url)
        assert img.status_code == 200 and img.headers["content-type"] == "image/png"
        assert img.content[:8] == PNG_MAGIC


def test_xband_pass_adds_spotlight_previews(client):
    body = client.post("/api/pass", data={"port_id": "IN-KRW", "sensor_mode": "X-BAND", "seed": "2"}).json()
    assert body["telemetry"]["telemetry_header"]["sensor_mode"] == "X-BAND_SAR_HIGH_RES"
    assert {"xband_raw_preview", "xband_overlay_preview"} <= set(body["images"])


def test_upload_png(client):
    tile = simulate_pass("IN-BEY", "CALM", "12:00", seed=9).tiles[0]
    buf = io.BytesIO()
    from PIL import Image

    Image.fromarray(to_display_uint8(tile.image[512:1536, 1000:2048])).save(buf, format="PNG")
    r = client.post("/api/pass", data={"port_id": "IN-BEY", "sea_state": "CALM"},
                    files={"image": ("scene.png", buf.getvalue(), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["source"] == "UPLOAD"


@pytest.mark.parametrize("form", [{"port_id": "IN-XXX"}, {"pass_time_ist": "09:15"},
                                  {"sensor_mode": "L-BAND"}, {"sea_state": "TSUNAMI"}])
def test_bad_parameters_give_400(client, form):
    r = client.post("/api/pass", data=form)
    assert r.status_code == 400 and r.json()["detail"]


def test_unreadable_upload_gives_400(client):
    r = client.post("/api/pass", data={"port_id": "IN-BEY"},
                    files={"image": ("scene.png", b"definitely not an image", "image/png")})
    assert r.status_code == 400


def test_latest_returns_most_recent_packet(client):
    client.post("/api/pass", data={"port_id": "IN-GOP", "seed": "7"})
    body = client.get("/api/telemetry/latest").json()
    _validate(body["telemetry"])
    assert body["telemetry"]["ports_summary"][0]["port_id"] == "IN-GOP"


def test_unknown_image_is_404(client):
    assert client.get("/api/passes/nope/images/raw_preview.png").status_code == 404


def test_cors_allows_vite_dev_server(client):
    r = client.options("/api/ports", headers={"Origin": "http://localhost:5173",
                                              "Access-Control-Request-Method": "GET"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_ground_station_hook_is_applied(monkeypatch):
    monkeypatch.setattr(server, "_load_enrich_hook", lambda: (lambda p: {"vessels_seen": len(p["vessels"])}))
    with TestClient(server.create_app()) as c:
        body = c.post("/api/pass", data={"port_id": "IN-MAL", "seed": "1"}).json()
    assert body["ground_station"] == {"vessels_seen": len(body["telemetry"]["vessels"])}
