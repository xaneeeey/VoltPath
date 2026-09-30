"""Copilot tool registry.

Guardrails live here, not in the prompt:
  * every tool runs as `principal.as_agent()` - the caller's own permissions and tenant, never more
  * inputs are validated (VIN pattern, numeric bounds) before any service call
  * read tools return data; the single action tool (`schedule_charging`) only *proposes* - it
    stores a pending action that a human must confirm through a separate endpoint
  * results are size-capped and PII-free (driver data is not exposed to the model at all)
"""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime

import orjson

from voltpath.api import services as svc
from voltpath.api.auth import Principal
from voltpath.api.deps import R

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
PENDING_TTL_S = 600


class ToolError(Exception):
    pass


def _vin(inp: dict) -> str:
    vin = str(inp.get("vin", "")).upper().strip()
    if not VIN_RE.match(vin):
        raise ToolError("vin must be a 17-character VIN (no I, O, Q)")
    return vin


def _need(p: Principal, perm: str) -> None:
    if not p.can(perm):
        raise ToolError(f"the user is not permitted to do this (missing {perm})")


TOOLS: list[dict] = [
    {
        "name": "fleet_summary",
        "description": "Live fleet KPIs for the user's organisation: vehicles online/driving/charging, "
                       "average state of charge, low-battery count, open alerts by type, today's charging cost.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_vehicle",
        "description": "Master data, live telemetry (SoC, SoH, temperature, status, location), ML range "
                       "prediction, battery forecast and open alerts for one vehicle.",
        "input_schema": {"type": "object", "properties": {"vin": {"type": "string", "description": "17-char VIN"}},
                         "required": ["vin"], "additionalProperties": False},
    },
    {
        "name": "find_reachable_chargers",
        "description": "Chargers with a free connector reachable within the vehicle's predicted range, ranked "
                       "by road distance, with ETA, arrival SoC and current price per kWh.",
        "input_schema": {"type": "object", "properties": {
            "vin": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 8}},
            "required": ["vin"], "additionalProperties": False},
    },
    {
        "name": "plan_charging",
        "description": "Compute the cost-optimal charging schedule (time-of-use tariff, DP optimiser) to reach "
                       "target_soc by depart_at. Returns cost, savings versus charging immediately, and slots. "
                       "Read-only: does not book anything.",
        "input_schema": {"type": "object", "properties": {
            "vin": {"type": "string"},
            "target_soc": {"type": "number", "minimum": 10, "maximum": 100},
            "depart_at": {"type": "string", "description": "ISO-8601 datetime; default next 07:00 local"},
            "charger_id": {"type": "integer"}},
            "required": ["vin"], "additionalProperties": False},
    },
    {
        "name": "battery_health_report",
        "description": "Fleet battery-health analytics: SoH distribution, vehicles predicted to hit 80% SoH "
                       "soonest, anomalous (fast-degrading) packs and per-model cohorts.",
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                         "additionalProperties": False},
    },
    {
        "name": "similar_degradation",
        "description": "Vehicles whose battery degradation curve and usage profile are most similar to the given "
                       "vehicle (vector similarity search).",
        "input_schema": {"type": "object", "properties": {"vin": {"type": "string"}},
                         "required": ["vin"], "additionalProperties": False},
    },
    {
        "name": "list_alerts",
        "description": "Most recent open alerts, optionally filtered by type or VIN.",
        "input_schema": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["LOW_RANGE_NO_CHARGER", "LOW_SOC", "BATTERY_OVERTEMP",
                                                "RAPID_SOH_DROP", "DTC_CRITICAL"]},
            "vin": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}},
            "additionalProperties": False},
    },
    {
        "name": "search_knowledge_base",
        "description": "Search the maintenance knowledge base (DTC meanings, battery care, charging policy).",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                         "required": ["query"], "additionalProperties": False},
    },
    {
        "name": "schedule_charging",
        "description": "PROPOSE booking a charging schedule. This never executes directly: it returns an "
                       "action_id that the human user must confirm in the UI. Only call when the user asks to "
                       "book/schedule charging.",
        "input_schema": {"type": "object", "properties": {
            "vin": {"type": "string"},
            "target_soc": {"type": "number", "minimum": 10, "maximum": 100},
            "depart_at": {"type": "string"}, "charger_id": {"type": "integer"}},
            "required": ["vin"], "additionalProperties": False},
    },
]
ACTION_TOOLS = {"schedule_charging"}


def _trim(obj, max_list: int = 10):
    if isinstance(obj, dict):
        return {k: _trim(v, max_list) for k, v in obj.items() if k not in ("route", "slots")}
    if isinstance(obj, list):
        return [_trim(v, max_list) for v in obj[:max_list]]
    return obj


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError("depart_at must be ISO-8601") from exc


async def run_tool(p: Principal, name: str, inp: dict) -> dict:
    """Execute a tool as the agent-tagged caller. Raises ToolError for model-fixable problems."""
    a = p.as_agent()
    if name == "fleet_summary":
        _need(a, "fleet:read")
        return await svc.fleet_summary(a)
    if name == "get_vehicle":
        _need(a, "vehicle:read")
        d = await svc.vehicle_detail(a, _vin(inp))
        d.pop("driver", None)  # PII never goes to the model
        return _trim(d)
    if name == "find_reachable_chargers":
        _need(a, "charging:plan")
        return _trim(await svc.reachable_chargers(a, _vin(inp), int(inp.get("k", 3))))
    if name == "plan_charging":
        _need(a, "charging:plan")
        plan = await svc.charging_plan(a, _vin(inp), inp.get("charger_id"), _parse_dt(inp.get("depart_at")),
                                       float(inp.get("target_soc", 80)))
        active = [s for s in plan["slots"] if s["kw"] > 0]
        out = _trim(plan)
        out["charging_windows"] = {"first_slot": active[0]["start"] if active else None,
                                   "last_slot": active[-1]["start"] if active else None, "slots_used": len(active)}
        return out
    if name == "battery_health_report":
        _need(a, "analytics:read")
        return _trim(await svc.battery_report(a, int(inp.get("limit", 10))))
    if name == "similar_degradation":
        _need(a, "analytics:read")
        return {"vin": _vin(inp), "similar": await svc.similar_degradation(a, _vin(inp), 6)}
    if name == "list_alerts":
        _need(a, "alert:read")
        from voltpath.api.deps import tenant_tx

        async with tenant_tx(a) as c:
            rows = await (await c.execute(
                "SELECT id, vin, type_code, message, created_at FROM alert WHERE status = 'OPEN' "
                "AND (%s::text IS NULL OR type_code = %s) AND (%s::text IS NULL OR vin = %s) "
                "ORDER BY id DESC LIMIT %s",
                (inp.get("type"), inp.get("type"), inp.get("vin"), inp.get("vin"),
                 int(inp.get("limit", 10))))).fetchall()
        return {"alerts": rows}
    if name == "search_knowledge_base":
        from voltpath.batch.kb import search_kb

        async with R.svc_pool.connection() as c:
            return {"results": await search_kb(c, str(inp.get("query", ""))[:300])}
    if name == "schedule_charging":
        _need(a, "copilot:act")
        _need(a, "charging:schedule")
        plan = await svc.charging_plan(a, _vin(inp), inp.get("charger_id"), _parse_dt(inp.get("depart_at")),
                                       float(inp.get("target_soc", 80)))
        action_id = secrets.token_urlsafe(12)
        await R.redis.set(f"pending:{a.tenant_id}:{action_id}",
                          orjson.dumps({"kind": "schedule_charging", "plan": plan, "user_id": a.user_id,
                                        "created": time.time()}), ex=PENDING_TTL_S)
        return {"status": "awaiting_user_confirmation", "action_id": action_id,
                "summary": f"Charge {plan['vin']} at {plan['charger']['station']} to {plan['target_soc']:.0f}% "
                           f"by {plan['depart_at']} for about INR {plan['cost']:.0f} "
                           f"(saves INR {plan['savings']:.0f} vs charging now)"}
    raise ToolError(f"unknown tool {name}")


async def confirm_action(p: Principal, action_id: str) -> dict:
    key = f"pending:{p.tenant_id}:{action_id}"
    raw = await R.redis.getdel(key)
    if raw is None:
        raise ToolError("action not found or expired")
    act = orjson.loads(raw)
    if act["user_id"] != p.user_id:
        raise ToolError("only the user who requested the action can confirm it")
    return await svc.create_schedule(p.as_agent(), act["plan"], "agent")
