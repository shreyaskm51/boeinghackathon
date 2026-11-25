// EdgePort API client + types for the React dashboard.
// Copy this file into the React project (e.g. src/api/edgeport.ts).
// Packet types mirror contracts/telemetry_schema.json; server: python -m api.server

export const API_BASE: string =
  (import.meta as any).env?.VITE_EDGEPORT_API ?? "http://127.0.0.1:8000";

export type SeaState = "CALM" | "MODERATE" | "MONSOON_ROUGH";
export type PassTimeIST = "06:00" | "12:00" | "18:00" | "23:30";
export type SensorMode = "C-BAND" | "X-BAND";
export type VesselType = "FISHING_BOAT" | "FERRY" | "CARGO_VESSEL";
export type Zone = "BERTH" | "ANCHORAGE_QUEUE" | "COASTAL_APPROACH" | "OFFSHORE_EEZ";
export type CongestionStatus = "NORMAL" | "MODERATE" | "HIGH_CONGESTION" | "CRITICAL_CONGESTION";
/** [min_lon, min_lat, max_lon, max_lat] in WGS84 */
export type BBox = [number, number, number, number];

export interface TelemetryHeader {
  satellite_id: string;
  sensor_mode: "C-BAND_SAR_ALL_WEATHER" | "X-BAND_SAR_HIGH_RES";
  pass_timestamp_utc: string;
  monitoring_limit_km: number;
  raw_scene_size_mb: number;
  cropped_roi_size_mb: number;
  downlinked_json_size_kb: number;
  bandwidth_saved_pct: number;
  onboard_inference_time_ms: number;
  sea_state: SeaState;
}

export interface PortSummary {
  port_id: string;
  port_name: string;
  state: string;
  max_berths: number;
  counts: {
    berth_occupied: number;
    anchorage_waiting: number;
    coastal_fishing_boats: number;
    approaching_ferries: number;
    offshore_eez_vessels: number;
  };
  utilization_index: number;
  congestion_status: CongestionStatus;
  estimated_cargo_tonnage_mt: number;
  estimated_trade_value_inr_cr: number;
  dark_vessels_detected: number;
}

export interface Vessel {
  vessel_id: string;
  port_id: string;
  vessel_type: VesselType;
  latitude: number;
  longitude: number;
  distance_from_shore_km: number;
  zone: Zone;
  estimated_length_m: number;
  confidence: number;
  ais_matched: boolean;
  eta_minutes: number | null;
}

export interface TelemetryPacket {
  telemetry_header: TelemetryHeader;
  ports_summary: PortSummary[];
  vessels: Vessel[];
}

export interface PortConfig {
  port_id: string;
  port_name: string;
  state: string;
  coast: "WEST" | "EAST";
  lat: number;
  lon: number;
  max_berths: number;
  operational_profile: string;
  berth_bbox: BBox;
  anchorage_bbox: BBox;
  eez_corridor_bbox: BBox;
}

export interface PortsResponse {
  ports: PortConfig[];
  pass_times_ist: PassTimeIST[];
  sea_states: SeaState[];
  sensor_modes: SensorMode[];
}

export interface PassStats {
  raw_scene_size_mb: number;
  cropped_roi_size_mb: number;
  downlinked_json_bytes: number;
  bandwidth_saved_pct: number;
  onboard_inference_time_ms: number;
  total_runtime_ms: number;
  cfar_candidates: number;
  rejected_as_clutter: number;
  vessels_detected: number;
  vessels_in_packet: number;
  preview_port_id: string | null;
  source: "SIMULATED" | "UPLOAD";
  sensor_mode: SensorMode;
}

export interface PassResponse {
  pass_id: string | null;
  telemetry: TelemetryPacket;
  /** Output of ground_station enrich_packet(), or null if that hook is not implemented. */
  ground_station: unknown | null;
  /** Present for passes run through POST /api/pass (relative URLs: prefix with API_BASE). */
  stats?: PassStats;
  images?: Partial<Record<"raw_preview" | "overlay_preview" | "xband_raw_preview" | "xband_overlay_preview", string>>;
  alert_chips?: { url: string; label: string }[];
  generated_at?: string;
}

export interface PassRequest {
  portId?: string;            // omit / "" = all ports
  seaState?: SeaState | string;
  passTimeIst?: PassTimeIST;
  sensorMode?: SensorMode;
  seed?: number;
  image?: File | Blob;        // optional .tif / .png / .npy upload
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const imageUrl = (relative: string) => `${API_BASE}${relative}`;

export const getPorts = () => fetch(`${API_BASE}/api/ports`).then((r) => json<PortsResponse>(r));

export const getLatest = () => fetch(`${API_BASE}/api/telemetry/latest`).then((r) => json<PassResponse>(r));

export function runPass(req: PassRequest = {}): Promise<PassResponse> {
  const form = new FormData();
  form.append("port_id", req.portId ?? "");
  form.append("sea_state", req.seaState ?? "MODERATE");
  form.append("pass_time_ist", req.passTimeIst ?? "18:00");
  form.append("sensor_mode", req.sensorMode ?? "C-BAND");
  if (req.seed !== undefined) form.append("seed", String(req.seed));
  if (req.image) form.append("image", req.image);
  return fetch(`${API_BASE}/api/pass`, { method: "POST", body: form }).then((r) => json<PassResponse>(r));
}
