from datetime import datetime, timedelta
from itertools import product

import numpy as np
import pytest

from voltcore import energy
from voltcore.charging_dp import (
    SLOT_H,
    DepotVehicle,
    TariffWindow,
    baseline_asap,
    plan_charging,
    price_at,
    schedule_depot,
    slot_prices,
    taper_kw,
)
from voltcore.segment import MOVING, STOPPED, segment, viterbi_states

TOU = [TariffWindow(22 * 60, 6 * 60, 4.0), TariffWindow(18 * 60, 22 * 60, 11.0)]


def test_price_at_wraps_midnight():
    assert price_at(TOU, 23 * 60, 7) == 4.0
    assert price_at(TOU, 2 * 60, 7) == 4.0
    assert price_at(TOU, 19 * 60, 7) == 11.0
    assert price_at(TOU, 12 * 60, 7) == 7


def test_slot_prices_length_and_values():
    p = slot_prices(TOU, datetime(2026, 1, 1, 21, 45), 3, 7.0)
    assert p.tolist() == [11.0, 4.0, 4.0]


def test_taper():
    assert taper_kw(50, 100) == 100
    assert taper_kw(90, 100) == pytest.approx(57.5)
    assert taper_kw(100, 100) == pytest.approx(15)


def test_plan_prefers_cheap_night_slots():
    start = datetime(2026, 1, 1, 18, 0)
    prices = slot_prices(TOU, start, 48, 7.0)
    plan = plan_charging(capacity_kwh=60, soc_pct=30, target_soc_pct=80, charger_kw=11, prices=prices, start=start)
    assert plan.feasible and plan.final_soc_pct >= 80
    expensive = [kw for kw, p in zip(plan.slot_kw, prices, strict=True) if p == 11.0]
    assert sum(expensive) == 0
    assert plan.cost < plan.baseline_cost and plan.savings > 0


def test_plan_matches_bruteforce_on_small_instance():
    prices = np.array([5.0, 1.0, 3.0, 2.0])
    kw = dict(capacity_kwh=10, soc_pct=50, target_soc_pct=70, charger_kw=4, step_kwh=0.5, wear_alpha=0.3,
              efficiency=1.0)
    plan = plan_charging(prices=prices, **kw)
    unit_cap = int(4 * SLOT_H / 0.5)  # 2 units per slot
    best = np.inf
    for xs in product(range(unit_cap + 1), repeat=4):
        if sum(xs) * 0.5 < 2.0:
            continue
        c = sum(p * x * 0.5 + 0.3 * ((x * 0.5 / SLOT_H) / 4) ** 2 * x * 0.5 for p, x in zip(prices, xs, strict=True))
        best = min(best, c)
    assert plan.cost == pytest.approx(best, abs=0.01)


def test_plan_infeasible_reports_best_effort():
    plan = plan_charging(capacity_kwh=80, soc_pct=10, target_soc_pct=90, charger_kw=7, prices=np.full(4, 5.0))
    assert not plan.feasible and plan.notes and plan.final_soc_pct < 90


def test_plan_noop_when_already_charged():
    plan = plan_charging(capacity_kwh=50, soc_pct=90, target_soc_pct=80, charger_kw=7, prices=np.full(4, 5.0))
    assert plan.feasible and plan.energy_kwh == 0 and plan.cost == 0


def test_plan_respects_site_allowance():
    plan = plan_charging(capacity_kwh=50, soc_pct=20, target_soc_pct=40, charger_kw=22, prices=np.full(8, 5.0),
                         site_allowance_kw=np.array([0, 0, 0, 0, 22, 22, 22, 22.0]))
    assert all(kw == 0 for kw in plan.slot_kw[:4])


def test_baseline_asap_monotone_in_target():
    p = np.full(40, 5.0)
    assert baseline_asap(capacity_kwh=50, soc_pct=20, target_soc_pct=50, charger_kw=11, prices=p) < \
        baseline_asap(capacity_kwh=50, soc_pct=20, target_soc_pct=80, charger_kw=11, prices=p)


def test_depot_schedule_never_exceeds_site_cap():
    prices = slot_prices(TOU, datetime(2026, 1, 1, 20, 0), 40, 7.0)
    vehicles = [DepotVehicle(f"V{i}", 60, 20 + i * 5, 80, 30 + i, 22) for i in range(6)]
    plans = schedule_depot(vehicles, prices, site_cap_kw=50)
    total = np.zeros(40)
    for p in plans.values():
        total[: len(p.slot_kw)] += p.slot_kw
    assert total.max() <= 50 + 1e-6
    assert len(plans) == 6


def test_viterbi_smooths_noise():
    speeds = [0, 0, 0, 40, 45, 1, 42, 50, 48, 0, 0, 0, 0]
    st = viterbi_states(speeds, p_switch=0.05)
    assert st[0] == STOPPED and st[4] == MOVING and st[5] == MOVING and st[-1] == STOPPED
    assert viterbi_states([]) == []


def test_segment_merges_short_stops():
    t0 = datetime(2026, 1, 1)
    speeds = [0] * 10 + [40] * 20 + [0] * 3 + [40] * 20 + [0] * 30
    pts = [(t0 + timedelta(seconds=10 * i), 0.0, 0.0, float(s)) for i, s in enumerate(speeds)]
    segs = segment(pts, min_stop_s=60)
    assert [s.kind for s in segs] == ["stop", "trip", "stop"]
    assert segs[1].duration_s > 300
    assert segment([]) == []


def test_energy_model_shapes():
    m = energy.MODELS_BY_CODE["VL-STRATA"]
    city = energy.wh_per_km(m, 10, 25, False)
    cruise = energy.wh_per_km(m, 50, 25, False)
    highway = energy.wh_per_km(m, 110, 25, False)
    assert city > cruise < highway
    assert energy.wh_per_km(m, 50, 0, True) > cruise
    r = energy.true_range_km(m, 100, 100, 50, 22, False)
    assert r == pytest.approx(m.rated_range_km, rel=0.1)
    assert energy.baseline_range_km(m, 50, 90) == pytest.approx(m.rated_range_km * 0.45)
