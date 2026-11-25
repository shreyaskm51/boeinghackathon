"""EdgePort Regional Port & Coastal Vessel Intelligence Dashboard (Person B - Track A)."""

from __future__ import annotations

import copy
import io
import json
import math
from pathlib import Path
from typing import Any

import folium
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from PIL import Image, ImageDraw
from streamlit_folium import st_folium

from ground_station.ais_connector import fetch_ais_feed
from ground_station.alert_formatter import MAX_SMS_LENGTH, format_all_alerts
from ground_station.analytics_engine import (
    DEFAULT_LATEST_TELEMETRY_PATH,
    DEFAULT_SAMPLE_TELEMETRY_PATH,
    analyze_telemetry,
    build_multi_pass_nowcast,
    load_ports_config,
    load_telemetry_packet,
)

try:
    from onboard_ai.edge_pipeline import run_pass as onboard_run_pass

    ONBOARD_PIPELINE_AVAILABLE = True
except ImportError:
    onboard_run_pass = None
    ONBOARD_PIPELINE_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parent

VESSEL_COLOR_MAP = {
    "FISHING_BOAT": ("#10B981", "green", "Fishing Boat (AIS Verified)"),
    "FERRY": ("#06B6D4", "lightblue", "Ferry (AIS Verified)"),
    "CARGO_VESSEL": ("#F59E0B", "orange", "Cargo Ship (AIS Verified)"),
    "DARK_VESSEL": ("#EF4444", "red", "Unmatched Dark Vessel / Alert"),
}

CONGESTION_BADGE_COLORS = {
    "NORMAL": "#10B981",
    "MODERATE": "#3B82F6",
    "HIGH_CONGESTION": "#F59E0B",
    "CRITICAL_CONGESTION": "#EF4444",
}

SEA_STATE_NOISE_SCALE = {
    "CALM": 18,
    "MODERATE": 34,
    "MONSOON_ROUGH": 56,
}

IST_TO_UTC = {
    "06:00": "2026-11-25T00:30:00Z",
    "12:00": "2026-11-25T06:30:00Z",
    "18:00": "2026-11-25T12:30:00Z",
    "23:30": "2026-11-25T18:00:00Z",
}


def _circle_polygon_latlon(lat: float, lon: float, radius_km: float, steps: int = 48) -> tuple[list[float], list[float]]:
    """Return approximate WGS84 circle boundary coordinates (lats, lons) for a radius in km."""
    lats: list[float] = []
    lons: list[float] = []
    lat_deg_per_km = 1.0 / 111.32
    lon_deg_per_km = 1.0 / max(1e-6, 111.32 * math.cos(math.radians(lat)))
    for i in range(steps + 1):
        theta = (2.0 * math.pi * i) / steps
        lats.append(lat + radius_km * math.sin(theta) * lat_deg_per_km)
        lons.append(lon + radius_km * math.cos(theta) * lon_deg_per_km)
    return lats, lons


def _bbox_to_rect(bbox: list[float]) -> list[tuple[float, float]]:
    """Convert [min_lon, min_lat, max_lon, max_lat] to closed [(lat, lon), ...] polygon."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return [
        (min_lat, min_lon),
        (min_lat, max_lon),
        (max_lat, max_lon),
        (max_lat, min_lon),
        (min_lat, min_lon),
    ]


def _vessel_visual_style(vessel: dict[str, Any]) -> tuple[str, str, str]:
    """Return (hex_color, folium_color, label) prioritizing dark vessel red flag."""
    if not vessel.get("ais_matched", False):
        return VESSEL_COLOR_MAP["DARK_VESSEL"]
    return VESSEL_COLOR_MAP.get(vessel.get("vessel_type", "CARGO_VESSEL"), VESSEL_COLOR_MAP["CARGO_VESSEL"])


def _vessel_popup_html(vessel: dict[str, Any]) -> str:
    """Build clean HTML metadata card for Folium popups and tooltips."""
    ais_status = (
        f"VERIFIED (MMSI {vessel['matched_ais_mmsi']} — {vessel['matched_ais_name']})"
        if vessel.get("ais_matched")
        else f"ALERT: {vessel.get('alert_flag', 'DARK_VESSEL')}"
    )
    eta = vessel.get("eta_minutes_effective", vessel.get("eta_minutes"))
    eta_str = f"{eta} min" if eta is not None else "N/A"
    return (
        f"<b>{vessel['vessel_id']}</b> ({vessel['vessel_type']})<br>"
        f"Port: {vessel['port_id']} | Zone: {vessel['zone']}<br>"
        f"Length: {vessel['estimated_length_m']} m | Conf: {vessel['confidence']:.0%}<br>"
        f"Offshore Dist: {vessel['distance_from_shore_km']} km | ETA: {eta_str}<br>"
        f"AIS: {ais_status}"
    )


def build_folium_map(
    analyzed: dict[str, Any],
    selected_port_id: str | None = None,
) -> folium.Map:
    """Build interactive Folium maritime map with port zones and color-coded vessels."""
    ports_cfg = analyzed["ports_config"].get("ports", [])
    coastal_km = float(analyzed["ports_config"].get("coastal_zone_km", 50))
    vessels = analyzed["enriched_vessels"]
    ports_summary = {p["port_id"]: p for p in analyzed["packet"].get("ports_summary", [])}

    visible_ports = [p for p in ports_cfg if selected_port_id in (None, "ALL", p["port_id"])]
    if selected_port_id not in (None, "ALL") and visible_ports:
        center_lat = visible_ports[0]["lat"]
        center_lon = visible_ports[0]["lon"]
        zoom = 11
    else:
        center_lat = 15.5
        center_lon = 79.5
        zoom = 5

    fmap = folium.Map(location=[center_lat, center_lon], zoom_start=zoom, tiles="OpenStreetMap")

    for port in visible_ports:
        pid = port["port_id"]
        summary = ports_summary.get(pid, {})
        util_pct = round(float(summary.get("utilization_index", 0.0)) * 100)
        dark_cnt = int(summary.get("dark_vessels_detected", 0))

        # 50 km Coastal Boundary
        folium.Circle(
            location=[port["lat"], port["lon"]],
            radius=coastal_km * 1000.0,
            color="#3B82F6",
            weight=1.5,
            dash_array="6, 4",
            fill=False,
            tooltip=f"{port['port_name']} — 50 km Coastal Approach Boundary",
        ).add_to(fmap)

        # Berth Zone
        folium.Polygon(
            locations=_bbox_to_rect(port["berth_bbox"]),
            color="#10B981",
            weight=2,
            fill=True,
            fill_opacity=0.22,
            tooltip=f"{pid} Berth Zone ({util_pct}% occupied)",
        ).add_to(fmap)

        # Anchorage Zone
        folium.Polygon(
            locations=_bbox_to_rect(port["anchorage_bbox"]),
            color="#F59E0B",
            weight=2,
            dash_array="4, 3",
            fill=True,
            fill_opacity=0.14,
            tooltip=f"{pid} Anchorage Queue Zone",
        ).add_to(fmap)

        folium.Marker(
            location=[port["lat"], port["lon"]],
            icon=folium.Icon(color="darkblue", icon="anchor", prefix="fa"),
            tooltip=f"{port['port_name']} [{pid}] — Util {util_pct}% | Dark: {dark_cnt}",
        ).add_to(fmap)

    for vessel in vessels:
        if selected_port_id not in (None, "ALL") and vessel["port_id"] != selected_port_id:
            continue
        hex_color, _, label = _vessel_visual_style(vessel)
        popup_html = _vessel_popup_html(vessel)
        radius = 9 if not vessel.get("ais_matched") else 7
        folium.CircleMarker(
            location=[vessel["latitude"], vessel["longitude"]],
            radius=radius,
            color="#111827" if not vessel.get("ais_matched") else hex_color,
            weight=2,
            fill=True,
            fill_color=hex_color,
            fill_opacity=0.92,
            popup=folium.Popup(popup_html, max_width=320),
            tooltip=f"{vessel['vessel_id']} · {label}",
        ).add_to(fmap)

    return fmap


def build_offline_plotly_map(
    analyzed: dict[str, Any],
    selected_port_id: str | None = None,
) -> go.Figure:
    """Build an offline-safe Plotly maritime map (no external tile servers required)."""
    ports_cfg = analyzed["ports_config"].get("ports", [])
    coastal_km = float(analyzed["ports_config"].get("coastal_zone_km", 50))
    vessels = analyzed["enriched_vessels"]
    ports_summary = {p["port_id"]: p for p in analyzed["packet"].get("ports_summary", [])}

    visible_ports = [p for p in ports_cfg if selected_port_id in (None, "ALL", p["port_id"])]
    fig = go.Figure()

    for port in visible_ports:
        pid = port["port_id"]
        c_lats, c_lons = _circle_polygon_latlon(port["lat"], port["lon"], coastal_km)
        fig.add_trace(
            go.Scattergeo(
                lat=c_lats,
                lon=c_lons,
                mode="lines",
                line={"width": 1.5, "color": "#3B82F6", "dash": "dot"},
                name=f"{pid} 50km Boundary",
                hoverinfo="text",
                text=f"{port['port_name']} 50 km Coastal Boundary",
                showlegend=False,
            )
        )
        anch_pts = _bbox_to_rect(port["anchorage_bbox"])
        fig.add_trace(
            go.Scattergeo(
                lat=[pt[0] for pt in anch_pts],
                lon=[pt[1] for pt in anch_pts],
                mode="lines",
                line={"width": 1.5, "color": "#F59E0B"},
                name=f"{pid} Anchorage",
                hoverinfo="text",
                text=f"{pid} Anchorage Zone",
                showlegend=False,
            )
        )
        berth_pts = _bbox_to_rect(port["berth_bbox"])
        fig.add_trace(
            go.Scattergeo(
                lat=[pt[0] for pt in berth_pts],
                lon=[pt[1] for pt in berth_pts],
                mode="lines",
                line={"width": 2, "color": "#10B981"},
                name=f"{pid} Berth",
                hoverinfo="text",
                text=f"{pid} Berth Zone",
                showlegend=False,
            )
        )

    if visible_ports:
        fig.add_trace(
            go.Scattergeo(
                lat=[p["lat"] for p in visible_ports],
                lon=[p["lon"] for p in visible_ports],
                mode="markers+text",
                text=[p["port_id"] for p in visible_ports],
                textposition="top center",
                marker={"size": 10, "color": "#1E3A8A", "symbol": "triangle-up"},
                hovertext=[
                    f"{p['port_name']} [{p['port_id']}] — Util {ports_summary.get(p['port_id'], {}).get('utilization_index', 0) * 100:.0f}%"
                    for p in visible_ports
                ],
                hoverinfo="text",
                name="Regional Ports",
            )
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for vessel in vessels:
        if selected_port_id not in (None, "ALL") and vessel["port_id"] != selected_port_id:
            continue
        _, _, label = _vessel_visual_style(vessel)
        grouped.setdefault(label, []).append(vessel)

    for label, group in grouped.items():
        hex_color, _, _ = _vessel_visual_style(group[0])
        fig.add_trace(
            go.Scattergeo(
                lat=[v["latitude"] for v in group],
                lon=[v["longitude"] for v in group],
                mode="markers",
                marker={
                    "size": [13 if not v.get("ais_matched") else 10 for v in group],
                    "color": hex_color,
                    "line": {"width": 1.2, "color": "#111827"},
                },
                text=[_vessel_popup_html(v).replace("<br>", " | ").replace("<b>", "").replace("</b>", "") for v in group],
                hoverinfo="text",
                name=label,
            )
        )

    if selected_port_id not in (None, "ALL") and visible_ports:
        p0 = visible_ports[0]
        lon_range = [p0["lon"] - 1.5, p0["lon"] + 1.5]
        lat_range = [p0["lat"] - 1.5, p0["lat"] + 1.5]
    else:
        lon_range = [67.0, 90.0]
        lat_range = [6.5, 22.5]

    fig.update_geos(
        resolution=50,
        showcoastlines=True,
        coastlinecolor="#64748B",
        showland=True,
        landcolor="#E2E8F0",
        showocean=True,
        oceancolor="#0F172A",
        showcountries=True,
        countrycolor="#475569",
        lonaxis_range=lon_range,
        lataxis_range=lat_range,
    )
    fig.update_layout(
        height=470,
        margin={"l": 0, "r": 0, "t": 10, "b": 0},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "xanchor": "left", "x": 0},
    )
    return fig


def _generate_ground_preview_images(
    packet: dict[str, Any],
    sea_state: str,
    uploaded_bytes: bytes | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate deterministic SAR & detected-overlay previews when onboard_ai isn't merged yet."""
    size = 384
    if uploaded_bytes:
        try:
            img = Image.open(io.BytesIO(uploaded_bytes)).convert("L").resize((size, size))
            raw_arr = np.asarray(img, dtype=np.uint8)
        except Exception:
            raw_arr = None
    else:
        raw_arr = None

    if raw_arr is None:
        rng = np.random.default_rng(abs(hash((sea_state, len(packet.get("vessels", []))))) % (2**32))
        noise_scale = SEA_STATE_NOISE_SCALE.get(sea_state, 34)
        sea = rng.gamma(shape=3.0, scale=noise_scale / 3.0, size=(size, size))
        raw_arr = np.clip(sea, 0, 255).astype(np.uint8)
        # Simulated coastal land strip on left edge
        x_idx = np.arange(size)[None, :]
        coast_curve = (58 + 14 * np.sin(np.linspace(0, 3.2 * np.pi, size)))[:, None]
        raw_arr[x_idx < coast_curve] = np.clip(
            135 + rng.normal(0, 12, size=(size, size)), 90, 220
        ).astype(np.uint8)[x_idx < coast_curve]

    vessels = packet.get("vessels", [])
    if uploaded_bytes is None:
        for idx, vessel in enumerate(vessels):
            vx = 110 + ((idx * 63) % 210)
            vy = 60 + ((idx * 79) % 260)
            half = 6 if vessel["vessel_type"] == "FISHING_BOAT" else (9 if vessel["vessel_type"] == "FERRY" else 13)
            raw_arr[max(0, vy - half // 2) : vy + half // 2, max(0, vx - half) : vx + half] = 248

    overlay_img = Image.fromarray(raw_arr).convert("RGB")
    draw = ImageDraw.Draw(overlay_img)
    for idx, vessel in enumerate(vessels):
        vx = 110 + ((idx * 63) % 210)
        vy = 60 + ((idx * 79) % 260)
        half = 8 if vessel["vessel_type"] == "FISHING_BOAT" else (11 if vessel["vessel_type"] == "FERRY" else 15)
        hex_color, _, _ = _vessel_visual_style(vessel)
        draw.rectangle([vx - half, vy - half, vx + half, vy + half], outline=hex_color, width=2)
        draw.text((vx - half, max(2, vy - half - 12)), vessel["vessel_id"], fill=hex_color)

    return raw_arr, np.asarray(overlay_img, dtype=np.uint8)


def _run_simulation_action(
    port_id: str | None,
    sea_state: str,
    pass_time_ist: str,
    uploaded_bytes: bytes | None,
) -> dict[str, Any]:
    """Execute onboard_ai.edge_pipeline.run_pass if available, else fall back to sample JSON."""
    if ONBOARD_PIPELINE_AVAILABLE and onboard_run_pass is not None:
        return onboard_run_pass(
            port_id=port_id,
            sea_state=sea_state,
            pass_time_ist=pass_time_ist,
            uploaded_image=uploaded_bytes,
            output_path=str(DEFAULT_LATEST_TELEMETRY_PATH),
        )

    packet, _ = load_telemetry_packet(DEFAULT_SAMPLE_TELEMETRY_PATH, prefer_latest=False)
    sim_packet = copy.deepcopy(packet)
    sim_packet["telemetry_header"]["sea_state"] = sea_state
    sim_packet["telemetry_header"]["pass_timestamp_utc"] = IST_TO_UTC.get(
        pass_time_ist, sim_packet["telemetry_header"]["pass_timestamp_utc"]
    )
    if port_id is not None:
        sim_packet["ports_summary"] = [
            p for p in sim_packet["ports_summary"] if p["port_id"] == port_id
        ]
        sim_packet["vessels"] = [
            v for v in sim_packet["vessels"] if v["port_id"] == port_id
        ]
        sim_packet["telemetry_header"]["downlinked_json_size_kb"] = round(
            len(json.dumps(sim_packet, separators=(",", ":")).encode("utf-8")) / 1024.0, 2
        )

    raw_preview, overlay_preview = _generate_ground_preview_images(sim_packet, sea_state, uploaded_bytes)
    return {
        "telemetry": sim_packet,
        "raw_preview": raw_preview,
        "overlay_preview": overlay_preview,
        "alert_chips": [],
        "fallback_mode": True,
    }


def main() -> None:
    """Render the 4-panel EdgePort Streamlit dashboard."""
    st.set_page_config(
        page_title="EdgePort — Regional Port & Coastal Vessel Intelligence",
        page_icon="🛰️",
        layout="wide",
    )

    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.2rem; padding-bottom: 2rem; }
        .edgeport-banner {
            background: linear-gradient(90deg, #0F172A 0%, #1E3A8A 100%);
            color: #F8FAFC;
            padding: 1rem 1.25rem;
            border-radius: 10px;
            margin-bottom: 1rem;
        }
        .edgeport-banner h2 { margin: 0; font-size: 1.45rem; }
        .edgeport-banner p { margin: 0.25rem 0 0 0; color: #CBD5E1; font-size: 0.9rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        """
        <div class="edgeport-banner">
            <h2>🛰️ EdgePort — Onboard Satellite AI & Regional Port Intelligence</h2>
            <p>Track A (Ground Station &amp; Operator Dashboard) · 0–370 km Indian EEZ · Low-Bandwidth Telemetry (&lt; 3 KB JSON)</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if "active_packet" not in st.session_state:
        initial_packet, initial_source = load_telemetry_packet()
        st.session_state["active_packet"] = initial_packet
        st.session_state["packet_source_label"] = str(initial_source.name)
        st.session_state["sim_result"] = None
        st.session_state["pass_history"] = []

    with st.sidebar:
        st.header("Ground Station Controls")
        ports_cfg = load_ports_config()
        port_options = {"ALL": "All 4 Ports (Constellation Sweep)"}
        for p in ports_cfg.get("ports", []):
            port_options[p["port_id"]] = f"{p['port_name']} [{p['port_id']}]"

        selected_map_port = st.selectbox(
            "Focus Port (Map & Operations)",
            options=list(port_options.keys()),
            format_func=lambda k: port_options[k],
        )
        use_live_ais = st.checkbox(
            "Attempt Live AISStream.io Feed",
            value=False,
            help="Requires AISSTREAM_API_KEY env var; gracefully falls back to data/ais_ground_truth.json.",
        )
        offline_map_mode = st.checkbox(
            "Offline Map Mode (Plotly Geo)",
            value=False,
            help="Renders built-in vector coastlines with zero external map tile requests.",
        )
        if st.button("Reload Telemetry from Disk", use_container_width=True):
            pkt, src = load_telemetry_packet()
            st.session_state["active_packet"] = pkt
            st.session_state["packet_source_label"] = str(src.name)
            st.session_state["sim_result"] = None

        st.divider()
        st.caption(
            "Onboard Pipeline (`onboard_ai`): "
            + ("🟢 Connected" if ONBOARD_PIPELINE_AVAILABLE else "🟡 Fallback (`sample_telemetry.json`)")
        )
        st.caption(f"Active Packet Source: `{st.session_state['packet_source_label']}`")

    ais_feed = fetch_ais_feed(use_live=use_live_ais)
    analyzed = analyze_telemetry(
        packet=st.session_state["active_packet"],
        ais_records=ais_feed["records"],
        ports_config=ports_cfg,
    )
    alerts_by_port = format_all_alerts(analyzed)
    kpis = analyzed["kpis"]
    header = analyzed["packet"]["telemetry_header"]

    if not st.session_state["pass_history"]:
        st.session_state["pass_history"] = build_multi_pass_nowcast(analyzed)

    # ------------------------------------------------------------------
    # PANEL 1: Top KPI Telemetry Strip (Track B + Track A Proof)
    # ------------------------------------------------------------------
    st.subheader("1. Orbital Telemetry & Ground KPI Strip")
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric(
        "Orbital Bandwidth Saved",
        f"{kpis['bandwidth_saved_pct']:.2f}%",
        f"{kpis['raw_scene_size_mb']:.1f} MB → {kpis['downlinked_json_size_kb']:.2f} KB JSON",
    )
    k2.metric(
        "Onboard Inference Time",
        f"{kpis['onboard_inference_time_ms']:.1f} ms",
        f"{header['sensor_mode'].split('_')[0]} · {header['sea_state']}",
    )
    k3.metric(
        "Active Vessels (≤ 370 km EEZ)",
        f"{kpis['total_active_vessels']}",
        f"{kpis['total_dark_vessels']} Dark Vessel(s) flagged",
        delta_color="inverse" if kpis["total_dark_vessels"] > 0 else "normal",
    )
    k4.metric(
        "Port Congestion Alerts (>75%)",
        f"{kpis['congested_ports_count']} / {kpis['total_ports']} Ports",
        "High / Critical Berth Load" if kpis["congested_ports_count"] else "All Ports Normal",
        delta_color="inverse" if kpis["congested_ports_count"] > 0 else "normal",
    )
    k5.metric(
        "Live Regional Trade Proxy",
        f"₹ {kpis['total_trade_value_inr_cr']:.1f} Cr",
        f"{kpis['total_cargo_tonnage_mt']:,.0f} MT Estimated Cargo",
    )

    st.caption(f"AIS Status: {ais_feed['status_message']}")
    st.divider()

    # ------------------------------------------------------------------
    # PANEL 2: Interactive Maritime Map (Track A — Port Operator View)
    # ------------------------------------------------------------------
    st.subheader("2. Interactive Maritime Map (0–50 km Coastal Zones & Dark Vessel Alerts)")
    legend_cols = st.columns(4)
    legend_cols[0].markdown("🟢 **Green**: Fishing Boat (AIS)")
    legend_cols[1].markdown("🔵 **Cyan**: Ferry (AIS)")
    legend_cols[2].markdown("🟡 **Gold**: Cargo Ship (AIS)")
    legend_cols[3].markdown("🔴 **Red**: Unmatched Dark Vessel")

    if offline_map_mode:
        st.plotly_chart(
            build_offline_plotly_map(analyzed, selected_map_port),
            use_container_width=True,
        )
    else:
        try:
            fmap = build_folium_map(analyzed, selected_map_port)
            st_folium(fmap, height=460, use_container_width=True, returned_objects=[])
        except Exception as exc:  # noqa: BLE001 — offline tile/folium fallback
            st.warning(f"Folium renderer unavailable ({exc}); switching to offline Plotly map.")
            st.plotly_chart(
                build_offline_plotly_map(analyzed, selected_map_port),
                use_container_width=True,
            )

    vessel_rows = [
        {
            "Vessel ID": v["vessel_id"],
            "Port": v["port_id"],
            "Type": v["vessel_type"],
            "Zone": v["zone"],
            "Length (m)": v["estimated_length_m"],
            "Dist (km)": v["distance_from_shore_km"],
            "ETA (min)": v["eta_minutes_effective"],
            "Confidence": f"{v['confidence']:.0%}",
            "AIS Status": "MATCHED" if v["ais_matched"] else f"DARK ({v['alert_flag']})",
            "AIS Vessel / MMSI": f"{v['matched_ais_name']} ({v['matched_ais_mmsi']})"
            if v["ais_matched"]
            else "No Transponder Match",
        }
        for v in analyzed["enriched_vessels"]
        if selected_map_port in ("ALL", None) or v["port_id"] == selected_map_port
    ]
    if vessel_rows:
        st.dataframe(pd.DataFrame(vessel_rows), use_container_width=True, hide_index=True)

    st.divider()

    # ------------------------------------------------------------------
    # PANEL 3: Onboard AI Live Simulator Panel
    # ------------------------------------------------------------------
    st.subheader("3. Onboard AI Live Simulator (Raw SAR → CFAR/ONNX → < 3 KB Telemetry)")
    sim_c1, sim_c2, sim_c3, sim_c4 = st.columns([2, 1.5, 1.2, 2.3])
    with sim_c1:
        sim_port = st.selectbox(
            "Target Port / Sweep",
            options=list(port_options.keys()),
            format_func=lambda k: port_options[k],
            key="sim_port_select",
        )
    with sim_c2:
        sim_sea_state = st.selectbox(
            "Weather / Sea State",
            options=["CALM", "MODERATE", "MONSOON_ROUGH"],
            index=1,
        )
    with sim_c3:
        sim_pass_time = st.selectbox(
            "LEO Pass (IST)",
            options=["06:00", "12:00", "18:00", "23:30"],
            index=2,
        )
    with sim_c4:
        uploaded_file = st.file_uploader(
            "Optional Sentinel-1 SAR (.tif / .png)",
            type=["tif", "tiff", "png"],
        )

    if st.button("🛰️ Run Orbital Pass Simulation", type="primary"):
        target_port = None if sim_port == "ALL" else sim_port
        uploaded_bytes = uploaded_file.read() if uploaded_file is not None else None
        sim_result = _run_simulation_action(
            port_id=target_port,
            sea_state=sim_sea_state,
            pass_time_ist=sim_pass_time,
            uploaded_bytes=uploaded_bytes,
        )
        st.session_state["active_packet"] = sim_result["telemetry"]
        st.session_state["sim_result"] = sim_result
        st.session_state["packet_source_label"] = (
            "onboard_ai.edge_pipeline.run_pass"
            if not sim_result.get("fallback_mode")
            else "sample_telemetry.json (simulator fallback)"
        )
        st.rerun()

    sim_result = st.session_state.get("sim_result")
    if sim_result is not None:
        if sim_result.get("fallback_mode"):
            st.info(
                "Displaying ground-station preview generated from `contracts/sample_telemetry.json` "
                "(Person A's `onboard_ai/edge_pipeline.py` will replace this automatically once merged)."
            )
        col_raw, col_overlay, col_json = st.columns(3)
        with col_raw:
            st.markdown("**Raw SAR Backscatter Scene**")
            st.image(sim_result["raw_preview"], clamp=True, use_container_width=True)
        with col_overlay:
            st.markdown("**Onboard CFAR + ONNX Overlay**")
            st.image(sim_result["overlay_preview"], clamp=True, use_container_width=True)
        with col_json:
            st.markdown("**Downlinked Telemetry Packet (< 3 KB)**")
            st.json(sim_result["telemetry"])

        chips = sim_result.get("alert_chips") or []
        if chips:
            st.markdown("**64×64 Alert Chips (Triggered on Critical Congestion / Offshore Dark Vessel)**")
            chip_cols = st.columns(min(6, len(chips)))
            for idx, chip_bytes in enumerate(chips[:6]):
                chip_cols[idx].image(chip_bytes, caption=f"Chip #{idx + 1}")
    else:
        with st.expander("Inspect Active Downlinked JSON Telemetry Packet", expanded=False):
            st.json(analyzed["packet"])

    st.divider()

    # ------------------------------------------------------------------
    # PANEL 4: Small-Port Operations, Trade Nowcasting & SMS Alert Center
    # ------------------------------------------------------------------
    st.subheader("4. Small-Port Operations, Trade Nowcasting & SMS Alert Center")

    op_left, op_right = st.columns([1.15, 1.0])

    with op_left:
        st.markdown("#### Port Berth Utilization & Automated Rerouting")
        for port in analyzed["packet"].get("ports_summary", []):
            util = float(port["utilization_index"])
            status = port["congestion_status"]
            color = CONGESTION_BADGE_COLORS.get(status, "#64748B")
            counts = port["counts"]
            st.markdown(
                f"**{port['port_name']} (`{port['port_id']}`)** — "
                f"<span style='color:{color};font-weight:700'>{status}</span> "
                f"({counts['berth_occupied']}/{port['max_berths']} berths · "
                f"{counts['anchorage_waiting']} queued · {port['dark_vessels_detected']} dark)",
                unsafe_allow_html=True,
            )
            st.progress(min(1.0, max(0.0, util)))

        if analyzed["rerouting_suggestions"]:
            for suggestion in analyzed["rerouting_suggestions"]:
                st.warning(f"⚠️ **Reroute Advisory (>80% Load):** {suggestion['message']}")
        else:
            st.success("All monitored regional ports are operating below the 80% rerouting threshold.")

        st.markdown("#### Incoming Traffic & ETA Queue")
        if analyzed["eta_queue"]:
            eta_df = pd.DataFrame(
                [
                    {
                        "Vessel": v["vessel_id"],
                        "Port": v["port_id"],
                        "Type": v["vessel_type"],
                        "Zone": v["zone"],
                        "ETA (min)": v["eta_minutes_effective"],
                        "AIS": "Verified" if v["ais_matched"] else "DARK VESSEL",
                    }
                    for v in analyzed["eta_queue"]
                ]
            )
            st.dataframe(eta_df, use_container_width=True, hide_index=True)
        else:
            st.caption("No inbound vessels currently queued outside berths.")

    with op_right:
        st.markdown("#### Multi-Pass Regional Trade Nowcasting (₹ Crores)")
        nowcast_rows = build_multi_pass_nowcast(analyzed)
        nowcast_df = pd.DataFrame(nowcast_rows)
        if not nowcast_df.empty:
            fig_trade = go.Figure()
            for pid, grp in nowcast_df.groupby("port_id"):
                fig_trade.add_trace(
                    go.Scatter(
                        x=grp["pass_time_ist"],
                        y=grp["estimated_trade_value_inr_cr"],
                        mode="lines+markers",
                        name=f"{pid} ({grp['port_name'].iloc[0]})",
                    )
                )
            fig_trade.update_layout(
                height=290,
                margin={"l": 10, "r": 10, "t": 15, "b": 10},
                xaxis_title="LEO Satellite Pass (IST)",
                yaxis_title="Estimated Trade Value (₹ Cr)",
                legend={"orientation": "h", "y": -0.25},
            )
            st.plotly_chart(fig_trade, use_container_width=True)

        st.markdown("#### Port Master SMS / WhatsApp Alert Center (≤ 160 Chars)")
        dispatch_lines: list[str] = []
        for pid, alert_text in alerts_by_port.items():
            char_len = len(alert_text)
            st.markdown(f"**`{pid}`** · `{char_len} / {MAX_SMS_LENGTH} chars`")
            st.code(alert_text, language="text")
            dispatch_lines.append(alert_text)

        st.download_button(
            label="Download SMS Dispatch Log (.txt)",
            data="\n".join(dispatch_lines),
            file_name="edgeport_sms_alerts.txt",
            mime="text/plain",
        )


if __name__ == "__main__":
    main()
