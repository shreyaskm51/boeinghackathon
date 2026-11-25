"""Unit and integration tests for Person B (Track A) ground station & dashboard."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from app import _run_simulation_action, build_folium_map, build_offline_plotly_map
from ground_station.ais_connector import fetch_ais_feed, load_local_ais
from ground_station.alert_formatter import MAX_SMS_LENGTH, format_all_alerts, format_port_alert
from ground_station.analytics_engine import (
    analyze_telemetry,
    classify_congestion,
    compute_port_utilization,
    estimate_trade_value_inr_cr,
    haversine_km,
    load_telemetry_packet,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "telemetry_schema.json"
SAMPLE_PATH = REPO_ROOT / "contracts" / "sample_telemetry.json"


def test_local_ais_ground_truth_valid() -> None:
    records = load_local_ais()
    assert len(records) >= 12
    ports_covered = {rec["port_id"] for rec in records}
    assert {"IN-KRW", "IN-BEY", "IN-KAK", "IN-GOP"} <= ports_covered
    for rec in records:
        assert rec["mmsi"]
        assert rec["name"]
        assert rec["type"] in {"FISHING_BOAT", "FERRY", "CARGO_VESSEL"}
        assert isinstance(rec["lat"], float)
        assert isinstance(rec["lon"], float)
        assert rec["timestamp"]


def test_ais_connector_skips_live_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AISSTREAM_API_KEY", raising=False)
    feed = fetch_ais_feed(use_live=True)
    assert feed["live_enabled"] is False
    assert feed["source"] == "local_simulated"
    assert len(feed["records"]) >= 12


def test_utilization_and_congestion_math() -> None:
    assert compute_port_utilization(2, 5) == 0.40
    assert compute_port_utilization(3, 6) == 0.50
    assert compute_port_utilization(5, 6) == 0.83
    assert compute_port_utilization(10, 10) == 1.00
    assert compute_port_utilization(12, 10) == 1.00
    assert compute_port_utilization(1, 0) == 0.00

    assert classify_congestion(0.40) == "NORMAL"
    assert classify_congestion(0.50) == "MODERATE"
    assert classify_congestion(0.75) == "MODERATE"
    assert classify_congestion(0.76) == "HIGH_CONGESTION"
    assert classify_congestion(0.90) == "HIGH_CONGESTION"
    assert classify_congestion(0.91) == "CRITICAL_CONGESTION"
    assert classify_congestion(1.00) == "CRITICAL_CONGESTION"


def test_haversine_and_trade_estimation() -> None:
    dist = haversine_km(14.7912, 74.0845, 14.7918, 74.0851)
    assert 0.05 < dist < 0.15
    assert estimate_trade_value_inr_cr(0) == 0.0
    assert estimate_trade_value_inr_cr(10000) == pytest.approx(4.35, rel=1e-3)


def test_dark_vessels_detected_and_schema_preserved_on_sample_data() -> None:
    sample_packet, _ = load_telemetry_packet(SAMPLE_PATH, prefer_latest=False)
    result = analyze_telemetry(packet=sample_packet)
    updated_packet = result["packet"]

    with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.validate(instance=updated_packet, schema=schema)

    vessels_by_id = {v["vessel_id"]: v for v in updated_packet["vessels"]}
    assert vessels_by_id["VSL-KRW-001"]["ais_matched"] is True
    assert vessels_by_id["VSL-BEY-001"]["ais_matched"] is True
    assert vessels_by_id["VSL-KAK-001"]["ais_matched"] is False
    assert vessels_by_id["VSL-GOP-001"]["ais_matched"] is False

    ports_by_id = {p["port_id"]: p for p in updated_packet["ports_summary"]}
    assert ports_by_id["IN-KRW"]["dark_vessels_detected"] == 0
    assert ports_by_id["IN-BEY"]["dark_vessels_detected"] == 0
    assert ports_by_id["IN-KAK"]["dark_vessels_detected"] == 1
    assert ports_by_id["IN-GOP"]["dark_vessels_detected"] == 1
    assert result["kpis"]["total_dark_vessels"] == 2

    reroutes = {r["port_id"]: r for r in result["rerouting_suggestions"]}
    assert set(reroutes.keys()) == {"IN-KRW", "IN-KAK"}
    assert reroutes["IN-KRW"]["recommended_port_id"] == "IN-BEY"
    assert reroutes["IN-KAK"]["recommended_port_id"] == "IN-GOP"


def test_sms_alerts_always_under_160_chars() -> None:
    sample_packet, _ = load_telemetry_packet(SAMPLE_PATH, prefer_latest=False)
    analyzed = analyze_telemetry(packet=sample_packet)
    alerts = format_all_alerts(analyzed)

    assert set(alerts.keys()) == {"IN-KRW", "IN-BEY", "IN-KAK", "IN-GOP"}
    for pid, text in alerts.items():
        assert text.startswith(f"EDGEPORT ALERT [{pid}]: ")
        assert len(text) <= MAX_SMS_LENGTH

    extreme_port = {
        "port_id": "IN-KRW-EXTREME-LONG-CODE",
        "max_berths": 999,
        "counts": {
            "berth_occupied": 999,
            "anchorage_waiting": 1234,
            "coastal_fishing_boats": 5678,
            "approaching_ferries": 99,
            "offshore_eez_vessels": 4321,
        },
        "dark_vessels_detected": 88,
    }
    extreme_vessels = [
        {
            "port_id": "IN-KRW-EXTREME-LONG-CODE",
            "vessel_type": "FERRY",
            "eta_minutes": 145,
            "distance_from_shore_km": 48.0,
        },
        {
            "port_id": "IN-KRW-EXTREME-LONG-CODE",
            "vessel_type": "FISHING_BOAT",
            "eta_minutes": 90,
            "distance_from_shore_km": 320.0,
        },
    ]
    extreme_alert = format_port_alert(
        extreme_port,
        vessels=extreme_vessels,
        reroute_suggestion={"recommended_port_id": "IN-BEY-ALTERNATE"},
    )
    assert len(extreme_alert) <= MAX_SMS_LENGTH
    assert extreme_alert.startswith("EDGEPORT ALERT [IN-KRW-EXTREME-LONG-CODE]: ")


def test_dashboard_map_and_simulator_fallback_smoke() -> None:
    sim_out = _run_simulation_action(
        port_id=None,
        sea_state="MONSOON_ROUGH",
        pass_time_ist="18:00",
        uploaded_bytes=None,
    )
    assert "telemetry" in sim_out
    assert sim_out["raw_preview"].shape == (384, 384)
    assert sim_out["overlay_preview"].shape == (384, 384, 3)

    analyzed = analyze_telemetry(packet=sim_out["telemetry"])
    fmap = build_folium_map(analyzed, selected_port_id="ALL")
    assert fmap is not None
    fig = build_offline_plotly_map(analyzed, selected_port_id="ALL")
    assert len(fig.data) > 0
