"""Georeferencing, vessel physics estimates and EEZ zone tagging (PRD Step C).

Converts detector pixel centroids into WGS84 ``(latitude, longitude)``, estimates physical
length / tonnage, measures cross-shore distance and tags each detection as one of
``BERTH``, ``ANCHORAGE_QUEUE``, ``COASTAL_APPROACH`` (0-50 km) or ``OFFSHORE_EEZ`` (50-370 km).

Coastline model
---------------
For each port the shoreline is approximated as the meridian through the landward edge of its
``berth_bbox`` (east edge on the WEST coast, west edge on the EAST coast). Distance from shore
is the seaward east-west distance from that meridian. This matches the simulator's scene
geometry exactly and is accurate to a few km for real scenes near the configured ports.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORTS_CONFIG_PATH = ROOT / "data" / "ports_config.json"

M_PER_DEG_LAT = 111_320.0
EARTH_RADIUS_KM = 6371.0088
COASTAL_ZONE_KM = 50.0
EEZ_LIMIT_KM = 370.0
KNOT_KMH = 1.852

# Typical service speeds used for ETA estimates (knots).
SERVICE_SPEED_KN = {"FISHING_BOAT": 7.0, "FERRY": 14.0, "CARGO_VESSEL": 11.0}


@lru_cache(maxsize=4)
def load_ports_config(path: str | None = None) -> dict:
    """Load and cache ``data/ports_config.json``."""
    with open(path or PORTS_CONFIG_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def get_port(port_id: str) -> dict:
    """Return the config entry for ``port_id`` (e.g. ``"IN-KRW"``)."""
    for port in load_ports_config()["ports"]:
        if port["port_id"] == port_id:
            return port
    known = [p["port_id"] for p in load_ports_config()["ports"]]
    raise ValueError(f"Unknown port_id {port_id!r}; expected one of {known}")


def port_code(port_id: str) -> str:
    """``"IN-KRW"`` -> ``"KRW"``."""
    return port_id.split("-", 1)[-1]


def coastline_lon(port: dict) -> float:
    """Longitude of the modelled shoreline (landward edge of the berth box)."""
    min_lon, _, max_lon, _ = port["berth_bbox"]
    return max_lon if port["coast"] == "WEST" else min_lon


def seaward_lon_sign(port: dict) -> int:
    """-1 if the sea lies to the west of the coast, +1 if it lies to the east."""
    return -1 if port["coast"] == "WEST" else 1


@dataclass(frozen=True)
class GeoTransform:
    """Affine pixel <-> WGS84 mapping for a north-up tile (pixel centres at integer coords)."""

    origin_lat: float  # latitude of the centre of pixel row 0
    origin_lon: float  # longitude of the centre of pixel column 0
    dlat: float        # degrees of latitude per pixel row (positive, rows go south)
    dlon: float        # degrees of longitude per pixel column
    pixel_m: float     # nominal ground sampling distance in metres

    @classmethod
    def anchored(cls, lat: float, lon: float, px: float, py: float, pixel_m: float) -> "GeoTransform":
        """Build a transform where pixel ``(px, py)`` maps to ``(lat, lon)``."""
        dlat = pixel_m / M_PER_DEG_LAT
        dlon = pixel_m / (M_PER_DEG_LAT * math.cos(math.radians(lat)))
        return cls(origin_lat=lat + py * dlat, origin_lon=lon - px * dlon,
                   dlat=dlat, dlon=dlon, pixel_m=pixel_m)

    def pixel_to_latlon(self, px: float, py: float) -> tuple[float, float]:
        return self.origin_lat - py * self.dlat, self.origin_lon + px * self.dlon

    def latlon_to_pixel(self, lat: float, lon: float) -> tuple[float, float]:
        return (lon - self.origin_lon) / self.dlon, (self.origin_lat - lat) / self.dlat

    def bbox_to_pixels(self, bbox: list[float]) -> tuple[float, float, float, float]:
        """``[min_lon, min_lat, max_lon, max_lat]`` -> ``(x0, y0, x1, y1)`` pixel rectangle."""
        min_lon, min_lat, max_lon, max_lat = bbox
        x0, y0 = self.latlon_to_pixel(max_lat, min_lon)
        x1, y1 = self.latlon_to_pixel(min_lat, max_lon)
        return x0, y0, x1, y1


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def in_bbox(lat: float, lon: float, bbox: list[float]) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def distance_from_shore_km(port: dict, lat: float, lon: float) -> float:
    """Seaward cross-shore distance from the port's modelled coastline (0 if landward)."""
    km_per_deg_lon = M_PER_DEG_LAT / 1000.0 * math.cos(math.radians(lat))
    seaward_deg = (lon - coastline_lon(port)) * seaward_lon_sign(port)
    return max(0.0, seaward_deg * km_per_deg_lon)


def classify_zone(port: dict, lat: float, lon: float, dist_km: float) -> str:
    if in_bbox(lat, lon, port["berth_bbox"]):
        return "BERTH"
    if in_bbox(lat, lon, port["anchorage_bbox"]):
        return "ANCHORAGE_QUEUE"
    if dist_km <= COASTAL_ZONE_KM:
        return "COASTAL_APPROACH"
    return "OFFSHORE_EEZ"


def estimate_tonnage_mt(vessel_type: str, length_m: float) -> float:
    """Rough deadweight (metric tonnes) from length using coastal-fleet power-law fits."""
    if vessel_type == "CARGO_VESSEL":
        return 4.5 * length_m ** 1.6
    if vessel_type == "FERRY":
        return 2.0 * length_m ** 1.5
    return 0.5 * length_m ** 1.5


def eta_minutes(port: dict, lat: float, lon: float, vessel_type: str, zone: str) -> int | None:
    """Minutes to reach the port at service speed. ``None`` for offshore fishing (not inbound)."""
    if zone == "BERTH":
        return 0
    if zone == "OFFSHORE_EEZ" and vessel_type == "FISHING_BOAT":
        return None
    km = haversine_km(lat, lon, port["lat"], port["lon"])
    return int(round(km / (SERVICE_SPEED_KN[vessel_type] * KNOT_KMH) * 60))


@dataclass
class VesselRecord:
    port_id: str
    vessel_type: str
    latitude: float
    longitude: float
    distance_from_shore_km: float
    zone: str
    estimated_length_m: float
    estimated_tonnage_mt: float
    confidence: float
    eta_minutes: int | None
    px: float
    py: float
    vessel_id: str = ""

    @property
    def dark_candidate(self) -> bool:
        """Ship-sized (non-fishing) target in the offshore EEZ corridor: alert-chip worthy."""
        return self.zone == "OFFSHORE_EEZ" and self.vessel_type != "FISHING_BOAT"

    def to_packet(self) -> dict:
        """Compact dict in the exact field order of ``contracts/telemetry_schema.json``."""
        return {
            "vessel_id": self.vessel_id,
            "port_id": self.port_id,
            "vessel_type": self.vessel_type,
            "latitude": round(self.latitude, 4),
            "longitude": round(self.longitude, 4),
            "distance_from_shore_km": round(self.distance_from_shore_km, 1),
            "zone": self.zone,
            "estimated_length_m": round(self.estimated_length_m, 1),
            "confidence": round(self.confidence, 2),
            "ais_matched": False,  # ground station is the authority (see TEAM_SPLIT.md)
            "eta_minutes": self.eta_minutes,
        }


def georeference(px: float, py: float, length_m: float, geo: GeoTransform, port: dict,
                 vessel_type: str, confidence: float) -> VesselRecord | None:
    """Turn one classified detection into a :class:`VesselRecord`.

    Returns ``None`` if the target lies beyond the 370 km EEZ monitoring limit.
    """
    lat, lon = geo.pixel_to_latlon(px, py)
    dist = distance_from_shore_km(port, lat, lon)
    if dist > EEZ_LIMIT_KM:
        return None
    zone = classify_zone(port, lat, lon, dist)
    return VesselRecord(
        port_id=port["port_id"],
        vessel_type=vessel_type,
        latitude=lat,
        longitude=lon,
        distance_from_shore_km=dist,
        zone=zone,
        estimated_length_m=length_m,
        estimated_tonnage_mt=estimate_tonnage_mt(vessel_type, length_m),
        confidence=float(confidence),
        eta_minutes=eta_minutes(port, lat, lon, vessel_type, zone),
        px=float(px),
        py=float(py),
    )
