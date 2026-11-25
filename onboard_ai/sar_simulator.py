"""Multi-pass orbital SAR simulator + uploaded GeoTIFF / PNG / NPY loader (PRD Step A).

Each simulated orbital pass over a port produces:

* one **coastal ROI tile** - 2048 x 2048 uint16 C-band backscatter at 10 m GSD (~8.4 MB),
  covering the port's quay, berths, anchorage and ~15 km of the coastal approach, and
* three **offshore EEZ tiles** - 512 x 512 chips tasked at random points 55-330 km offshore
  inside the port's ``eez_corridor_bbox`` (Zone 2 fishing banks / shipping lanes).

Physics modelled:

* Gamma-distributed multi-look sea speckle with a log-normal wave texture whose strength
  depends on sea state (CALM / MODERATE / MONSOON_ROUGH).
* Bright, textured land with point scatterers (buildings, quay cranes, a breakwater) and a
  binary land mask - the "cached coastline mask" the onboard detector uses.
* Vessels rendered as sub-pixel-accurate ellipses with per-class radar cross-section density:
  FISHING_BOAT 10-15 m, FERRY 20-45 m, CARGO_VESSEL 50-180 m. At 10 m C-band resolution small
  fishing boats are 1-2 pixel targets and are often lost in rough seas, as in reality.

Traffic levels depend on the port profile, the LEO pass time (06:00 / 12:00 / 18:00 / 23:30 IST)
and the sea state (fishing fleets stay in harbour during the monsoon).
"""
from __future__ import annotations

import io
import logging
import math
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage

from onboard_ai.georeferencer import (
    GeoTransform,
    coastline_lon,
    get_port,
    seaward_lon_sign,
)

log = logging.getLogger("edgeport.onboard.sim")

PIXEL_M = 10.0                 # C-band IW GRD-like ground sampling distance
TILE_SIZE = 2048               # coastal ROI tile (2048^2 x 2 bytes = 8.39 MB)
XBAND_PIXEL_M = 3.0            # X-band high-resolution spotlight GSD (PRD: 1-3 m)
XBAND_TILE_SIZE = 3072         # 9.2 km x 9.2 km spotlight over the inner harbour approach
XBAND_LAND_PX = 170            # ~0.5 km of shoreline included so berths are imaged
OFFSHORE_TILE_SIZE = 512
N_OFFSHORE_TILES = 3
FULL_SWATH_MB = 142.4          # full-swath scene the ROI is cropped from (PRD 4.2 Step A)
COAST_COL = {"WEST": 1500, "EAST": 548}  # coastline column in the coastal tile
PASS_TIMES_IST = ("06:00", "12:00", "18:00", "23:30")
QUICKLOOK_DB_RANGE = 25.0      # dB span assumed for 8-bit uploaded quicklook PNGs
LAND_BUFFER_PX = 2             # CFAR exclusion buffer around the coastline mask

VESSEL_LENGTH_M = {
    "FISHING_BOAT": (10.0, 15.0),
    "FERRY": (20.0, 45.0),
    "CARGO_VESSEL": (50.0, 180.0),
}
# Backscatter density (DN per fully-covered pixel) - steel hulls and superstructure.
VESSEL_RCS_DENSITY = {
    "FISHING_BOAT": (700.0, 2200.0),
    "FERRY": (2000.0, 6000.0),
    "CARGO_VESSEL": (3000.0, 9000.0),
}


@dataclass(frozen=True)
class SeaState:
    name: str
    mean: float      # mean sea backscatter (DN)
    looks: float     # equivalent number of looks of the speckle
    texture: float   # std-dev of the log-normal wave modulation


SEA_STATES = {
    "CALM": SeaState("CALM", mean=60.0, looks=6.0, texture=0.05),
    "MODERATE": SeaState("MODERATE", mean=100.0, looks=3.5, texture=0.15),
    "MONSOON_ROUGH": SeaState("MONSOON_ROUGH", mean=170.0, looks=1.6, texture=0.35),
}

# Port traffic profiles: berth fill fraction range, and (lo, hi) counts for the rest.
PORT_PROFILE = {
    "IN-KRW": dict(berth=(0.6, 0.95), anchorage=(2, 5), ferries=(1, 3), fishing=(10, 22),
                   offshore=(5, 10), ferry_berth_share=0.15),
    "IN-BEY": dict(berth=(0.3, 0.65), anchorage=(0, 2), ferries=(1, 3), fishing=(14, 26),
                   offshore=(4, 8), ferry_berth_share=0.4),
    "IN-KAK": dict(berth=(0.75, 1.0), anchorage=(4, 8), ferries=(0, 1), fishing=(8, 16),
                   offshore=(6, 12), ferry_berth_share=0.05),
    "IN-GOP": dict(berth=(0.4, 0.75), anchorage=(1, 3), ferries=(0, 1), fishing=(6, 12),
                   offshore=(3, 7), ferry_berth_share=0.05),
    # --- contract v2 ports
    "IN-PBD": dict(berth=(0.5, 0.85), anchorage=(1, 3), ferries=(0, 1), fishing=(12, 22),
                   offshore=(5, 10), ferry_berth_share=0.05),
    "IN-VRL": dict(berth=(0.4, 0.8), anchorage=(0, 2), ferries=(0, 1), fishing=(20, 32),
                   offshore=(8, 14), ferry_berth_share=0.0),   # India's biggest fishing harbour
    "IN-DIG": dict(berth=(0.5, 0.9), anchorage=(2, 4), ferries=(0, 1), fishing=(6, 12),
                   offshore=(3, 7), ferry_berth_share=0.05),
    "IN-RTC": dict(berth=(0.4, 0.75), anchorage=(0, 2), ferries=(0, 2), fishing=(12, 22),
                   offshore=(5, 9), ferry_berth_share=0.1),
    "IN-MAL": dict(berth=(0.5, 0.9), anchorage=(0, 1), ferries=(0, 1), fishing=(18, 30),
                   offshore=(8, 14), ferry_berth_share=0.0),
    "IN-MRM": dict(berth=(0.4, 0.8), anchorage=(1, 2), ferries=(1, 2), fishing=(12, 22),
                   offshore=(5, 9), ferry_berth_share=0.3),   # Lakshadweep supply vessels
    "IN-QUI": dict(berth=(0.3, 0.7), anchorage=(0, 2), ferries=(0, 1), fishing=(12, 22),
                   offshore=(5, 9), ferry_berth_share=0.05),
    "IN-KRK": dict(berth=(0.6, 0.95), anchorage=(2, 5), ferries=(0, 0), fishing=(6, 12),
                   offshore=(3, 7), ferry_berth_share=0.0),
    "IN-CDL": dict(berth=(0.3, 0.7), anchorage=(0, 2), ferries=(0, 0), fishing=(10, 18),
                   offshore=(4, 8), ferry_berth_share=0.0),
    "IN-KPT": dict(berth=(0.75, 1.0), anchorage=(4, 8), ferries=(0, 0), fishing=(6, 12),
                   offshore=(5, 10), ferry_berth_share=0.0),
    "IN-MTP": dict(berth=(0.3, 0.6), anchorage=(0, 2), ferries=(0, 1), fishing=(12, 20),
                   offshore=(4, 8), ferry_berth_share=0.0),
    "IN-DHA": dict(berth=(0.6, 0.95), anchorage=(3, 6), ferries=(0, 0), fishing=(5, 10),
                   offshore=(3, 7), ferry_berth_share=0.0),
}
DEFAULT_PROFILE = PORT_PROFILE["IN-GOP"]   # any port added to the config later

# Diurnal multipliers per LEO pass time.
TIME_FACTOR = {
    "06:00": dict(fishing=1.3, ferry=0.6, cargo=0.9),   # fleets returning at dawn
    "12:00": dict(fishing=0.7, ferry=1.0, cargo=1.1),   # cargo operations peak
    "18:00": dict(fishing=1.2, ferry=1.3, cargo=1.0),   # evening ferries, fleets heading out
    "23:30": dict(fishing=0.5, ferry=0.3, cargo=0.8),   # night
}
SEA_FISHING_FACTOR = {"CALM": 1.0, "MODERATE": 0.85, "MONSOON_ROUGH": 0.35}


def normalize_sea_state(value: str) -> str:
    """Accept UI labels like ``"Clear Calm"`` / ``"Monsoon Rough Sea"`` -> canonical names."""
    v = value.strip().upper().replace("-", "_").replace(" ", "_")
    if "CALM" in v:
        return "CALM"
    if "ROUGH" in v or "MONSOON" in v:
        return "MONSOON_ROUGH"
    if "MODERATE" in v:
        return "MODERATE"
    raise ValueError(f"Unknown sea state {value!r}; use CALM, MODERATE or MONSOON_ROUGH")


def normalize_pass_time(value: str) -> str:
    v = value.strip()
    if v not in PASS_TIMES_IST:
        raise ValueError(f"Unknown pass time {value!r}; use one of {PASS_TIMES_IST}")
    return v


def normalize_sensor_mode(value: str) -> str:
    """``"C"``, ``"c_band"``, ``"C-BAND_SAR_ALL_WEATHER"`` -> ``"C-BAND"``; same for X."""
    v = value.strip().upper().replace("_", "-").replace(" ", "-")
    if v.startswith("X"):
        return "X-BAND"
    if v.startswith("C"):
        return "C-BAND"
    raise ValueError(f"Unknown sensor mode {value!r}; use C-BAND or X-BAND")


@dataclass
class GroundTruthVessel:
    vessel_type: str
    length_m: float
    px: float
    py: float
    heading_deg: float
    density: float = 0.0


def buffer_land_mask(land_mask: np.ndarray) -> np.ndarray:
    """Coastline mask grown by :data:`LAND_BUFFER_PX` (the onboard cached CFAR exclusion mask)."""
    if not land_mask.any():
        return land_mask
    return ndimage.binary_dilation(land_mask, iterations=LAND_BUFFER_PX)


@dataclass
class SceneTile:
    name: str
    image: np.ndarray | None          # uint16 backscatter; set to None once purged
    land_mask: np.ndarray | None      # bool, True = land
    geo: GeoTransform
    truth: list[GroundTruthVessel] = field(default_factory=list)
    sensor: str = "C-BAND"            # "C-BAND" (10 m) or "X-BAND" (3 m spotlight)
    cfar_mask: np.ndarray | None = None  # pre-buffered land mask, cached with the coastline

    def __post_init__(self) -> None:
        if self.cfar_mask is None and self.land_mask is not None:
            self.cfar_mask = buffer_land_mask(self.land_mask)

    @property
    def nbytes(self) -> int:
        return 0 if self.image is None else int(self.image.nbytes)

    def purge(self) -> None:
        """Delete the raw raster from memory (the satellite never downlinks it)."""
        raw_raster = self.image
        self.image = None
        self.land_mask = None
        self.cfar_mask = None
        del raw_raster


@dataclass
class OrbitalPass:
    port_id: str
    sea_state: str
    pass_time_ist: str
    source: str                       # "SIMULATED" or "UPLOAD"
    tiles: list[SceneTile]

    @property
    def roi_bytes(self) -> int:
        return sum(t.nbytes for t in self.tiles)

    def purge(self) -> None:
        for tile in self.tiles:
            tile.purge()


# --------------------------------------------------------------------------- rendering

def sea_clutter(rng: np.random.Generator, shape: tuple[int, int], state: SeaState) -> np.ndarray:
    """Gamma speckle x log-normal wave texture (K-distribution-like heavy tail)."""
    img = rng.gamma(state.looks, state.mean / state.looks, size=shape).astype(np.float32)
    if state.texture > 0:
        h, w = shape
        lo = rng.standard_normal((max(h // 8, 4), max(w // 8, 4))).astype(np.float32)
        lo = ndimage.gaussian_filter(lo, 1.5)
        lo /= lo.std() + 1e-6
        tex = ndimage.zoom(lo, (h / lo.shape[0], w / lo.shape[1]), order=1)[:h, :w]
        img *= np.exp(state.texture * tex - 0.5 * state.texture ** 2)
    return img


def vessel_width_px(length_m: float, pixel_m: float = PIXEL_M) -> float:
    return max(length_m / 6.5 / pixel_m, 0.8)


def render_vessel(img: np.ndarray, cx: float, cy: float, length_px: float, width_px: float,
                  heading_deg: float, density: float, rng: np.random.Generator,
                  supersample: int = 8) -> None:
    """Add a vessel signature in place, with sub-pixel area coverage."""
    half = int(math.ceil(max(length_px, width_px) / 2)) + 2
    size = 2 * half + 1
    x0, y0 = int(round(cx)) - half, int(round(cy)) - half
    g = (np.arange(size * supersample) + 0.5) / supersample - 0.5
    gx, gy = np.meshgrid(g + x0, g + y0)
    dx, dy = gx - cx, gy - cy
    th = math.radians(heading_deg)          # 0 deg = north (up, -y)
    u = dx * math.sin(th) - dy * math.cos(th)   # along-ship
    v = dx * math.cos(th) + dy * math.sin(th)   # across-ship
    inside = (u / (length_px / 2)) ** 2 + (v / (width_px / 2)) ** 2 <= 1.0
    cover = inside.reshape(size, supersample, size, supersample).mean(axis=(1, 3))
    signal = cover * density * (0.6 + 0.8 * rng.random(cover.shape))

    h, w = img.shape
    ys0, xs0 = max(y0, 0), max(x0, 0)
    ys1, xs1 = min(y0 + size, h), min(x0 + size, w)
    if ys0 >= ys1 or xs0 >= xs1:
        return
    img[ys0:ys1, xs0:xs1] += signal[ys0 - y0:ys1 - y0, xs0 - x0:xs1 - x0].astype(img.dtype)


def _place(rng, sampler, others: list[tuple[float, float]], min_sep: float, tries: int = 60):
    """Rejection-sample a position from ``sampler()`` at least ``min_sep`` px from ``others``."""
    for _ in range(tries):
        pos = sampler()
        if pos is None:
            continue
        if all((pos[0] - ox) ** 2 + (pos[1] - oy) ** 2 >= min_sep ** 2 for ox, oy in others):
            return pos
    return None


def _count(rng, lo_hi: tuple[float, float], factor: float) -> int:
    lo, hi = lo_hi
    return max(0, int(round(rng.integers(int(lo), int(hi) + 1) * factor)))


def _add_vessel(img, truth, rng, vtype, cx, cy, heading, length_m=None):
    length_m = length_m if length_m is not None else rng.uniform(*VESSEL_LENGTH_M[vtype])
    density = rng.uniform(*VESSEL_RCS_DENSITY[vtype])
    render_vessel(img, cx, cy, length_m / PIXEL_M, vessel_width_px(length_m), heading, density, rng)
    truth.append(GroundTruthVessel(vtype, float(length_m), float(cx), float(cy), float(heading), float(density)))


# --------------------------------------------------------------------------- scene tiles

def _coastal_tile(port: dict, state: SeaState, pass_time: str, rng) -> SceneTile:
    n = TILE_SIZE
    coast = port["coast"]
    seaward = -1 if coast == "WEST" else 1           # +col direction pointing to sea
    coast_col = COAST_COL[coast]
    geo = GeoTransform.anchored(port["lat"], coastline_lon(port), coast_col, n // 2, PIXEL_M)
    bx0, by0, bx1, by1 = geo.bbox_to_pixels(port["berth_bbox"])
    ax0, ay0, ax1, ay1 = geo.bbox_to_pixels(port["anchorage_bbox"])
    prof = PORT_PROFILE.get(port["port_id"], DEFAULT_PROFILE)
    tf = TIME_FACTOR[pass_time]
    fish_factor = tf["fishing"] * SEA_FISHING_FACTOR[state.name]

    # --- coastline: wiggly shore, straight quay along the berths, plus a breakwater
    rows = np.arange(n)
    wiggle = ndimage.gaussian_filter1d(rng.standard_normal(n), 60)
    wiggle *= 35.0 / (np.abs(wiggle).max() + 1e-9)
    dist_to_quay = np.maximum(0, np.maximum((by0 - 40) - rows, rows - (by1 + 40)))
    coast_cols = coast_col + wiggle * np.clip(dist_to_quay / 150.0, 0, 1)
    cols = np.arange(n)[None, :]
    land = cols >= coast_cols[:, None] if coast == "WEST" else cols <= coast_cols[:, None]
    bw_r0 = int(by0) - 12
    bw_c0, bw_c1 = sorted((coast_col, coast_col + seaward * 80))
    land[max(bw_r0, 0):max(bw_r0 + 4, 0), bw_c0:bw_c1 + 1] = True

    # --- backscatter
    img = sea_clutter(rng, (n, n), state)
    land_vals = (420.0 * rng.gamma(2.0, 0.5, size=(n, n))).astype(np.float32)
    img[land] = land_vals[land]
    ly, lx = np.nonzero(land)
    pick = rng.choice(ly.size, size=min(600, ly.size), replace=False)
    img[ly[pick], lx[pick]] = rng.uniform(15000, 45000, size=pick.size)  # buildings, towers
    for r in range(int(by0), int(by1), 30):                              # quay cranes
        if 0 <= r < n:
            img[r, coast_col - seaward * 2] = 40000.0
    img[max(bw_r0, 0):max(bw_r0 + 4, 0), bw_c0:bw_c1 + 1:7] = 30000.0     # breakwater lights

    truth: list[GroundTruthVessel] = []
    occupied: list[tuple[float, float]] = []

    # --- berthed vessels: one per occupied berth slot along the quay
    max_berths = int(port["max_berths"])
    fill = rng.uniform(*prof["berth"]) * tf["cargo"]
    n_berth = int(np.clip(round(max_berths * fill), 0, max_berths))
    slot_len = (by1 - by0 - 20) / max_berths
    for slot in sorted(rng.choice(max_berths, size=n_berth, replace=False)):
        vtype = "FERRY" if rng.random() < prof["ferry_berth_share"] else "CARGO_VESSEL"
        lo, hi = VESSEL_LENGTH_M[vtype]
        length = rng.uniform(lo, min(hi, 0.8 * slot_len * PIXEL_M))
        cy = by0 + 10 + slot_len * (slot + 0.5)
        cx = coast_col + seaward * (vessel_width_px(length) / 2 + 4)
        _add_vessel(img, truth, rng, vtype, cx, cy, rng.choice([0.0, 180.0]), length)
        occupied.append((cx, cy))

    def in_rect(p, r, pad=0):
        return r[0] - pad <= p[0] <= r[2] + pad and r[1] - pad <= p[1] <= r[3] + pad

    def sea_point(margin_land=10):
        x, y = rng.uniform(20, n - 20), rng.uniform(20, n - 20)
        seaward_px = (x - coast_cols[int(y)]) * seaward
        if seaward_px < margin_land:  # on land or hugging the shore
            return None
        return x, y

    # --- anchorage queue
    for _ in range(_count(rng, prof["anchorage"], tf["cargo"])):
        pos = _place(rng, lambda: (rng.uniform(ax0 + 15, ax1 - 15), rng.uniform(ay0 + 15, ay1 - 15)),
                     occupied, 28)
        if pos:
            _add_vessel(img, truth, rng, "CARGO_VESSEL", *pos, rng.uniform(0, 360))
            occupied.append(pos)

    # --- ferries on the approach channel
    port_px = (coast_col + seaward * 20, n / 2)
    for _ in range(_count(rng, prof["ferries"], tf["ferry"])):
        def ferry_pos():
            start = (coast_col + seaward * 1350, n / 2 + rng.uniform(-400, 400))
            t = rng.uniform(0.1, 0.75)
            p = (start[0] + t * (port_px[0] - start[0]) + rng.uniform(-30, 30),
                 start[1] + t * (port_px[1] - start[1]) + rng.uniform(-30, 30))
            if in_rect(p, (ax0, ay0, ax1, ay1), 20) or in_rect(p, (bx0, by0, bx1, by1), 20):
                return None
            return p
        pos = _place(rng, ferry_pos, occupied, 25)
        if pos:
            heading = math.degrees(math.atan2(port_px[0] - pos[0], -(port_px[1] - pos[1]))) % 360
            _add_vessel(img, truth, rng, "FERRY", *pos, heading)
            occupied.append(pos)

    # --- coastal fishing fleet: half clustered on the harbour approach lanes, half spread out
    for _ in range(_count(rng, prof["fishing"], fish_factor)):
        def fish_pos():
            if rng.random() < 0.5:
                x = coast_col + seaward * (20 + abs(rng.normal(0, 250)))
                p = (x, n / 2 + rng.normal(0, 300))
                if not (20 <= p[0] < n - 20 and 20 <= p[1] < n - 20) or \
                        (p[0] - coast_cols[int(p[1])]) * seaward < 10:
                    return None
            else:
                p = sea_point()
            if p is None or in_rect(p, (ax0, ay0, ax1, ay1), 10) or in_rect(p, (bx0, by0, bx1, by1), 10):
                return None
            return p
        pos = _place(rng, fish_pos, occupied, 15)
        if pos:
            _add_vessel(img, truth, rng, "FISHING_BOAT", *pos, rng.uniform(0, 360))
            occupied.append(pos)

    image = np.clip(img, 0, 65535).astype(np.uint16)
    return SceneTile("COASTAL", image, land, geo, truth)


def _offshore_tiles(port: dict, state: SeaState, pass_time: str, rng) -> list[SceneTile]:
    n = OFFSHORE_TILE_SIZE
    prof = PORT_PROFILE.get(port["port_id"], DEFAULT_PROFILE)
    fish_factor = TIME_FACTOR[pass_time]["fishing"] * SEA_FISHING_FACTOR[state.name]
    total_fishing = _count(rng, prof["offshore"], fish_factor)
    per_tile = rng.multinomial(total_fishing, [1 / N_OFFSHORE_TILES] * N_OFFSHORE_TILES)
    _, min_lat, _, max_lat = port["eez_corridor_bbox"]
    tiles = []
    for i, d_km in enumerate(sorted(rng.uniform(55, 330, size=N_OFFSHORE_TILES))):
        lat = float(np.clip(port["lat"] + rng.uniform(-1.0, 1.0), min_lat + 0.1, max_lat - 0.1))
        km_per_deg_lon = 111.32 * math.cos(math.radians(lat))
        lon = coastline_lon(port) + seaward_lon_sign(port) * d_km / km_per_deg_lon
        geo = GeoTransform.anchored(lat, lon, n / 2, n / 2, PIXEL_M)
        img = sea_clutter(rng, (n, n), state)
        truth: list[GroundTruthVessel] = []
        occupied: list[tuple[float, float]] = []
        jobs = ["FISHING_BOAT"] * int(per_tile[i])
        if rng.random() < 0.35:
            jobs.append("CARGO_VESSEL")       # shipping-lane transit / possible dark vessel
        for vtype in jobs:
            pos = _place(rng, lambda: (rng.uniform(30, n - 30), rng.uniform(30, n - 30)), occupied, 25)
            if pos:
                _add_vessel(img, truth, rng, vtype, *pos, rng.uniform(0, 360))
                occupied.append(pos)
        tiles.append(SceneTile(f"OFFSHORE-{i + 1}", np.clip(img, 0, 65535).astype(np.uint16),
                               np.zeros((n, n), dtype=bool), geo, truth))
    return tiles


def _xband_spotlight(port: dict, state: SeaState, coastal: SceneTile, rng) -> SceneTile:
    """3 m X-band spotlight over the inner approach, imaging the *same* vessels as ``coastal``.

    Covers ~0.5 km of shoreline (berths) and ~5.6 km seaward, 6.1 km along-shore, centred on
    the port. At 3 m a 12 m fishing boat spans ~4 x 1 fully-resolved pixels instead of one
    partially-filled 10 m pixel, so small craft survive rough seas far better.
    """
    n = XBAND_TILE_SIZE
    coast = port["coast"]
    x_coast_col = n - XBAND_LAND_PX if coast == "WEST" else XBAND_LAND_PX
    geo = GeoTransform.anchored(port["lat"], coastline_lon(port), x_coast_col, n // 2, XBAND_PIXEL_M)

    # Land mask: resample the cached C-band coastline onto the 3 m grid (nearest neighbour).
    cgeo = coastal.geo
    lats = geo.origin_lat - np.arange(n) * geo.dlat
    lons = geo.origin_lon + np.arange(n) * geo.dlon
    crow = np.clip(np.round((cgeo.origin_lat - lats) / cgeo.dlat).astype(int), 0, coastal.land_mask.shape[0] - 1)
    ccol = np.clip(np.round((lons - cgeo.origin_lon) / cgeo.dlon).astype(int), 0, coastal.land_mask.shape[1] - 1)
    land = coastal.land_mask[crow[:, None], ccol[None, :]]

    img = sea_clutter(rng, (n, n), state)
    land_vals = (420.0 * rng.gamma(2.0, 0.5, size=(n, n))).astype(np.float32)
    img[land] = land_vals[land]
    ly, lx = np.nonzero(land)
    if ly.size:
        pick = rng.choice(ly.size, size=min(1500, ly.size), replace=False)
        img[ly[pick], lx[pick]] = rng.uniform(15000, 45000, size=pick.size)
        shore = land & ~ndimage.binary_erosion(land, iterations=4)        # quay-side cranes
        sy, sx = np.nonzero(shore)
        if sy.size:
            pick = rng.choice(sy.size, size=min(250, sy.size), replace=False)
            img[sy[pick], sx[pick]] = 40000.0

    truth: list[GroundTruthVessel] = []
    for v in coastal.truth:
        lat, lon = cgeo.pixel_to_latlon(v.px, v.py)
        x, y = geo.latlon_to_pixel(lat, lon)
        half = v.length_m / XBAND_PIXEL_M / 2 + 2
        if -half <= x < n + half and -half <= y < n + half:
            render_vessel(img, x, y, v.length_m / XBAND_PIXEL_M, vessel_width_px(v.length_m, XBAND_PIXEL_M),
                          v.heading_deg, v.density, rng)
            if 0 <= x < n and 0 <= y < n:
                truth.append(GroundTruthVessel(v.vessel_type, v.length_m, float(x), float(y), v.heading_deg, v.density))
    image = np.clip(img, 0, 65535).astype(np.uint16)
    return SceneTile("XBAND-SPOT", image, land, geo, truth, sensor="X-BAND")


def simulate_pass(port: dict | str, sea_state: str = "MODERATE", pass_time_ist: str = "18:00",
                  seed: int | None = None, sensor_mode: str = "C-BAND") -> OrbitalPass:
    """Synthesize one LEO SAR pass over ``port``.

    ``C-BAND``: coastal ROI + offshore EEZ tiles at 10 m.
    ``X-BAND``: the same, plus a 3 m X-band spotlight tile over the inner harbour approach.
    """
    port = get_port(port) if isinstance(port, str) else port
    state = SEA_STATES[normalize_sea_state(sea_state)]
    pass_time = normalize_pass_time(pass_time_ist)
    sensor = normalize_sensor_mode(sensor_mode)
    rng = np.random.default_rng(seed)
    coastal = _coastal_tile(port, state, pass_time, rng)
    tiles = [coastal, *_offshore_tiles(port, state, pass_time, rng)]
    if sensor == "X-BAND":
        tiles.append(_xband_spotlight(port, state, coastal, rng))
    orbital = OrbitalPass(port["port_id"], state.name, pass_time, "SIMULATED", tiles)
    log.info("Simulated %s %s pass %s IST (%s): %d tiles, %.2f MB ROI, %d true vessels",
             port["port_id"], sensor, pass_time, state.name, len(tiles), orbital.roi_bytes / 1e6,
             sum(len(t.truth) for t in tiles if t.sensor == "C-BAND"))
    return orbital


# --------------------------------------------------------------------------- uploads

def _to_uint16(arr: np.ndarray) -> np.ndarray:
    arr = np.nan_to_num(np.asarray(arr, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    arr = np.abs(arr)  # complex / signed products -> amplitude-like
    if arr.max() > 65535 or arr.dtype.kind == "f" and arr.max() <= 1.0:
        arr = arr / max(arr.max(), 1e-12) * 60000.0
    return np.clip(arr, 0, 65535).astype(np.uint16)


def _block_downsample(arr: np.ndarray, max_side: int = 4096) -> np.ndarray:
    f = int(math.ceil(max(arr.shape) / max_side))
    if f <= 1:
        return arr
    h, w = (arr.shape[0] // f) * f, (arr.shape[1] // f) * f
    return arr[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))


def estimate_land_mask(img: np.ndarray) -> np.ndarray:
    """Heuristic land mask for scenes without a cached coastline.

    1. Work in dB; pick the land/sea threshold with Otsu's method on a 4x-reduced,
       median-filtered image (the median ignores ships, which are small and bright).
    2. Deep land = the coarse land mask eroded well away from the shoreline.
    3. Final land = every fine-scale (3x3) above-threshold region physically connected to deep
       land. This keeps thin piers and breakwaters but not berthed ships, which are separated
       from the quay by open water.
    """
    db = 10.0 * np.log10(np.maximum(img.astype(np.float32), 1.0))
    f = 4
    h, w = (db.shape[0] // f) * f, (db.shape[1] // f) * f
    no_land = np.zeros(img.shape, dtype=bool)
    if h < 16 * f or w < 16 * f:
        return no_land
    coarse = ndimage.median_filter(db[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3)), size=7)

    thr, sea_db, land_db = _otsu(coarse.ravel())
    if land_db - sea_db < 3.0:      # unimodal scene: open ocean
        return no_land
    core = coarse > thr
    lab, n = ndimage.label(core)
    if n:
        sizes = np.bincount(lab.ravel())
        keep = sizes >= 0.005 * core.size
        keep[0] = False
        core = keep[lab]
    core = ndimage.binary_fill_holes(core)
    deep = ndimage.binary_erosion(core, iterations=3)
    if not deep.any():
        return no_land
    deep_full = np.zeros(img.shape, dtype=bool)
    deep_full[:h, :w] = np.repeat(np.repeat(deep, f, axis=0), f, axis=1)

    fine = ndimage.uniform_filter(db, 3) > thr
    lab, _ = ndimage.label(fine | deep_full)
    ids = np.unique(lab[deep_full])
    return ndimage.binary_fill_holes(np.isin(lab, ids[ids > 0]))


def _otsu(values: np.ndarray, bins: int = 256) -> tuple[float, float, float]:
    """Otsu threshold. Returns ``(threshold, mean_below, mean_above)``."""
    hist, edges = np.histogram(values, bins=bins)
    centres = (edges[:-1] + edges[1:]) / 2
    hist = hist.astype(np.float64)
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    s0 = np.cumsum(hist * centres)
    m0 = s0 / np.maximum(w0, 1e-12)
    m1 = (s0[-1] - s0) / np.maximum(w1, 1e-12)
    between = w0 * w1 * (m0 - m1) ** 2
    i = int(np.argmax(between[:-1]))
    return float(centres[i]), float(m0[i]), float(m1[i])


def load_uploaded_image(data: bytes, port: dict | str, sea_state: str = "MODERATE",
                        pass_time_ist: str = "18:00", pixel_m: float = PIXEL_M) -> OrbitalPass:
    """Load a user-supplied Sentinel-1 GeoTIFF / PNG / 16-bit TIFF / ``.npy`` scene.

    Georeferencing: a GeoTIFF in EPSG:4326 uses its own transform (needs optional ``rasterio``);
    anything else is assumed to have ``pixel_m`` GSD (10 m C-band by default, 3 m for X-band)
    and to be centred on the selected port.
    """
    port = get_port(port) if isinstance(port, str) else port
    arr, geo = None, None

    if data[:6] == b"\x93NUMPY":
        arr = np.load(io.BytesIO(data), allow_pickle=False)
        if arr.ndim == 3:
            arr = arr[..., 0] if arr.shape[-1] <= 4 else arr[0]

    if arr is None and data[:4] in (b"II*\x00", b"MM\x00*"):
        try:
            import rasterio  # optional dependency
            from rasterio.io import MemoryFile

            with MemoryFile(data) as mem, mem.open() as ds:
                arr = ds.read(1)
                if ds.crs is not None and ds.crs.to_epsg() == 4326 and not ds.transform.is_identity:
                    t = ds.transform
                    lat_c = t.f + t.e / 2
                    geo = GeoTransform(origin_lat=lat_c, origin_lon=t.c + t.a / 2, dlat=-t.e, dlon=t.a,
                                       pixel_m=abs(t.a) * 111_320.0 * math.cos(math.radians(lat_c)))
        except ImportError:
            log.info("rasterio not installed; reading TIFF without georeferencing via Pillow")
        except Exception as exc:  # malformed GeoTIFF -> try Pillow below
            log.warning("rasterio could not read upload (%s); falling back to Pillow", exc)

    if arr is None:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            if im.mode not in ("I;16", "I;16B", "I;16L", "I", "F", "L"):
                im = im.convert("L")
            arr = np.array(im)

    if np.asarray(arr).dtype == np.uint8:
        # 8-bit SAR quicklooks are dB-scaled; CFAR needs linear intensity statistics.
        arr = 10.0 ** (np.asarray(arr, dtype=np.float64) * QUICKLOOK_DB_RANGE / 255.0 / 10.0)

    full_shape = np.asarray(arr).shape
    arr = _block_downsample(np.asarray(arr, dtype=np.float64))
    if geo is not None and arr.shape != full_shape:
        f = full_shape[0] // arr.shape[0]
        geo = GeoTransform(origin_lat=geo.origin_lat - (f - 1) / 2 * geo.dlat,
                           origin_lon=geo.origin_lon + (f - 1) / 2 * geo.dlon,
                           dlat=geo.dlat * f, dlon=geo.dlon * f, pixel_m=geo.pixel_m * f)
    image = _to_uint16(arr)
    if geo is None:
        f = full_shape[0] // image.shape[0]
        geo = GeoTransform.anchored(port["lat"], port["lon"], image.shape[1] / 2, image.shape[0] / 2, pixel_m * f)
    land = estimate_land_mask(image)
    sensor = "X-BAND" if geo.pixel_m < 5.0 else "C-BAND"
    tile = SceneTile("UPLOAD", image, land, geo, [], sensor=sensor)
    log.info("Loaded uploaded scene %s for %s (%.2f MB, land %.1f%%)", image.shape, port["port_id"],
             image.nbytes / 1e6, 100 * land.mean())
    return OrbitalPass(port["port_id"], normalize_sea_state(sea_state), normalize_pass_time(pass_time_ist),
                       "UPLOAD", [tile])


# --------------------------------------------------------------------------- display

def to_display_uint8(img: np.ndarray) -> np.ndarray:
    """dB-scaled, percentile-stretched 8-bit rendering of a backscatter array."""
    db = 10.0 * np.log10(np.maximum(img.astype(np.float32), 1.0))
    sample = db[:: max(1, db.shape[0] // 512), :: max(1, db.shape[1] // 512)]
    lo, hi = np.percentile(sample, [2.0, 99.7])
    return np.clip((db - lo) / max(hi - lo, 1e-6) * 255.0, 0, 255).astype(np.uint8)
