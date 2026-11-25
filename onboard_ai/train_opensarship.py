"""Train the onboard vessel classifier on real Sentinel-1 ship chips (OpenSARShip / OSSDD).

Dataset: Hugging Face ``sylviaHoch/OpenSARShip-Ship-Detection-Dataset`` (WebDataset shards).
Download a few shards into ``data/opensarship/`` (git-ignored), e.g.::

    webdataset/train/train-000000.tar   (526 MB)
    webdataset/train/train-000001.tar   (526 MB)
    webdataset/test/test-000000.tar     (475 MB, held out for evaluation)

then run::

    python -m onboard_ai.train_opensarship

How real chips become training rows
-----------------------------------
Every VV chip (10 m Sentinel-1 IW GRD amplitude) is squared to intensity and pushed through
the *exact* onboard detector (land mask -> CA-CFAR -> blob features). Each CFAR blob is then
labelled against the dataset's rotated ship boxes:

* blob on an annotated ship  -> a vessel row. OpenSARShip has no ship-type labels, so the
  class is a **size class** from the rotated-box length, using the PRD's length bands
  (< 20 m FISHING_BOAT, 20-50 m FERRY, >= 50 m CARGO_VESSEL).
* blob on no annotated ship  -> a CLUTTER row (real sea clutter, sidelobes, harbour structures).
* blob on a ship cut off by the chip edge -> skipped (its true length is unknown).

Real rows are combined with the simulator rows (which also cover the 3 m X-band mode) and
the MLP is retrained, exported to ONNX and int8-quantized to
``onboard_ai/models/vessel_classifier_opensarship.onnx``. Both the simulator-only model and the
new model are then scored on the held-out test shard; the report is written next to the model.

License: OpenSARShip annotations are CC-BY-NC-SA-4.0 and the imagery is modified Copernicus
Sentinel data, so the resulting model is for non-commercial use, shared alike, with attribution:
"Contains modified Copernicus Sentinel data, processed and annotated by the dataset authors."
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import math
import struct
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np

from onboard_ai.cfar_onnx_detector import (
    CLASS_NAMES,
    MODEL_PATH,
    OPENSARSHIP_MODEL_PATH,
    Detection,
    VesselClassifier,
    classify_detections,
    detect_targets,
    train_and_export,
)
from onboard_ai.sar_simulator import estimate_land_mask

log = logging.getLogger("edgeport.onboard.opensarship")

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "opensarship"
REPORT_PATH = OPENSARSHIP_MODEL_PATH.with_name("opensarship_eval.json")
PIXEL_M = 10.0                 # Sentinel-1 IW GRD-HR pixel spacing
LAND_FRACTION_FOR_MASK = 0.01  # estimate a land mask when the chip has >= 1 % land
CLUTTER_PER_SHIP = 2.0         # cap real clutter rows at this multiple of real ship rows
REAL_ROW_WEIGHT = 2            # real rows are duplicated so they outweigh simulator rows
ATTRIBUTION = ("Contains modified Copernicus Sentinel data, processed and annotated by the "
               "OpenSARShip (OSSDD) dataset authors. Annotations CC-BY-NC-SA-4.0.")


# --------------------------------------------------------------------------- data reading

def read_float_tiff(data: bytes) -> np.ndarray:
    """Decode an uncompressed, strip-organised, single-band 32-bit float TIFF.

    The dataset's chips use photometric 34892 (LinearRaw), which Pillow refuses, so this small
    reader avoids an extra dependency. Raises ``ValueError`` on anything else.
    """
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        raise ValueError("not a classic TIFF")
    bo = "<" if data[:2] == b"II" else ">"
    ifd = struct.unpack(bo + "I", data[4:8])[0]
    (n,) = struct.unpack(bo + "H", data[ifd:ifd + 2])
    tags: dict[int, tuple] = {}
    for i in range(n):
        entry = data[ifd + 2 + 12 * i: ifd + 14 + 12 * i]
        tag, typ, cnt = struct.unpack(bo + "HHI", entry[:8])
        fmt = {3: "H", 4: "I"}.get(typ)
        if fmt is None:
            continue
        size = struct.calcsize(fmt) * cnt
        raw = entry[8:8 + size] if size <= 4 else data[struct.unpack(bo + "I", entry[8:12])[0]:][:size]
        tags[tag] = struct.unpack(bo + fmt * cnt, raw)
    if tags.get(259, (1,))[0] != 1 or tags.get(258, (0,))[0] != 32 or tags.get(339, (1,))[0] != 3:
        raise ValueError("only uncompressed float32 TIFFs are supported")
    w, h = tags[256][0], tags[257][0]
    raw = b"".join(data[o:o + c] for o, c in zip(tags[273], tags[279]))
    return np.frombuffer(raw, dtype=bo + "f4", count=w * h).reshape(h, w).astype(np.float32)


def iter_shard(path: Path) -> Iterator[dict[str, bytes]]:
    """Stream WebDataset samples from a .tar without extracting anything to disk."""
    current_key, sample = None, {}
    with tarfile.open(path, mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            key, _, fld = member.name.partition(".")
            if key != current_key and sample:
                yield {"__key__": current_key, **sample}
                sample = {}
            current_key = key
            fh = tar.extractfile(member)
            if fh is not None:
                sample[fld] = fh.read()
    if sample:
        yield {"__key__": current_key, **sample}


@dataclass
class Ship:
    cx: float
    cy: float
    length_px: float
    width_px: float
    at_edge: bool

    @property
    def length_m(self) -> float:
        return self.length_px * PIXEL_M

    @property
    def size_class(self) -> str:
        if self.length_m < 20.0:
            return "FISHING_BOAT"
        if self.length_m < 50.0:
            return "FERRY"
        return "CARGO_VESSEL"


def parse_rbb(text: str, shape: tuple[int, int]) -> list[Ship]:
    """Rotated boxes: ``index x1 y1 x2 y2 x3 y3 x4 y4 cx cy`` per line (first line = count)."""
    ships = []
    h, w = shape
    for line in text.strip().splitlines()[1:]:
        parts = [float(v) for v in line.replace("||", " ").split()]
        if len(parts) < 11:
            continue
        xs, ys = parts[1:9:2], parts[2:9:2]
        sides = [math.hypot(xs[(i + 1) % 4] - xs[i], ys[(i + 1) % 4] - ys[i]) for i in range(2)]
        at_edge = min(xs) <= 0 or min(ys) <= 0 or max(xs) >= w - 1 or max(ys) >= h - 1
        ships.append(Ship(parts[9], parts[10], max(sides), min(sides), at_edge))
    return ships


@dataclass
class ChipResult:
    key: str
    ships: list[Ship]
    dets: list[Detection]
    match: list[int]          # index into ships for each det, -1 = clutter
    area_km2: float
    features: np.ndarray = field(default_factory=lambda: np.zeros((0, 0), np.float32))


def process_sample(sample: dict[str, bytes]) -> ChipResult | None:
    try:
        amp = read_float_tiff(sample["vv.tif"])
    except (KeyError, ValueError) as exc:
        log.debug("skip %s: %s", sample.get("__key__"), exc)
        return None
    intensity = np.square(np.clip(np.nan_to_num(amp, nan=0.0), 0.0, None))
    meta = json.loads(sample.get("meta.json", b"{}") or b"{}")
    land = estimate_land_mask(intensity) if meta.get("land_fraction", 0.0) >= LAND_FRACTION_FOR_MASK else None
    ships = parse_rbb(sample.get("rbb.txt", b"0").decode("utf-8", "replace"), intensity.shape)
    dets = detect_targets(intensity, land, PIXEL_M)

    match = []
    for d in dets:
        best, best_d = -1, float("inf")
        for i, s in enumerate(ships):
            dist = math.hypot(d.px - s.cx, d.py - s.cy)
            if dist <= max(s.length_px, s.width_px) / 2 + 3 and dist < best_d:
                best, best_d = i, dist
        match.append(best)
    feats = np.stack([d.features() for d in dets]) if dets else np.zeros((0, 9), np.float32)
    area = intensity.size * (PIXEL_M / 1000) ** 2
    return ChipResult(sample["__key__"], ships, dets, match, area, feats)


def load_shards(paths: list[Path]) -> list[ChipResult]:
    chips = []
    for p in paths:
        t0 = time.perf_counter()
        n0 = len(chips)
        for sample in iter_shard(p):
            res = process_sample(sample)
            if res is not None:
                chips.append(res)
        log.info("%s: %d chips in %.1fs", p.name, len(chips) - n0, time.perf_counter() - t0)
    return chips


# --------------------------------------------------------------------------- training rows

def training_rows(chips: list[ChipResult], seed: int = 0) -> tuple[np.ndarray, np.ndarray, dict]:
    rng = np.random.default_rng(seed)
    ship_rows, ship_y, clutter_rows = [], [], []
    for c in chips:
        for f, m in zip(c.features, c.match):
            if m < 0:
                clutter_rows.append(f)
            elif not c.ships[m].at_edge:
                ship_rows.append(f)
                ship_y.append(CLASS_NAMES.index(c.ships[m].size_class))
    n_clutter_all = len(clutter_rows)
    cap = int(CLUTTER_PER_SHIP * max(len(ship_rows), 1))
    if len(clutter_rows) > cap:
        idx = rng.choice(len(clutter_rows), size=cap, replace=False)
        clutter_rows = [clutter_rows[i] for i in idx]
    X = np.vstack(ship_rows + clutter_rows).astype(np.float32) if (ship_rows or clutter_rows) else np.zeros((0, 9))
    y = np.array(ship_y + [CLASS_NAMES.index("CLUTTER")] * len(clutter_rows), dtype=np.int64)
    stats = {
        "real_ship_rows": len(ship_rows),
        "real_clutter_rows_used": len(clutter_rows),
        "real_clutter_rows_available": n_clutter_all,
        "real_rows_by_class": {CLASS_NAMES[k]: int((y == k).sum()) for k in range(len(CLASS_NAMES))},
    }
    return np.repeat(X, REAL_ROW_WEIGHT, axis=0), np.repeat(y, REAL_ROW_WEIGHT), stats


# --------------------------------------------------------------------------- evaluation

def evaluate(chips: list[ChipResult], clf: VesselClassifier) -> dict:
    """Object-level scores of one classifier on real held-out chips."""
    ships_total = cfar_hit = detected = 0
    false_alarms = vessel_calls = vessel_calls_on_ships = 0
    size_ok = size_n = 0
    length_err = []
    area = 0.0
    for c in chips:
        area += c.area_km2
        labels, _ = classify_detections(c.dets, clf)
        per_ship_hit = [False] * len(c.ships)
        per_ship_det = [False] * len(c.ships)
        per_ship_best: list[tuple[float, str] | None] = [None] * len(c.ships)
        for d, m, lab in zip(c.dets, c.match, labels):
            is_vessel = lab != "CLUTTER"
            vessel_calls += is_vessel
            if m < 0:
                false_alarms += is_vessel
                continue
            per_ship_hit[m] = True
            if is_vessel:
                vessel_calls_on_ships += 1
                per_ship_det[m] = True
                if per_ship_best[m] is None or d.length_m > per_ship_best[m][0]:
                    per_ship_best[m] = (d.length_m, lab)
        for s, hit, det, best in zip(c.ships, per_ship_hit, per_ship_det, per_ship_best):
            ships_total += 1
            cfar_hit += hit
            detected += det
            if best is not None and not s.at_edge:
                size_n += 1
                size_ok += best[1] == s.size_class
                length_err.append(abs(best[0] - s.length_m))
    return {
        "chips": len(chips),
        "area_km2": round(area, 1),
        "ships": ships_total,
        "cfar_recall": round(cfar_hit / max(ships_total, 1), 3),
        "ship_recall": round(detected / max(ships_total, 1), 3),
        "precision": round(vessel_calls_on_ships / max(vessel_calls, 1), 3),
        "false_alarms": false_alarms,
        "false_alarms_per_100_km2": round(100 * false_alarms / max(area, 1e-9), 2),
        "size_class_accuracy": round(size_ok / max(size_n, 1), 3),
        "median_length_error_m": round(float(np.median(length_err)), 1) if length_err else None,
    }


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train the EdgePort classifier on OpenSARShip chips")
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    parser.add_argument("--train-glob", default="train-*.tar")
    parser.add_argument("--test-glob", default="test-*.tar")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    logging.getLogger().handlers[0].addFilter(lambda r: r.name != "root")  # quantizer chatter

    data_dir = Path(args.data_dir)
    train_paths = sorted(data_dir.glob(args.train_glob))
    test_paths = sorted(data_dir.glob(args.test_glob))
    if not train_paths:
        raise SystemExit(f"No training shards matching {args.train_glob} in {data_dir} - see module docstring.")

    log.info("Reading %d training shard(s)", len(train_paths))
    train_chips = load_shards(train_paths)
    X_real, y_real, row_stats = training_rows(train_chips)
    log.info("Real training rows: %s", row_stats)

    train_and_export(OPENSARSHIP_MODEL_PATH, extra_data=(X_real, y_real),
                     doc="OpenSARShip real Sentinel-1 chips + simulator chips")
    report = {
        "dataset": "sylviaHoch/OpenSARShip-Ship-Detection-Dataset (Hugging Face)",
        "attribution": ATTRIBUTION,
        "train_shards": [p.name for p in train_paths],
        "test_shards": [p.name for p in test_paths],
        "train_chips": len(train_chips),
        "training_rows": row_stats,
        "size_class_note": "OpenSARShip has no ship-type labels; classes are length bands "
                           "(<20 m fishing, 20-50 m ferry, >=50 m cargo) from the rotated boxes.",
    }

    if test_paths:
        log.info("Evaluating on %d held-out test shard(s)", len(test_paths))
        test_chips = load_shards(test_paths)
        if not MODEL_PATH.exists():
            train_and_export(MODEL_PATH)
        report["test_simulator_only_model"] = evaluate(test_chips, VesselClassifier(MODEL_PATH))
        report["test_opensarship_model"] = evaluate(test_chips, VesselClassifier(OPENSARSHIP_MODEL_PATH))

    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k.startswith(("training", "test_"))}, indent=2))
    print(f"\nModel  -> {OPENSARSHIP_MODEL_PATH}\nReport -> {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
