// What the single-port screen shows for the picked port (pure, unit-tested).

/** A real scene older than this many days gets the "old satellite pass" treatment. */
export const OLD_PASS_DAYS = 30;

/** The remembered port if it still exists in the config, otherwise the first port. */
export function pickInitialPortId(ports, storedId) {
  return ports.some((p) => p.port_id === storedId) ? storedId : ports[0]?.port_id ?? "";
}

/**
 * Everything the view needs for one port, taken only from that port's slice of the
 * dashboard payload.
 * state: "current" | "old" | "no_scene" | "not_fetched"
 */
export function selectPortView(data, portId) {
  const hasAnyRealData = Boolean(data.real_data?.has_packet);
  const real = data.real_data?.ports?.[portId] ?? null;

  let state;
  if (!real || !hasAnyRealData) state = "not_fetched";
  else if (real.status !== "OK") state = "no_scene";
  else if (real.age_days > OLD_PASS_DAYS) state = "old";
  else state = "current";

  return {
    port: data.ports_config?.ports?.find((p) => p.port_id === portId) ?? null,
    summary: data.packet?.ports_summary?.find((s) => s.port_id === portId) ?? null,
    vessels: (data.enriched_vessels ?? []).filter((v) => v.port_id === portId),
    alert: data.alerts_by_port?.[portId] ?? null,
    reroute: (data.rerouting_suggestions ?? []).find((r) => r.port_id === portId) ?? null,
    real,
    state,
    ageDays: real?.age_days ?? null,
    hasAnyRealData,
  };
}

/** Radar layer for a port, cache-busted by fetch time so a refresh shows the new scene. */
export function radarFor(real) {
  if (!real || real.status !== "OK") return null;
  const v = real.fetched_utc ? `?v=${encodeURIComponent(real.fetched_utc)}` : "";
  return { url: `${real.raw_preview_url}${v}`, bounds: real.bounds };
}
