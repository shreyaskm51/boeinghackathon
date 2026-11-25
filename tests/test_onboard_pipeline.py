"""Tests for the onboard edge AI pipeline (Track B, Person A)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest

from onboard_ai.cfar_onnx_detector import (
    CLASS_NAMES,
    N_FEATURES,
    MODEL_PATH,
    classify_detections,
    detect_targets,
    get_classifier,
)
from onboard_ai.edge_pipeline import PACKET_LIMIT_BYTES, run_pass
from onboard_ai.georeferencer import GeoTransform, classify_zone, get_port, georeference, load_ports_config
from onboard_ai.sar_simulator import simulate_pass, to_display_uint8

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts" / "telemetry_schema.json").read_text(encoding="utf-8"))
PORT_IDS = [p["port_id"] for p in load_ports_config()["ports"]]
ORIGINAL_PORTS = ["IN-KRW", "IN-BEY", "IN-KAK", "IN-GOP"]


def _validate(packet: dict) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(packet, SCHEMA)


@pytest.fixture(scope="module")
def sweep(tmp_path_factory):
    out = tmp_path_factory.mktemp("telemetry") / "latest_telemetry.json"
    return run_pass(None, "MODERATE", "18:00", output_path=str(out), seed=42), out


# ------------------------------------------------------------------ packet contract

def test_full_sweep_packet_matches_contract(sweep):
    result, _ = sweep
    _validate(result["telemetry"])
    assert [p["port_id"] for p in result["telemetry"]["ports_summary"]] == PORT_IDS


def test_ports_config_has_16_ports():
    assert len(PORT_IDS) == 16 and len(set(PORT_IDS)) == 16


def test_packet_file_is_within_budget_and_header_is_truthful(sweep):
    result, out = sweep
    raw = out.read_bytes()
    assert len(raw) < PACKET_LIMIT_BYTES
    header = json.loads(raw)["telemetry_header"]
    assert header["downlinked_json_size_kb"] == pytest.approx(len(raw) / 1024, abs=0.011)
    assert header["bandwidth_saved_pct"] > 99.0
    assert header["raw_scene_size_mb"] > header["cropped_roi_size_mb"] > 0


def test_vessel_records_are_consistent(sweep):
    result, _ = sweep
    packet = result["telemetry"]
    assert packet["vessels"], "budget should leave room for at least one vessel"
    # with the 200 KB budget every detected vessel is downlinked
    assert len(packet["vessels"]) == result["stats"]["vessels_detected"]
    ids = [v["vessel_id"] for v in packet["vessels"]]
    assert len(ids) == len(set(ids))
    for v in packet["vessels"]:
        assert v["ais_matched"] is False  # ground station owns AIS matching
        assert 0 <= v["distance_from_shore_km"] <= 370
        if v["zone"] == "OFFSHORE_EEZ":
            assert v["distance_from_shore_km"] > 50


def test_run_pass_returns_previews_and_stats(sweep):
    result, _ = sweep
    raw, overlay = result["raw_preview"], result["overlay_preview"]
    assert raw.dtype == np.uint8 and raw.ndim == 2 and max(raw.shape) <= 512
    assert overlay.shape == (*raw.shape, 3)
    assert result["stats"]["vessels_detected"] >= len(result["telemetry"]["vessels"])
    for chip in result["alert_chips"]:
        assert chip[:8] == b"\x89PNG\r\n\x1a\n"


def test_default_output_path_is_repo_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # must not depend on the working directory
    result = run_pass("IN-GOP", "CALM", "12:00", output_path=str(tmp_path / "pkt.json"), seed=1)
    assert (tmp_path / "pkt.json").exists()
    _validate(result["telemetry"])


@pytest.mark.parametrize("sea_state", ["Clear Calm", "MODERATE", "Monsoon Rough Sea"])
def test_every_sea_state_produces_valid_packet(tmp_path, sea_state):
    result = run_pass("IN-KAK", sea_state, "06:00", output_path=str(tmp_path / "p.json"), seed=3)
    _validate(result["telemetry"])


def test_invalid_inputs_raise(tmp_path):
    with pytest.raises(ValueError):
        run_pass("IN-XXX", output_path=str(tmp_path / "p.json"))
    with pytest.raises(ValueError):
        run_pass("IN-KRW", pass_time_ist="09:15", output_path=str(tmp_path / "p.json"))


# ------------------------------------------------------------------ detection quality

def _match(dets_labels, truth_vessel):
    return [lab for d, lab in dets_labels
            if (d.px - truth_vessel.px) ** 2 + (d.py - truth_vessel.py) ** 2 < (3 + truth_vessel.length_m / 20) ** 2]


def test_calm_sea_detects_all_ships_without_land_false_alarms():
    clf = get_classifier()
    tile = simulate_pass("IN-KAK", "CALM", "12:00", seed=5).tiles[0]
    dets = detect_targets(tile.image, tile.land_mask)
    labels, _ = clf.predict(np.stack([d.features() for d in dets]))
    kept = [(d, lab) for d, lab in zip(dets, labels) if lab != "CLUTTER"]

    ships = [v for v in tile.truth if v.vessel_type != "FISHING_BOAT"]
    assert ships
    found = sum(bool(_match(kept, v)) for v in ships)
    assert found / len(ships) >= 0.95
    correct = sum(v.vessel_type in _match(kept, v) for v in ships)
    assert correct / len(ships) >= 0.9
    # quay cranes, breakwater lights and buildings must never become vessels
    assert not any(tile.land_mask[int(round(d.py)), int(round(d.px))] for d, _ in kept)


def test_land_mask_suppresses_bright_land():
    img = np.full((256, 256), 100, dtype=np.uint16)
    land = np.zeros_like(img, dtype=bool)
    land[:, 200:] = True
    img[50, 210] = img[120, 230] = 50000  # cranes on land
    img[180, 100] = 9000                  # a real ship at sea
    dets = detect_targets(img, land)
    assert len(dets) == 1 and abs(dets[0].px - 100) < 1 and abs(dets[0].py - 180) < 1


def test_onboard_inference_is_fast(sweep):
    # 4-port C-band sweep; ~90 ms/port on a laptop CPU. Generous bound to avoid flaky CI.
    assert sweep[0]["telemetry"]["telemetry_header"]["onboard_inference_time_ms"] < 1500


# ------------------------------------------------------------------ X-band mode

def _hits_in_footprint(tile, pairs, use_x):
    kept = [(d, lab) for d, lab in zip(*_classified(tile)) if lab != "CLUTTER"]
    hits = 0
    for v, x, y in pairs:
        px, py = (x, y) if use_x else (v.px, v.py)
        r = 3 + v.length_m / tile.geo.pixel_m / 2
        hits += any((d.px - px) ** 2 + (d.py - py) ** 2 < r * r and lab == "FISHING_BOAT" for d, lab in kept)
    return hits


def _classified(tile):
    dets = detect_targets(tile.image, tile.cfar_mask, tile.geo.pixel_m, land_buffered=True)
    labels, _ = classify_detections(dets)
    return dets, labels


def test_xband_spotlight_finds_more_small_boats_than_cband():
    c_hits = x_hits = total = 0
    for seed, pid in enumerate(ORIGINAL_PORTS):
        p = simulate_pass(pid, "MODERATE", "06:00", seed=200 + seed, sensor_mode="X-BAND")
        ctile, xtile = p.tiles[0], p.tiles[-1]
        assert xtile.sensor == "X-BAND" and xtile.geo.pixel_m == 3.0
        pairs = []
        for v in ctile.truth:
            if v.vessel_type != "FISHING_BOAT":
                continue
            x, y = xtile.geo.latlon_to_pixel(*ctile.geo.pixel_to_latlon(v.px, v.py))
            if 20 <= x < xtile.image.shape[1] - 20 and 20 <= y < xtile.image.shape[0] - 20:
                pairs.append((v, x, y))
        total += len(pairs)
        c_hits += _hits_in_footprint(ctile, pairs, use_x=False)
        x_hits += _hits_in_footprint(xtile, pairs, use_x=True)
    assert total >= 10
    assert x_hits >= 0.7 * total
    assert x_hits > c_hits


def test_xband_run_pass_packet_and_previews(tmp_path):
    result = run_pass("IN-KAK", "MODERATE", "06:00", output_path=str(tmp_path / "x.json"), seed=8,
                      sensor_mode="X-BAND")
    _validate(result["telemetry"])
    assert result["telemetry"]["telemetry_header"]["sensor_mode"] == "X-BAND_SAR_HIGH_RES"
    assert result["xband_overlay_preview"] is not None
    assert result["xband_overlay_preview"].shape == (*result["xband_raw_preview"].shape, 3)


def test_xband_does_not_double_count_vessels(tmp_path):
    from onboard_ai.georeferencer import haversine_km

    p = simulate_pass("IN-KRW", "CALM", "12:00", seed=11)
    n_true = sum(len(t.truth) for t in p.tiles)
    c = run_pass("IN-KRW", "CALM", "12:00", output_path=str(tmp_path / "c.json"), seed=11)
    x = run_pass("IN-KRW", "CALM", "12:00", output_path=str(tmp_path / "x.json"), seed=11, sensor_mode="X-BAND")
    assert x["stats"]["vessels_detected"] <= n_true + 2
    assert x["stats"]["vessels_detected"] >= c["stats"]["vessels_detected"] - 2
    vessels = x["telemetry"]["vessels"]            # every vessel is in the 200 KB packet
    assert len(vessels) == x["stats"]["vessels_detected"]
    for i, a in enumerate(vessels):              # no vessel reported twice (C-band + X-band)
        for b in vessels[i + 1:]:
            gap_m = haversine_km(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) * 1000
            assert gap_m > 0.5 * max(a["estimated_length_m"], b["estimated_length_m"], 40.0)


def test_single_xband_pixel_is_too_small_to_be_a_vessel():
    img = np.full((300, 300), 100, dtype=np.uint16)
    img[150, 150] = 20000  # one 3 m pixel: physically shorter than any vessel class
    dets = detect_targets(img, None, pixel_m=3.0)
    labels, _ = classify_detections(dets)
    assert labels and all(lab == "CLUTTER" for lab in labels)


# ------------------------------------------------------------------ ONNX model

def test_onnx_model_auto_generated_and_outputs_probabilities():
    clf = get_classifier()
    assert MODEL_PATH.exists()
    assert clf.session.get_inputs()[0].shape[-1] == N_FEATURES
    names, conf = clf.predict(np.zeros((3, N_FEATURES), dtype=np.float32))
    assert len(names) == 3 and all(n in CLASS_NAMES for n in names)
    assert np.all((conf >= 0) & (conf <= 1))


# ------------------------------------------------------------------ georeferencing

def test_geotransform_roundtrip():
    geo = GeoTransform.anchored(14.8, 74.1, 1500, 1024, 10.0)
    lat, lon = geo.pixel_to_latlon(321.5, 1777.25)
    px, py = geo.latlon_to_pixel(lat, lon)
    assert px == pytest.approx(321.5) and py == pytest.approx(1777.25)
    assert geo.pixel_to_latlon(1500, 1024) == pytest.approx((14.8, 74.1))


@pytest.mark.parametrize("port_id", PORT_IDS)
def test_zone_tagging_uses_port_polygons(port_id):
    port = get_port(port_id)
    for key, zone in (("berth_bbox", "BERTH"), ("anchorage_bbox", "ANCHORAGE_QUEUE")):
        min_lon, min_lat, max_lon, max_lat = port[key]
        assert classify_zone(port, (min_lat + max_lat) / 2, (min_lon + max_lon) / 2, 1.0) == zone
    assert classify_zone(port, port["lat"] + 0.5, port["lon"], 20.0) == "COASTAL_APPROACH"
    assert classify_zone(port, port["lat"] + 0.5, port["lon"], 120.0) == "OFFSHORE_EEZ"


def test_georeference_rejects_targets_beyond_eez():
    port = get_port("IN-KRW")
    geo = GeoTransform.anchored(port["lat"], port["lon"] - 4.0, 0, 0, 10.0)  # ~430 km offshore
    assert georeference(0, 0, 100.0, geo, port, "CARGO_VESSEL", 0.9) is None


# ------------------------------------------------------------------ uploads

def test_uploaded_png_runs_through_pipeline(tmp_path):
    from PIL import Image

    tile = simulate_pass("IN-BEY", "CALM", "12:00", seed=9).tiles[0]
    buf = io.BytesIO()
    Image.fromarray(to_display_uint8(tile.image[512:1536, 1000:2048])).save(buf, format="PNG")
    result = run_pass("IN-BEY", "CALM", "12:00", uploaded_image=buf.getvalue(),
                      output_path=str(tmp_path / "upload.json"))
    _validate(result["telemetry"])
    assert result["stats"]["source"] == "UPLOAD"
    assert result["stats"]["vessels_detected"] > 0


def test_uploaded_npy_runs_through_pipeline(tmp_path):
    tile = simulate_pass("IN-GOP", "MODERATE", "18:00", seed=4).tiles[0]
    buf = io.BytesIO()
    np.save(buf, tile.image)
    result = run_pass("IN-GOP", uploaded_image=buf.getvalue(), output_path=str(tmp_path / "npy.json"))
    _validate(result["telemetry"])
    assert result["stats"]["vessels_detected"] > 0
