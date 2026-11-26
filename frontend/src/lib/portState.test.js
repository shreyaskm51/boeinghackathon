import { describe, expect, it } from "vitest";
import { OLD_PASS_DAYS, pickInitialPortId, radarFor, selectPortView } from "./portState.js";

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

describe("radarFor", () => {
  it("cache-busts the radar image with the fetch time so a refresh shows the new scene", () => {
    const real = { status: "OK", raw_preview_url: "/api/real/preview/IN-PNJ/raw.png", bounds: [1, 2, 3, 4], fetched_utc: "2026-11-25T21:34:00Z" };
    expect(radarFor(real)).toEqual({ url: "/api/real/preview/IN-PNJ/raw.png?v=2026-11-25T21%3A34%3A00Z", bounds: [1, 2, 3, 4] });
    expect(radarFor({ status: "NO_SCENE" })).toBeNull();
    expect(radarFor(null)).toBeNull();
  });
});
