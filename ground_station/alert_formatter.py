"""SMS and WhatsApp plain-text alert formatter (strictly <= 160 characters)."""

from __future__ import annotations

from typing import Any

MAX_SMS_LENGTH = 160


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _short_date(iso: Any) -> str | None:
    """'2026-11-24T00:55:27Z' -> '24 Nov' (None if missing or unparseable)."""
    try:
        text = str(iso)
        return f"{int(text[8:10])} {_MONTHS[int(text[5:7]) - 1]}" if iso else None
    except (ValueError, IndexError):
        return None


def _pluralize(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _earliest_ferry_eta(port_id: str, vessels: list[dict[str, Any]]) -> int | None:
    etas = [
        int(v["eta_minutes"])
        for v in vessels
        if v.get("port_id") == port_id
        and v.get("vessel_type") == "FERRY"
        and v.get("eta_minutes") is not None
    ]
    return min(etas) if etas else None


def _max_fishing_distance_km(port_id: str, vessels: list[dict[str, Any]]) -> int | None:
    dists = [
        float(v["distance_from_shore_km"])
        for v in vessels
        if v.get("port_id") == port_id
        and v.get("vessel_type") == "FISHING_BOAT"
        and v.get("distance_from_shore_km") is not None
    ]
    return int(round(max(dists))) if dists else None


def format_port_alert(
    port_summary: dict[str, Any],
    vessels: list[dict[str, Any]] | None = None,
    reroute_suggestion: dict[str, Any] | None = None,
    max_length: int = MAX_SMS_LENGTH,
    ais_checked: bool = True,
) -> str:
    """Format a single plain-text SMS/WhatsApp alert per port, strictly <= max_length chars."""
    vessels = vessels or []
    port_id = str(port_summary.get("port_id", "PORT"))
    counts = port_summary.get("counts", {})

    berth_occ = int(counts.get("berth_occupied", 0))
    max_berths = int(port_summary.get("max_berths", 0))
    queue = int(counts.get("anchorage_waiting", 0))
    ferries = int(counts.get("approaching_ferries", 0))
    fishing = int(counts.get("coastal_fishing_boats", 0))
    dark = int(port_summary.get("dark_vessels_detected", 0))

    ferry_eta = _earliest_ferry_eta(port_id, vessels)
    fish_dist = _max_fishing_distance_km(port_id, vessels)

    # Real Sentinel-1 data: a port without a recent scene was not observed at all.
    if port_summary.get("data_source") == "NO_SCENE":
        return f"EDGEPORT ALERT [{port_id}]: No recent satellite pass, port status unknown."[:max_length]

    # Real scenes can be days old, so say when the port was observed.
    observed = _short_date(port_summary.get("observed_utc"))
    prefix = f"EDGEPORT ALERT [{port_id}] {observed}: " if observed else f"EDGEPORT ALERT [{port_id}]: "

    # Candidate segment sets from most detailed to most compact.
    ferry_seg_full = (
        f"{_pluralize(ferries, 'Ferry', 'Ferries')} ETA {ferry_eta}m"
        if ferries > 0 and ferry_eta is not None
        else (f"{_pluralize(ferries, 'Ferry', 'Ferries')} approaching" if ferries > 0 else None)
    )
    ferry_seg_short = (
        f"{ferries}F ETA {ferry_eta}m"
        if ferries > 0 and ferry_eta is not None
        else (f"{ferries}F" if ferries > 0 else None)
    )

    fish_seg_full = (
        f"{_pluralize(fishing, 'Fishing Boat', 'Fishing Boats')} {fish_dist}km offshore"
        if fishing > 0 and fish_dist is not None
        else (f"{_pluralize(fishing, 'Fishing Boat', 'Fishing Boats')} offshore" if fishing > 0 else None)
    )
    fish_seg_mid = f"{fishing} Boats" if fishing > 0 else None

    if ais_checked:
        dark_seg_full = f"{_pluralize(dark, 'Dark Vessel', 'Dark Vessels')} flagged" if dark > 0 else None
        dark_seg_short = f"{dark} Dark" if dark > 0 else None
    else:
        # No AIS from the time of the scene: absence of dark vessels means "not checked", not "none".
        dark_seg_full = "AIS not checked"
        dark_seg_short = "AIS n/a"

    reroute_id = reroute_suggestion.get("recommended_port_id") if reroute_suggestion else None
    reroute_seg = f"Reroute->{reroute_id}" if reroute_id else None

    candidate_variants: list[list[str | None]] = [
        # Variant 1: Full PRD narrative
        [
            f"{berth_occ}/{max_berths} Berths Full",
            f"{queue} Cargo in Queue",
            ferry_seg_full,
            fish_seg_full,
            dark_seg_full,
        ],
        # Variant 2: Drop offshore distance phrase on fishing boats
        [
            f"{berth_occ}/{max_berths} Berths Full",
            f"{queue} Cargo in Queue",
            ferry_seg_full,
            fish_seg_mid,
            dark_seg_full,
        ],
        # Variant 3: Shorten labels while retaining dark vessel & ETA info
        [
            f"{berth_occ}/{max_berths} Berths",
            f"{queue} Queue",
            ferry_seg_short,
            dark_seg_full,
            fish_seg_mid,
        ],
        # Variant 4: Prioritize berths, queue, dark vessels, ferry ETA
        [
            f"{berth_occ}/{max_berths} Berths",
            f"{queue} Queue",
            ferry_seg_short,
            dark_seg_short,
        ],
        # Variant 5: Minimal core
        [
            f"{berth_occ}/{max_berths} Berths",
            f"{queue}Q",
            dark_seg_short,
        ],
    ]

    # If reroute hint fits in Variant 1/2, include it cleanly.
    if reroute_seg:
        candidate_variants.insert(1, candidate_variants[0] + [reroute_seg])

    for variant in candidate_variants:
        parts = [seg for seg in variant if seg]
        candidate = prefix + ", ".join(parts) + "."
        if len(candidate) <= max_length:
            return candidate

    # Graceful hard truncation fallback if extreme numbers push past max_length
    fallback_parts = [seg for seg in candidate_variants[-1] if seg]
    fallback = prefix + ", ".join(fallback_parts) + "."
    if max_length <= 3:
        return fallback[:max_length]
    return fallback[: max_length - 3].rstrip(" ,.") + "..."


def format_all_alerts(analyzed_result: dict[str, Any], max_length: int = MAX_SMS_LENGTH) -> dict[str, str]:
    """Generate a mapping of port_id -> <=160-char SMS/WhatsApp alert string."""
    packet = analyzed_result["packet"]
    vessels = packet.get("vessels", [])
    reroutes_by_port = {
        item["port_id"]: item for item in analyzed_result.get("rerouting_suggestions", [])
    }

    ais_checked = analyzed_result.get("kpis", {}).get("ais_checked", True)
    alerts: dict[str, str] = {}
    for port_summary in packet.get("ports_summary", []):
        pid = port_summary["port_id"]
        alerts[pid] = format_port_alert(
            port_summary=port_summary,
            vessels=vessels,
            reroute_suggestion=reroutes_by_port.get(pid),
            max_length=max_length,
            ais_checked=ais_checked,
        )
    return alerts
