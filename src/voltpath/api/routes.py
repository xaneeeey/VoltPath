"""REST + WebSocket endpoints (v1). Pagination is keyset-based everywhere (no OFFSET scans)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Annotated, Literal

import orjson
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from voltpath.api import services as svc
from voltpath.api.auth import Principal, check_password, current_principal, decode_token, issue_token, keys, require
from voltpath.api.deps import R, decode_cursor, encode_cursor, tenant_tx
from voltpath.config import settings
from voltpath.processor.state import cell_center

router = APIRouter(prefix="/v1")
public = APIRouter()

VIN = Annotated[str, Field(pattern=r"^[A-HJ-NPR-Z0-9]{17}$")]


def _vin(vin: str) -> str:
    import re

    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", vin):
        raise HTTPException(422, "malformed VIN")
    return vin


# ================================================================== auth
@public.get("/.well-known/jwks.json", tags=["auth"])
def jwks():
    return {"keys": [keys().jwk]}


@public.get("/.well-known/openid-configuration", tags=["auth"])
def oidc_config():
    s = settings()
    return {"issuer": s.jwt_issuer, "jwks_uri": "/.well-known/jwks.json", "token_endpoint": "/v1/auth/token",
            "grant_types_supported": ["password", "client_credentials"], "id_token_signing_alg_values_supported": ["RS256"]}


@router.post("/auth/token", tags=["auth"])
async def token(grant_type: Annotated[str, Form()], username: Annotated[str | None, Form()] = None,
                password: Annotated[str | None, Form()] = None, client_id: Annotated[str | None, Form()] = None,
                client_secret: Annotated[str | None, Form()] = None):
    async with R.svc_pool.connection() as c:
        if grant_type == "password":
            if not username or not password:
                raise HTTPException(400, "username and password required")
            u = await (await c.execute("SELECT * FROM auth_lookup(%s)", (username,))).fetchone()
            if not check_password(password, u["password_hash"] if u else None) or u["disabled"]:
                raise HTTPException(401, "invalid credentials")
            tok, ttl = issue_token(sub=u["email"], user_id=u["id"], tenant_id=u["tenant_id"], role=u["role_code"],
                                   name=u["full_name"])
        elif grant_type == "client_credentials":
            if not client_id or not client_secret:
                raise HTTPException(400, "client_id and client_secret required")
            cl = await (await c.execute("SELECT * FROM client_lookup(%s)", (client_id,))).fetchone()
            if not check_password(client_secret, cl["secret_hash"] if cl else None):
                raise HTTPException(401, "invalid client")
            tok, ttl = issue_token(sub=cl["client_id"], user_id=None, tenant_id=cl["tenant_id"], role=cl["role_code"],
                                   name=cl["client_id"])
        else:
            raise HTTPException(400, "unsupported_grant_type")
    return {"access_token": tok, "token_type": "Bearer", "expires_in": ttl}


@router.get("/auth/me", tags=["auth"])
async def me(p: Principal = Depends(current_principal)):
    async with R.svc_pool.connection() as c:
        t = await (await c.execute("SELECT name FROM tenant WHERE id = %s", (p.tenant_id,))).fetchone()
    return {"sub": p.sub, "name": p.name, "role": p.role, "tenant_id": p.tenant_id,
            "tenant": t["name"] if t else None, "permissions": sorted(p.permissions)}


# ================================================================== fleet
@router.get("/fleet/summary", tags=["fleet"])
async def fleet_summary(p: Principal = Depends(require("fleet:read"))):
    return await svc.fleet_summary(p)


@router.get("/fleets", tags=["fleet"])
async def fleets(p: Principal = Depends(require("fleet:read"))):
    async with tenant_tx(p) as c:
        return await (await c.execute(
            "SELECT f.id, f.name, d.name AS depot, d.site_cap_kw, count(v.vin) AS vehicles FROM fleet f "
            "LEFT JOIN depot d ON d.id = f.depot_id LEFT JOIN vehicle v ON v.fleet_id = f.id "
            "GROUP BY f.id, d.name, d.site_cap_kw ORDER BY f.id")).fetchall()


@router.get("/vehicles", tags=["fleet"])
async def vehicles(p: Principal = Depends(require("fleet:read")), fleet_id: int | None = None,
                   model: str | None = None, q: Annotated[str | None, Query(max_length=17)] = None,
                   cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    after = decode_cursor(cursor)
    where, args = ["TRUE"], []
    if after:
        where.append("v.vin > %s")
        args.append(after[0])
    if fleet_id is not None:
        where.append("v.fleet_id = %s")
        args.append(fleet_id)
    if model:
        where.append("v.model_code = %s")
        args.append(model)
    if q:
        where.append("(v.vin LIKE %s OR v.registration ILIKE %s)")
        args += [q.upper() + "%", q + "%"]
    async with tenant_tx(p) as c:
        rows = await (await c.execute(
            f"SELECT v.vin, v.registration, v.model_code, v.fleet_id, v.model_year FROM vehicle v "
            f"WHERE {' AND '.join(where)} ORDER BY v.vin LIMIT %s", (*args, limit + 1))).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    states = await svc.live_states(p, [r["vin"] for r in rows])
    for r in rows:
        r["state"] = states.get(r["vin"])
    return {"items": rows, "next_cursor": encode_cursor(rows[-1]["vin"]) if more else None}


@router.get("/vehicles/{vin}", tags=["fleet"])
async def vehicle(vin: str, p: Principal = Depends(require("vehicle:read"))):
    return await svc.vehicle_detail(p, _vin(vin))


@router.get("/vehicles/{vin}/telemetry", tags=["fleet"])
async def telemetry(vin: str, p: Principal = Depends(require("telemetry:read")),
                    hours: Annotated[int, Query(ge=1, le=720)] = 6):
    _vin(vin)
    async with tenant_tx(p) as c:
        if hours <= 6:
            rows = await (await c.execute(
                "SELECT ts, soc_pct AS soc, speed_kmh AS speed, batt_temp_c AS temp, power_kw AS power, lat, lon, evt "
                "FROM telemetry_v WHERE vin = %s AND ts > now() - make_interval(hours => %s) ORDER BY ts",
                (vin, hours))).fetchall()
        else:
            rows = await (await c.execute(
                "SELECT bucket AS ts, avg_soc AS soc, avg_speed AS speed, max_temp AS temp, km FROM telemetry_15m_v "
                "WHERE vin = %s AND bucket > now() - make_interval(hours => %s) ORDER BY bucket",
                (vin, hours))).fetchall()
    if not p.can("location:precise"):
        for r in rows:
            r.pop("lat", None)
            r.pop("lon", None)
    return {"vin": vin, "resolution": "10s" if hours <= 6 else "15m", "points": rows}


@router.get("/vehicles/{vin}/battery", tags=["battery"])
async def battery(vin: str, p: Principal = Depends(require("analytics:read"))):
    _vin(vin)
    async with tenant_tx(p) as c:
        hist = await (await c.execute(
            "SELECT day, soh_pct, km, energy_kwh, dc_fast_kwh, max_temp_c, charge_cost FROM battery_daily "
            "WHERE vin = %s ORDER BY day", (vin,))).fetchall()
        fc = await (await c.execute("SELECT * FROM soh_forecast WHERE vin = %s", (vin,))).fetchone()
    return {"vin": vin, "history": hist, "forecast": fc, "similar": await svc.similar_degradation(p, vin)}


# ================================================================== map
@router.get("/map/vehicles", tags=["map"])
async def map_vehicles(p: Principal = Depends(require("fleet:read")),
                       bbox: Annotated[str | None, Query(pattern=r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$")] = None,
                       zoom: Annotated[float, Query(ge=0, le=22)] = 11):
    """Low zoom (or masked users): ~4 km grid clusters. High zoom: individual vehicles in bbox."""
    box = tuple(float(x) for x in bbox.split(",")) if bbox else None
    if zoom < 13 or not p.can("location:precise"):
        cells: dict[str, list[float]] = {}
        async for key in R.redis.scan_iter(f"cells:{p.tenant_id}:*", count=100):
            for gh, v in (await R.redis.hgetall(key)).items():
                n, soc, low = v.decode().split("|")
                c = cells.setdefault(gh.decode(), [0, 0.0, 0])
                c[0] += int(n)
                c[1] += float(soc)
                c[2] += int(low)
        out = []
        for gh, (n, soc, low) in cells.items():
            lat, lon = cell_center(gh)
            if svc.bbox_filter(lat, lon, box):
                out.append({"gh": gh, "lat": round(lat, 5), "lon": round(lon, 5), "n": n,
                            "avg_soc": round(soc / n, 1), "low": low})
        return {"mode": "clusters", "cells": out}
    w, s, e, n = box or (77.45, 12.85, 77.75, 13.10)
    width_km = abs(e - w) * 108
    height_km = abs(n - s) * 111
    members = await R.redis.geosearch(f"geo:{p.tenant_id}", longitude=(w + e) / 2, latitude=(s + n) / 2,
                                      width=width_km, height=height_km, unit="km", count=4000)
    vins = [m.decode() for m in members]
    states = await svc.live_states(p, vins)
    pts = [{"vin": v, "lat": st["lat"], "lon": st["lon"], "soc": st["soc"], "status": st["status"],
            "speed": st["speed"]} for v, st in states.items()]
    return {"mode": "points", "vehicles": pts, "truncated": len(vins) >= 4000}


@router.get("/map/chargers", tags=["map"])
async def map_chargers(p: Principal = Depends(require("fleet:read")),
                       bbox: Annotated[str | None, Query(pattern=r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$")] = None):
    box = tuple(float(x) for x in bbox.split(",")) if bbox else None
    occ = await svc.occupancy()
    minute = svc.now_ist_minute()
    return {"stations": [svc.station_view(sid, occ, minute) for sid, s in R.stations.items()
                         if svc.bbox_filter(s.lat, s.lon, box)]}


# ================================================================== charging
class PlanRequest(BaseModel):
    vin: VIN
    charger_id: int | None = None
    depart_at: datetime | None = None
    target_soc: float = Field(80, ge=10, le=100)


@router.get("/vehicles/{vin}/reachable-chargers", tags=["charging"])
async def reachable(vin: str, p: Principal = Depends(require("charging:plan")),
                    k: Annotated[int, Query(ge=1, le=10)] = 5, explain: bool = False):
    return await svc.reachable_chargers(p, _vin(vin), k, explain)


@router.post("/charging/plan", tags=["charging"])
async def plan(req: PlanRequest, p: Principal = Depends(require("charging:plan"))):
    return await svc.charging_plan(p, req.vin, req.charger_id, req.depart_at, req.target_soc)


@router.post("/charging/schedules", status_code=201, tags=["charging"])
async def schedule(req: PlanRequest, p: Principal = Depends(require("charging:schedule"))):
    plan_ = await svc.charging_plan(p, req.vin, req.charger_id, req.depart_at, req.target_soc)
    return await svc.create_schedule(p, plan_, "ui")


@router.get("/charging/schedules", tags=["charging"])
async def schedules(p: Principal = Depends(require("charging:plan")), cursor: str | None = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 25):
    after = decode_cursor(cursor)
    async with tenant_tx(p) as c:
        rows = await (await c.execute(
            "SELECT id, vin, charger_id, created_via, created_at, window_start, window_end, target_soc, expected_cost, "
            "status FROM charging_schedule WHERE (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
            (after[0] if after else None, after[0] if after else None, limit + 1))).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_cursor": encode_cursor(rows[-1]["id"]) if more else None}


@router.get("/charging/sessions", tags=["charging"])
async def sessions(p: Principal = Depends(require("fleet:read")), vin: str | None = None, cursor: str | None = None,
                   limit: Annotated[int, Query(ge=1, le=200)] = 50):
    after = decode_cursor(cursor)
    async with tenant_tx(p) as c:
        rows = await (await c.execute(
            "SELECT id, vin, charger_id, started_at, ended_at, start_soc, end_soc, energy_kwh, cost FROM charging_session "
            "WHERE (%s::text IS NULL OR vin = %s) AND (%s::timestamptz IS NULL OR (started_at, id) < (%s::timestamptz, %s)) "
            "ORDER BY started_at DESC, id DESC LIMIT %s",
            (vin, vin, after[0] if after else None, after[0] if after else None, after[1] if after else None,
             limit + 1))).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows,
            "next_cursor": encode_cursor(rows[-1]["started_at"].isoformat(), rows[-1]["id"]) if more else None}


# ================================================================== alerts
@router.get("/alerts", tags=["alerts"])
async def alerts(p: Principal = Depends(require("alert:read")),
                 status: Literal["OPEN", "ACK", "RESOLVED"] | None = "OPEN", type: str | None = None,  # noqa: A002
                 vin: str | None = None, cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = 50):
    after = decode_cursor(cursor)
    async with tenant_tx(p) as c:
        rows = await (await c.execute(
            "SELECT a.id, a.vin, a.type_code, t.severity, a.message, a.event_ts, a.created_at, a.status, a.details "
            "FROM alert a JOIN alert_type t ON t.code = a.type_code "
            "WHERE (%s::text IS NULL OR a.status = %s) AND (%s::text IS NULL OR a.type_code = %s) "
            "AND (%s::text IS NULL OR a.vin = %s) AND (%s::bigint IS NULL OR a.id < %s) "
            "ORDER BY a.id DESC LIMIT %s",
            (status, status, type, type, vin, vin, after[0] if after else None, after[0] if after else None,
             limit + 1))).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_cursor": encode_cursor(rows[-1]["id"]) if more else None}


@router.post("/alerts/{alert_id}/ack", tags=["alerts"])
async def ack(alert_id: int, p: Principal = Depends(require("alert:ack"))):
    async with tenant_tx(p) as c:
        row = await (await c.execute(
            "UPDATE alert SET status = 'ACK', acked_by = %s, acked_at = now() WHERE id = %s AND status = 'OPEN' "
            "RETURNING id, status, acked_at", (p.user_id, alert_id))).fetchone()
    if row is None:
        raise HTTPException(404, "alert not found or not open")
    return row


@router.websocket("/ws/live")
async def live(ws: WebSocket, token: str):
    """Pushes alerts (Redis pub/sub) and a KPI snapshot every 2 s. Token via query param because
    browsers cannot set headers on WebSocket upgrades; it is short-lived and audited."""
    try:
        claims = decode_token(token)
    except HTTPException:
        await ws.close(code=4401)
        return
    perms = R.role_permissions.get(claims["role"], frozenset())
    if "alert:read" not in perms or claims.get("tid") is None:
        await ws.close(code=4403)
        return
    p = Principal(claims["sub"], claims.get("uid"), claims["tid"], claims["role"], perms, claims.get("name", ""))
    await ws.accept()
    pubsub = R.redis.pubsub()
    await pubsub.subscribe(f"alerts:{p.tenant_id}")

    async def kpis():
        while True:
            await ws.send_text(orjson.dumps({"kind": "kpi", "data": await svc.fleet_summary(p)}).decode())
            await asyncio.sleep(2)

    task = asyncio.create_task(kpis())
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg:
                alert = orjson.loads(msg["data"])
                if not p.can("location:precise"):
                    alert["details"] = {k: v for k, v in alert.get("details", {}).items() if k not in ("lat", "lon")}
                await ws.send_text(orjson.dumps({"kind": "alert", "data": alert}).decode())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        task.cancel()
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe()
            await pubsub.aclose()


# ================================================================== analytics
@router.get("/analytics/battery-report", tags=["battery"])
async def battery_report(p: Principal = Depends(require("analytics:read")),
                         limit: Annotated[int, Query(ge=1, le=100)] = 20):
    return await svc.battery_report(p, limit)


@router.get("/analytics/energy", tags=["analytics"])
async def energy(p: Principal = Depends(require("analytics:read")), days: Annotated[int, Query(ge=1, le=365)] = 30):
    async with tenant_tx(p) as c:
        return await (await c.execute(
            "SELECT day, sum(km) AS km, sum(energy_kwh) AS energy_kwh, sum(charge_cost) AS charge_cost, "
            "sum(vehicles) AS vehicles FROM fleet_energy_daily WHERE day > current_date - %s "
            "GROUP BY day ORDER BY day", (days,))).fetchall()


@router.get("/analytics/hot-zones", tags=["analytics"])
async def hot_zones(p: Principal = Depends(require("analytics:read"))):
    return {"window": "15m", "algorithm": "count-min sketch top-k", "stations": await svc.hot_zones()}


@router.get("/analytics/unapproved-depots", tags=["analytics"])
async def unapproved_depots(p: Principal = Depends(require("analytics:read")),
                            radius_m: Annotated[int, Query(ge=20, le=500)] = 120,
                            min_size: Annotated[int, Query(ge=3, le=50)] = 6):
    """Parked-vehicle clusters (single-linkage via union-find) further than 1 km from any depot
    or charging station."""
    from voltcore.graph import proximity_clusters

    now = time.time()
    raw = await R.redis.hvals(f"vs:{p.tenant_id}")
    parked = [svc.public_state(r, now) for r in raw]
    parked = [s for s in parked if s["status"] == "PARKED" and now - s["ts"] < 600]
    clusters = proximity_clusters([(s["lat"], s["lon"]) for s in parked], radius_m, min_size)
    async with tenant_tx(p) as c:
        depots = await (await c.execute("SELECT lat, lon FROM depot")).fetchall()
    known = [(d["lat"], d["lon"]) for d in depots] + [(s.lat, s.lon) for s in R.stations.values()]
    out = []
    for cl in clusters:
        lat = sum(parked[i]["lat"] for i in cl) / len(cl)
        lon = sum(parked[i]["lon"] for i in cl) / len(cl)
        if min(svc.km_between(lat, lon, a, b) for a, b in known) > 1.0:
            out.append(svc.mask(p, {"lat": lat, "lon": lon, "vehicles": len(cl),
                                    "vins": [parked[i]["vin"] for i in cl][:20]}))
    return {"parked_considered": len(parked), "clusters": out}


# ================================================================== compliance / admin
@router.get("/audit", tags=["admin"])
async def audit(p: Principal = Depends(require("audit:read")), actor_type: str | None = None, cursor: str | None = None,
                limit: Annotated[int, Query(ge=1, le=200)] = 50):
    after = decode_cursor(cursor)
    async with tenant_tx(p) as c:
        rows = await (await c.execute(
            "SELECT id, ts, user_id, actor_type, action, resource, status_code, host(ip) AS ip, details FROM audit_log "
            "WHERE (%s::text IS NULL OR actor_type = %s) AND (%s::bigint IS NULL OR id < %s) ORDER BY id DESC LIMIT %s",
            (actor_type, actor_type, after[0] if after else None, after[0] if after else None, limit + 1))).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_cursor": encode_cursor(rows[-1]["id"]) if more else None}


@router.get("/drivers", tags=["admin"])
async def drivers(p: Principal = Depends(require("driver:read")), cursor: str | None = None,
                  limit: Annotated[int, Query(ge=1, le=200)] = 50):
    after = decode_cursor(cursor)
    async with tenant_tx(p) as c:
        rows = await (await c.execute(
            "SELECT id, full_name, email, phone, license_no, erased_at FROM driver WHERE (%s::int IS NULL OR id > %s) "
            "ORDER BY id LIMIT %s", (after[0] if after else None, after[0] if after else None, limit + 1))).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {"items": rows, "next_cursor": encode_cursor(rows[-1]["id"]) if more else None}


@router.delete("/drivers/{driver_id}/personal-data", tags=["admin"])
async def erase(driver_id: int, request: Request, p: Principal = Depends(require("driver:erase"))):
    """Right to erasure (GDPR Art. 17 / DPDP). PII is nulled, trips are unlinked (kept as
    pseudonymous fleet statistics), assignments deleted; an erasure_request row is the receipt."""
    async with tenant_tx(p) as c:
        try:
            res = await (await c.execute("SELECT erase_driver(%s, %s) AS r", (driver_id, p.user_id))).fetchone()
        except Exception as exc:
            if "not found" in str(exc):
                raise HTTPException(404, "driver not found") from exc
            raise
    request.state.audit_details = {"erasure": res["r"]}
    return {"driver_id": driver_id, "status": "erased", **res["r"]}
