"""Onboard vessel detection: land mask + 2D CA-CFAR + quantized ONNX classifier (PRD Step B).

1. **Land mask** - the cached coastline mask (dilated by a small buffer) removes quay cranes,
   breakwaters and buildings before anything else runs.
2. **Cell-Averaging CFAR** - fully vectorized with ``scipy.ndimage.uniform_filter``: the local
   sea mean is averaged over a training ring (outer window minus guard window), using sea
   pixels only, and a pixel is a hit when ``I > k x local_sea_mean``. ``k`` is set for a fixed
   false-alarm rate from the Gamma speckle model, with the number of looks estimated from the
   scene itself so rough monsoon seas automatically get a higher threshold.
3. **Connected components** - ``scipy.ndimage.label`` groups hits; per-blob features are length,
   width, area, peak and integrated signal-to-clutter ratio, and elongation.
4. **ONNX classifier** - a small MLP (FISHING_BOAT / FERRY / CARGO_VESSEL / CLUTTER) run with
   ONNX Runtime. On first boot it is trained on simulator-rendered chips pushed through this
   exact detector, exported with skl2onnx and dynamically int8-quantized, so the repo ships
   with zero weight files.
"""
from __future__ import annotations

import logging
import math
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import ndimage
from scipy.stats import gamma

from onboard_ai.sar_simulator import (
    PIXEL_M,
    SEA_STATES,
    VESSEL_LENGTH_M,
    VESSEL_RCS_DENSITY,
    render_vessel,
    sea_clutter,
    vessel_width_px,
)

log = logging.getLogger("edgeport.onboard.detector")

MODEL_PATH = Path(__file__).resolve().parent / "models" / "vessel_classifier.onnx"
CLASS_NAMES = ("CLUTTER", "FISHING_BOAT", "FERRY", "CARGO_VESSEL")
FEATURE_NAMES = ("length_m", "width_m", "log10_area_m2", "log10_peak_scr", "log10_sum_scr", "elongation",
                 "log10_excess_over_threshold", "log10_neighbour_scr")
MIN_CONFIDENCE = 0.5    # detections below this classifier confidence are dropped as clutter
# Single-pixel FISHING_BOAT calls are indistinguishable from speckle spikes at 10 m C-band
# resolution, so they need a higher confidence (trades a little small-boat recall for far
# fewer false alarms).
MIN_CONFIDENCE_FISHING = float(os.environ.get("EDGEPORT_MIN_CONF_FISHING", "0.7"))
N_FEATURES = len(FEATURE_NAMES)

GUARD_WIN = 31          # px (310 m) - larger than half the biggest ship
TRAIN_WIN = 71          # px (710 m)
PFA = 1e-6              # design false-alarm probability per pixel
LAND_BUFFER_PX = 2
MIN_VALID_TRAIN_FRACTION = 0.25


@dataclass
class Detection:
    px: float               # intensity-weighted centroid (column)
    py: float               # intensity-weighted centroid (row)
    area_px: int
    length_m: float
    width_m: float
    heading_deg: float      # 0-180, major-axis orientation from north
    peak_scr: float         # peak / local sea mean
    sum_scr: float          # integrated signal / local sea mean (RCS proxy)
    bbox: tuple[int, int, int, int]  # (y0, x0, y1, x1) pixel slice bounds
    cfar_k: float = 1.0     # operating CFAR multiplier for this scene (sea-state proxy)
    neighbour_scr: float = 8.0  # summed SCR of the 8 pixels around the peak (8 ~ pure speckle)
    pixel_m: float = PIXEL_M

    def features(self) -> np.ndarray:
        """Feature vector in :data:`FEATURE_NAMES` order."""
        return np.array([
            self.length_m,
            self.width_m,
            math.log10(max(self.area_px * self.pixel_m ** 2, 1.0)),
            math.log10(max(self.peak_scr, 1e-3)),
            math.log10(max(self.sum_scr, 1e-3)),
            self.length_m / max(self.width_m, 1e-6),
            math.log10(max(self.peak_scr / self.cfar_k, 1e-3)),
            math.log10(max(self.neighbour_scr, 1e-3)),
        ], dtype=np.float32)


def estimate_looks(img: np.ndarray, sea_mask: np.ndarray) -> float:
    """Method-of-moments equivalent number of looks of the sea clutter (1-12)."""
    vals = img[sea_mask][::7].astype(np.float64)
    if vals.size < 100:
        return 3.0
    vals = vals[vals <= np.quantile(vals, 0.999)]  # drop ships / outliers
    m, v = vals.mean(), vals.var()
    return float(np.clip(m * m / max(v, 1e-9), 1.0, 12.0))


def cfar_factor(looks: float, pfa: float = PFA) -> float:
    """Threshold multiplier ``k`` so that P(I > k*mean) = pfa for unit-mean Gamma(looks) speckle."""
    return float(gamma.ppf(1.0 - pfa, a=looks, scale=1.0 / looks))


def ca_cfar(img: np.ndarray, land_mask: np.ndarray | None = None, pfa: float = PFA,
            guard: int = GUARD_WIN, train: int = TRAIN_WIN) -> tuple[np.ndarray, np.ndarray, float]:
    """Vectorized 2D CA-CFAR. Returns ``(hits, local_sea_mean, k)``."""
    x = img.astype(np.float32)
    if land_mask is None:
        sea = np.ones(x.shape, dtype=bool)
    else:
        sea = ~ndimage.binary_dilation(land_mask, iterations=LAND_BUFFER_PX)
    seaf = sea.astype(np.float32)
    xs = x * seaf

    n_o, n_g = float(train * train), float(guard * guard)
    sum_o = ndimage.uniform_filter(xs, train, mode="reflect") * n_o
    sum_g = ndimage.uniform_filter(xs, guard, mode="reflect") * n_g
    cnt_o = ndimage.uniform_filter(seaf, train, mode="reflect") * n_o
    cnt_g = ndimage.uniform_filter(seaf, guard, mode="reflect") * n_g
    ring_sum, ring_cnt = sum_o - sum_g, cnt_o - cnt_g

    global_mean = float(xs.sum() / max(seaf.sum(), 1.0))
    valid = ring_cnt >= MIN_VALID_TRAIN_FRACTION * (n_o - n_g)
    local_mean = np.where(valid, ring_sum / np.maximum(ring_cnt, 1.0), global_mean).astype(np.float32)
    local_mean = np.maximum(local_mean, 1e-3)

    k = cfar_factor(estimate_looks(x, sea), pfa)
    hits = (x > k * local_mean) & sea
    return hits, local_mean, k


def detect_targets(img: np.ndarray, land_mask: np.ndarray | None = None, pixel_m: float = PIXEL_M,
                   pfa: float = PFA) -> list[Detection]:
    """Land mask -> CA-CFAR -> connected components -> per-blob features."""
    hits, local_mean, k = ca_cfar(img, land_mask, pfa)
    labels, n = ndimage.label(hits, structure=np.ones((3, 3), dtype=bool))
    if n == 0:
        return []
    x = img.astype(np.float32)
    h, w = img.shape
    dets: list[Detection] = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        sub = labels[sl] == i
        ys, xs = np.nonzero(sub)
        vals = x[sl][sub]
        ys = ys + sl[0].start
        xs = xs + sl[1].start
        wts = vals / vals.sum()
        cy, cx = float((ys * wts).sum()), float((xs * wts).sum())

        cov = np.cov(np.vstack([xs, ys]).astype(np.float64), bias=True) if ys.size > 1 else np.zeros((2, 2))
        cov = cov + np.eye(2) / 12.0               # finite pixel size
        evals, evecs = np.linalg.eigh(cov)         # ascending
        length_px = 4.0 * math.sqrt(max(evals[1], 0.0))
        width_px = 4.0 * math.sqrt(max(evals[0], 0.0))
        vx, vy = evecs[:, 1]
        heading = math.degrees(math.atan2(vx, -vy)) % 180.0

        lm = float(local_mean[min(int(round(cy)), h - 1), min(int(round(cx)), w - 1)])
        j = int(np.argmax(vals))
        pr, pc = int(ys[j]), int(xs[j])
        win = x[max(pr - 1, 0):pr + 2, max(pc - 1, 0):pc + 2]
        neighbour_scr = (float(win.sum()) - float(vals[j])) / lm
        det = Detection(
            px=cx, py=cy, area_px=int(ys.size),
            length_m=length_px * pixel_m, width_m=width_px * pixel_m, heading_deg=heading,
            peak_scr=float(vals.max()) / lm, sum_scr=float(vals.sum()) / lm,
            bbox=(sl[0].start, sl[1].start, sl[0].stop, sl[1].stop),
            cfar_k=k, neighbour_scr=neighbour_scr, pixel_m=pixel_m,
        )
        dets.append(det)
    return dets


# --------------------------------------------------------------------------- classifier

def build_training_set(n_per_class: int = 260, seed: int = 1234) -> tuple[np.ndarray, np.ndarray]:
    """Render labelled chips with the simulator and push them through :func:`detect_targets`,
    so training features have exactly the same distribution as onboard features."""
    rng = np.random.default_rng(seed)
    states = list(SEA_STATES.values())
    X: list[np.ndarray] = []
    y: list[int] = []
    size = 128
    for label, cls in enumerate(CLASS_NAMES):
        if cls == "CLUTTER":
            continue
        got = tries = 0
        while got < n_per_class and tries < 4 * n_per_class:
            tries += 1
            chip = sea_clutter(rng, (size, size), states[rng.integers(len(states))])
            length = rng.uniform(*VESSEL_LENGTH_M[cls])
            cx, cy = size / 2 + rng.uniform(-0.5, 0.5), size / 2 + rng.uniform(-0.5, 0.5)
            render_vessel(chip, cx, cy, length / PIXEL_M, vessel_width_px(length), rng.uniform(0, 360),
                          rng.uniform(*VESSEL_RCS_DENSITY[cls]), rng)
            dets = detect_targets(chip, None, PIXEL_M)
            if not dets:
                continue
            best = min(dets, key=lambda d: (d.px - cx) ** 2 + (d.py - cy) ** 2)
            if (best.px - cx) ** 2 + (best.py - cy) ** 2 > (3 + length / PIXEL_M / 2) ** 2:
                continue
            X.append(best.features())
            y.append(label)
            got += 1

    # Clutter: genuine false alarms mined from empty seas at the operating threshold (mostly
    # monsoon seas, where wave texture breaks the pure-speckle model).
    clutter: list[np.ndarray] = []
    mining_states = [SEA_STATES["MONSOON_ROUGH"]] * 3 + [SEA_STATES["MODERATE"], SEA_STATES["CALM"]]
    for attempt in range(600):
        if len(clutter) >= n_per_class:
            break
        chip = sea_clutter(rng, (512, 512), mining_states[attempt % len(mining_states)])
        clutter.extend(d.features() for d in detect_targets(chip, None, PIXEL_M))
    clutter = clutter[:n_per_class]
    X.extend(clutter)
    y.extend([CLASS_NAMES.index("CLUTTER")] * len(clutter))
    return np.vstack(X).astype(np.float32), np.asarray(y, dtype=np.int64)


def train_and_export(path: Path = MODEL_PATH, seed: int = 1234) -> Path:
    """Train the MLP, export to ONNX with skl2onnx and int8-quantize it with ONNX Runtime."""
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    X, y = build_training_set(seed=seed)
    model = Pipeline([
        ("scale", StandardScaler()),
        ("mlp", MLPClassifier(hidden_layer_sizes=(32, 16), max_iter=1500, random_state=seed)),
    ])
    model.fit(X, y)
    acc = float((model.predict(X) == y).mean())
    log.info("Trained vessel classifier on %d samples (train acc %.3f, class counts %s)",
             len(y), acc, np.bincount(y, minlength=len(CLASS_NAMES)).tolist())

    onx = convert_sklearn(
        model,
        initial_types=[("features", FloatTensorType([None, N_FEATURES]))],
        options={MLPClassifier: {"zipmap": False}},
        target_opset={"": 17, "ai.onnx.ml": 3},
    )
    onx.doc_string = "EdgePort vessel classifier. Classes: " + ",".join(CLASS_NAMES)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, float_path = tempfile.mkstemp(suffix=".onnx", dir=path.parent)
    os.close(fd)
    try:
        with open(float_path, "wb") as fh:
            fh.write(onx.SerializeToString())
        try:
            from onnxruntime.quantization import QuantType, quantize_dynamic

            quantize_dynamic(float_path, str(path), weight_type=QuantType.QInt8)
            log.info("Exported int8-quantized ONNX classifier -> %s", path)
        except Exception as exc:  # quantization is an optimisation, never a hard failure
            log.warning("int8 quantization failed (%s); keeping float32 ONNX model", exc)
            os.replace(float_path, path)
    finally:
        if os.path.exists(float_path):
            os.remove(float_path)
    return path


class VesselClassifier:
    """ONNX Runtime wrapper; creates the model on first use if it does not exist."""

    def __init__(self, path: Path = MODEL_PATH):
        import onnxruntime as ort

        if not Path(path).exists():
            log.info("No ONNX weights found - generating %s on first boot", path)
            train_and_export(Path(path))
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        if self.session.get_inputs()[0].shape[-1] != N_FEATURES:  # stale model from an older build
            log.info("ONNX model expects a different feature set - regenerating %s", path)
            del self.session
            train_and_export(Path(path))
            self.session = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self.input_name = self.session.get_inputs()[0].name
        self.path = Path(path)

    def predict(self, features: np.ndarray) -> tuple[list[str], np.ndarray]:
        """Return ``(class names, confidence)`` for an ``(n, N_FEATURES)`` array."""
        if len(features) == 0:
            return [], np.zeros(0, dtype=np.float32)
        labels, probs = self.session.run(None, {self.input_name: np.asarray(features, dtype=np.float32)})
        probs = np.asarray(probs, dtype=np.float32)
        conf = probs.max(axis=1)
        names = []
        for i, c in zip(labels, conf):
            name = CLASS_NAMES[int(i)]
            floor = MIN_CONFIDENCE_FISHING if name == "FISHING_BOAT" else MIN_CONFIDENCE
            names.append(name if c >= floor else "CLUTTER")
        return names, conf


@lru_cache(maxsize=1)
def get_classifier() -> VesselClassifier:
    """Process-wide cached classifier (Streamlit reruns reuse the same ONNX session)."""
    return VesselClassifier()
