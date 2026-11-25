"""Tests for the real Sentinel-1 path (offline: no network access needed)."""
from __future__ import annotations

import io
import json
import socket
from pathlib import Path

import numpy as np
import pytest

from onboard_ai import real_sentinel1 as rs
from onboard_ai.cfar_onnx_detector import BRIGHT_TARGET_SCR, classify_detections, detect_targets
from onboard_ai.georeferencer import get_port, load_ports_config

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts" / "telemetry_schema.json").read_text(encoding="utf-8"))


@pytest.fixture
def real_cache(tmp_path, monkeypatch):
    """Point the real-data cache at a temp folder."""
    monkeypatch.setattr(rs, "REAL_DIR", tmp_path / "real")
    monkeypatch.setattr(rs, "REAL_PACKET_PATH", tmp_path / "real_telemetry.json")
    (tmp_path / "real").mkdir()
    return tmp_path


def _fake_result(port_id: str, n: int = 3, when: str = "2026-11-20T00:55:31Z") -> dict:
    port = get_port(port_id)
    vessels = [{"vessel_id": f"S1-X-{i:03d}", "port_id": port_id, "vessel_type": "CARGO_VESSEL",
                "latitude": port["lat"] + 0.01 * i, "longitude": port["lon"] - 0.05, "distance_from_shore_km": 5.0,
                "zone": "COASTAL_APPROACH", "estimated_length_m": 120.0, "confidence": 0.9, "ais_matched": False,
                "eta_minutes": 30} for i in range(n)]
    summary = {"port_id": port_id, "port_name": port["port_name"], "state": port["state"],
               "max_berths": port["max_berths"],
               "counts": {"berth_occupied": 0, "anchorage_waiting": 0, "coastal_fishing_boats": 0,
                          "approaching_ferries": 0, "offshore_eez_vessels": 0},
               "utilization_index": 0.0, "congestion_status": "NORMAL", "estimated_cargo_tonnage_mt": 0,
               "estimated_trade_value_inr_cr": 0.0, "dark_vessels_detected": 0,
               "observed_utc": when, "data_source": "SENTINEL-1"}
    return {"port_id": port_id, "status": "OK", "scene": {"id": "S1X", "datetime": when, "platform": "sentinel-1d",
                                                          "full_scene_mb": 1800.0},
            "fetched_utc": "2026-11-26T10:00:00Z", "sea_state": "MODERATE",
            "window": {"bounds": [0, 0, 1, 1], "shape": [1500, 1800], "roi_mb": 5.4},
            "stats": {"cfar_candidates": 10, "rejected_as_clutter": 7, "vessels_detected": n,
                      "onboard_inference_time_ms": 70.0, "fetch_s": 14.0},
            "summary": summary, "vessels": vessels, "previews": {}}


def _cache(tmp, result):
    (rs.REAL_DIR / f"{result['port_id']}.json").write_text(json.dumps(result), encoding="utf-8")


@pytest.mark.parametrize("port_id", ["IN-KRW", "IN-KAK", "IN-PBL"])
def test_window_covers_harbour_and_approach(port_id):
    port = get_port(port_id)
    min_lon, min_lat, max_lon, max_lat = rs.window_bounds(port)
    assert min_lon < port["lon"] < max_lon and min_lat < port["lat"] < max_lat
    for key in ("berth_bbox", "anchorage_bbox"):
        b = port[key]
        assert min_lon <= b[0] and b[2] <= max_lon and min_lat <= b[1] and b[3] <= max_lat
    width_km = (max_lon - min_lon) * 111.32 * np.cos(np.radians(port["lat"]))
    assert 12 < width_km < 40          # tens of km, never a whole 250 km scene


def test_real_packet_is_schema_valid_and_marks_ais_unavailable(real_cache):
    jsonschema = pytest.importorskip("jsonschema")
    _cache(real_cache, _fake_result("IN-KRW", 3, "2026-07-27T00:55:31Z"))
    _cache(real_cache, _fake_result("IN-KSP", 5, "2026-11-21T00:40:00Z"))
    (rs.REAL_DIR / "IN-KVT.json").write_text(json.dumps({"port_id": "IN-KVT", "status": "NO_SCENE"}), encoding="utf-8")
    packet = rs.build_real_packet()
    jsonschema.validate(packet, SCHEMA)
    h = packet["telemetry_header"]
    assert h["data_source"] == "REAL_SENTINEL1" and h["ais_available"] is False
    assert h["ports_observed"] == 2
    assert h["pass_timestamp_utc"].startswith("2026-11-21") and h["oldest_scene_utc"].startswith("2026-07-27")
    assert len(packet["ports_summary"]) == len(load_ports_config()["ports"])
    assert len(packet["vessels"]) == 8
    kvt = next(p for p in packet["ports_summary"] if p["port_id"] == "IN-KVT")
    assert kvt["data_source"] == "NO_SCENE"
    assert rs.REAL_PACKET_PATH.exists()


def test_build_real_packet_without_results_raises(real_cache):
    with pytest.raises(RuntimeError):
        rs.build_real_packet()


def test_ground_station_does_not_flag_dark_without_ais(real_cache):
    analytics = pytest.importorskip("ground_station.analytics_engine")
    _cache(real_cache, _fake_result("IN-KRW", 4))
    analyzed = analytics.analyze_telemetry(packet=rs.build_real_packet(), ais_records=[])
    assert analyzed["kpis"]["ais_checked"] is False
    assert analyzed["kpis"]["total_dark_vessels"] == 0
    assert {v["alert_flag"] for v in analyzed["enriched_vessels"]} == {"AIS_NOT_CHECKED"}


def test_sea_state_from_backscatter():
    land = np.zeros((100, 100), dtype=bool)
    assert rs.sea_state_from_backscatter(np.full((100, 100), 0.01, np.float32), land) == "CALM"
    assert rs.sea_state_from_backscatter(np.full((100, 100), 0.04, np.float32), land) == "MODERATE"
    assert rs.sea_state_from_backscatter(np.full((100, 100), 0.2, np.float32), land) == "MONSOON_ROUGH"


def test_very_bright_sidelobe_smeared_target_is_a_vessel():
    rng = np.random.default_rng(0)
    img = rng.gamma(4.0, 100 / 4.0, size=(400, 400)).astype(np.float32)
    img[195:206, 195:206] = 40000.0          # saturated, near-square blob: a "+" smeared ship
    img[200, 175:226] = 3000.0               # horizontal sidelobe (~250 m each side)
    img[175:226, 200] = 3000.0               # vertical sidelobe
    dets = detect_targets(np.clip(img, 0, 65535).astype(np.uint16), None, 10.0)
    labels, _ = classify_detections(dets)
    bright = [(d, lab) for d, lab in zip(dets, labels) if d.peak_scr >= BRIGHT_TARGET_SCR]
    assert bright and all(lab != "CLUTTER" for _, lab in bright)


def test_process_port_reports_catalogue_failure_without_network(real_cache, monkeypatch):
    def offline(*_a, **_k):
        raise OSError("network disabled in tests")

    monkeypatch.setattr(rs, "_http_json", offline)
    res = rs.process_port("IN-KRW")
    assert res["status"] == "ERROR" and "catalogue" in res["error"]
    assert json.loads((rs.REAL_DIR / "IN-KRW.json").read_text(encoding="utf-8"))["status"] == "ERROR"


def test_server_real_endpoints(real_cache, monkeypatch):
    api = pytest.importorskip("ground_station.api_server")
    _cache(real_cache, _fake_result("IN-KRW", 2))
    rs.build_real_packet()
    from PIL import Image

    Image.fromarray(np.zeros((8, 8), np.uint8)).save(rs.REAL_DIR / "IN-KRW_overlay.png")
    state = api.EdgePortState()
    assert state.default_source() == "real"
    payload = state.build_dashboard_payload(source="real")
    assert payload["data_source"] == "real" and payload["kpis"]["total_dark_vessels"] == 0
    assert payload["real_data"]["ports"]["IN-KRW"]["overlay_preview_url"] == "/api/real/preview/IN-KRW/overlay.png"
    assert state.build_dashboard_payload(source="simulated")["data_source"] == "simulated"
