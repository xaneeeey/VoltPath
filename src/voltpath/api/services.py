"""Domain services shared by REST routes and copilot tools.

Every function takes the calling Principal, runs tenant-scoped (Redis keys per tenant + RLS
transactions) and applies privacy masking, so an agent tool can never see more than the human
who invoked it.
"""

from __future__ import annotations

import math
from dataclasses import asdict
import time
from datetime import UTC, datetime, timedelta

import orjson
from fastapi import HTTPException

from voltcore.charging_dp import SLOT_MIN, plan_charging, price_at, slot_prices
from voltcore.dtc import describe
from voltcore.energy import MODELS_BY_CODE
from voltcore.geo import mask_location
from voltpath.api.auth import Principal
from voltpath.api.deps import R, tenant_tx
from voltpath.ml import predict_range
from voltpath.simulator.world import ambient_c

IST = timedelta(hours=5, minutes=30)
OFFLINE_S = 180
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, ttl: float):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    return None


def mask(p: Principal, d: dict) -> dict:
    """Location privacy: without location:precise, coordinates snap to a ~5 km geohash cell."""
    if not p.can("location:precise") and d.get("lat") is not None:
        d = dict(d)
        d["lat"], d["lon"] = mask_location(d["lat"], d["lon"], 5)
        d["location_masked"] = True
    return d


PUBLIC_FIELDS = ("vin", "ts", "lat", "lon", "speed", "soc", "soh", "temp", "power", "odo", "status", "charger_id",
                 "dtc")


def public_state(raw: bytes, now: float | None = None) -> dict:
    """Project the processor's checkpoint onto the public live-state contract."""
    d = orjson.loads(raw)
    st = {k: d.get(k) for k in PUBLIC_FIELDS}
    for k in ("lat", "lon"):
        st[k] = round(st[k], 6)
    for k in ("speed", "soc", "temp", "power", "odo"):
        st[k] = round(st[k], 1)
    st["soh"] = round(st["soh"], 2)
    if (now or time.time()) - st["ts"] > OFFLINE_S:
        st["status"] = "OFFLINE"
    return st


async def live_state(p: Principal, vin: str) -> dict | None:
    raw = await R.redis.hget(f"vs:{p.tenant_id}", vin)
    return public_state(raw) if raw is not None else None


async def live_states(p: Principal, vins: list[str]) -> dict[str, dict]:
    if not vins:
        return {}
    raws = await R.redis.hmget(f"vs:{p.tenant_id}", vins)
    now = time.time()
    return {v: mask(p, public_state(raw, now)) for v, raw in zip(vins, raws, strict=True) if raw}


async def vehicle_master(p: Principal, vin: str) -> dict:
    async with tenant_tx(p) as c:
        row = await (await c.execute(
            "SELECT v.vin, v.registration, v.model_year, v.commissioned_on, v.fleet_id, f.name AS fleet, "
            "v.model_code, m.name AS model, m.oem_code AS oem, m.capacity_kwh, m.rated_range_km, "
            "b.chemistry, b.serial AS pack_serial "
            "FROM vehicle v JOIN fleet f ON f.id = v.fleet_id JOIN vehicle_model m ON m.code = v.model_code "
            "LEFT JOIN battery_pack b ON b.vin = v.vin WHERE v.vin = %s", (vin,))).fetchone()
    if row is None:
        raise HTTPException(404, "vehicle not found")
    row["capacity_kwh"] = float(row["capacity_kwh"])
    return row


async def recent_consumption(p: Principal, vin: str) -> tuple[float | None, float]:
    """(recent Wh/km from the last trips, recent average speed) - the vehicle-specific features."""
    async with tenant_tx(p) as c:
        r = await (await c.execute(
            "SELECT sum(energy_kwh) AS kwh, sum(distance_km) AS km, "
            "sum(distance_km) / NULLIF(sum(extract(epoch FROM ended_at - started_at)) / 3600, 0) AS spd "
            "FROM (SELECT energy_kwh, distance_km, started_at, ended_at FROM trip "
            "      WHERE vin = %s AND distance_km > 0.5 ORDER BY started_at DESC LIMIT 5) t", (vin,))).fetchone()
    whkm = float(r["kwh"]) * 1000 / float(r["km"]) if r and r["km"] and r["kwh"] else None
    spd = float(r["spd"]) if r and r["spd"] else 28.0
    if whkm is not None and not (60 <= whkm <= 600):
        whkm = None
    return whkm, spd


async def range_for(p: Principal, vin: str, st: dict, model_code: str) -> dict:
    model = MODELS_BY_CODE[model_code]
    whkm, spd = await recent_consumption(p, vin)
    amb = ambient_c(datetime.now(UTC))
    return predict_range(R.range_model, model, st["soc"], st["soh"], amb, spd, amb > 27, whkm) | {
        "recent_wh_per_km": round(whkm, 1) if whkm else None, "ambient_c": round(amb, 1)}


async def vehicle_detail(p: Principal, vin: str) -> dict:
    master = await vehicle_master(p, vin)
    st = await live_state(p, vin)
    out = {"vehicle": master, "state": mask(p, st) if st else None}
    if st:
        out["range"] = await range_for(p, vin, st, master["model_code"])
        out["dtc"] = [asdict(describe(c)) for c in st.get("dtc", [])]
    async with tenant_tx(p) as c:
        out["forecast"] = await (await c.execute(
            "SELECT soh_now, slope_per_year, days_to_80, anomaly_score, dc_fast_share, computed_at "
            "FROM soh_forecast WHERE vin = %s", (vin,))).fetchone()
        out["open_alerts"] = await (await c.execute(
            "SELECT id, type_code, message, created_at FROM alert WHERE vin = %s AND status = 'OPEN' "
            "ORDER BY created_at DESC LIMIT 10", (vin,))).fetchall()
        if p.can("driver:read"):
            out["driver"] = await (await c.execute(
                "SELECT d.id, d.full_name, d.phone, d.erased_at FROM driver_assignment a JOIN driver d ON d.id = a.driver_id "
                "WHERE a.vin = %s AND a.to_ts IS NULL", (vin,))).fetchone()
    return out


# ------------------------------------------------------------------ charging infrastructure
async def occupancy() -> dict[int, int]:
    hit = _cached("occ", 3)
    if hit is not None:
        return hit
    occ: dict[int, int] = {}
    async for key in R.redis.scan_iter("occ:*", count=100):
        for sid, n in (await R.redis.hgetall(key)).items():
            occ[int(sid)] = occ.get(int(sid), 0) + int(n)
    _cache["occ"] = (time.time(), occ)
    return occ


def now_ist_minute(ts: datetime | None = None) -> int:
    t = (ts or datetime.now(UTC)) + IST
    return t.hour * 60 + t.minute


def connector_price(tariff_id: int, minute: int) -> float:
    default, windows = R.tariffs[tariff_id]
    return price_at(windows, minute, default)


def station_view(sid: int, occ: dict[int, int], minute: int) -> dict:
    s = R.stations[sid]
    busy = occ.get(sid, 0)
    return {"id": s.id, "name": s.name, "operator": s.operator, "lat": s.lat, "lon": s.lon,
            "connectors": len(s.connectors), "free": max(0, len(s.connectors) - busy),
            "max_kw": max(c["max_kw"] for c in s.connectors),
            "dc": any(c["type"] != "TYPE2" for c in s.connectors),
            "price_now": round(min(connector_price(c["tariff_id"], minute) for c in s.connectors), 2)}


async def reachable_chargers(p: Principal, vin: str, k: int = 5, explain: bool = False) -> dict:
    master = await vehicle_master(p, vin)
    st = await live_state(p, vin)
    if st is None:
        raise HTTPException(409, "no live telemetry for this vehicle yet")
    rng = await range_for(p, vin, st, master["model_code"])
    occ = await occupancy()
    minute = now_ist_minute()
    free_nodes = {R.stations[sid].node: sid for sid in R.stations
                  if occ.get(sid, 0) < len(R.stations[sid].connectors)}
    g = R.graph
    src = g.nearest_node(st["lat"], st["lon"])
    cutoff = rng["range_km"] * 1000
    found = g.nearest_targets(src, free_nodes, k=k, cutoff_m=cutoff)
    beyond = []
    if not found:
        beyond = g.nearest_targets(src, free_nodes, k=1, cutoff_m=80_000)
    results = []
    for sid, route in found or beyond:
        km = route.dist_m / 1000
        v = station_view(sid, occ, minute)
        v.update({"road_km": round(km, 2), "eta_min": round(km / 25 * 60, 1),
                  "arrival_soc": round(max(0.0, st["soc"] - km / max(rng["range_km"], 0.1) * st["soc"]), 1),
                  "reachable": route.dist_m <= cutoff,
                  "route": g.polyline(route.nodes) if p.can("location:precise") else None})
        results.append(v)
    out = {"vin": vin, "soc": st["soc"], "range": rng, "chargers": results,
           "warning": None if found else "No free charger within predicted range - dispatch assistance / reroute"}
    if explain and results:
        target = R.stations[results[0]["id"]].node
        a, d = g.astar(src, target), g.dijkstra(src, target)
        out["search"] = {"multi_target_expanded": (found or beyond)[0][1].stats.expanded,
                         "astar_expanded": a.stats.expanded if a else None,
                         "dijkstra_expanded": d.stats.expanded if d else None}
    return out


def _next_departure(now: datetime) -> datetime:
    local = now + IST
    dep = local.replace(hour=7, minute=0, second=0, microsecond=0)
    if dep <= local + timedelta(hours=1):
        dep += timedelta(days=1)
    return dep - IST


async def charging_plan(p: Principal, vin: str, charger_id: int | None = None, depart_at: datetime | None = None,
                        target_soc: float = 80.0) -> dict:
    master = await vehicle_master(p, vin)
    st = await live_state(p, vin)
    soc = st["soc"] if st else 50.0
    model = MODELS_BY_CODE[master["model_code"]]
    now = datetime.now(UTC)
    if charger_id is None:
        # default: best AC connector at the nearest station (overnight plans favour AC)
        node = R.graph.nearest_node(st["lat"], st["lon"]) if st else R.graph.nearest_node(12.97, 77.59)
        near = R.graph.nearest_targets(node, R.station_by_node, k=1, cutoff_m=50_000)
        station = R.stations[near[0][0]] if near else next(iter(R.stations.values()))
        conn = max((c for c in station.connectors if c["type"] == "TYPE2"), key=lambda c: c["max_kw"],
                   default=station.connectors[0])
    else:
        sid = R.charger_station.get(charger_id)
        if sid is None:
            raise HTTPException(404, "charger not found")
        station = R.stations[sid]
        conn = next(c for c in station.connectors if c["id"] == charger_id)
    depart = depart_at or _next_departure(now)
    if depart.tzinfo is None:
        depart = depart.replace(tzinfo=UTC)
    start = now.replace(second=0, microsecond=0) + timedelta(minutes=SLOT_MIN - now.minute % SLOT_MIN)
    n_slots = int((depart - start).total_seconds() // (SLOT_MIN * 60))
    if n_slots < 1:
        raise HTTPException(422, "departure must be at least 15 minutes from now")
    n_slots = min(n_slots, 96 * 2)
    default, windows = R.tariffs[conn["tariff_id"]]
    prices = slot_prices(windows, (start + IST).replace(tzinfo=None), n_slots, default)
    kw_limit = model.max_ac_kw if conn["type"] == "TYPE2" else model.max_dc_kw
    plan = plan_charging(capacity_kwh=model.capacity_kwh * (st["soh"] if st else 100) / 100, soc_pct=soc,
                         target_soc_pct=target_soc, charger_kw=min(conn["max_kw"], kw_limit), prices=prices,
                         start=start)
    slots = [{"start": (start + timedelta(minutes=SLOT_MIN * i)).isoformat(), "kw": kw, "kwh": kwh, "price": pr}
             for i, (kw, kwh, pr) in enumerate(zip(plan.slot_kw, plan.slot_kwh, plan.slot_price, strict=True))]
    return {"vin": vin, "charger": {"id": conn["id"], "type": conn["type"], "max_kw": conn["max_kw"],
                                    "station": station.name, "station_id": station.id},
            "soc_now": round(soc, 1), "target_soc": target_soc, "depart_at": depart.isoformat(),
            "feasible": plan.feasible, "energy_kwh": plan.energy_kwh, "cost": plan.cost, "wear_cost": plan.wear_cost,
            "baseline_cost": plan.baseline_cost, "savings": plan.savings, "final_soc": plan.final_soc_pct,
            "currency": "INR", "notes": plan.notes, "slots": slots}


async def create_schedule(p: Principal, plan: dict, via: str) -> dict:
    charging = [s for s in plan["slots"] if s["kw"] > 0]
    if not charging:
        raise HTTPException(422, "plan has no charging slots")
    async with tenant_tx(p) as c:
        row = await (await c.execute(
            "INSERT INTO charging_schedule(tenant_id, vin, charger_id, created_by, created_via, window_start, window_end, "
            "target_soc, expected_cost, plan) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at",
            (p.tenant_id, plan["vin"], plan["charger"]["id"], p.user_id, via, charging[0]["start"],
             (datetime.fromisoformat(charging[-1]["start"]) + timedelta(minutes=SLOT_MIN)).isoformat(),
             plan["target_soc"], plan["cost"], orjson.dumps(plan).decode()))).fetchone()
    return {"schedule_id": row["id"], "created_at": row["created_at"], "vin": plan["vin"],
            "window_start": charging[0]["start"], "expected_cost": plan["cost"], "savings": plan["savings"]}


# ------------------------------------------------------------------ fleet level
async def fleet_summary(p: Principal) -> dict:
    key = f"summary:{p.tenant_id}"
    hit = _cached(key, 2)
    if hit is not None:
        return hit
    agg: dict[str, float] = {}
    async for k in R.redis.scan_iter(f"kpi:{p.tenant_id}:*", count=100):
        for f, v in (await R.redis.hgetall(k)).items():
            agg[f.decode()] = agg.get(f.decode(), 0.0) + float(v)
    async with tenant_tx(p) as c:
        vehicles = (await (await c.execute("SELECT count(*) AS n FROM vehicle")).fetchone())["n"]
        alerts = await (await c.execute(
            "SELECT type_code, count(*) AS n FROM alert WHERE status = 'OPEN' AND created_at > now() - interval '24 hours' "
            "GROUP BY type_code")).fetchall()
        midnight_ist = (datetime.now(UTC) + IST).replace(hour=0, minute=0, second=0, microsecond=0) - IST
        cost = await (await c.execute(
            "SELECT coalesce(sum(cost),0) AS cost, coalesce(sum(energy_kwh),0) AS kwh, count(*) AS sessions "
            "FROM charging_session WHERE started_at >= %s", (midnight_ist,))).fetchone()
        risk = await (await c.execute(
            "SELECT count(*) FILTER (WHERE days_to_80 < 365) AS soh_risk, count(*) AS analysed "
            "FROM soh_forecast")).fetchone()
    online = agg.get("online", 0)
    out = {"vehicles": vehicles, "online": int(online), "driving": int(agg.get("DRIVING", 0)),
           "charging": int(agg.get("CHARGING", 0)), "parked": int(agg.get("PARKED", 0)),
           "offline": int(vehicles - online), "low_soc": int(agg.get("low_soc", 0)),
           "avg_soc": round(agg.get("soc_sum", 0) / online, 1) if online else None,
           "charging_power_kw": round(agg.get("charging_kw", 0), 1),
           "open_alerts": {r["type_code"]: r["n"] for r in alerts},
           "today": {"charge_cost": float(cost["cost"]), "energy_kwh": float(cost["kwh"]), "sessions": cost["sessions"]},
           "battery_risk": {"soh_below_80_within_1y": risk["soh_risk"], "analysed": risk["analysed"]},
           "currency": "INR", "ts": time.time()}
    _cache[key] = (time.time(), out)
    return out


async def battery_report(p: Principal, limit: int = 20) -> dict:
    async with tenant_tx(p) as c:
        dist = await (await c.execute(
            "SELECT width_bucket(soh_now, 70, 100, 6) AS b, count(*) AS n FROM soh_forecast GROUP BY 1 ORDER BY 1")).fetchall()
        at_risk = await (await c.execute(
            "SELECT f.vin, v.model_code, v.fleet_id, f.soh_now, f.slope_per_year, f.days_to_80, f.anomaly_score "
            "FROM soh_forecast f JOIN vehicle v ON v.vin = f.vin WHERE f.days_to_80 IS NOT NULL "
            "ORDER BY f.days_to_80 ASC LIMIT %s", (limit,))).fetchall()
        anomalies = await (await c.execute(
            "SELECT f.vin, v.model_code, f.soh_now, f.slope_per_year, f.anomaly_score, f.dc_fast_share "
            "FROM soh_forecast f JOIN vehicle v ON v.vin = f.vin WHERE f.anomaly_score > 3.5 "
            "ORDER BY f.anomaly_score DESC LIMIT %s", (limit,))).fetchall()
        cohort = await (await c.execute(
            "SELECT v.model_code, round(avg(f.soh_now)::numeric, 2) AS avg_soh, "
            "round(avg(f.slope_per_year)::numeric, 2) AS avg_loss_per_year, count(*) AS n "
            "FROM soh_forecast f JOIN vehicle v ON v.vin = f.vin GROUP BY 1 ORDER BY 1")).fetchall()
    labels = ["<70", "70-75", "75-80", "80-85", "85-90", "90-95", "95-100", "100"]
    return {"distribution": [{"bucket": labels[min(r["b"], 7)], "n": r["n"]} for r in dist],
            "at_risk": at_risk, "anomalies": anomalies, "cohorts": cohort}


async def similar_degradation(p: Principal, vin: str, k: int = 8) -> list[dict]:
    async with tenant_tx(p) as c:
        await c.execute("SET LOCAL hnsw.ef_search = 64")
        rows = await (await c.execute(
            "WITH q AS (SELECT embedding FROM battery_embedding WHERE vin = %s) "
            "SELECT e.vin, v.model_code, f.soh_now, f.slope_per_year, "
            "       round((1 - (e.embedding <=> q.embedding))::numeric, 4) AS similarity "
            "FROM battery_embedding e CROSS JOIN q JOIN vehicle v ON v.vin = e.vin "
            "LEFT JOIN soh_forecast f ON f.vin = e.vin WHERE e.vin <> %s "
            "ORDER BY e.embedding <=> q.embedding LIMIT %s", (vin, vin, k))).fetchall()
    return rows


async def hot_zones() -> list[dict]:
    merged: dict[str, int] = {}
    async for key in R.redis.scan_iter("hot:*", count=50):
        raw = await R.redis.get(key)
        for sid, n in orjson.loads(raw or b"[]"):
            merged[sid] = merged.get(sid, 0) + n
    occ = await occupancy()
    minute = now_ist_minute()
    out = []
    for sid, n in sorted(merged.items(), key=lambda kv: -kv[1])[:15]:
        if int(sid) in R.stations:
            v = station_view(int(sid), occ, minute)
            v["charge_starts_15m"] = n
            out.append(v)
    return out


def bbox_filter(lat: float, lon: float, bbox: tuple[float, float, float, float] | None) -> bool:
    if bbox is None:
        return True
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n


def km_between(a_lat, a_lon, b_lat, b_lon) -> float:
    return math.hypot((a_lat - b_lat) * 111, (a_lon - b_lon) * 108)
