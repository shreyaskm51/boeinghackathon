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
