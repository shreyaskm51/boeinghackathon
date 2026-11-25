"""Unit and integration tests for Person B (Track A) ground station & dashboard."""

from __future__ import annotations

import io
import json
from pathlib import Path

import jsonschema
import pytest

from ground_station.ais_connector import fetch_ais_feed, load_local_ais
from ground_station.alert_formatter import MAX_SMS_LENGTH, format_all_alerts, format_port_alert
from ground_station.analytics_engine import (
    analyze_telemetry,
    build_swath_telemetry_for_port,
    classify_congestion,
    compute_port_utilization,
    estimate_trade_value_inr_cr,
    haversine_km,
    load_ports_config,
    load_telemetry_packet,
    summarize_national_port_network,
)
from ground_station.api_server import EdgePortState, make_handler

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "telemetry_schema.json"
SAMPLE_PATH = REPO_ROOT / "contracts" / "sample_telemetry.json"
FRONTEND_INDEX = REPO_ROOT / "frontend" / "dist" / "index.html"


@pytest.fixture(autouse=True)
def isolated_outputs(tmp_path, monkeypatch):
    """Keep tests independent of (and from overwriting) the shared outputs/ files.

    Simulated passes write the latest packet and the simulated AIS feed; point both at a
    temp folder so every test starts from the sample packet.
    """
    import ground_station.ais_connector as ais_connector
    import ground_station.analytics_engine as analytics_engine
    import ground_station.api_server as api_server

    latest = tmp_path / "latest_telemetry.json"
    sim_ais = tmp_path / "simulated_ais.json"
    monkeypatch.setattr(analytics_engine, "DEFAULT_LATEST_TELEMETRY_PATH", latest)
    monkeypatch.setattr(api_server, "DEFAULT_LATEST_TELEMETRY_PATH", latest)
    monkeypatch.setattr(ais_connector, "SIMULATED_AIS_PATH", sim_ais)
    try:
        import onboard_ai.edge_pipeline as edge_pipeline

        monkeypatch.setattr(edge_pipeline, "SIMULATED_AIS_PATH", sim_ais)
    except ImportError:
        pass
    try:  # real Sentinel-1 cache: tests start without any real data
        import onboard_ai.real_sentinel1 as real_sentinel1

        monkeypatch.setattr(real_sentinel1, "REAL_DIR", tmp_path / "real")
        monkeypatch.setattr(real_sentinel1, "REAL_PACKET_PATH", tmp_path / "real_telemetry.json")
    except ImportError:
        pass


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


class _FakeSocket:
    """In-memory socket stand-in for testing BaseHTTPRequestHandler without TCP sockets."""

    def __init__(self, raw_request: bytes) -> None:
        self._rfile = io.BytesIO(raw_request)
        self._wfile = io.BytesIO()

    def makefile(self, mode: str, *args: object, **kwargs: object) -> io.BytesIO:
        return self._rfile if "r" in mode else self._wfile

    def sendall(self, data: bytes) -> None:
        self._wfile.write(data)

    def close(self) -> None:
        return None


def _dispatch_http(handler_cls: type, raw_request: bytes) -> tuple[int, bytes]:
    fake_sock = _FakeSocket(raw_request)
    handler_cls(fake_sock, ("127.0.0.1", 0), None)
    raw_resp = fake_sock._wfile.getvalue()
    head, _, body = raw_resp.partition(b"\r\n\r\n")
    status_line = head.splitlines()[0].decode("ascii")
    status_code = int(status_line.split()[1])
    return status_code, body


def test_api_server_endpoints_and_simulation_smoke() -> None:
    state = EdgePortState()
    handler_cls = make_handler(state)

    status, body = _dispatch_http(handler_cls, b"GET /api/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert status == 200
    health = json.loads(body.decode("utf-8"))
    assert health["status"] == "ok"

    status, body = _dispatch_http(handler_cls, b"GET /api/dashboard HTTP/1.1\r\nHost: localhost\r\n\r\n")
    assert status == 200
    dash = json.loads(body.decode("utf-8"))
    assert dash["kpis"]["total_network_ports"] == 60
    assert len(dash["national_network"]) == 60
    assert len(dash["alerts_by_port"]) == 4

    sim_body = json.dumps(
        {"port_id": "IN-OKH", "sea_state": "MONSOON_ROUGH", "pass_time_ist": "18:00"}
    ).encode("utf-8")
    sim_req = (
        b"POST /api/simulate HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(sim_body)}\r\n\r\n".encode("ascii")
        + sim_body
    )
    status, body = _dispatch_http(handler_cls, sim_req)
    assert status == 200
    sim_payload = json.loads(body.decode("utf-8"))
    assert sim_payload["packet"]["ports_summary"][0]["port_id"] == "IN-OKH"
    assert sim_payload["simulation"]["raw_preview_url"].startswith("data:image/png;base64,")
    assert sim_payload["simulation"]["overlay_preview_url"].startswith("data:image/png;base64,")

    if FRONTEND_INDEX.exists():  # the React app is only served after `npm run build`
        status, html_body = _dispatch_http(handler_cls, b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        assert status == 200
        assert b"EdgePort" in html_body


def test_sixty_port_network_and_swath_generation() -> None:
    cfg = load_ports_config()
    ports = cfg["ports"]
    assert 50 <= len(ports) <= 100
    port_ids = [p["port_id"] for p in ports]
    assert len(set(port_ids)) == len(ports)
    assert {"IN-KRW", "IN-BEY", "IN-KAK", "IN-GOP", "IN-OKH", "IN-PBL", "IN-DHM"} <= set(port_ids)

    with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
        schema = json.load(fh)

    for target_pid in ("IN-OKH", "IN-PBL", "IN-DHM"):
        swath_packet = build_swath_telemetry_for_port(target_pid, ports_config=cfg)
        minified_bytes = len(json.dumps(swath_packet, separators=(",", ":")).encode("utf-8"))
        assert minified_bytes < 3 * 1024
        assert swath_packet["telemetry_header"]["downlinked_json_size_kb"] < 3.0
        jsonschema.validate(instance=swath_packet, schema=schema)

        analyzed = analyze_telemetry(packet=swath_packet, ports_config=cfg)
        alerts = format_all_alerts(analyzed)
        assert target_pid in alerts
        assert all(len(msg) <= MAX_SMS_LENGTH for msg in alerts.values())

        network = summarize_national_port_network(analyzed)
        assert len(network) == len(ports)
        assert sum(1 for row in network if row["in_active_swath"]) == len(swath_packet["ports_summary"])

    state = EdgePortState()
    sim_single = state.run_simulation(port_id="IN-OKH", sea_state="CALM", pass_time_ist="06:00")
    assert sim_single["packet"]["ports_summary"][0]["port_id"] == "IN-OKH"




def _post_json(handler_cls: type, path: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    req = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
           f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body
    status, raw = _dispatch_http(handler_cls, req)
    return status, json.loads(raw.decode("utf-8"))


def test_simulate_rejects_bad_input_with_400() -> None:
    handler_cls = make_handler(EdgePortState())
    for bad in ({"port_id": "IN-KRW", "pass_time_ist": "09:15"}, {"port_id": "IN-XXX"},
                {"port_id": "IN-KRW", "sensor_mode": "L-BAND"}):
        status, payload = _post_json(handler_cls, "/api/simulate", bad)
        assert status == 400, bad
        assert payload["error"]


def test_simulated_pass_flags_only_few_dark_vessels() -> None:
    pytest.importorskip("onboard_ai.edge_pipeline")
    payload = EdgePortState().run_simulation(port_id="IN-KRW", sea_state="MODERATE", pass_time_ist="18:00")
    vessels = payload["packet"]["vessels"]
    dark = sum(not v["ais_matched"] for v in vessels)
    assert vessels and dark <= 0.4 * len(vessels)


def test_xband_simulation_returns_spotlight_previews() -> None:
    pytest.importorskip("onboard_ai.edge_pipeline")
    payload = EdgePortState().run_simulation(port_id="IN-KAK", sensor_mode="X-BAND")
    assert payload["simulation"]["sensor_mode"] == "X-BAND"
    assert payload["simulation"]["xband_overlay_preview_url"].startswith("data:image/png;base64,")


def test_alert_wording_for_real_scenes() -> None:
    summary = {"port_id": "IN-RDI", "max_berths": 4, "dark_vessels_detected": 0,
               "observed_utc": "2026-11-24T00:55:27Z", "data_source": "SENTINEL-1",
               "counts": {"berth_occupied": 1, "anchorage_waiting": 2, "coastal_fishing_boats": 0,
                          "approaching_ferries": 0, "offshore_eez_vessels": 0}}
    alert = format_port_alert(summary, [], ais_checked=False)
    assert alert.startswith("EDGEPORT ALERT [IN-RDI] 24 Nov:")
    assert "AIS not checked" in alert and len(alert) <= MAX_SMS_LENGTH

    unseen = format_port_alert({"port_id": "IN-KVT", "max_berths": 4, "data_source": "NO_SCENE", "counts": {}}, [],
                               ais_checked=False)
    assert unseen == "EDGEPORT ALERT [IN-KVT]: No recent satellite pass, port status unknown."

    # simulated / AIS-checked alerts keep the original wording
    simulated = format_port_alert({**summary, "observed_utc": None, "data_source": None}, [])
    assert "AIS" not in simulated and simulated.startswith("EDGEPORT ALERT [IN-RDI]: ")
