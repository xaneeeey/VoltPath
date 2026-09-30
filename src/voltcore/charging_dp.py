"""Optimal EV charging schedule under time-of-use tariffs (dynamic programming).

Why DP and not "pick the cheapest slots": the energy a slot can deliver depends on the state
of charge reached so far (CC-CV taper above ~80 % SoC) and fast charging carries a convex
battery-wear cost. Both make slot choices interact, so greedy is not optimal.

State   : (slot t, energy delivered e) in units of `step_kwh`
Choice  : units x charged in slot t, 0 <= x <= cap_t(e) (charger kW, taper, site allowance)
Cost    : price_t * x * step + wear(x) ; wear = alpha * (P/Pmax)^2 * energy
Goal    : min cost with e_T >= E_needed  (e is capped at E_needed: overshoot is never useful)

Complexity: O(T * E * X) time, O(T * E) memory for back-pointers, with T slots, E energy
units and X max units per slot; inner loop is vectorised over E with numpy.
Fleet level: `schedule_depot` runs earliest-deadline-first over vehicles, each DP respecting
the remaining site power allowance per slot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

SLOT_MIN = 15
SLOT_H = SLOT_MIN / 60


@dataclass(frozen=True)
class TariffWindow:
    start_min: int  # minutes from midnight, inclusive
    end_min: int    # exclusive; may wrap past midnight via end < start
    price: float    # currency per kWh


def price_at(windows: list[TariffWindow], minute_of_day: int, default: float) -> float:
    for w in windows:
        if w.start_min <= w.end_min:
            if w.start_min <= minute_of_day < w.end_min:
                return w.price
        elif minute_of_day >= w.start_min or minute_of_day < w.end_min:
            return w.price
    return default


def slot_prices(windows: list[TariffWindow], start: datetime, n_slots: int, default: float) -> np.ndarray:
    return np.array([
        price_at(windows, ((start + timedelta(minutes=SLOT_MIN * i)).hour * 60
                           + (start + timedelta(minutes=SLOT_MIN * i)).minute), default)
        for i in range(n_slots)
    ])


@dataclass
class ChargePlan:
    feasible: bool
    slot_kwh: list[float]
    slot_kw: list[float]
    slot_price: list[float]
    energy_kwh: float
    cost: float
    wear_cost: float
    final_soc_pct: float
    baseline_cost: float  # "plug in and charge at full power now"
    start: datetime | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def savings(self) -> float:
        return round(self.baseline_cost - self.cost, 2)


def taper_kw(soc_pct: np.ndarray | float, max_kw: float) -> np.ndarray | float:
    """CC-CV approximation: full power to 80 % SoC then linear taper to 15 % power at 100 %."""
    soc = np.asarray(soc_pct, dtype=float)
    frac = np.where(soc < 80, 1.0, 1.0 - (soc - 80) / 20 * 0.85)
    return np.clip(frac, 0.15, 1.0) * max_kw


def plan_charging(*, capacity_kwh: float, soc_pct: float, target_soc_pct: float, charger_kw: float,
                  prices: np.ndarray, step_kwh: float = 0.5, wear_alpha: float = 0.02,
                  site_allowance_kw: np.ndarray | None = None, efficiency: float = 0.92,
                  start: datetime | None = None) -> ChargePlan:
    T = len(prices)
    need_batt = max(0.0, (target_soc_pct - soc_pct) / 100 * capacity_kwh)
    E = int(np.ceil(need_batt / step_kwh))
    allowance = np.full(T, charger_kw) if site_allowance_kw is None else np.minimum(site_allowance_kw, charger_kw)
    if E == 0:
        return ChargePlan(True, [0.0] * T, [0.0] * T, prices.tolist(), 0.0, 0.0, 0.0, soc_pct, 0.0, start)

    # Extended state space [0, E + Xmax]: finishing with a small overshoot is allowed (discrete
    # units), answer = cheapest e >= E. Avoids special-casing overshoot transitions.
    x_global = int(np.floor(charger_kw * SLOT_H * efficiency / step_kwh))
    top = E + max(1, x_global)
    e_idx = np.arange(top + 1)
    soc_at_e = soc_pct + e_idx * step_kwh / capacity_kwh * 100
    room_units = np.floor(np.maximum(0.0, 100 - soc_at_e) / 100 * capacity_kwh / step_kwh + 1e-9).astype(int)
    INF = np.inf
    dp = np.full(top + 1, INF)
    dp[0] = 0.0
    choice = np.zeros((T, top + 1), dtype=np.int32)  # units charged in slot t to *reach* e

    for t in range(T):
        cap_units = np.floor(np.minimum(taper_kw(soc_at_e, charger_kw), allowance[t]) * SLOT_H * efficiency
                             / step_kwh + 1e-9).astype(int)
        cap_units = np.minimum(cap_units, room_units)  # never above 100 % SoC
        best = dp.copy()  # x = 0
        best_x = np.zeros(top + 1, dtype=np.int32)
        for x in range(1, int(cap_units.max(initial=0)) + 1):
            src = np.arange(top + 1 - x)
            grid_kwh = x * step_kwh / efficiency
            kw = grid_kwh / SLOT_H
            cost = prices[t] * grid_kwh + wear_alpha * (kw / charger_kw) ** 2 * grid_kwh
            cand = np.where(cap_units[src] >= x, dp[src] + cost, INF)
            dst = src + x
            better = cand < best[dst]
            best[dst] = np.where(better, cand, best[dst])
            best_x[dst] = np.where(better, x, best_x[dst])
        dp = best
        choice[t] = best_x

    tail = dp[E:]
    feasible = bool(np.isfinite(tail).any())
    if feasible:
        target_e = E + int(np.argmin(tail))
    else:
        reach = np.nonzero(np.isfinite(dp))[0]
        target_e = int(reach.max())
    units = [0] * T
    e = target_e
    for t in range(T - 1, -1, -1):
        units[t] = int(choice[t, e])
        e -= units[t]
    slot_batt = np.array(units) * step_kwh
    slot_grid = slot_batt / efficiency
    slot_kw = slot_grid / SLOT_H
    energy_cost = float((slot_grid * prices).sum())
    wear = float((wear_alpha * (slot_kw / charger_kw) ** 2 * slot_grid).sum())
    final_soc = min(100.0, soc_pct + slot_batt.sum() / capacity_kwh * 100)

    base = baseline_asap(capacity_kwh=capacity_kwh, soc_pct=soc_pct, target_soc_pct=target_soc_pct,
                         charger_kw=charger_kw, prices=prices, wear_alpha=wear_alpha, efficiency=efficiency)
    notes = [] if feasible else [f"target unreachable before departure; best reachable {final_soc:.0f}%"]
    return ChargePlan(bool(feasible), np.round(slot_grid, 3).tolist(), np.round(slot_kw, 2).tolist(),
                      prices.tolist(), round(float(slot_grid.sum()), 3), round(energy_cost + wear, 2),
                      round(wear, 2), round(final_soc, 1), base, start, notes)


def baseline_asap(*, capacity_kwh, soc_pct, target_soc_pct, charger_kw, prices, wear_alpha=0.02,
                  efficiency=0.92) -> float:
    """Naive policy: charge at max (tapered) power from now until target."""
    soc, cost = soc_pct, 0.0
    for p in prices:
        if soc >= target_soc_pct - 1e-9:
            break
        kw = float(taper_kw(soc, charger_kw))
        batt = min(kw * SLOT_H * efficiency, (target_soc_pct - soc) / 100 * capacity_kwh)
        grid = batt / efficiency
        real_kw = grid / SLOT_H
        cost += p * grid + wear_alpha * (real_kw / charger_kw) ** 2 * grid
        soc += batt / capacity_kwh * 100
    return round(cost, 2)


@dataclass
class DepotVehicle:
    vin: str
    capacity_kwh: float
    soc_pct: float
    target_soc_pct: float
    departure_slot: int  # index into the price array
    charger_kw: float


def schedule_depot(vehicles: list[DepotVehicle], prices: np.ndarray, site_cap_kw: float,
                   **kw) -> dict[str, ChargePlan]:
    """Earliest-deadline-first over vehicles; each gets a DP plan within the remaining site
    allowance. Heuristic at fleet level (the joint problem is NP-hard-ish knapsack-like),
    exact per vehicle. O(n log n + n * DP)."""
    remaining = np.full(len(prices), float(site_cap_kw))
    plans: dict[str, ChargePlan] = {}
    for v in sorted(vehicles, key=lambda x: x.departure_slot):
        horizon = max(1, min(v.departure_slot, len(prices)))
        plan = plan_charging(capacity_kwh=v.capacity_kwh, soc_pct=v.soc_pct, target_soc_pct=v.target_soc_pct,
                             charger_kw=v.charger_kw, prices=prices[:horizon],
                             site_allowance_kw=remaining[:horizon], **kw)
        remaining[:horizon] -= np.array(plan.slot_kw)
        remaining = np.maximum(remaining, 0)
        plans[v.vin] = plan
    return plans
