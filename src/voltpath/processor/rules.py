"""Real-time alert rules (pure functions -> unit-tested without Kafka/DB).

LOW_RANGE_NO_CHARGER is the flagship rule: predicted remaining range vs *road* distance to the
nearest charger that currently has a free connector (multi-target Dijkstra bounded by range).
"""

from __future__ import annotations

from dataclasses import dataclass

from voltcore.dtc import KNOWN
from voltcore.energy import EvModel
from voltcore.graph import RoadGraph

OVERTEMP_C = 55.0
LOW_SOC_PCT = 15.0
RANGE_CHECK_SOC = 25.0
RANGE_CHECK_EVERY_S = 60.0
RANGE_SAFETY = 1.15   # need 15 % margin over road distance
CRITICAL_DTCS = {c for c, (_d, sev) in KNOWN.items() if sev == "critical"} - {"P0A7E"}

COOLDOWN_S = {"BATTERY_OVERTEMP": 1800, "DTC_CRITICAL": 3600, "LOW_SOC": 1800, "LOW_RANGE_NO_CHARGER": 900}


@dataclass
class Alert:
    type_code: str
    vin: str
    tenant_id: int
    event_ts: float
    message: str
    details: dict


def effective_range_km(model: EvModel, soc: float, soh: float, temp_c: float) -> float:
    """Conservative in-stream estimate (the ML model refines it in the API): rated range scaled by
    SoC, SoH, and a city-driving / temperature derate."""
    derate = 0.85 if 15 <= temp_c <= 35 else 0.75
    return model.rated_range_km * soc / 100 * soh / 100 * derate


def simple_rules(vin: str, tenant_id: int, e: dict, status: str, ts: float) -> list[Alert]:
    out: list[Alert] = []
    if e["batt_temp_c"] >= OVERTEMP_C:
        out.append(Alert("BATTERY_OVERTEMP", vin, tenant_id, ts,
                         f"Battery pack at {e['batt_temp_c']:.1f} °C (limit {OVERTEMP_C:.0f} °C)",
                         {"temp_c": round(e["batt_temp_c"], 1), "status": status, "power_kw": e["power_kw"]}))
    crit = [d for d in e.get("dtc") or [] if d in CRITICAL_DTCS]
    if crit:
        out.append(Alert("DTC_CRITICAL", vin, tenant_id, ts, f"Critical fault {', '.join(crit)}: "
                         f"{KNOWN[crit[0]][0]}", {"dtc": crit}))
    if status == "DRIVING" and e["soc_pct"] < LOW_SOC_PCT:
        out.append(Alert("LOW_SOC", vin, tenant_id, ts, f"State of charge {e['soc_pct']:.0f} % while driving",
                         {"soc": round(e["soc_pct"], 1)}))
    return out


def range_rule(vin: str, tenant_id: int, e: dict, ts: float, model: EvModel, graph: RoadGraph,
               free_station_nodes: dict[int, int]) -> Alert | None:
    """None if a free charger is reachable with margin; otherwise a critical alert."""
    rng_km = effective_range_km(model, e["soc_pct"], e["soh_pct"], e["batt_temp_c"])
    src = graph.nearest_node(e["lat"], e["lon"])
    found = graph.nearest_targets(src, free_station_nodes, k=1, cutoff_m=rng_km * 1000 / RANGE_SAFETY)
    if found:
        return None
    nearest = graph.nearest_targets(src, free_station_nodes, k=1, cutoff_m=60_000)
    dist = round(nearest[0][1].dist_m / 1000, 1) if nearest else None
    msg = (f"Predicted range {rng_km:.1f} km but nearest free charger is "
           f"{dist} km by road" if dist else f"Predicted range {rng_km:.1f} km and no free charger nearby")
    return Alert("LOW_RANGE_NO_CHARGER", vin, tenant_id, ts, msg,
                 {"range_km": round(rng_km, 1), "nearest_charger_km": dist,
                  "station_id": nearest[0][0] if nearest else None, "soc": round(e["soc_pct"], 1)})
