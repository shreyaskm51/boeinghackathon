"""Main orbital orchestrator (PRD Step D).

ROI crop -> land mask + CA-CFAR -> ONNX classification -> georeferencing & zone tagging ->
per-port summary -> **raw raster purged from memory** -> < 200 KB JSON telemetry packet
(``outputs/latest_telemetry.json``, format = ``contracts/telemetry_schema.json``).

64x64 PNG image chips are generated only for alert-worthy vessels: anchorage queues of ports
in ``CRITICAL_CONGESTION`` and ship-sized ``DARK_VESSEL_OFFSHORE`` candidates in the EEZ
corridor. Chips are returned to the caller and written to ``outputs/chips/``; they are not part
of the telemetry packet.

Run once from the repo root to create the first packet::

    python -m onboard_ai.edge_pipeline            # full constellation sweep, all 16 ports
    python -m onboard_ai.edge_pipeline --port IN-KAK --sea-state MONSOON_ROUGH --pass-time 06:00
"""
from __future__ import annotations

import argparse
import gc
import io
import json
import logging
import math
import time
import zlib
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

import numpy as np

from onboard_ai.cfar_onnx_detector import Detection, classify_detections, detect_targets, get_classifier
from onboard_ai.georeferencer import (
    VesselRecord,
    georeference,
    get_port,
    haversine_km,
    load_ports_config,
    port_code,
)
from onboard_ai.sar_simulator import (
    FULL_SWATH_MB,
    PIXEL_M,
    XBAND_PIXEL_M,
    OrbitalPass,
    SceneTile,
    load_uploaded_image,
    normalize_pass_time,
    normalize_sea_state,
    normalize_sensor_mode,
    simulate_pass,
    simulated_ais_records,
    to_display_uint8,
)

log = logging.getLogger("edgeport.onboard.pipeline")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = "outputs/latest_telemetry.json"
CHIP_DIR = ROOT / "outputs" / "chips"
# AIS reports matching the simulated scene (ground station cross-matches against these)
SIMULATED_AIS_PATH = ROOT / "outputs" / "simulated_ais.json"

SATELLITE_ID = "EOS-EDGEPORT-LEO-04"
SENSOR_MODE_LABEL = {"C-BAND": "C-BAND_SAR_ALL_WEATHER", "X-BAND": "X-BAND_SAR_HIGH_RES"}
MONITORING_LIMIT_KM = 370
PACKET_LIMIT_BYTES = 200 * 1024        # hard downlink budget (< 200 KB, contract v2)
PACKET_BUDGET_BYTES = PACKET_LIMIT_BYTES - 64  # headroom for the self-referencing size fields
CHIP_SIZE = 64
MAX_CHIPS = 16
PREVIEW_SIDE = 512
TRADE_VALUE_INR_PER_TONNE = 4300.0     # blended coastal cargo value (iron ore, rice, coal, containers)
IST = timezone(timedelta(hours=5, minutes=30))

TYPE_COLOURS = {  # dashboard legend: Green fishing, Cyan ferry, Gold cargo, Red dark/alert
    "FISHING_BOAT": (40, 200, 60),
    "FERRY": (0, 220, 230),
    "CARGO_VESSEL": (255, 200, 0),
    "ALERT": (255, 40, 40),
}


# --------------------------------------------------------------------------- helpers

def _resolve_output(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _pass_datetime_utc(pass_time_ist: str) -> datetime:
    hh, mm = (int(v) for v in pass_time_ist.split(":"))
    local = datetime.combine(datetime.now(IST).date(), dtime(hh, mm), tzinfo=IST)
    return local.astimezone(timezone.utc)


def _pass_timestamp_utc(pass_time_ist: str) -> str:
    return _pass_datetime_utc(pass_time_ist).strftime("%Y-%m-%dT%H:%M:%SZ")


def _congestion_status(utilization: float) -> str:
    if utilization > 0.9:
        return "CRITICAL_CONGESTION"
    if utilization > 0.75:
        return "HIGH_CONGESTION"
    if utilization > 0.5:
        return "MODERATE"
    return "NORMAL"


def summarize_port(port: dict, vessels: list[VesselRecord]) -> dict:
    """Per-port counts, utilization, congestion and trade proxy (schema ``ports_summary`` item)."""
    def n(pred) -> int:
        return sum(1 for v in vessels if pred(v))

    berth = n(lambda v: v.zone == "BERTH" and v.vessel_type != "FISHING_BOAT")
    utilization = round(min(1.0, berth / max(int(port["max_berths"]), 1)), 2)
    tonnage = sum(v.estimated_tonnage_mt for v in vessels
                  if v.vessel_type == "CARGO_VESSEL" and v.zone in ("BERTH", "ANCHORAGE_QUEUE"))
    return {
        "port_id": port["port_id"],
        "port_name": port["port_name"],
        "state": port["state"],
        "max_berths": int(port["max_berths"]),
        "counts": {
            "berth_occupied": berth,
            "anchorage_waiting": n(lambda v: v.zone == "ANCHORAGE_QUEUE" and v.vessel_type != "FISHING_BOAT"),
            "coastal_fishing_boats": n(lambda v: v.vessel_type == "FISHING_BOAT" and v.zone != "OFFSHORE_EEZ"),
            "approaching_ferries": n(lambda v: v.vessel_type == "FERRY" and v.zone == "COASTAL_APPROACH"),
            # cargo ships in the 0-50 km approach (not yet at anchorage or berth)
            "coastal_cargo_vessels": n(lambda v: v.vessel_type == "CARGO_VESSEL" and v.zone == "COASTAL_APPROACH"),
            "offshore_eez_vessels": n(lambda v: v.zone == "OFFSHORE_EEZ"),
        },
        "utilization_index": utilization,
        "congestion_status": _congestion_status(utilization),
        "estimated_cargo_tonnage_mt": int(round(tonnage, -2)),
        "estimated_trade_value_inr_cr": round(tonnage * TRADE_VALUE_INR_PER_TONNE / 1e7, 1),
        "dark_vessels_detected": 0,  # ground station recomputes from AIS (TEAM_SPLIT.md)
    }


def _priority(v: VesselRecord) -> tuple:
    """Lower = more important to downlink if the packet budget ever forces a cut."""
    if v.dark_candidate:
        rank = 0
    elif v.zone == "ANCHORAGE_QUEUE" and v.vessel_type != "FISHING_BOAT":
        rank = 1
    elif v.zone == "COASTAL_APPROACH" and v.vessel_type == "FERRY":
        rank = 2
    elif v.zone == "BERTH" and v.vessel_type != "FISHING_BOAT":
        rank = 3
    elif v.zone == "OFFSHORE_EEZ":
        rank = 4
    else:
        rank = 5
    eta = v.eta_minutes if v.eta_minutes is not None else 10 ** 6
    return rank, eta, -v.estimated_length_m


def _packet_bytes(packet: dict) -> int:
    return len(json.dumps(packet, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _bandwidth_saved_pct(json_bytes: int, raw_mb: float) -> float:
    if raw_mb <= 0:
        return 0.0
    saved = 100.0 * (1.0 - json_bytes / (raw_mb * 1e6))
    return math.floor(saved * 100) / 100  # truncate, never round up to 100 %


def build_packet(header: dict, summaries: list[dict], per_port: list[list[VesselRecord]]) -> dict:
    """Fit the header, all port summaries and as many priority vessels as the budget allows."""
    packet = {"telemetry_header": header, "ports_summary": summaries, "vessels": []}
    queues = [sorted(v, key=_priority) for v in per_port]
    order = [q[i] for i in range(max((len(q) for q in queues), default=0)) for q in queues if i < len(q)]
    order.sort(key=lambda v: _priority(v)[0])  # stable: round-robin across ports within each rank
    for rec in order:
        packet["vessels"].append(rec.to_packet())
        if _packet_bytes(packet) > PACKET_BUDGET_BYTES:
            packet["vessels"].pop()
            break
    raw_mb = header["raw_scene_size_mb"]
    for _ in range(3):  # size fields reference the packet's own size
        nbytes = _packet_bytes(packet)
        header["downlinked_json_size_kb"] = round(nbytes / 1024, 2)
        header["bandwidth_saved_pct"] = _bandwidth_saved_pct(nbytes, raw_mb)
    while _packet_bytes(packet) >= PACKET_LIMIT_BYTES and packet["vessels"]:
        packet["vessels"].pop()
        header["downlinked_json_size_kb"] = round(_packet_bytes(packet) / 1024, 2)
    return packet


def _merge_sensors(found: list[tuple[SceneTile, Detection, VesselRecord]]
                   ) -> list[tuple[SceneTile, Detection, VesselRecord]]:
    """Inside an X-band spotlight footprint the 3 m detections replace the 10 m ones.

    A C-band detection is dropped if it lies inside the footprint, or within
    ``max(60 m, 0.6 x length)`` of an X-band detection (ships straddling the footprint edge).
    """
    xtiles = [t for t in {id(t): t for t, _, _ in found}.values() if t.sensor == "X-BAND"]
    if not xtiles:
        return found
    boxes = []
    for t in xtiles:
        h, w = t.image.shape
        lat_top, lon_left = t.geo.pixel_to_latlon(-0.5, -0.5)
        lat_bot, lon_right = t.geo.pixel_to_latlon(w - 0.5, h - 0.5)
        boxes.append((lat_bot, lat_top, lon_left, lon_right))
    xrecs = [r for t, _, r in found if t.sensor == "X-BAND"]

    def shadowed(r: VesselRecord) -> bool:
        if any(b0 <= r.latitude <= b1 and l0 <= r.longitude <= l1 for b0, b1, l0, l1 in boxes):
            return True
        return any(haversine_km(r.latitude, r.longitude, x.latitude, x.longitude) * 1000
                   < max(60.0, 0.6 * max(r.estimated_length_m, x.estimated_length_m)) for x in xrecs)

    return [(t, d, r) for t, d, r in found if t.sensor == "X-BAND" or not shadowed(r)]


def _chip_png(tile: SceneTile, det: Detection) -> bytes:
    from PIL import Image

    half = CHIP_SIZE // 2
    r, c = int(round(det.py)), int(round(det.px))
    padded = np.pad(tile.image, half, mode="edge")
    chip = padded[r:r + CHIP_SIZE, c:c + CHIP_SIZE]
    buf = io.BytesIO()
    Image.fromarray(to_display_uint8(chip)).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _previews(tile: SceneTile, kept: list[tuple[Detection, VesselRecord]]) -> tuple[np.ndarray, np.ndarray]:
    """Downsampled raw SAR (uint8, HxW) and detection overlay (uint8, HxWx3)."""
    img = tile.image.astype(np.float32)
    f = max(1, int(math.ceil(max(img.shape) / PREVIEW_SIDE)))
    h, w = (img.shape[0] // f) * f, (img.shape[1] // f) * f
    small = img[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))
    raw = to_display_uint8(small)

    rgb = np.repeat(raw[..., None], 3, axis=2).astype(np.float32)
    if tile.land_mask is not None:
        land = tile.land_mask[:h:f, :w:f][: rgb.shape[0], : rgb.shape[1]]
        rgb[land] = rgb[land] * 0.45 + np.array([110.0, 85.0, 55.0]) * 0.55
    overlay = rgb.astype(np.uint8)
    H, W = raw.shape
    for det, rec in kept:
        colour = TYPE_COLOURS["ALERT" if rec.dark_candidate else rec.vessel_type]
        cy, cx = det.py / f, det.px / f
        half = max(4, int(det.length_m / tile.geo.pixel_m / f / 2) + 3)
        y0, y1 = int(max(cy - half, 0)), int(min(cy + half, H - 1))
        x0, x1 = int(max(cx - half, 0)), int(min(cx + half, W - 1))
        if y0 >= y1 or x0 >= x1:
            continue
        overlay[y0:y0 + 2, x0:x1 + 1] = colour
        overlay[y1 - 1:y1 + 1, x0:x1 + 1] = colour
        overlay[y0:y1 + 1, x0:x0 + 2] = colour
        overlay[y0:y1 + 1, x1 - 1:x1 + 1] = colour
    return raw, overlay


# --------------------------------------------------------------------------- main entry

def run_pass(
    port_id: str | None = None,
    sea_state: str = "MODERATE",
    pass_time_ist: str = "18:00",
    uploaded_image: bytes | None = None,
    output_path: str = DEFAULT_OUTPUT,
    *,
    seed: int | None = None,
    sensor_mode: str = "C-BAND",
) -> dict:
    """Run one onboard orbital pass and write the telemetry packet.

    Args:
        port_id: any ``port_id`` in ``data/ports_config.json`` (e.g. ``"IN-KRW"``); ``None`` = full
            constellation sweep over all ports (an upload with ``None`` is placed at IN-KRW).
        sea_state: ``"CALM"`` | ``"MODERATE"`` | ``"MONSOON_ROUGH"`` (UI labels like
            ``"Clear Calm"`` or ``"Monsoon Rough Sea"`` are accepted).
        pass_time_ist: ``"06:00"`` | ``"12:00"`` | ``"18:00"`` | ``"23:30"``.
        uploaded_image: optional raw bytes of a ``.tif`` / ``.png`` / ``.npy`` SAR scene.
        output_path: where to write the packet (relative paths are relative to the repo root).
        seed: optional RNG seed; by default each (pass time, sea state) is reproducible.
        sensor_mode: ``"C-BAND"`` (10 m, default) or ``"X-BAND"`` - adds a 3 m X-band
            spotlight over each harbour's inner approach for small craft (an uploaded image is
            then assumed to be 3 m GSD).

    Returns:
        ``{"telemetry": packet dict, "raw_preview": uint8 HxW, "overlay_preview": uint8 HxWx3,
        "xband_raw_preview": uint8 HxW | None, "xband_overlay_preview": uint8 HxWx3 | None,
        "alert_chips": list[PNG bytes], "simulated_ais": list[AIS record], "stats": {...}}``
        ``simulated_ais`` (also written to ``outputs/simulated_ais.json``) holds AIS reports for
        the simulated vessels; ~5-20 % are deliberately missing (dark vessels).
    """
    t_start = time.perf_counter()
    sea = normalize_sea_state(sea_state)
    pass_time = normalize_pass_time(pass_time_ist)
    sensor = normalize_sensor_mode(sensor_mode)
    if uploaded_image is not None and port_id is None:
        port_id = "IN-KRW"
    port_ids = [get_port(port_id)["port_id"]] if port_id else [p["port_id"] for p in load_ports_config()["ports"]]
    base_seed = seed if seed is not None else zlib.crc32(f"{pass_time}|{sea}".encode())
    pass_utc = _pass_datetime_utc(pass_time)
    ais_records: list[dict] = []

    classifier = get_classifier()
    summaries: list[dict] = []
    per_port: list[list[VesselRecord]] = []
    chips: list[bytes] = []
    chip_paths: list[str] = []
    raw_preview = overlay_preview = x_raw_preview = x_overlay_preview = None
    preview_port = None
    roi_bytes = 0
    xband_bytes = 0
    infer_s = 0.0
    n_candidates = n_clutter = 0

    CHIP_DIR.mkdir(parents=True, exist_ok=True)
    for old in CHIP_DIR.glob("*.png"):
        old.unlink()

    for idx, pid in enumerate(port_ids):
        port = get_port(pid)
        if uploaded_image is not None:
            gsd = XBAND_PIXEL_M if sensor == "X-BAND" else PIXEL_M
            orbital: OrbitalPass = load_uploaded_image(uploaded_image, port, sea, pass_time, pixel_m=gsd)
        else:
            orbital = simulate_pass(port, sea, pass_time, seed=(base_seed + 7919 * idx) % 2 ** 32,
                                    sensor_mode=sensor)
        roi_bytes += orbital.roi_bytes
        if uploaded_image is None:
            xband_bytes += sum(t.nbytes for t in orbital.tiles if t.sensor == "X-BAND")

        found: list[tuple[SceneTile, Detection, VesselRecord]] = []
        for tile in orbital.tiles:
            t0 = time.perf_counter()
            dets = detect_targets(tile.image, tile.cfar_mask, tile.geo.pixel_m, land_buffered=True)
            labels, conf = classify_detections(dets, classifier)
            infer_s += time.perf_counter() - t0
            n_candidates += len(dets)
            kept_here = []
            for det, label, c in zip(dets, labels, conf):
                if label == "CLUTTER":
                    n_clutter += 1
                    continue
                rec = georeference(det.px, det.py, det.length_m, tile.geo, port, label, float(c))
                if rec is not None:
                    found.append((tile, det, rec))
                    kept_here.append((det, rec))
            if raw_preview is None:
                raw_preview, overlay_preview = _previews(tile, kept_here)
                preview_port = pid
            elif tile.sensor == "X-BAND" and x_raw_preview is None and pid == preview_port:
                x_raw_preview, x_overlay_preview = _previews(tile, kept_here)

        found = _merge_sensors(found)
        records = [rec for _, _, rec in found]
        for n, rec in enumerate(sorted(records, key=_priority), start=1):
            rec.vessel_id = f"VSL-{port_code(pid)}-{n:03d}"
        summary = summarize_port(port, records)
        summaries.append(summary)
        per_port.append(records)

        critical = summary["congestion_status"] == "CRITICAL_CONGESTION"
        for tile, det, rec in sorted(found, key=lambda t: _priority(t[2])):
            if len(chips) >= MAX_CHIPS:
                break
            reason = ("DARK_VESSEL_OFFSHORE" if rec.dark_candidate else
                      "CRITICAL_CONGESTION" if critical and rec.zone == "ANCHORAGE_QUEUE" else None)
            if reason is None:
                continue
            png = _chip_png(tile, det)
            chips.append(png)
            path = CHIP_DIR / f"{rec.vessel_id}_{reason}.png"
            path.write_bytes(png)
            chip_paths.append(str(path.relative_to(ROOT)).replace("\\", "/"))

        # ---- purge the raw raster: nothing but kilobytes of insight leaves the satellite
        found.clear()
        ais_records.extend(simulated_ais_records(orbital, port, pass_utc,
                                                 seed=(base_seed + 7919 * idx + 1) % 2 ** 32))
        orbital.purge()
        del orbital
        gc.collect()

    raw_mb = round(FULL_SWATH_MB * len(port_ids) + xband_bytes / 1e6, 1)
    header = {
        "satellite_id": SATELLITE_ID,
        "sensor_mode": SENSOR_MODE_LABEL[sensor],
        "pass_timestamp_utc": pass_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "monitoring_limit_km": MONITORING_LIMIT_KM,
        "raw_scene_size_mb": raw_mb,
        "cropped_roi_size_mb": round(roi_bytes / 1e6, 1),
        "downlinked_json_size_kb": 0.0,
        "bandwidth_saved_pct": 0.0,
        "onboard_inference_time_ms": round(infer_s * 1000, 1),
        "sea_state": sea,
    }
    packet = build_packet(header, summaries, per_port)

    out = _resolve_output(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(packet, separators=(",", ":"), ensure_ascii=False)
    out.write_text(payload, encoding="utf-8")
    SIMULATED_AIS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIMULATED_AIS_PATH.write_text(json.dumps({
        "_note": "AIS reports for the vessels of the latest *simulated* pass (ground truth for the "
                 "synthetic scene; vessels missing here are dark). Empty for uploaded images.",
        "feed_source": "EDGEPORT_SIMULATOR_AIS",
        "generated_utc": pass_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "records": ais_records,
    }, indent=1), encoding="utf-8")

    detected = sum(len(v) for v in per_port)
    stats = {
        "raw_scene_size_mb": raw_mb,
        "cropped_roi_size_mb": header["cropped_roi_size_mb"],
        "downlinked_json_bytes": len(payload.encode("utf-8")),
        "bandwidth_saved_pct": header["bandwidth_saved_pct"],
        "onboard_inference_time_ms": header["onboard_inference_time_ms"],
        "total_runtime_ms": round((time.perf_counter() - t_start) * 1000, 1),
        "cfar_candidates": n_candidates,
        "rejected_as_clutter": n_clutter,
        "vessels_detected": detected,
        "vessels_in_packet": len(packet["vessels"]),
        "alert_chip_paths": chip_paths,
        "preview_port_id": preview_port,
        "output_path": str(out),
        "source": "UPLOAD" if uploaded_image is not None else "SIMULATED",
        "simulated_ais_records": len(ais_records),
        "sensor_mode": sensor,
    }
    log.info("Pass %s IST %s %s over %s: raw %.1f MB -> JSON %.2f KB (%.2f%% bandwidth saved), "
             "%d vessels detected, %d in packet, inference %.1f ms",
             pass_time, sea, sensor, ",".join(port_ids), raw_mb, len(payload) / 1024,
             stats["bandwidth_saved_pct"], detected, len(packet["vessels"]), stats["onboard_inference_time_ms"])
    return {
        "telemetry": packet,
        "raw_preview": raw_preview,
        "overlay_preview": overlay_preview,
        "xband_raw_preview": x_raw_preview,
        "xband_overlay_preview": x_overlay_preview,
        "alert_chips": chips,
        "simulated_ais": ais_records,
        "stats": stats,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EdgePort onboard orbital pass (Track B)")
    parser.add_argument("--port", default=None, help="a port_id from data/ports_config.json, e.g. IN-KRW (default: all)")
    parser.add_argument("--sea-state", default="MODERATE", help="CALM | MODERATE | MONSOON_ROUGH")
    parser.add_argument("--pass-time", default="18:00", help="06:00 | 12:00 | 18:00 | 23:30 (IST)")
    parser.add_argument("--upload", default=None, help="path to a .tif/.png/.npy SAR scene")
    parser.add_argument("--sensor-mode", default="C-BAND", help="C-BAND (10 m) | X-BAND (adds 3 m spotlight)")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger().handlers[0].addFilter(lambda r: r.name != "root")  # quantizer chatter
    upload = Path(args.upload).read_bytes() if args.upload else None
    result = run_pass(args.port, args.sea_state, args.pass_time, upload, args.output, seed=args.seed,
                      sensor_mode=args.sensor_mode)
    s = result["stats"]
    print(f"\n[{s['sensor_mode']}] Raw SAR {s['raw_scene_size_mb']} MB  ->  JSON "
          f"{s['downlinked_json_bytes'] / 1024:.2f} KB  "
          f"({s['bandwidth_saved_pct']}% bandwidth saved)")
    print(f"Onboard inference {s['onboard_inference_time_ms']} ms | {s['vessels_detected']} vessels detected, "
          f"{s['vessels_in_packet']} listed in packet, {s['rejected_as_clutter']} clutter rejected")
    for p in result["telemetry"]["ports_summary"]:
        c = p["counts"]
        print(f"  {p['port_id']}: {c['berth_occupied']}/{p['max_berths']} berths, {c['anchorage_waiting']} waiting, "
              f"{c['approaching_ferries']} ferries, {c['coastal_fishing_boats']} fishing, "
              f"{c['offshore_eez_vessels']} offshore -> {p['congestion_status']}")
    if s["alert_chip_paths"]:
        print("Alert chips: " + ", ".join(s["alert_chip_paths"]))
    print(f"Telemetry written to {s['output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
