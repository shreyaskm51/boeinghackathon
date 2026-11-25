# Single-Port Full-Screen Map Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the multi-port dashboard with one port's own screen: a full-screen MapLibre light map of the picked port with frosted floating panels, driven by real Sentinel-1 data.

**Architecture:** Two small server additions (`bounds`, `age_days`, `NOT_FETCHED`) feed a rewritten React app. Pure logic (port state, formatting) lives in `frontend/src/lib/` with Vitest unit tests; presentational components in `frontend/src/components/`; one `PortMap` component owns MapLibre. The old Leaflet map and multi-port panels are deleted.

**Tech Stack:** React 19, Vite 6, `maplibre-gl` ^6.19.0, Vitest (new dev dependency), OpenFreeMap Positron style; Python 3.12 server + pytest.

**Spec:** `docs/superpowers/specs/2026-11-26-port-view-redesign-design.md` (mockup: `docs/superpowers/specs/assets/2026-11-26-port-view-mockup.html`, option A)

## Global Constraints

- Map style URL: `https://tiles.openfreemap.org/styles/positron` (no key). Map attribution control must stay visible.
- Real-data attribution text `real_data.attribution` must be visible on screen.
- Data source: `GET /api/dashboard?source=real`; refresh: `POST /api/real/refresh` body `{"port_id": "<ID>"}`.
- Port list from `ports_config.ports` (60 ports incl. 4 `ISLAND`); never hard-code ports.
- Old pass threshold: **30 days** (`OLD_PASS_DAYS = 30`).
- Last picked port stored in `localStorage` key `edgeport.port`.
- Vessel colours: cargo `#f59e0b`, ferry `#06b6d4`, fishing `#10b981`; anchorage outline amber `#e08a00`, berths blue `#1d6fd8`.
- Panels: white ~72 % opacity, `backdrop-filter: blur(10px)`, radius 14 px, dark navy text, one blue accent.
- Radar layer default **off**, ~70 % opacity when on.
- Must be usable at 1280×720 in current Chrome/Edge.
- No other-port data on screen; no simulated-data UI; no live-AIS checkbox.
- All existing pytest tests keep passing.

## Review Focus

1. A current port with **zero detected ships** → ship panel says "No ships detected in this pass", map still fits the port, no crash. *(Task 3 test, Task 5 browser check)*
2. `localStorage` holds a **port id no longer in the config** → falls back to the first port. *(Task 3 test)*
3. **Refresh fails** (server error / network) → toast shows the server's `error` text, previous data stays on screen, button re-enables. *(Task 5 browser check)*
4. **Long port names** ("Pipavav Regional Terminal", "Ratnagiri Mirkarwada Port") at 1280×720 → top bar does not overflow or overlap the pass info. *(Task 5 browser check)*
5. **Map style unreachable** (offline) → panels still render and work; map area shows a plain background, no uncaught error. *(Task 4 browser check)*

---

### Task 1: Server: per-port `bounds` / `age_days` and a NOT_FETCHED state

**Files:**
- Modify: `ground_station/api_server.py` (`EdgePortState.real_ports_info`)
- Modify: `onboard_ai/real_sentinel1.py` (`build_real_packet`, `_empty_summary`)
- Modify: `ground_station/alert_formatter.py` (`format_port_alert`)
- Test: `tests/test_real_sentinel1.py`, `tests/test_ground_analytics.py`

**Interfaces:**
- Produces: `real_data.ports[pid]` for `status == "OK"` additionally has `bounds: [min_lon, min_lat, max_lon, max_lat]` (from cached `window.bounds`) and `age_days: int` (whole days from `scene_datetime` to now, UTC). Ports with no cache file stay absent from `real_data.ports`.
- Produces: in `build_real_packet()`, `ports_summary[i].data_source` is `"NOT_FETCHED"` when the port has no cache file, `"NO_SCENE"` when its cache status is not `"OK"`.
- Produces: `format_port_alert` returns exactly `"EDGEPORT ALERT [<ID>]: Not observed yet, fetch a satellite pass."` for `data_source == "NOT_FETCHED"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_real_sentinel1.py` (use the existing `real_cache` fixture and `_fake_result`):
```python
def test_unfetched_ports_are_marked_not_fetched(real_cache):
    _cache(real_cache, _fake_result("IN-KRW", 2))
    (rs.REAL_DIR / "IN-KVT.json").write_text(json.dumps({"port_id": "IN-KVT", "status": "NO_SCENE"}), encoding="utf-8")
    packet = rs.build_real_packet()
    by_id = {p["port_id"]: p for p in packet["ports_summary"]}
    assert by_id["IN-KVT"]["data_source"] == "NO_SCENE"
    assert by_id["IN-PNJ"]["data_source"] == "NOT_FETCHED"
    assert by_id["IN-KRW"]["data_source"] == "SENTINEL-1"

def test_real_ports_info_has_bounds_and_age(real_cache):
    api = pytest.importorskip("ground_station.api_server")
    res = _fake_result("IN-KRW", 1, "2026-11-20T00:55:31Z")
    res["window"]["bounds"] = [73.9, 14.7, 74.2, 14.9]
    _cache(real_cache, res)
    info = api.EdgePortState().real_ports_info()["IN-KRW"]
    assert info["bounds"] == [73.9, 14.7, 74.2, 14.9]
    assert isinstance(info["age_days"], int) and info["age_days"] >= 0
```
In `tests/test_ground_analytics.py`:
```python
def test_alert_for_not_fetched_port() -> None:
    alert = format_port_alert({"port_id": "IN-PNJ", "max_berths": 5, "data_source": "NOT_FETCHED", "counts": {}}, [],
                              ais_checked=False)
    assert alert == "EDGEPORT ALERT [IN-PNJ]: Not observed yet, fetch a satellite pass."
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_real_sentinel1.py tests/test_ground_analytics.py -q -k "not_fetched or bounds_and_age"`
Expected: 3 failed (`NO_SCENE != NOT_FETCHED`, `KeyError: 'bounds'`, wrong alert text).

- [ ] **Step 3: Implement**

`_empty_summary(port, data_source)` takes the label; `build_real_packet` passes `"NOT_FETCHED"` when `load_cached` returns `None`, else `"NO_SCENE"`. `real_ports_info` adds `bounds` from `res["window"]["bounds"]` and `age_days` computed from `scene_datetime`. `format_port_alert` gets an early return for `NOT_FETCHED` next to the existing `NO_SCENE` one.

- [ ] **Step 4: Run the full Python suite**

Run: `python -m pytest tests/ -q -p no:cacheprovider`
Expected: all pass (≥ 120 tests).

- [ ] **Step 5: Commit**

```bash
git add ground_station/api_server.py onboard_ai/real_sentinel1.py ground_station/alert_formatter.py tests/
git commit -m "Server: per-port radar bounds and scene age; NOT_FETCHED port state"
```

---

### Task 2: Frontend tooling and formatting helpers

**Files:**
- Modify: `frontend/package.json` (add `maplibre-gl` ^6.19.0 dependency; add `vitest` dev dependency; add script `"test": "vitest run"`; remove `leaflet`)
- Create: `frontend/src/lib/format.js`
- Test: `frontend/src/lib/format.test.js`

**Interfaces:**
- Produces: `formatPassTimeIST(iso: string) -> string` e.g. `"24 Nov, 06:25 IST"`; `daysAgoLabel(ageDays: number) -> string` (`0 → "today"`, `1 → "1 day ago"`, `n → "n days ago"`); `approachingCount(counts) -> number` = `coastal_cargo_vessels + approaching_ferries + coastal_fishing_boats` (missing keys count as 0); `vesselLabel(vessel) -> string` e.g. `"Cargo 139 m"`; `etaLabel(vessel) -> string` (`zone === "BERTH" → "at berth"`, else `"<eta_minutes_effective> min"`, null → `"—"`).

- [ ] **Step 1: Install dependencies**

Run: `cd frontend && npm install maplibre-gl@^6.19.0 && npm install -D vitest && npm uninstall leaflet`
Expected: `package.json` updated, no errors. (`MaritimeMap.jsx` still imports leaflet until Task 4 deletes it — do not run `npm run build` in this task.)

- [ ] **Step 2: Write the failing tests** (`format.test.js`)

```js
import { describe, expect, it } from "vitest";
import { approachingCount, daysAgoLabel, etaLabel, formatPassTimeIST, vesselLabel } from "./format.js";

describe("format", () => {
  it("formats a UTC pass time in IST", () => {
    expect(formatPassTimeIST("2026-11-24T00:55:27.515477Z")).toBe("24 Nov, 06:25 IST");
    expect(formatPassTimeIST("2026-07-26T20:00:00Z")).toBe("27 Jul, 01:30 IST");
  });
  it("labels ages", () => {
    expect(daysAgoLabel(0)).toBe("today");
    expect(daysAgoLabel(1)).toBe("1 day ago");
    expect(daysAgoLabel(121)).toBe("121 days ago");
  });
  it("counts approaching ships across the three coastal buckets", () => {
    expect(approachingCount({ coastal_cargo_vessels: 10, approaching_ferries: 2, coastal_fishing_boats: 3 })).toBe(15);
    expect(approachingCount({ approaching_ferries: 1 })).toBe(1);
  });
  it("labels vessels and ETAs", () => {
    expect(vesselLabel({ vessel_type: "CARGO_VESSEL", estimated_length_m: 138.9 })).toBe("Cargo 139 m");
    expect(vesselLabel({ vessel_type: "FISHING_BOAT", estimated_length_m: 12 })).toBe("Fishing 12 m");
    expect(etaLabel({ zone: "BERTH", eta_minutes_effective: 0 })).toBe("at berth");
    expect(etaLabel({ zone: "ANCHORAGE_QUEUE", eta_minutes_effective: 13 })).toBe("13 min");
    expect(etaLabel({ zone: "OFFSHORE_EEZ", eta_minutes_effective: null })).toBe("—");
  });
});
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/format.test.js`
Expected: FAIL, cannot resolve `./format.js`.

- [ ] **Step 4: Implement `frontend/src/lib/format.js`**

IST = UTC + 5 h 30 min: add 330 minutes to the epoch and read `getUTC*` parts (deterministic, no locale/ICU dependence). Month names `Jan`…`Dec`, two-digit hour/minute.

- [ ] **Step 5: Run to verify it passes**

Run: `cd frontend && npm test`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add frontend/package.json frontend/package-lock.json frontend/src/lib/
git commit -m "Frontend: add MapLibre and Vitest; formatting helpers"
```

---

### Task 3: Port view model (which port, which state, what to show)

**Files:**
- Create: `frontend/src/lib/portState.js`
- Test: `frontend/src/lib/portState.test.js`

**Interfaces:**
- Consumes: dashboard payload shape (spec "Data the view uses").
- Produces:
  - `OLD_PASS_DAYS = 30`
  - `pickInitialPortId(ports: Port[], storedId: string | null) -> string` — `storedId` if it is in `ports`, else `ports[0].port_id`.
  - `selectPortView(data, portId) -> PortView` where
    `PortView = { port, summary, vessels, alert, reroute, real, state, ageDays, hasAnyRealData }`;
    `vessels` = `data.enriched_vessels` filtered to `portId` (order preserved);
    `alert` = `data.alerts_by_port[portId] ?? null`;
    `reroute` = first `data.rerouting_suggestions` item with `port_id === portId`, else `null`;
    `real` = `data.real_data.ports[portId] ?? null`;
    `state` ∈ `"current" | "old" | "no_scene" | "not_fetched"`:
    `real == null || !data.real_data.has_packet` → `"not_fetched"`; `real.status !== "OK"` → `"no_scene"`;
    `real.age_days > OLD_PASS_DAYS` → `"old"`; else `"current"`.
    `ageDays` = `real?.age_days ?? null`.

- [ ] **Step 1: Write the failing tests** (`portState.test.js`), building a minimal payload fixture inline:

```js
import { describe, expect, it } from "vitest";
import { OLD_PASS_DAYS, pickInitialPortId, selectPortView } from "./portState.js";

const ports = [{ port_id: "IN-KRW" }, { port_id: "IN-PNJ" }, { port_id: "IN-KVT" }, { port_id: "IN-DGH" }];
const data = {
  ports_config: { ports },
  packet: { ports_summary: ports.map((p) => ({ port_id: p.port_id, counts: {} })) },
  enriched_vessels: [
    { vessel_id: "S1-PNJ-001", port_id: "IN-PNJ" }, { vessel_id: "S1-KRW-001", port_id: "IN-KRW" },
    { vessel_id: "S1-PNJ-002", port_id: "IN-PNJ" },
  ],
  alerts_by_port: { "IN-PNJ": "EDGEPORT ALERT [IN-PNJ] 24 Nov: …" },
  rerouting_suggestions: [{ port_id: "IN-PNJ", recommended_port_id: "IN-MRM" }],
  real_data: { has_packet: true, ports: {
    "IN-PNJ": { status: "OK", age_days: 2 }, "IN-KRW": { status: "OK", age_days: 121 },
    "IN-KVT": { status: "NO_SCENE", error: "Newest Sentinel-1 scene is from 2020-04-22" },
  } },
};

describe("pickInitialPortId", () => {
  it("keeps a stored port that still exists", () => expect(pickInitialPortId(ports, "IN-PNJ")).toBe("IN-PNJ"));
  it("falls back to the first port for unknown or missing ids", () => {
    expect(pickInitialPortId(ports, "IN-OLD")).toBe("IN-KRW");
    expect(pickInitialPortId(ports, null)).toBe("IN-KRW");
  });
});

describe("selectPortView", () => {
  it("shows only the picked port's vessels, alert and reroute", () => {
    const v = selectPortView(data, "IN-PNJ");
    expect(v.vessels.map((x) => x.vessel_id)).toEqual(["S1-PNJ-001", "S1-PNJ-002"]);
    expect(v.alert).toMatch(/^EDGEPORT ALERT \[IN-PNJ\]/);
    expect(v.reroute.recommended_port_id).toBe("IN-MRM");
    expect(v.state).toBe("current");
  });
  it("derives old / no_scene / not_fetched states", () => {
    expect(OLD_PASS_DAYS).toBe(30);
    expect(selectPortView(data, "IN-KRW").state).toBe("old");
    expect(selectPortView(data, "IN-KVT").state).toBe("no_scene");
    expect(selectPortView(data, "IN-DGH").state).toBe("not_fetched");
    expect(selectPortView({ ...data, real_data: { has_packet: false, ports: {} } }, "IN-PNJ").state).toBe("not_fetched");
  });
  it("handles a current port with no ships", () => {
    const v = selectPortView({ ...data, enriched_vessels: [] }, "IN-PNJ");
    expect(v.vessels).toEqual([]);
    expect(v.state).toBe("current");
    expect(v.reroute.port_id).toBe("IN-PNJ");
    expect(selectPortView(data, "IN-DGH").reroute).toBeNull();
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && npx vitest run src/lib/portState.test.js`
Expected: FAIL, cannot resolve `./portState.js`.

- [ ] **Step 3: Implement `frontend/src/lib/portState.js`** with the signatures above.

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && npm test`
Expected: all Vitest tests pass.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/lib/portState.js frontend/src/lib/portState.test.js
git commit -m "Frontend: port view model (state, filtering, initial port)"
```

---

### Task 4: Full-screen MapLibre port map and new app shell

**Files:**
- Create: `frontend/src/components/PortMap.jsx`
- Rewrite: `frontend/src/App.jsx` (shell only in this task: load data, port picker in a minimal top bar, `PortMap` full screen)
- Rewrite: `frontend/src/styles.css` (new light theme base: page, map container, `.glass` panel class)
- Delete: `frontend/src/components/MaritimeMap.jsx`, `frontend/src/components/RealDataPanel.jsx`

**Interfaces:**
- Consumes: `selectPortView`, `pickInitialPortId` (Task 3).
- Produces `PortMap` props:
  `{ port, vessels, selectedVesselId: string|null, onSelectVessel(id: string), radar: { url: string, bounds: number[] } | null, radarOn: boolean, onToggleRadar(): void, padding: {top,right,bottom,left} }`.
  Map sources/layers: `zones` (GeoJSON polygons from `anchorage_bbox` + `berth_bbox`, fill ~8 % + dashed outline + small labels "ANCHORAGE"/"BERTHS"), `vessels` (GeoJSON points, `circle` layer coloured by type with white stroke; selected vessel larger), `radar` (`image` source at `bounds` corners, raster opacity 0.7, visible only when `radarOn`). Camera: `fitBounds(union(berth_bbox, anchorage_bbox))` with `padding` on every port change; `flyTo` the selected vessel. Clicking a vessel calls `onSelectVessel`; hover shows a small popup with `vesselLabel`. Radar toggle is a map-corner button rendered by `PortMap`, shown only when `radar` is non-null.
- Produces in `App.jsx`: state `portId`, `data`, `selectedVesselId`, `radarOn`; `loadDashboard()` → `GET /api/dashboard?source=real`; port change saves `localStorage["edgeport.port"]` and clears selection.
- On MapLibre `error` events: keep running, log once to console, never throw.

- [ ] **Step 1: Implement `PortMap.jsx`, the shell `App.jsx`, base `styles.css`; delete the two old components**

Style URL from the Global Constraints, overridable by `import.meta.env.VITE_MAP_STYLE` (used only by the offline check below). Import `maplibre-gl/dist/maplibre-gl.css`. Create the map once; update sources with `setData` / `updateImage` on prop changes (wait for the style `load` event before adding sources).

- [ ] **Step 2: Build**

Run: `cd frontend && npm run build && npm test`
Expected: build succeeds with no leaflet import errors; Vitest still green.

- [ ] **Step 3: Browser check (dev server + API)**

Start `python -m ground_station.api_server`, open `http://127.0.0.1:8000`.
Expected: Positron light map fills the window; picker lists 60 ports; IN-PNJ shows the Mandovi estuary with amber anchorage and blue berth boxes and 17 dots; switching to IN-PBL (island) re-fits to Port Blair; clicking a dot selects it; radar toggle overlays the grey radar image aligned with the coastline; reload remembers the last port.

- [ ] **Step 4: Offline-style check (Review Focus 5)**

Run: `cd frontend && VITE_MAP_STYLE=http://127.0.0.1:9/none.json npm run build`, reload the page.
Expected: no uncaught error in the console, the port picker still works; rebuild without the variable afterwards.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src
git commit -m "Frontend: full-screen MapLibre port map and new app shell"
```

---

### Task 5: Floating frosted panels and per-port states

**Files:**
- Create: `frontend/src/components/TopBar.jsx`, `PortStatsPanel.jsx`, `ShipsPanel.jsx`, `SmsPanel.jsx`, `StatePanel.jsx`, `Toast.jsx`
- Modify: `frontend/src/App.jsx` (compose panels; refresh handler), `frontend/src/styles.css` (panel layout and states)

**Interfaces:**
- Consumes: `PortView` (Task 3), `format.js` helpers (Task 2), `PortMap` (Task 4).
- Components and props:
  - `TopBar({ ports, portId, onChangePort, view, refreshing, onRefresh })` — wordmark "EdgePort", picker (name · state), pass text `"Sentinel-1 pass {formatPassTimeIST} · {daysAgoLabel}"` (state current/old), amber badge `"Old satellite pass: {n} days ago"` when `state === "old"`, ⟳ button (spins and disabled while `refreshing`).
  - `PortStatsPanel({ view })` (left) — berths `berth_occupied / max_berths` + bar, waiting at anchorage, approaching (`approachingCount`), offshore (only if > 0), cargo value `₹{estimated_trade_value_inr_cr} Cr`, congestion chip; class `stale` (numbers at ~60 % opacity) + the old-pass badge when `state === "old"`.
  - `ShipsPanel({ view, selectedVesselId, onSelectVessel })` (right) — header `"Ships detected · {n}"`, optional reroute line at top (`"Busy — consider {recommended_port_id}"`), rows with type dot, `vesselLabel`, zone, `etaLabel`; selected row highlighted; empty list text `"No ships detected in this pass"`.
  - `SmsPanel({ alert })` (bottom centre) — monospace alert + copy button that shows "Copied" for 1.5 s; not rendered when `alert` is null or state is `not_fetched`.
  - `StatePanel({ view, refreshing, onFetch })` (centre) — for `no_scene`: "No recent satellite pass" + `real.error`; for `not_fetched`: "Not fetched yet" + button "Fetch this port (~15 s)".
  - `Toast({ message, onClose })` — bottom-right, dismissible.
- Refresh: `POST /api/real/refresh` with `{port_id}`; on success replace `data` with the response; on non-2xx show `json.error` (or `"Refresh failed (status)"`) in the toast and keep the previous `data`.
- `real_data.attribution` shown as small text above the map attribution.
- Panel positions: top bar full width; left panel top-left under the bar (~220 px wide); ships panel top-right (~260 px, scrolls internally, max height so it never covers the SMS); SMS bottom-centre (~60 % width). Pass `padding` to `PortMap` matching these so the port is not hidden behind panels.

- [ ] **Step 1: Implement the six components, compose them in `App.jsx`, style them**

- [ ] **Step 2: Build and unit tests**

Run: `cd frontend && npm run build && npm test`
Expected: build OK, Vitest green.

- [ ] **Step 3: Browser check per state at 1280×720**

With the server running and current cached data:
- IN-PNJ (current): all four panels, pass "24 Nov, 06:25 IST · N days ago", 17 ships, clicking a row flies the map; SMS copy shows "Copied".
- IN-KRW (old, 121 days): amber "Old satellite pass" badge in top bar and left panel, faded numbers, ships still listed.
- IN-KVT (no scene): "No recent satellite pass" with the 2020 reason, no ships.
- Not fetched: temporarily move `outputs/real/IN-DGH.json` aside and rebuild the packet (`python -c "from onboard_ai.real_sentinel1 import build_real_packet; build_real_packet()"`), select IN-DGH → "Not fetched yet" + fetch button, no SMS; click it → after ~15 s the port shows current data. Restore nothing (the fetch recreated the file).
- Long names (Review Focus 4): IN-PPV "Pipavav Regional Terminal" and IN-RTN — top bar fits on one line or wraps cleanly without overlap.
- Refresh failure (Review Focus 3): stop the API server, click ⟳ → toast with an error, panels keep their data, button re-enables.

- [ ] **Step 4: Commit**

```bash
git add -A frontend/src
git commit -m "Frontend: frosted floating panels and per-port states"
```

---

### Task 6: Clean-up, docs and final verification

**Files:**
- Modify: `README.md` (dashboard section: one port at a time, picker, light map, radar toggle, old-pass rule), `docs/NOTES_FOR_PERSON_B.md` (frontend rewritten: list removed components and new ones)
- Modify: `ground_station/api_server.py` only if nothing in the UI still needs `nowcast` / `national_network` — **leave the payload unchanged** (other clients and tests use it).

- [ ] **Step 1: Check for leftovers**

Run: `grep -rn "leaflet\|MaritimeMap\|RealDataPanel\|SimulatorPanel\|TradeNowcast" frontend/src frontend/package.json`
Expected: no matches.

- [ ] **Step 2: Update the two docs**

- [ ] **Step 3: Full verification**

Run: `python -m pytest tests/ -q -p no:cacheprovider` → all pass;
`cd frontend && npm test && npm run build` → green;
browser: load the dashboard fresh, pick 3 ports (current, old, island), radar toggle on/off — no console errors.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/NOTES_FOR_PERSON_B.md
git commit -m "Docs: single-port dashboard"
```
