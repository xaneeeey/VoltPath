from pathlib import Path

import pytest

from voltcore.energy import MODELS_BY_CODE
from voltcore.graph import RoadGraph
from voltpath.processor.rules import effective_range_km, range_rule, simple_rules
from voltpath.processor.state import DUPLICATE, NEW, TOO_LATE, WINDOW, VehicleState, cell_center, cell_key

VIN = "1HGCM82633A004352"
GRAPH = Path(__file__).resolve().parents[2] / "data" / "bangalore_graph.npz"


def ev(**kw):
    base = {"vin": VIN, "evt": "PERIODIC", "lat": 12.97, "lon": 77.59, "speed_kmh": 0.0, "soc_pct": 60.0,
            "soh_pct": 95.0, "batt_temp_c": 30.0, "odo_km": 1000.0, "power_kw": 0.0, "dtc": [], "charger_id": None}
    base.update(kw)
    return base


def test_dedup_window_exact():
    st = VehicleState(VIN, 1)
    assert st.check_seq(10) == NEW
    assert st.check_seq(10) == DUPLICATE
    assert st.check_seq(12) == NEW
    assert st.check_seq(11) == NEW          # out of order but inside the window
    assert st.check_seq(11) == DUPLICATE
    assert st.check_seq(12 - WINDOW) == TOO_LATE


def test_dedup_large_jump_resets_bitmap():
    st = VehicleState(VIN, 1)
    st.check_seq(5)
    assert st.check_seq(5 + 10 * WINDOW) == NEW
    assert st.check_seq(5) == TOO_LATE


def test_sequence_reset_detected_when_time_moves_forward():
    st = VehicleState(VIN, 1, max_seq=1_000_000, ts=100.0)
    assert st.check_seq(10, ts=50.0) == TOO_LATE        # older event: genuinely late
    assert st.check_seq(10, ts=200.0) == NEW            # device rebooted, counter restarted
    assert st.max_seq == 10


def test_lifecycle_facts():
    st = VehicleState(VIN, 1)
    assert st.apply(ev(evt="TRIP_START", soc_pct=80, odo_km=100), 1.0) == []
    assert st.status == "DRIVING"
    facts = st.apply(ev(evt="TRIP_END", soc_pct=70, odo_km=112), 2.0)
    assert facts[0][0] == "trip" and facts[0][1]["end"]["odo"] == 112
    st.apply(ev(evt="CHARGE_START", soc_pct=20, charger_id=7), 3.0)
    assert st.status == "CHARGING"
    facts = st.apply(ev(evt="CHARGE_STOP", soc_pct=80), 4.0)
    assert facts[0][0] == "charge" and facts[0][1]["start"]["charger_id"] == 7
    assert st.status == "PARKED"


def test_status_inferred_from_periodic():
    st = VehicleState(VIN, 1)
    st.apply(ev(power_kw=11.0), 1)
    assert st.status == "CHARGING"
    st.apply(ev(speed_kmh=30), 2)
    assert st.status == "DRIVING"
    st.apply(ev(), 3)
    assert st.status == "PARKED"


def test_checkpoint_roundtrip_and_public():
    st = VehicleState(VIN, 2, max_seq=9, seen_mask=3, trip={"ts": 1, "soc": 50, "odo": 3})
    st2 = VehicleState.from_checkpoint({**st.to_checkpoint(), "unknown_future_field": 1})
    assert st2 == st
    assert set(st.to_public()) == set(VehicleState.PUBLIC)


def test_moved_only_on_real_movement():
    st = VehicleState(VIN, 1, lat=12.97, lon=77.59)
    assert st.moved() is True
    assert st.moved() is False
    st.lat += 0.00001
    assert st.moved() is False
    st.lat += 0.001
    assert st.moved() is True


def test_cell_key_center_roundtrip():
    k = cell_key(12.9716, 77.5946)
    lat, lon = cell_center(k)
    assert abs(lat - 12.9716) < 0.04 and abs(lon - 77.5946) < 0.04
    assert cell_key(lat, lon) == k


def test_simple_rules():
    kinds = {a.type_code for a in simple_rules(VIN, 1, ev(batt_temp_c=58, dtc=["P0AA6"], soc_pct=9), "DRIVING", 1.0)}
    assert kinds == {"BATTERY_OVERTEMP", "DTC_CRITICAL", "LOW_SOC"}
    assert simple_rules(VIN, 1, ev(), "PARKED", 1.0) == []
    # P0A7E is covered by the temperature rule, not double-alerted as a DTC
    assert all(a.type_code != "DTC_CRITICAL" for a in simple_rules(VIN, 1, ev(dtc=["P0A7E"]), "PARKED", 1.0))


def test_effective_range_derates_in_heat():
    m = MODELS_BY_CODE["VL-STRATA"]
    assert effective_range_km(m, 50, 100, 25) > effective_range_km(m, 50, 100, 40)


@pytest.mark.skipif(not GRAPH.exists(), reason="road graph not built")
def test_range_rule_alerts_only_when_no_charger_reachable():
    g = RoadGraph.load(GRAPH)
    m = MODELS_BY_CODE["KS-SWIFT"]
    node = g.nearest_node(12.97, 77.59)
    near = {node: 1}
    far_node = g.nearest_node(13.08, 77.72)
    assert range_rule(VIN, 1, ev(soc_pct=30), 1.0, m, g, near) is None
    a = range_rule(VIN, 1, ev(soc_pct=2), 1.0, m, g, {far_node: 9})
    assert a is not None and a.type_code == "LOW_RANGE_NO_CHARGER" and a.details["nearest_charger_km"] > 5
