"""Real Sentinel-1 data: fetch the newest real scene over each port and run the onboard detector.

Source: Copernicus Sentinel-1 IW, radiometrically terrain corrected (``sentinel-1-rtc``, 10 m,
VV gamma0) from Microsoft Planetary Computer — free, no account. Only the window around each
harbour (~20-25 km) is read from the Cloud-Optimized GeoTIFF over HTTP, never the whole
~1-2 GB scene, so a port takes ~10-15 s.

    python -m onboard_ai.real_sentinel1                 # all ports -> outputs/real_telemetry.json
    python -m onboard_ai.real_sentinel1 --port IN-KRW   # one port (refreshes its cache entry)

Per port, results are cached in ``outputs/real/<port_id>.json`` (+ raw / overlay preview PNGs);
:func:`build_real_packet` assembles the cached ports into one schema-valid telemetry packet.

There is no AIS for the real scenes yet, so ``ais_available`` is ``false`` in the header and
the ground station must not treat unmatched vessels as dark.

Attribution: contains modified Copernicus Sentinel data (processed by Microsoft Planetary
Computer); the detector's classifier is trained on OpenSARShip (CC-BY-NC-SA-4.0).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image

from onboard_ai.cfar_onnx_detector import classify_detections, detect_targets, get_classifier
from onboard_ai.edge_pipeline import (
    MONITORING_LIMIT_KM,
    _previews,
    _priority,
    build_packet,
    summarize_port,
)
from onboard_ai.georeferencer import (
    GeoTransform,
    coastline_lon,
    georeference,
    get_port,
    load_ports_config,
    port_code,
    seaward_lon_sign,
)
from onboard_ai.sar_simulator import SceneTile, estimate_land_mask

log = logging.getLogger("edgeport.onboard.real")

ROOT = Path(__file__).resolve().parents[1]
REAL_DIR = ROOT / "outputs" / "real"
REAL_PACKET_PATH = ROOT / "outputs" / "real_telemetry.json"

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
SAS_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{collection}"
COLLECTION = "sentinel-1-rtc"
PIXEL_M = 10.0
DN_SCALE = 10_000.0           # linear gamma0 -> uint16 DN for the onboard detector
WINDOW_MARGIN_KM = 4.0        # around the berth + anchorage boxes
SEAWARD_KM = 14.0             # approach sea included seaward of the quay
HTTP_TIMEOUT_S = 60
MAX_SCENE_AGE_DAYS = 180      # older scenes are reported, not shown as current
USER_AGENT = "EdgePort-hackathon/1.0"
ATTRIBUTION = ("Contains modified Copernicus Sentinel data, processed by Microsoft Planetary Computer. "
               "Vessel classifier trained on OpenSARShip (OSSDD), CC-BY-NC-SA-4.0.")

_token_lock = threading.Lock()
_token_cache: dict[str, tuple[str, float]] = {}


# --------------------------------------------------------------------------- catalogue + access

def _http_json(url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                           "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        return json.load(resp)


def sas_token(collection: str = COLLECTION) -> str:
    """Anonymous read token for the collection's blob storage (cached until near expiry)."""
    with _token_lock:
        tok, exp = _token_cache.get(collection, ("", 0.0))
        if time.time() < exp - 300:
            return tok
        payload = _http_json(SAS_TOKEN_URL.format(collection=collection))
        tok = payload["token"]
        expiry = payload.get("msft:expiry", "")
        try:
            exp = datetime.fromisoformat(expiry.replace("Z", "+00:00")).timestamp()
        except ValueError:
            exp = time.time() + 1800
        _token_cache[collection] = (tok, exp)
        return tok


def latest_scene(port: dict, limit: int = 1) -> list[dict]:
    """Newest Sentinel-1 RTC items whose footprint covers the harbour (newest first)."""
    d = 0.02
    body = {"collections": [COLLECTION],
            "bbox": [port["lon"] - d, port["lat"] - d, port["lon"] + d, port["lat"] + d],
            "limit": limit, "sortby": [{"field": "datetime", "direction": "desc"}]}
    return _http_json(STAC_SEARCH_URL, body).get("features", [])


def window_bounds(port: dict) -> tuple[float, float, float, float]:
    """Lon/lat window: berth + anchorage boxes with a margin, plus the approach sea."""
    b, a = port["berth_bbox"], port["anchorage_bbox"]
    min_lon, min_lat = min(b[0], a[0]), min(b[1], a[1])
    max_lon, max_lat = max(b[2], a[2]), max(b[3], a[3])
    lat = port["lat"]
    km_lon = 111.32 * math.cos(math.radians(lat))
    mlat, mlon = WINDOW_MARGIN_KM / 111.32, WINDOW_MARGIN_KM / km_lon
    min_lon, max_lon, min_lat, max_lat = min_lon - mlon, max_lon + mlon, min_lat - mlat, max_lat + mlat
    sea_edge = coastline_lon(port) + seaward_lon_sign(port) * SEAWARD_KM / km_lon
    land_edge = coastline_lon(port) - seaward_lon_sign(port) * 3.0 / km_lon
    if port["coast"] == "ISLAND":                     # sea on every side of the island
        return (coastline_lon(port) - 9.0 / km_lon, lat - 8.0 / 111.32,
                coastline_lon(port) + 9.0 / km_lon, lat + 8.0 / 111.32)
    return (min(min_lon, sea_edge, land_edge), min_lat, max(max_lon, sea_edge, land_edge), max_lat)


def read_window(href: str, bounds: tuple[float, float, float, float]) -> tuple[np.ndarray, GeoTransform]:
    """Read a 10 m EPSG:4326 window of a COG over HTTP (only the needed tiles are fetched)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.vrt import WarpedVRT

    min_lon, min_lat, max_lon, max_lat = bounds
    lat_c = (min_lat + max_lat) / 2
    width = int(round((max_lon - min_lon) * 111_320.0 * math.cos(math.radians(lat_c)) / PIXEL_M))
    height = int(round((max_lat - min_lat) * 111_320.0 / PIXEL_M))
    grid = transform_from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_MULTIRANGE="YES",
                      GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES", GDAL_HTTP_USERAGENT=USER_AGENT):
        with rasterio.open(href) as src, WarpedVRT(src, crs="EPSG:4326", transform=grid, width=width,
                                                    height=height, resampling=Resampling.bilinear,
                                                    src_nodata=src.nodata, nodata=0) as vrt:
            arr = vrt.read(1)          # areas outside the scene come back as 0 (no data)
    dlat, dlon = (max_lat - min_lat) / height, (max_lon - min_lon) / width
    geo = GeoTransform(origin_lat=max_lat - dlat / 2, origin_lon=min_lon + dlon / 2,
                       dlat=dlat, dlon=dlon, pixel_m=PIXEL_M)
    return np.nan_to_num(arr.astype(np.float32), nan=0.0, posinf=0.0, neginf=0.0), geo


def _remote_size_mb(href: str) -> float | None:
    try:
        req = urllib.request.Request(href, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return round(int(resp.headers.get("Content-Length", "0")) / 1e6, 1) or None
    except Exception:
        return None


def sea_state_from_backscatter(gamma0: np.ndarray, land: np.ndarray) -> str:
    """Rough sea state from the median sea-surface gamma0 (C-band VV, wind-roughened sea)."""
    sea = gamma0[(~land) & (gamma0 > 0)]
    if sea.size < 1000:
        return "MODERATE"
    med = float(np.median(sea))
    return "CALM" if med < 0.02 else "MODERATE" if med < 0.08 else "MONSOON_ROUGH"


# --------------------------------------------------------------------------- per-port processing

def _save_png(arr: np.ndarray, path: Path) -> str:
    Image.fromarray(arr).save(path, optimize=True)
    return str(path.relative_to(ROOT)).replace("\\", "/")


def process_port(port_id: str) -> dict:
    """Fetch the newest real scene over one port, detect vessels, cache and return the result."""
    port = get_port(port_id)
    REAL_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REAL_DIR / f"{port_id}.json"
    t0 = time.perf_counter()
    try:
        items = latest_scene(port)
    except Exception as exc:
        result = {"port_id": port_id, "status": "ERROR", "error": f"catalogue search failed: {exc}"}
        out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result
    if not items:
        result = {"port_id": port_id, "status": "NO_SCENE",
                  "error": "No Sentinel-1 scene in the catalogue covers this port."}
        out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result

    item = items[0]
    props = item["properties"]
    try:
        scene_time = datetime.fromisoformat(props["datetime"].replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - scene_time).days
    except (KeyError, ValueError):
        age_days = 0
    if age_days > MAX_SCENE_AGE_DAYS:
        result = {"port_id": port_id, "status": "NO_SCENE",
                  "error": f"Newest Sentinel-1 scene is from {props['datetime'][:10]} ({age_days} days old).",
                  "scene": {"id": item["id"], "datetime": props.get("datetime")}}
        out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result
    try:
        href = item["assets"]["vv"]["href"] + "?" + sas_token()
        gamma0, geo = read_window(href, window_bounds(port))
    except Exception as exc:
        result = {"port_id": port_id, "status": "ERROR", "error": f"scene read failed: {exc}",
                  "scene": {"id": item["id"], "datetime": props.get("datetime")}}
        out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result
    fetch_s = time.perf_counter() - t0
    valid = gamma0 > 0
    if valid.mean() < 0.2:
        result = {"port_id": port_id, "status": "NO_SCENE",
                  "error": "Newest scene only clips the edge of this port's window.",
                  "scene": {"id": item["id"], "datetime": props.get("datetime")}}
        out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        return result

    image = np.clip(gamma0 * DN_SCALE, 0, 65535).astype(np.uint16)
    land = estimate_land_mask(image) | ~valid             # no-data counts as "land": never searched
    tile = SceneTile("REAL", image, land, geo, [], sensor="C-BAND")

    t1 = time.perf_counter()
    dets = detect_targets(tile.image, tile.cfar_mask, PIXEL_M, land_buffered=True)
    labels, conf = classify_detections(dets, get_classifier())
    infer_ms = (time.perf_counter() - t1) * 1000

    kept = []
    for det, label, c in zip(dets, labels, conf):
        if label == "CLUTTER":
            continue
        rec = georeference(det.px, det.py, det.length_m, geo, port, label, float(c))
        if rec is not None:
            kept.append((det, rec))
    records = [rec for _, rec in kept]
    for n, rec in enumerate(sorted(records, key=_priority), start=1):
        rec.vessel_id = f"S1-{port_code(port_id)}-{n:03d}"

    raw_preview, overlay_preview = _previews(tile, kept)
    summary = summarize_port(port, records)
    summary.update({"observed_utc": props.get("datetime", "")[:19] + "Z", "data_source": "SENTINEL-1"})
    result = {
        "port_id": port_id,
        "status": "OK",
        "scene": {"id": item["id"], "datetime": props.get("datetime"), "platform": props.get("platform"),
                  "orbit_state": props.get("sat:orbit_state"), "full_scene_mb": _remote_size_mb(href)},
        "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sea_state": sea_state_from_backscatter(gamma0, land),
        "window": {"bounds": [round(v, 5) for v in window_bounds(port)], "shape": list(image.shape),
                   "roi_mb": round(image.nbytes / 1e6, 2)},
        "stats": {"cfar_candidates": len(dets), "rejected_as_clutter": len(dets) - len(kept),
                  "vessels_detected": len(records), "onboard_inference_time_ms": round(infer_ms, 1),
                  "fetch_s": round(fetch_s, 1)},
        "summary": summary,
        "vessels": [rec.to_packet() for rec in sorted(records, key=_priority)],
        "previews": {"raw": _save_png(raw_preview, REAL_DIR / f"{port_id}_raw.png"),
                     "overlay": _save_png(overlay_preview, REAL_DIR / f"{port_id}_overlay.png")},
    }
    out_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    tile.purge()
    log.info("%s: %s scene %s -> %d vessels (%.1fs fetch, %.0f ms inference)", port_id, props.get("platform"),
             props.get("datetime", "")[:10], len(records), fetch_s, infer_ms)
    return result


def load_cached(port_id: str) -> dict | None:
    path = REAL_DIR / f"{port_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------- packet assembly

def _empty_summary(port: dict, data_source: str) -> dict:
    """Summary for a port without a usable scene: "NOT_FETCHED" (no cache) or "NO_SCENE"."""
    s = summarize_port(port, [])
    s.update({"observed_utc": None, "data_source": data_source})
    return s


def build_real_packet(output_path: str | Path | None = None) -> dict:
    """Assemble cached per-port real results into one telemetry packet (< 200 KB)."""
    from onboard_ai.georeferencer import VesselRecord

    ports = load_ports_config()["ports"]
    summaries, per_port, observed, roi_mb, full_mb, infer_ms, states = [], [], [], 0.0, 0.0, 0.0, []
    n_ok = 0
    for port in ports:
        res = load_cached(port["port_id"])
        if not res or res.get("status") != "OK":
            summaries.append(_empty_summary(port, "NOT_FETCHED" if res is None else "NO_SCENE"))
            per_port.append([])
            continue
        n_ok += 1
        summary = res["summary"]
        summary["counts"].setdefault("coastal_cargo_vessels", sum(
            1 for v in res["vessels"] if v["vessel_type"] == "CARGO_VESSEL" and v["zone"] == "COASTAL_APPROACH"))
        summaries.append(summary)
        recs = []
        for v in res["vessels"]:
            rec = VesselRecord(port_id=v["port_id"], vessel_type=v["vessel_type"], latitude=v["latitude"],
                               longitude=v["longitude"], distance_from_shore_km=v["distance_from_shore_km"],
                               zone=v["zone"], estimated_length_m=v["estimated_length_m"], estimated_tonnage_mt=0.0,
                               confidence=v["confidence"], eta_minutes=v["eta_minutes"], px=0.0, py=0.0,
                               vessel_id=v["vessel_id"])
            recs.append(rec)
        per_port.append(recs)
        observed.append(res["scene"]["datetime"])
        roi_mb += res["window"]["roi_mb"]
        full_mb += res["scene"].get("full_scene_mb") or 0.0
        infer_ms += res["stats"]["onboard_inference_time_ms"]
        states.append(res.get("sea_state", "MODERATE"))

    if not observed:
        raise RuntimeError("No real Sentinel-1 results cached yet - run a refresh first.")
    newest = max(observed)
    header = {
        "satellite_id": "SENTINEL-1 (Copernicus)",
        "sensor_mode": "C-BAND_SAR_ALL_WEATHER",
        "pass_timestamp_utc": newest[:19] + "Z",
        "monitoring_limit_km": MONITORING_LIMIT_KM,
        "raw_scene_size_mb": round(full_mb, 1) if full_mb else round(roi_mb, 1),
        "cropped_roi_size_mb": round(roi_mb, 1),
        "downlinked_json_size_kb": 0.0,
        "bandwidth_saved_pct": 0.0,
        "onboard_inference_time_ms": round(infer_ms, 1),
        "sea_state": max(set(states), key=states.count) if states else "MODERATE",
        "data_source": "REAL_SENTINEL1",
        "ais_available": False,
        "ports_observed": n_ok,
        "oldest_scene_utc": (min(observed)[:19] + "Z") if observed else None,
        "attribution": ATTRIBUTION,
    }
    packet = build_packet(header, summaries, per_port)
    out = Path(output_path) if output_path else REAL_PACKET_PATH
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(packet, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    return packet


def refresh(port_ids: list[str] | None = None, workers: int = 4,
            progress: "callable | None" = None) -> dict:
    """Fetch + process ports (all by default) in parallel, then rebuild the real packet."""
    ids = port_ids or [p["port_id"] for p in load_ports_config()["ports"]]
    get_classifier()  # load once before the threads start
    done = 0
    results = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(process_port, ids):
            results[res["port_id"]] = res
            done += 1
            if progress:
                progress(done, len(ids), res)
    packet = build_real_packet()
    return {"results": results, "packet": packet}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the onboard detector on real Sentinel-1 scenes")
    parser.add_argument("--port", action="append", help="port_id (repeatable); default: all ports")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger().handlers[0].addFilter(lambda r: r.name not in ("root", "rasterio._env"))
    t = time.perf_counter()

    def progress(done, total, res):
        print(f"[{done:2d}/{total}] {res['port_id']}: {res['status']}"
              + (f" {res['scene']['datetime'][:10]} {res['stats']['vessels_detected']} vessels"
                 if res["status"] == "OK" else f" ({res.get('error', '')[:70]})"), flush=True)

    out = refresh(args.port, args.workers, progress)
    h = out["packet"]["telemetry_header"]
    print(f"\n{h['ports_observed']} ports observed, {len(out['packet']['vessels'])} vessels in packet, "
          f"{h['downlinked_json_size_kb']} KB, scenes {h['oldest_scene_utc'][:10] if h['oldest_scene_utc'] else '-'}"
          f" .. {h['pass_timestamp_utc'][:10]}, {time.perf_counter() - t:.0f}s total -> {REAL_PACKET_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
