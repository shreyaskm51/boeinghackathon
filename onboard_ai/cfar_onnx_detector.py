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
    LAND_BUFFER_PX,
    PIXEL_M,
    bright_scatterer_count,
    buffer_land_mask,
    SEA_STATES,
    VESSEL_LENGTH_M,
    VESSEL_RCS_DENSITY,
    XBAND_PIXEL_M,
    render_vessel,
    sea_clutter,
    vessel_width_px,
)

log = logging.getLogger("edgeport.onboard.detector")

MODEL_PATH = Path(__file__).resolve().parent / "models" / "vessel_classifier.onnx"
# Trained on real Sentinel-1 chips (OpenSARShip) + simulator chips by onboard_ai/train_opensarship.py.
# Committed to the repo because it cannot be regenerated without the ~1.5 GB dataset download;
# preferred over the simulator-only MODEL_PATH whenever it is present and compatible.
OPENSARSHIP_MODEL_PATH = MODEL_PATH.with_name("vessel_classifier_opensarship.onnx")
CLASS_NAMES = ("CLUTTER", "FISHING_BOAT", "FERRY", "CARGO_VESSEL")
FEATURE_NAMES = ("length_m", "width_m", "log10_area_m2", "log10_peak_scr", "log10_sum_scr", "elongation",
                 "log10_excess_over_threshold", "log10_neighbour_scr", "log10_pixel_m")
TRAINING_GSDS_M = (PIXEL_M, XBAND_PIXEL_M)   # classifier is trained for C-band and X-band
MIN_CONFIDENCE = 0.5    # detections below this classifier confidence are dropped as clutter
# Single-pixel FISHING_BOAT calls are indistinguishable from speckle spikes at 10 m C-band
# resolution, so they need a higher confidence (trades a little small-boat recall for far
# fewer false alarms).
MIN_CONFIDENCE_FISHING = float(os.environ.get("EDGEPORT_MIN_CONF_FISHING", "0.7"))
# Physics gate: the smallest vessel class is 10 m, so a blob measured shorter than this cannot
# be a vessel. At 10 m GSD a single pixel already measures ~11.5 m, so this only bites at
# high resolution (X-band), where lone speckle pixels measure ~3.5 m.
MIN_VESSEL_LENGTH_M = 6.0
N_FEATURES = len(FEATURE_NAMES)

GUARD_M = 310.0         # guard window (m) - wider than the biggest ship
TRAIN_M = 710.0         # training window (m)
BACKGROUND_DECIMATION_M = 40.0  # sea background is estimated on a ~40 m grid (fast, smooth)
PFA = 1e-6              # design false-alarm probability per pixel
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
            math.log10(self.pixel_m),
        ], dtype=np.float32)


def estimate_looks(img: np.ndarray, sea_mask: np.ndarray) -> float:
    """Method-of-moments equivalent number of looks of the sea clutter (1-12)."""
    vals = img[::4, ::4][sea_mask[::4, ::4]].astype(np.float64)
    if vals.size < 100:
        return 3.0
    cut = vals.size - max(vals.size // 1000, 1)
    vals = np.partition(vals, cut)[:cut]           # drop the brightest 0.1 % (ships / outliers)
    m, v = vals.mean(), vals.var()
    return float(np.clip(m * m / max(v, 1e-9), 1.0, 12.0))


def cfar_factor(looks: float, pfa: float = PFA) -> float:
    """Threshold multiplier ``k`` so that P(I > k*mean) = pfa for unit-mean Gamma(looks) speckle."""
    return float(gamma.ppf(1.0 - pfa, a=looks, scale=1.0 / looks))


def _odd(n: float, minimum: int = 3) -> int:
    n = max(int(round(n)), minimum)
    return n if n % 2 else n + 1


def _pad_to(a: np.ndarray, f: int) -> np.ndarray:
    ph, pw = (-a.shape[0]) % f, (-a.shape[1]) % f
    return np.pad(a, ((0, ph), (0, pw)), mode="edge") if (ph or pw) else a


def _block_mean(a: np.ndarray, f: int) -> np.ndarray:
    """Mean over ``f x f`` blocks (input already a multiple of ``f``)."""
    H, W = a.shape
    return a.reshape(H // f, f, W // f, f).mean(axis=(1, 3), dtype=np.float32)


@dataclass
class Background:
    """Coarse CA-CFAR sea-clutter background (one value per ``f x f`` block)."""

    mean: np.ndarray   # (H/f, W/f) float32
    f: int

    def at(self, row: float, col: float) -> float:
        r = min(max(int(row) // self.f, 0), self.mean.shape[0] - 1)
        c = min(max(int(col) // self.f, 0), self.mean.shape[1] - 1)
        return float(self.mean[r, c])

    def full(self, shape: tuple[int, int]) -> np.ndarray:
        """Full-resolution background (for display / debugging only)."""
        return np.repeat(np.repeat(self.mean, self.f, 0), self.f, 1)[: shape[0], : shape[1]]


def ca_cfar(img: np.ndarray, land_mask: np.ndarray | None = None, pfa: float = PFA,
            pixel_m: float = PIXEL_M, land_buffered: bool = False) -> tuple[np.ndarray, Background, float]:
    """Vectorized 2D CA-CFAR. Returns ``(hits, background, k)``.

    The training-ring sea mean is a smooth field, so it is computed on a decimated grid
    (~40 m cells); every full-resolution pixel is still tested against its block's value.
    Windows are specified in metres so C-band (10 m) and X-band (3 m) behave identically.
    Pass ``land_buffered=True`` with a cached, already-buffered coastline mask (onboard case).
    """
    h, w = img.shape
    f = max(1, int(round(BACKGROUND_DECIMATION_M / pixel_m)))
    x = _pad_to(img, f).astype(np.float32)
    if land_mask is None or not land_mask.any():
        sea = np.ones(x.shape, dtype=bool)
    else:
        sea = ~_pad_to(land_mask if land_buffered else buffer_land_mask(land_mask), f)
    seaf = sea.astype(np.float32)

    xs_r = _block_mean(x * seaf, f)          # sea-only intensity, block means
    sea_r = _block_mean(seaf, f)             # sea fraction per block
    guard = _odd(GUARD_M / pixel_m / f)
    train = _odd(TRAIN_M / pixel_m / f, minimum=guard + 2)

    n_o, n_g = float(train * train), float(guard * guard)
    ring_sum = (ndimage.uniform_filter(xs_r, train, mode="reflect") * n_o
                - ndimage.uniform_filter(xs_r, guard, mode="reflect") * n_g)
    ring_cnt = (ndimage.uniform_filter(sea_r, train, mode="reflect") * n_o
                - ndimage.uniform_filter(sea_r, guard, mode="reflect") * n_g)

    global_mean = float((xs_r * f * f).sum() / max(float(sea_r.sum()) * f * f, 1.0))
    valid = ring_cnt >= MIN_VALID_TRAIN_FRACTION * (n_o - n_g)
    local_r = np.where(valid, ring_sum / np.maximum(ring_cnt, 1e-6), global_mean).astype(np.float32)
    local_r = np.maximum(local_r, 1e-3)

    k = cfar_factor(estimate_looks(x, sea), pfa)
    H, W = x.shape
    thr = (k * local_r)[:, None, :, None]    # broadcast block thresholds, no full-res copy
    hits = ((x.reshape(H // f, f, W // f, f) > thr).reshape(H, W) & sea)[:h, :w]
    return hits, Background(local_r, f), k


def detect_targets(img: np.ndarray, land_mask: np.ndarray | None = None, pixel_m: float = PIXEL_M,
                   pfa: float = PFA, land_buffered: bool = False) -> list[Detection]:
    """Land mask -> CA-CFAR -> connected components -> per-blob features."""
    hits, background, k = ca_cfar(img, land_mask, pfa, pixel_m, land_buffered)
    labels, n = ndimage.label(hits, structure=np.ones((3, 3), dtype=bool))
    if n == 0:
        return []
    h, w = img.shape
    dets: list[Detection] = []
    for i, sl in enumerate(ndimage.find_objects(labels), start=1):
        if sl is None:
            continue
        sub = labels[sl] == i
        ys, xs = np.nonzero(sub)
        vals = img[sl][sub].astype(np.float32)
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

        lm = max(background.at(round(cy), round(cx)), 1e-3)
        j = int(np.argmax(vals))
        pr, pc = int(ys[j]), int(xs[j])
        win = img[max(pr - 1, 0):pr + 2, max(pc - 1, 0):pc + 2].astype(np.float32)
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
    for gsd in TRAINING_GSDS_M:
        # chip must hold the full CFAR training window around the target
        size = int(math.ceil(max(128.0, (TRAIN_M + 120.0) / gsd) / 8) * 8)
        for label, cls in enumerate(CLASS_NAMES):
            if cls == "CLUTTER":
                continue
            got = tries = 0
            while got < n_per_class and tries < 4 * n_per_class:
                tries += 1
                chip = sea_clutter(rng, (size, size), states[rng.integers(len(states))])
                length = rng.uniform(*VESSEL_LENGTH_M[cls])
                cx, cy = size / 2 + rng.uniform(-0.5, 0.5), size / 2 + rng.uniform(-0.5, 0.5)
                render_vessel(chip, cx, cy, length / gsd, vessel_width_px(length, gsd), rng.uniform(0, 360),
                              rng.uniform(*VESSEL_RCS_DENSITY[cls]), rng,
                              n_scatterers=bright_scatterer_count(cls, length))
                dets = detect_targets(chip, None, gsd)
                if not dets:
                    continue
                best = min(dets, key=lambda d: (d.px - cx) ** 2 + (d.py - cy) ** 2)
                if (best.px - cx) ** 2 + (best.py - cy) ** 2 > (3 + length / gsd / 2) ** 2:
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
            clutter.extend(d.features() for d in detect_targets(chip, None, gsd))
        clutter = clutter[:n_per_class]
        X.extend(clutter)
        y.extend([CLASS_NAMES.index("CLUTTER")] * len(clutter))
    return np.vstack(X).astype(np.float32), np.asarray(y, dtype=np.int64)


def train_and_export(path: Path = MODEL_PATH, seed: int = 1234,
                     extra_data: tuple[np.ndarray, np.ndarray] | None = None,
                     doc: str = "simulator chips") -> Path:
    """Train the MLP, export to ONNX with skl2onnx and int8-quantize it with ONNX Runtime.

    ``extra_data`` = ``(X, y)`` rows (e.g. real OpenSARShip detections) appended to the
    simulator training set.
    """
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    X, y = build_training_set(seed=seed)
    if extra_data is not None and len(extra_data[1]):
        X = np.vstack([X, np.asarray(extra_data[0], dtype=np.float32)])
        y = np.concatenate([y, np.asarray(extra_data[1], dtype=np.int64)])
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
    onx.doc_string = f"EdgePort vessel classifier ({doc}). Classes: " + ",".join(CLASS_NAMES)
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


def classify_detections(dets: list[Detection], classifier: "VesselClassifier | None" = None
                        ) -> tuple[list[str], np.ndarray]:
    """ONNX classification plus the physical size gate. Returns ``(labels, confidence)``."""
    if not dets:
        return [], np.zeros(0, dtype=np.float32)
    clf = classifier or get_classifier()
    labels, conf = clf.predict(np.stack([d.features() for d in dets]))
    labels = ["CLUTTER" if d.length_m < MIN_VESSEL_LENGTH_M else lab for d, lab in zip(dets, labels)]
    return labels, conf


def _model_input_width(path: Path) -> int | None:
    try:
        import onnxruntime as ort

        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        return int(sess.get_inputs()[0].shape[-1])
    except Exception:
        return None


@lru_cache(maxsize=1)
def get_classifier() -> VesselClassifier:
    """Process-wide cached classifier (Streamlit reruns reuse the same ONNX session).

    Uses the committed OpenSARShip-trained model when present and compatible, otherwise the
    simulator-only model (generated on first boot).
    """
    if OPENSARSHIP_MODEL_PATH.exists():
        if _model_input_width(OPENSARSHIP_MODEL_PATH) == N_FEATURES:
            log.info("Using OpenSARShip-trained classifier %s", OPENSARSHIP_MODEL_PATH.name)
            return VesselClassifier(OPENSARSHIP_MODEL_PATH)
        log.warning("%s does not match the current feature set; falling back to the simulator "
                    "model. Re-run python -m onboard_ai.train_opensarship.", OPENSARSHIP_MODEL_PATH.name)
    return VesselClassifier(MODEL_PATH)
