// Small display helpers for the single-port view (pure functions, unit-tested).

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const IST_OFFSET_MIN = 330; // UTC + 5 h 30 min
const pad2 = (n) => String(n).padStart(2, "0");

/** "2026-11-24T00:55:27Z" -> "24 Nov, 06:25 IST" (deterministic, no locale data needed). */
export function formatPassTimeIST(iso) {
  const ist = new Date(Date.parse(iso) + IST_OFFSET_MIN * 60_000);
  return `${ist.getUTCDate()} ${MONTHS[ist.getUTCMonth()]}, ${pad2(ist.getUTCHours())}:${pad2(ist.getUTCMinutes())} IST`;
}

/** 0 -> "today", 1 -> "1 day ago", n -> "n days ago". */
export function daysAgoLabel(ageDays) {
  if (ageDays <= 0) return "today";
  return ageDays === 1 ? "1 day ago" : `${ageDays} days ago`;
}

/** Ships in the 0-50 km approach: cargo + ferries + fishing boats not yet at anchorage/berth. */
export function approachingCount(counts) {
  return (counts.coastal_cargo_vessels ?? 0) + (counts.approaching_ferries ?? 0) + (counts.coastal_fishing_boats ?? 0);
}

const TYPE_NAMES = { CARGO_VESSEL: "Cargo", FERRY: "Ferry", FISHING_BOAT: "Fishing" };

/** { vessel_type: "CARGO_VESSEL", estimated_length_m: 138.9 } -> "Cargo 139 m". */
export function vesselLabel(vessel) {
  return `${TYPE_NAMES[vessel.vessel_type] ?? vessel.vessel_type} ${Math.round(vessel.estimated_length_m)} m`;
}

/** "at berth" for berthed ships, "13 min" for an ETA, "—" when unknown. */
export function etaLabel(vessel) {
  if (vessel.zone === "BERTH") return "at berth";
  const eta = vessel.eta_minutes_effective;
  return eta === null || eta === undefined ? "—" : `${eta} min`;
}
