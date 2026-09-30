// Thin typed client for the VoltPath API. The access token lives in memory; a copy in
// sessionStorage keeps a reload signed in (per-tab convenience only - never required).

export type Me = { sub: string; name: string; role: string; tenant: string; tenant_id: number; permissions: string[] }

export type LiveState = {
  vin: string; ts: number; lat: number; lon: number; speed: number; soc: number; soh: number; temp: number
  power: number; odo: number; status: 'DRIVING' | 'PARKED' | 'CHARGING' | 'OFFLINE'; charger_id: number | null
  dtc: string[]; location_masked?: boolean
}

export type Summary = {
  vehicles: number; online: number; driving: number; charging: number; parked: number; offline: number
  low_soc: number; avg_soc: number | null; charging_power_kw: number; open_alerts: Record<string, number>
  today: { charge_cost: number; energy_kwh: number; sessions: number }
  battery_risk: { soh_below_80_within_1y: number; analysed: number }; ts: number
}

export type Range = { baseline_km: number; ml_km: number | null; range_km: number; model_version: string | null
  recent_wh_per_km: number | null; ambient_c: number }

export type VehicleDetail = {
  vehicle: { vin: string; registration: string; model_year: number; fleet: string; fleet_id: number; model: string
    model_code: string; oem: string; capacity_kwh: number; rated_range_km: number; chemistry: string; pack_serial: string }
  state: LiveState | null
  range?: Range
  dtc?: { code: string; description: string; severity: string }[]
  forecast: { soh_now: number; slope_per_year: number; days_to_80: number | null; anomaly_score: number
    dc_fast_share: number } | null
  open_alerts: { id: number; type_code: string; message: string; created_at: string }[]
  driver?: { id: number; full_name: string | null; phone: string | null } | null
}

export type Station = { id: number; name: string; operator: string; lat: number; lon: number; connectors: number
  free: number; max_kw: number; dc: boolean; price_now: number; road_km?: number; eta_min?: number
  arrival_soc?: number; reachable?: boolean; route?: [number, number][] | null; charge_starts_15m?: number }

export type Reachable = { vin: string; soc: number; range: Range; chargers: Station[]; warning: string | null
  search?: { multi_target_expanded: number; astar_expanded: number | null; dijkstra_expanded: number | null } }

export type Plan = { vin: string; charger: { id: number; type: string; max_kw: number; station: string; station_id: number }
  soc_now: number; target_soc: number; depart_at: string; feasible: boolean; energy_kwh: number; cost: number
  wear_cost: number; baseline_cost: number; savings: number; final_soc: number; currency: string; notes: string[]
  slots: { start: string; kw: number; kwh: number; price: number }[] }

export type Alert = { id: number; vin: string; type_code: string; severity?: string; message: string
  event_ts?: string | number; created_at: string; status?: string; details?: Record<string, unknown> }

export type Page<T> = { items: T[]; next_cursor: string | null }

let token: string | null = null
try { token = sessionStorage.getItem('vp-token') } catch { /* storage unavailable */ }

export const auth = {
  get token() { return token },
  set(t: string | null) {
    token = t
    try { if (t) sessionStorage.setItem('vp-token', t); else sessionStorage.removeItem('vp-token') } catch { /* ignore */ }
  },
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) { super(message); this.status = status }
}

let onUnauthorized: () => void = () => {}
export function setUnauthorizedHandler(fn: () => void) { onUnauthorized = fn }

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (token) headers.set('Authorization', `Bearer ${token}`)
  if (init.body && typeof init.body === 'string') headers.set('Content-Type', 'application/json')
  const res = await fetch(path, { ...init, headers })
  if (res.status === 401) { auth.set(null); onUnauthorized() }
  if (!res.ok) {
    let msg = res.statusText
    try { const j = await res.json(); msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch { /* noop */ }
    throw new ApiError(res.status, msg)
  }
  return res.json() as Promise<T>
}

export async function login(email: string, password: string) {
  const body = new URLSearchParams({ grant_type: 'password', username: email, password })
  const res = await fetch('/v1/auth/token', { method: 'POST', body })
  if (!res.ok) throw new ApiError(res.status, res.status === 401 ? 'Incorrect email or password' : 'Sign-in failed')
  const j = await res.json()
  auth.set(j.access_token)
}

export function liveSocketUrl() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  return `${proto}://${location.host}/v1/ws/live?token=${encodeURIComponent(token ?? '')}`
}
