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
            # Fall back to nearest port in the broader 60-port network not marked congested in swath
            network_candidates: list[tuple[int, float, dict[str, Any]]] = []
            for cand_id, cand_meta in port_meta.items():
                if cand_id == src_id:
                    continue
                known_summary = summary_by_id.get(cand_id)
                if known_summary and float(known_summary.get("utilization_index", 0.0)) > utilization_threshold:
                    continue
                same_coast_rank = 0 if cand_meta.get("coast") == src_coast else 1
                dist_km = haversine_km(
                    src_lat,
                    src_lon,
                    float(cand_meta.get("lat", src_lat)),
                    float(cand_meta.get("lon", src_lon)),
                )
                network_candidates.append((same_coast_rank, dist_km, cand_meta))

            if network_candidates:
                network_candidates.sort(key=lambda item: (item[0], item[1]))
                _, dist_km, best_meta = network_candidates[0]
                waiting = int(port.get("counts", {}).get("anchorage_waiting", 0))
                suggestions.append(
                    {
                        "port_id": src_id,
                        "port_name": port["port_name"],
                        "utilization_pct": round(util * 100),
                        "anchorage_waiting": waiting,
                        "recommended_port_id": best_meta["port_id"],
                        "recommended_port_name": best_meta["port_name"],
                        "recommended_utilization_pct": None,
                        "distance_km": round(dist_km, 1),
                        "message": (
                            f"Reroute {waiting} queued vessels from {port['port_name']} ({util * 100:.0f}% full) "
                            f"to regional alternate {best_meta['port_name']} [{best_meta['port_id']}] "
                            f"(~{dist_km:.0f} km)."
                        ),
                    }
                )
                continue

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

    # Real Sentinel-1 packets carry "ais_available": false until a same-time AIS source exists:
    # then nothing can be called dark, so skip matching instead of flagging every vessel.
    ais_checked = working_packet.get("telemetry_header", {}).get("ais_available", True) is not False
    if ais_checked:
        updated_vessels, enriched_vessels = match_vessels_to_ais(
            working_packet.get("vessels", []),
            ais_feed_info["records"],
            max_distance_km=match_threshold_km,
        )
    else:
        updated_vessels = copy.deepcopy(working_packet.get("vessels", []))
        enriched_vessels = []
        for vessel in updated_vessels:
            enriched = copy.deepcopy(vessel)
            enriched["eta_minutes_effective"] = estimate_eta_minutes(vessel)
            enriched["matched_ais_mmsi"] = None
            enriched["matched_ais_name"] = None
            enriched["ais_distance_km"] = None
            enriched["alert_flag"] = "AIS_NOT_CHECKED"
            enriched_vessels.append(enriched)
        ais_feed_info = {**ais_feed_info, "status_message": "Real Sentinel-1 scenes: no same-time AIS "
                         "source yet, so dark-vessel checks are off."}
    working_packet["vessels"] = updated_vessels

    dark_counts_by_port: dict[str, int] = {}
    vessels_by_port: dict[str, list[dict[str, Any]]] = {}
    for vessel in enriched_vessels:
        pid = vessel["port_id"]
        vessels_by_port.setdefault(pid, []).append(vessel)
        if ais_checked and not vessel["ais_matched"]:
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
            + int(counts.get("coastal_cargo_vessels", 0))
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
        "total_network_ports": len(cfg.get("ports", [])),
        "ais_checked": ais_checked,
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


def build_swath_telemetry_for_port(
    port_id: str,
    ports_config: dict[str, Any] | None = None,
    sea_state: str = "MODERATE",
    pass_timestamp_utc: str = "2026-11-25T18:00:00Z",
) -> dict[str, Any]:
    """Synthesize a schema-valid < 3 KB swath telemetry packet for any port in ports_config."""
    cfg = ports_config or load_ports_config()
    all_ports = cfg.get("ports", [])
    by_id = {p["port_id"]: p for p in all_ports}
    if port_id not in by_id:
        raise KeyError(f"Unknown port_id: {port_id}")

    target = by_id[port_id]
    neighbors = sorted(
        [p for p in all_ports if p["port_id"] != port_id],
        key=lambda p: (
            0 if p.get("coast") == target.get("coast") else 1,
            haversine_km(target["lat"], target["lon"], p["lat"], p["lon"]),
        ),
    )
    swath_ports = [target] + neighbors[:3]

    ports_summary: list[dict[str, Any]] = []
    vessels: list[dict[str, Any]] = []

    for idx, port in enumerate(swath_ports):
        pid = port["port_id"]
        max_b = int(port.get("max_berths", 6))
        seed_val = sum(ord(ch) for ch in pid) + idx * 7
        occ_ratio = [0.85, 0.45, 0.95, 0.55][idx % 4]
        berth_occ = max(1, min(max_b, int(round(max_b * occ_ratio))))
        util = compute_port_utilization(berth_occ, max_b)
        anch_wait = max(0, (seed_val % 5) + (2 if util > 0.80 else 0))
        fishing_cnt = 8 + (seed_val % 19)
        ferry_cnt = 1 if (seed_val % 2 == 0) else 0
        offshore_cnt = 4 + (seed_val % 9)
        tonnage = round(berth_occ * 6800.0 + anch_wait * 3400.0, 1)

        ports_summary.append(
            {
                "port_id": pid,
                "port_name": port["port_name"],
                "state": port["state"],
                "max_berths": max_b,
                "counts": {
                    "berth_occupied": berth_occ,
                    "anchorage_waiting": anch_wait,
                    "coastal_fishing_boats": fishing_cnt,
                    "approaching_ferries": ferry_cnt,
                    "offshore_eez_vessels": offshore_cnt,
                },
                "utilization_index": util,
                "congestion_status": classify_congestion(util),
                "estimated_cargo_tonnage_mt": tonnage,
                "estimated_trade_value_inr_cr": estimate_trade_value_inr_cr(tonnage),
                "dark_vessels_detected": 0,
            }
        )

        # Keep <= 4 priority vessels total to stay strictly < 3 KB
        anch = port["anchorage_bbox"]
        v_lat = round((anch[1] + anch[3]) / 2.0, 4)
        v_lon = round((anch[0] + anch[2]) / 2.0, 4)
        v_type = "FERRY" if (idx % 2 == 1 and ferry_cnt > 0) else "CARGO_VESSEL"
        vessels.append(
            {
                "vessel_id": f"VSL-{pid.split('-')[1]}-001",
                "port_id": pid,
                "vessel_type": v_type,
                "latitude": v_lat,
                "longitude": v_lon,
                "distance_from_shore_km": round(4.2 + (seed_val % 6), 1),
                "zone": "COASTAL_APPROACH" if v_type == "FERRY" else "ANCHORAGE_QUEUE",
                "estimated_length_m": 38.0 if v_type == "FERRY" else 132.0,
                "confidence": 0.93,
                "ais_matched": False,
                "eta_minutes": 26 if v_type == "FERRY" else 85,
            }
        )

    packet: dict[str, Any] = {
        "telemetry_header": {
            "satellite_id": "EOS-EDGEPORT-LEO-04",
            "sensor_mode": "C-BAND_SAR_ALL_WEATHER",
            "pass_timestamp_utc": pass_timestamp_utc,
            "monitoring_limit_km": 370,
            "raw_scene_size_mb": 142.4,
            "cropped_roi_size_mb": 4.2,
            "downlinked_json_size_kb": 0.0,
            "bandwidth_saved_pct": 99.99,
            "onboard_inference_time_ms": 34.6,
            "sea_state": sea_state,
        },
        "ports_summary": ports_summary,
        "vessels": vessels,
    }
    minified = json.dumps(packet, separators=(",", ":")).encode("utf-8")
    size_kb = round(len(minified) / 1024.0, 2)
    packet["telemetry_header"]["downlinked_json_size_kb"] = size_kb
    packet["telemetry_header"]["bandwidth_saved_pct"] = round(
        (1.0 - (size_kb / (142.4 * 1024.0))) * 100.0, 2
    )
    return packet


def summarize_national_port_network(analyzed_result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return status rows across all configured Indian non-major ports."""
    active_by_id = {
        p["port_id"]: p for p in analyzed_result["packet"].get("ports_summary", [])
    }
    rows: list[dict[str, Any]] = []
    for port in analyzed_result["ports_config"].get("ports", []):
        pid = port["port_id"]
        active = active_by_id.get(pid)
        rows.append(
            {
                "port_id": pid,
                "port_name": port["port_name"],
                "state": port["state"],
                "coast": port.get("coast", "WEST"),
                "lat": port["lat"],
                "lon": port["lon"],
                "max_berths": port["max_berths"],
                "operational_profile": port.get("operational_profile", ""),
                "in_active_swath": active is not None,
                "utilization_index": active["utilization_index"] if active else None,
                "congestion_status": active["congestion_status"] if active else "AWAITING_NEXT_PASS",
                "dark_vessels_detected": active["dark_vessels_detected"] if active else 0,
                "estimated_trade_value_inr_cr": active["estimated_trade_value_inr_cr"] if active else 0.0,
            }
        )
    return rows

