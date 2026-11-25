"""Ground station analytics engine for EdgePort (Person B - Track A)."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

from ground_station.ais_connector import fetch_ais_feed, load_local_ais

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORTS_CONFIG_PATH = REPO_ROOT / "data" / "ports_config.json"
DEFAULT_LATEST_TELEMETRY_PATH = REPO_ROOT / "outputs" / "latest_telemetry.json"
DEFAULT_SAMPLE_TELEMETRY_PATH = REPO_ROOT / "contracts" / "sample_telemetry.json"

DEFAULT_TRADE_RATE_CR_PER_MT = 0.000435
VESSEL_SPEED_KMH = {
    "FERRY": 33.3,
    "CARGO_VESSEL": 22.2,
    "FISHING_BOAT": 18.5,
}
VESSEL_TONNAGE_BY_TYPE = {
    "FISHING_BOAT": 45.0,
    "FERRY": 420.0,
    "CARGO_VESSEL": 8500.0,
}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance between two WGS84 points in kilometers."""
    radius_km = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2.0) ** 2
    return 2.0 * radius_km * math.asin(min(1.0, math.sqrt(a)))


def load_ports_config(ports_path: str | Path | None = None) -> dict[str, Any]:
    """Load shared ports configuration from data/ports_config.json."""
    path = Path(ports_path) if ports_path else DEFAULT_PORTS_CONFIG_PATH
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_telemetry_packet(
    telemetry_path: str | Path | None = None,
    prefer_latest: bool = True,
) -> tuple[dict[str, Any], Path]:
    """Load orbital telemetry JSON, preferring outputs/latest_telemetry.json when present."""
    if telemetry_path is not None:
        path = Path(telemetry_path)
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh), path

    if prefer_latest and DEFAULT_LATEST_TELEMETRY_PATH.exists():
        try:
            with DEFAULT_LATEST_TELEMETRY_PATH.open("r", encoding="utf-8") as fh:
                packet = json.load(fh)
            if isinstance(packet, dict) and {"telemetry_header", "ports_summary", "vessels"} <= packet.keys():
                return packet, DEFAULT_LATEST_TELEMETRY_PATH
        except (json.JSONDecodeError, OSError):
            pass

    with DEFAULT_SAMPLE_TELEMETRY_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh), DEFAULT_SAMPLE_TELEMETRY_PATH


def compute_port_utilization(berth_occupied: int, max_berths: int) -> float:
    """Compute Port Utilization Index (berth_occupied / max_berths), clamped to [0, 1]."""
    if max_berths <= 0:
        return 0.0
    ratio = float(berth_occupied) / float(max_berths)
    return round(max(0.0, min(1.0, ratio)), 2)


def classify_congestion(utilization_index: float) -> str:
    """Classify congestion status from Port Utilization Index."""
    if utilization_index > 0.90:
        return "CRITICAL_CONGESTION"
    if utilization_index > 0.75:
        return "HIGH_CONGESTION"
    if utilization_index >= 0.50:
        return "MODERATE"
    return "NORMAL"


def estimate_trade_value_inr_cr(
    cargo_tonnage_mt: float,
    rate_cr_per_mt: float = DEFAULT_TRADE_RATE_CR_PER_MT,
) -> float:
    """Estimate regional maritime trade value in INR Crores from cargo tonnage."""
    if cargo_tonnage_mt <= 0:
        return 0.0
    return round(float(cargo_tonnage_mt) * rate_cr_per_mt, 2)


def estimate_eta_minutes(vessel: dict[str, Any]) -> int:
    """Return vessel ETA in minutes, computing a fallback from distance and speed if absent."""
    eta = vessel.get("eta_minutes")
    if eta is not None:
        return max(0, int(eta))
    if vessel.get("zone") == "BERTH":
        return 0
    dist_km = float(vessel.get("distance_from_shore_km", 0.0))
    speed_kmh = VESSEL_SPEED_KMH.get(str(vessel.get("vessel_type", "CARGO_VESSEL")), 22.2)
    return max(1, int(round((dist_km / speed_kmh) * 60.0)))


def match_vessels_to_ais(
    vessels: list[dict[str, Any]],
    ais_records: list[dict[str, Any]],
    max_distance_km: float = 1.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cross-match SAR-detected vessels against AIS records within max_distance_km."""
    updated_vessels: list[dict[str, Any]] = []
    enriched_vessels: list[dict[str, Any]] = []

    for vessel in vessels:
        v_copy = copy.deepcopy(vessel)
        v_lat = float(v_copy["latitude"])
        v_lon = float(v_copy["longitude"])

        best_record: dict[str, Any] | None = None
        best_dist_km = float("inf")

        for ais in ais_records:
            dist_km = haversine_km(v_lat, v_lon, float(ais["lat"]), float(ais["lon"]))
            if dist_km < best_dist_km:
                best_dist_km = dist_km
                best_record = ais

        matched = best_record is not None and best_dist_km <= max_distance_km
        v_copy["ais_matched"] = matched
        updated_vessels.append(v_copy)

        if matched and best_record is not None:
            alert_flag = "AIS_VERIFIED"
        elif v_copy.get("zone") == "OFFSHORE_EEZ" or float(v_copy.get("distance_from_shore_km", 0.0)) > 50.0:
            alert_flag = "DARK_VESSEL_OFFSHORE"
        else:
            alert_flag = "DARK_VESSEL_COASTAL"

        enriched = copy.deepcopy(v_copy)
        enriched["eta_minutes_effective"] = estimate_eta_minutes(v_copy)
        enriched["matched_ais_mmsi"] = best_record["mmsi"] if matched and best_record else None
        enriched["matched_ais_name"] = best_record["name"] if matched and best_record else None
        enriched["ais_distance_km"] = round(best_dist_km, 3) if best_record else None
        enriched["alert_flag"] = alert_flag
        enriched_vessels.append(enriched)

    return updated_vessels, enriched_vessels


def build_eta_queue(
    enriched_vessels: list[dict[str, Any]],
    port_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build an ETA-ordered queue of approaching/waiting vessels."""
    filtered = [
        v
        for v in enriched_vessels
        if (port_id is None or v.get("port_id") == port_id) and v.get("zone") != "BERTH"
    ]
    return sorted(
        filtered,
        key=lambda v: (
            v.get("eta_minutes_effective", estimate_eta_minutes(v)),
            float(v.get("distance_from_shore_km", 0.0)),
        ),
    )


def compute_rerouting_suggestions(
    ports_summary: list[dict[str, Any]],
    ports_config: dict[str, Any],
    utilization_threshold: float = 0.80,
) -> list[dict[str, Any]]:
    """Generate automated vessel rerouting alerts for ports exceeding utilization_threshold."""
    port_meta = {p["port_id"]: p for p in ports_config.get("ports", [])}
    summary_by_id = {p["port_id"]: p for p in ports_summary}
    suggestions: list[dict[str, Any]] = []

    for port in ports_summary:
        util = float(port.get("utilization_index", 0.0))
        if util <= utilization_threshold:
            continue

        src_id = port["port_id"]
        src_meta = port_meta.get(src_id, {})
        src_coast = src_meta.get("coast")
        src_lat = float(src_meta.get("lat", 0.0))
        src_lon = float(src_meta.get("lon", 0.0))

        candidates: list[tuple[int, float, float, dict[str, Any]]] = []
        for cand_id, cand_summary in summary_by_id.items():
            if cand_id == src_id:
                continue
            cand_util = float(cand_summary.get("utilization_index", 1.0))
            if cand_util > utilization_threshold:
                continue
            cand_meta = port_meta.get(cand_id, {})
            same_coast_rank = 0 if cand_meta.get("coast") == src_coast else 1
            dist_km = haversine_km(
                src_lat,
                src_lon,
                float(cand_meta.get("lat", src_lat)),
                float(cand_meta.get("lon", src_lon)),
            )
            candidates.append((same_coast_rank, cand_util, dist_km, cand_summary))

        if not candidates:
            suggestions.append(
                {
                    "port_id": src_id,
                    "port_name": port["port_name"],
                    "utilization_pct": round(util * 100),
                    "anchorage_waiting": int(port.get("counts", {}).get("anchorage_waiting", 0)),
                    "recommended_port_id": None,
                    "recommended_port_name": None,
                    "distance_km": None,
                    "message": (
                        f"{port['port_name']} ({src_id}) at {util * 100:.0f}% berth capacity — "
                        "hold inbound cargo in outer anchorage; all regional alternates busy."
                    ),
                }
            )
            continue

        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        _, cand_util, dist_km, best_alt = candidates[0]
        waiting = int(port.get("counts", {}).get("anchorage_waiting", 0))
        suggestions.append(
            {
                "port_id": src_id,
                "port_name": port["port_name"],
                "utilization_pct": round(util * 100),
                "anchorage_waiting": waiting,
                "recommended_port_id": best_alt["port_id"],
                "recommended_port_name": best_alt["port_name"],
                "recommended_utilization_pct": round(cand_util * 100),
                "distance_km": round(dist_km, 1),
                "message": (
                    f"Reroute {waiting} queued vessels from {port['port_name']} ({util * 100:.0f}% full) "
                    f"to {best_alt['port_name']} [{best_alt['port_id']}] "
                    f"({cand_util * 100:.0f}% full, ~{dist_km:.0f} km)."
                ),
            }
        )

    return suggestions


def analyze_telemetry(
    packet: dict[str, Any] | None = None,
    ais_records: list[dict[str, Any]] | None = None,
    ports_config: dict[str, Any] | None = None,
    match_threshold_km: float = 1.0,
    use_live_ais: bool = False,
) -> dict[str, Any]:
    """Run full ground-station analytics on an orbital telemetry packet."""
    if packet is None:
        raw_packet, packet_source = load_telemetry_packet()
    else:
        raw_packet = packet
        packet_source = None

    working_packet = copy.deepcopy(raw_packet)
    cfg = ports_config or load_ports_config()

    if ais_records is not None:
        ais_feed_info = {
            "records": ais_records,
            "source": "provided",
            "live_enabled": False,
            "live_count": 0,
            "status_message": f"Using {len(ais_records)} provided AIS records.",
        }
    elif use_live_ais:
        ais_feed_info = fetch_ais_feed(use_live=True)
    else:
        local_ais = load_local_ais()
        ais_feed_info = {
            "records": local_ais,
            "source": "local_simulated",
            "live_enabled": False,
            "live_count": 0,
            "status_message": f"Using {len(local_ais)} local simulated AIS records.",
        }

    updated_vessels, enriched_vessels = match_vessels_to_ais(
        working_packet.get("vessels", []),
        ais_feed_info["records"],
        max_distance_km=match_threshold_km,
    )
    working_packet["vessels"] = updated_vessels

    dark_counts_by_port: dict[str, int] = {}
    vessels_by_port: dict[str, list[dict[str, Any]]] = {}
    for vessel in enriched_vessels:
        pid = vessel["port_id"]
        vessels_by_port.setdefault(pid, []).append(vessel)
        if not vessel["ais_matched"]:
            dark_counts_by_port[pid] = dark_counts_by_port.get(pid, 0) + 1

    total_active_vessels = 0
    congested_ports_count = 0
    total_trade_cr = 0.0
    total_tonnage_mt = 0.0

    for port in working_packet.get("ports_summary", []):
        pid = port["port_id"]
        counts = port.get("counts", {})
        berth_occupied = int(counts.get("berth_occupied", 0))
        max_berths = int(port.get("max_berths", 1))

        utilization = compute_port_utilization(berth_occupied, max_berths)
        port["utilization_index"] = utilization
        port["congestion_status"] = classify_congestion(utilization)
        port["dark_vessels_detected"] = int(dark_counts_by_port.get(pid, 0))

        tonnage_mt = float(port.get("estimated_cargo_tonnage_mt", 0.0))
        if tonnage_mt <= 0 and pid in vessels_by_port:
            tonnage_mt = sum(
                VESSEL_TONNAGE_BY_TYPE.get(v["vessel_type"], 500.0)
                for v in vessels_by_port[pid]
            )
            port["estimated_cargo_tonnage_mt"] = round(tonnage_mt, 1)

        trade_cr = float(port.get("estimated_trade_value_inr_cr", 0.0))
        if trade_cr <= 0 and tonnage_mt > 0:
            trade_cr = estimate_trade_value_inr_cr(tonnage_mt)
            port["estimated_trade_value_inr_cr"] = trade_cr

        port_total_vessels = (
            int(counts.get("berth_occupied", 0))
            + int(counts.get("anchorage_waiting", 0))
            + int(counts.get("coastal_fishing_boats", 0))
            + int(counts.get("approaching_ferries", 0))
            + int(counts.get("offshore_eez_vessels", 0))
        )
        total_active_vessels += port_total_vessels
        if utilization > 0.75:
            congested_ports_count += 1
        total_trade_cr += trade_cr
        total_tonnage_mt += tonnage_mt

    rerouting_suggestions = compute_rerouting_suggestions(
        working_packet.get("ports_summary", []),
        cfg,
        utilization_threshold=0.80,
    )
    eta_queue = build_eta_queue(enriched_vessels)

    total_dark_vessels = sum(
        int(p.get("dark_vessels_detected", 0)) for p in working_packet.get("ports_summary", [])
    )

    kpis = {
        "raw_scene_size_mb": float(working_packet["telemetry_header"].get("raw_scene_size_mb", 0.0)),
        "downlinked_json_size_kb": float(working_packet["telemetry_header"].get("downlinked_json_size_kb", 0.0)),
        "bandwidth_saved_pct": float(working_packet["telemetry_header"].get("bandwidth_saved_pct", 0.0)),
        "onboard_inference_time_ms": float(working_packet["telemetry_header"].get("onboard_inference_time_ms", 0.0)),
        "total_active_vessels": total_active_vessels,
        "listed_priority_vessels": len(enriched_vessels),
        "total_dark_vessels": total_dark_vessels,
        "congested_ports_count": congested_ports_count,
        "total_ports": len(working_packet.get("ports_summary", [])),
        "total_trade_value_inr_cr": round(total_trade_cr, 2),
        "total_cargo_tonnage_mt": round(total_tonnage_mt, 1),
    }

    return {
        "packet": working_packet,
        "packet_source": str(packet_source) if packet_source else "in_memory",
        "ports_config": cfg,
        "ais_feed": ais_feed_info,
        "enriched_vessels": enriched_vessels,
        "eta_queue": eta_queue,
        "rerouting_suggestions": rerouting_suggestions,
        "kpis": kpis,
    }


def build_multi_pass_nowcast(analyzed_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a 4-pass diurnal trade nowcasting series (INR Crores) from port summaries."""
    pass_multipliers = [
        ("06:00 IST", 0.84),
        ("12:00 IST", 0.96),
        ("18:00 IST", 1.00),
        ("23:30 IST", 0.91),
    ]
    rows: list[dict[str, Any]] = []
    for port in analyzed_result["packet"].get("ports_summary", []):
        base_trade = float(port.get("estimated_trade_value_inr_cr", 0.0))
        base_tonnage = float(port.get("estimated_cargo_tonnage_mt", 0.0))
        for pass_time, factor in pass_multipliers:
            rows.append(
                {
                    "pass_time_ist": pass_time,
                    "port_id": port["port_id"],
                    "port_name": port["port_name"],
                    "estimated_trade_value_inr_cr": round(base_trade * factor, 2),
                    "estimated_cargo_tonnage_mt": round(base_tonnage * factor, 1),
                }
            )
    return rows
