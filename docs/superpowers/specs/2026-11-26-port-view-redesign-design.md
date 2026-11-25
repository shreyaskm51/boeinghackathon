# EdgePort dashboard redesign: single-port, full-screen light map

Date: 2026-11-26 · Status: ready for approval

## Intent and why
EdgePort is used **by one port at a time**. A port master (e.g. at Panaji) only wants
information about their own port, not the other 59. Today's dashboard is a long, evenly
weighted multi-port page (national KPIs, 60-row berth list, 60-port directory), which is
noise for that user and doesn't look polished.

The redesign makes the page **one port's own screen**: the port's map fills the whole
window and the key facts float on top in frosted panels. The visual direction comes from a
reference the user shared (a light, frosted "command centre": a large central map filling
the screen, translucent white panels with small charts floating left and right, thin top
navigation, pale blue-grey palette), and from mockup **A – light map**, which the user
picked: [`assets/2026-11-26-port-view-mockup.html`](assets/2026-11-26-port-view-mockup.html)
(option A is the visual bar; option B was rejected). The mockup's basemap is Esri light
gray; the build uses OpenFreeMap Positron, which looks similar.

## Goals
- Pick a port; see only that port.
- Full-screen, light-themed map of the picked port, zoomed to its harbour and approach.
- Floating frosted panels with that port's status, its ships, and its SMS alert.
- Real Sentinel-1 data front and centre (pass date, real detections, the real radar image as
  an optional map layer).
- Looks polished enough to demo to judges and to give to a port master.

## Non-goals
- No multi-port overview, national totals, port directory or all-ports map.
- No simulated-data mode in the UI (the `/api/simulate` endpoint may stay on the server).
- No new backend features beyond what the view needs (see Server change).
- No login / per-port accounts (the port picker is the only way to choose a port).
- No 3D terrain / elevation data.

## Anti-goals (would make this a failure even if it works)
- Showing other ports' data on the port's screen.
- Information hidden behind clicks that a port master needs at a glance (berths, queue,
  approaching ships, pass date, SMS).
- Implying data is live or AIS-verified when it isn't (keep "pass N days ago" and
  "AIS not checked" visible).
- Panels so large or opaque that the map stops being the main thing on screen.

## Constraints
- Lives in `frontend/` (React 19 + Vite 6), served by `ground_station/api_server.py`
  (stdlib HTTP server) from `frontend/dist`; dev mode via `npm run dev` proxying `/api`.
- Data comes from the existing `GET /api/dashboard?source=real` payload (field list below);
  refresh via existing `POST /api/real/refresh {"port_id": ...}` (~12–15 s per port).
- Map tiles must be free and keyless: **OpenFreeMap "Positron"** style
  (`https://tiles.openfreemap.org/styles/positron`, verified HTTP 200, no key).
  Attribution must stay visible (OpenFreeMap, OpenMapTiles, OpenStreetMap contributors).
- Real-data attribution must stay visible somewhere on screen: "Contains modified Copernicus
  Sentinel data … OpenSARShip (CC-BY-NC-SA)" (string is `real_data.attribution`).
- Port list is `ports_config.ports` (60 ports incl. 4 `ISLAND` ports); never hard-code.
- Must work in current Chrome/Edge on a laptop (≥ 1280×720). Phone layout is not required
  but panels must not overlap into unusability at 1280×720.

## Approaches considered
1. **Leaflet + custom CSS** (rebuild on the existing map library). Fastest, no new
   dependency, but raster maps and less smooth zoom; less like the reference.
2. **MapLibre GL + custom CSS** — *chosen by the user*. Vector map (crisp at any zoom, smooth
   camera, slight tilt possible), closest to the reference's polished feel. Costs a new
   dependency (`maplibre-gl` ^6, MIT) and a from-scratch map component.
3. **UI kit (Tailwind / MUI) on top of 1.** Ready-made components but a new build setup and a
   generic look for only ~5 panels. Rejected.

## The design (as decided)

### Layout (desktop)
```
┌───────────────────────────────────────────────────────────────────┐
│ EdgePort      [ Panaji Mandovi Jetty · Goa ▾ ]   Pass 24 Nov · 2d ago ⟳│  top bar (frosted)
│┌─────────────┐                                      ┌────────────┐ │
││ Berths 3/5  │     FULL-SCREEN LIGHT MAP (MapLibre) │ Ships (17) │ │  left: port stats
││ ███░░       │   anchorage ▭   berths ▭   ● ● ●     │ ● Cargo 139│ │  right: ship list
││ Queue    4  │                                      │ …          │ │
││ Approach 10 │      [ Radar layer toggle ]          └────────────┘ │
││ ₹34.3 Cr    │   ┌─────────────────────────────────┐               │
│└─────────────┘   │ SMS: EDGEPORT ALERT [IN-PNJ] … ⧉ │               │  bottom: SMS
│                  └─────────────────────────────────┘               │
└───────────────────────────────────────────────────────────────────┘
```
- The map is `position: fixed; inset: 0` behind everything. Panels float over it with
  frosted glass: white ~72 % opacity, `backdrop-filter: blur(10px)`, 14 px radius, soft
  blue-grey shadow, dark navy text, one blue accent. (Matches mockup A.)
- **Top bar:** "EdgePort" wordmark · port picker (dropdown of all 60 ports, name + state) ·
  pass info ("Sentinel-1 pass 24 Nov, 06:25 IST · 2 days ago", local IST) · ⟳ refresh button.
- **Left panel (port status):** berths occupied `berth_occupied / max_berths` with a bar;
  waiting at anchorage; approaching (0–50 km: `coastal_cargo_vessels + approaching_ferries +
  coastal_fishing_boats`); offshore vessels (only if > 0); estimated cargo value (₹ Cr);
  congestion status as a coloured chip.
- **Right panel (ships):** list of this port's vessels (type dot colour, type, length, zone,
  ETA or "at berth"), sorted as the API already sorts them (priority). Clicking a row flies
  the map to that ship and highlights it. Header shows the count. If the port has a
  rerouting suggestion, show it at the top of this panel; ETA queue information is the ETA
  column (no separate ETA panel).
- **Bottom panel (SMS):** the port's alert text from `alerts_by_port[port_id]`, monospace,
  with a copy button ("Copied" feedback).
- **Map content:**
  - Camera fits the port's `anchorage_bbox ∪ berth_bbox` with padding so panels don't cover
    it; slight tilt is the builder's call.
  - Anchorage and berth areas drawn as outlined, lightly filled polygons with small labels
    (anchorage amber, berths blue, as in mockup A).
  - Vessels as circle markers coloured by type (cargo amber, ferry cyan, fishing green),
    white outline; hover shows a small label; click selects (same as list click).
  - **Radar layer toggle:** a small control on the map; when on, the port's real Sentinel-1
    raw preview PNG is laid over the map as a georeferenced image at ~70 % opacity, exactly
    covering the scene window bounds. Default **off**.
- **Per-port states** (decided by `real_data.ports[pid]`):

  | State | Condition | What the screen shows |
  |---|---|---|
  | Current | `status == "OK"` and scene ≤ 30 days old | Everything as described above |
  | Old pass *(user decision: option A, 30 days)* | `status == "OK"` and scene > 30 days old | Everything as above, **plus** an amber "Old satellite pass: N days ago" badge in the top bar and on the left panel, and the left-panel numbers slightly faded. Ships still listed and drawn. |
  | No recent scene | `status` present and not `"OK"` (e.g. Kavaratti, `NO_SCENE`) | Map of the port; panels say "No recent satellite pass" with `error` as the reason; no ships; SMS shows the server's "No recent satellite pass…" alert |
  | Not fetched yet *(my call)* | `real_data.ports[pid]` absent (no cache file) | Map of the port; a panel "Not fetched yet" with a "Fetch this port (~15 s)" button; **no SMS panel** (the server's text would be wrong) |
  | No real data at all | `real_data.has_packet` false | Same as "Not fetched yet" for whichever port is selected |

  (Today all ports are cached except Kavaratti = `NO_SCENE`; IN-KRW, IN-BLK, IN-HNV, IN-TAD
  are 121 days old and IN-MNC 43 days, so "Old pass" is exercised by real data.)
- **Loading / errors:** while refreshing, the ⟳ button spins and is disabled; errors appear
  as a small dismissible toast, not a page-wide banner.

### Removed from the UI
Left sidebar, national KPI strip, 60-port berth list, port directory/search, data-source
(real/simulated) switch, live-AIS checkbox, offline-SVG map mode, the old Leaflet
`MaritimeMap`, the old `RealDataPanel`. (Leaflet can be removed from `package.json` if
nothing else uses it.)

### Port picker behaviour
- Default port on first load: the last port the user picked (remembered in
  `localStorage`), else the first port in `ports_config`.
- Changing the port re-centres the map and swaps all panels; it does **not** re-fetch from
  the network beyond the existing dashboard payload (all ports' data is already in it).

### Server changes (small)
- `real_data.ports[pid]` gains the scene window `bounds: [min_lon, min_lat, max_lon, max_lat]`
  (already stored per port in `outputs/real/<pid>.json` under `window.bounds`) so the radar
  image can be georeferenced, and `age_days` (whole days since `scene_datetime`).
- *(my call)* Ports with no cache file get `data_source: "NOT_FETCHED"` (not `"NO_SCENE"`) in
  `onboard_ai/real_sentinel1.build_real_packet()`, and `ground_station/alert_formatter.py`
  gives them "EDGEPORT ALERT [ID]: Not observed yet, fetch a satellite pass." instead of the
  wrong "No recent satellite pass" text.
- No other backend changes. Existing tests must keep passing; add tests for the new fields.

### Data the view uses (all already in `GET /api/dashboard?source=real`)
- `ports_config.ports[]`: `port_id, port_name, state, coast, lat, lon, max_berths,
  berth_bbox, anchorage_bbox`
- `packet.ports_summary[]` (match by `port_id`): `counts{berth_occupied, anchorage_waiting,
  coastal_fishing_boats, approaching_ferries, coastal_cargo_vessels, offshore_eez_vessels}`,
  `utilization_index, congestion_status, estimated_trade_value_inr_cr, observed_utc, data_source`
- `enriched_vessels[]` (filter by `port_id`): `vessel_id, vessel_type, latitude, longitude,
  zone, estimated_length_m, eta_minutes_effective, confidence, alert_flag`
- `alerts_by_port[port_id]`, `rerouting_suggestions[]` (filter by `port_id`)
- `real_data.ports[port_id]`: `status, error, scene_datetime, platform, sea_state,
  vessels_detected, raw_preview_url, overlay_preview_url` (+ new `bounds`)
- `real_data.attribution`, `real_data.has_packet`

## Left to the builder
- Exact spacing, font sizes, colours within the frosted-light direction; font choice
  (a self-hosted or system font; no external font CDN required).
- Map tilt/zoom limits, marker sizes, label collision handling.
- Component breakdown and state management (plain React state is fine).
- Whether to keep `/api/simulate` and simulator code on the server (UI must not expose it).
- Responsive behaviour below 1280 px (must stay usable, needn't be pretty).
- Tests: at least a build check and a browser check per state (current port with ships, old
  pass, island port, no-scene port, not-fetched port, no-data-yet); Python tests for the new
  `bounds` / `age_days` / `NOT_FETCHED` behaviour.
