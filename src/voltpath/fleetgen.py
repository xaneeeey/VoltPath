"""Deterministic synthetic fleet + charging-infrastructure generator.

The seed script (master data -> Postgres) and the simulator (live telemetry) both call
`generate(seed)`, so they agree on every VIN, charger and depot without sharing a database.
Hidden ground truth (pack degradation rate, defects) is only used by the simulator/history.
Everything is synthetic: fictional OEMs, operators and people.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache

import numpy as np

from voltcore.energy import MODELS_BY_CODE
from voltcore.geo import encode
from voltcore.graph import RoadGraph
from voltcore.vin import make_vin

CENTER = (12.9716, 77.5946)
TODAY = date(2026, 9, 30)
YEAR_CODE = {2022: "N", 2023: "P", 2024: "R", 2025: "S", 2026: "T"}
WMI = {"voltra": "9VL", "nimbus": "9NB", "kestrel": "9KS"}
VDS = {"VL-AURA": "AU40E", "VL-STRATA": "ST72E", "NB-CIRRUS": "CR58E", "NB-HAULER": "HV90E",
       "KS-SWIFT": "SW30E", "KS-RANGER": "RG60E"}
CHEMISTRY = {"VL-AURA": "LFP", "KS-SWIFT": "LFP", "VL-STRATA": "NMC", "NB-CIRRUS": "NMC",
             "NB-HAULER": "NMC", "KS-RANGER": "LFP"}

OPERATORS = ["GridBee", "AmpHub", "VoltNest", "PlugPoint", "EcoCharge"]

TENANTS = [
    # name, share, plan, model mix, daily km range, fleet names
    ("Acme Logistics", 0.55, "enterprise", {"NB-HAULER": 0.45, "KS-RANGER": 0.35, "VL-STRATA": 0.20},
     (90, 150), ["Last-mile North", "Last-mile South", "Last-mile East", "Line-haul", "Cold chain",
                 "Returns"]),
    ("Zenith Rentals", 0.30, "enterprise", {"VL-STRATA": 0.40, "NB-CIRRUS": 0.40, "VL-AURA": 0.20},
     (30, 110), ["Airport", "City centre", "Weekend leisure", "Corporate"]),
    ("Metro Cabs", 0.15, "growth", {"VL-AURA": 0.55, "KS-SWIFT": 0.45}, (160, 260),
     ["Day shift", "Night shift", "Airport shuttle"]),
]

FIRST = ["Aarav", "Vivaan", "Aditya", "Diya", "Ananya", "Ishaan", "Kavya", "Rohan", "Meera", "Arjun",
         "Saanvi", "Kabir", "Nisha", "Vikram", "Priya", "Rahul", "Sneha", "Farhan", "Lakshmi", "Tara"]
LAST = ["Rao", "Iyer", "Nair", "Reddy", "Sharma", "Gowda", "Menon", "Khan", "Das", "Pillai",
        "Hegde", "Shetty", "Kulkarni", "Joshi", "Bose", "Varma"]


@dataclass
class Station:
    id: int
    operator_id: int
    name: str
    lat: float
    lon: float
    geohash6: str
    node: int


@dataclass
class Charger:
    id: int
    station_id: int
    connector_type: str
    max_kw: float
    tariff_id: int


@dataclass
class Tariff:
    id: int
    operator_id: int
    name: str
    default_price: float
    windows: list[tuple[int, int, float]]


@dataclass
class Depot:
    id: int
    tenant_id: int
    name: str
    lat: float
    lon: float
    node: int
    site_cap_kw: float


@dataclass
class Fleet:
    id: int
    tenant_id: int
    name: str
    depot_id: int


@dataclass
class Tenant:
    id: int
    name: str
    plan: str
    km_range: tuple[int, int]


@dataclass
class World:
    tenants: list[Tenant]
    depots: list[Depot]
    fleets: list[Fleet]
    stations: list[Station]
    chargers: list[Charger]
    tariffs: list[Tariff]
    # vehicles as parallel arrays (100K rows)
    vin: list[str]
    tenant_id: np.ndarray
    fleet_id: np.ndarray
    model_code: list[str]
    model_year: np.ndarray
    commissioned: list[date]
    registration: list[str]
    oem: list[str]
    home_node: np.ndarray
    daily_km: np.ndarray
    # hidden ground truth
    soh0: np.ndarray            # SoH at TODAY
    degr_per_year: np.ndarray   # SoH points lost per year
    defect: np.ndarray          # bool - faulty pack (rapid degradation, heat)
    dc_share: np.ndarray        # fraction of energy from DC fast charging
    drivers: list[tuple[int, int, str, str, str, str]] = field(default_factory=list)  # id,tenant,name,email,phone,lic
    assignments: list[tuple[str, int]] = field(default_factory=list)  # vin, driver_id


def _weighted_nodes(g: RoadGraph, rng: np.random.Generator, n: int, sigma_km: float) -> np.ndarray:
    """Sample graph nodes with density decaying away from the city centre (Gaussian)."""
    dy = (g.lat - CENTER[0]) * 111.0
    dx = (g.lon - CENTER[1]) * 111.0 * np.cos(np.radians(CENTER[0]))
    w = np.exp(-(dx ** 2 + dy ** 2) / (2 * sigma_km ** 2)) + 0.05
    return rng.choice(g.n, size=n, replace=False, p=w / w.sum())


@lru_cache(maxsize=2)
def generate(seed: int = 42, n_vehicles: int = 100_000, graph_path: str | None = None) -> World:
    from voltpath.config import settings

    g = RoadGraph.load(graph_path or settings().graph_path)
    rng = np.random.default_rng(seed)
    prng = random.Random(seed)

    # ---- tariffs: per operator one AC and one DC tariff with time-of-use windows (INR/kWh)
    tariffs: list[Tariff] = []
    for oi, op in enumerate(OPERATORS, start=1):
        base_ac = round(float(rng.uniform(9, 13)), 2)
        base_dc = round(float(rng.uniform(17, 23)), 2)
        for kind, base in (("AC", base_ac), ("DC", base_dc)):
            windows = [(22 * 60, 6 * 60, round(base * 0.62, 2)),    # night off-peak
                       (6 * 60, 10 * 60, round(base * 1.0, 2)),
                       (18 * 60, 22 * 60, round(base * 1.35, 2))]   # evening peak
            tariffs.append(Tariff(len(tariffs) + 1, oi, f"{op} {kind} ToU", base, windows))

    # ---- charger stations and connectors
    n_st = 1200
    st_nodes = _weighted_nodes(g, rng, n_st, sigma_km=7.0)
    stations, chargers = [], []
    for si, node in enumerate(st_nodes, start=1):
        op = int(rng.integers(1, len(OPERATORS) + 1))
        la, lo = float(g.lat[node]), float(g.lon[node])
        stations.append(Station(si, op, f"{OPERATORS[op - 1]} #{si:04d}", la, lo, encode(la, lo, 6), int(node)))
        n_conn = int(rng.choice([2, 2, 4, 4, 4, 6, 8]))
        dc_hub = rng.random() < 0.45
        for _ in range(n_conn):
            if dc_hub and rng.random() < 0.7:
                ctype, kw = ("CCS2", float(rng.choice([30, 60, 120]))) if rng.random() < 0.9 else ("CHADEMO", 50.0)
                tid = (op - 1) * 2 + 2
            else:
                ctype, kw = "TYPE2", float(rng.choice([7.4, 11, 22]))
                tid = (op - 1) * 2 + 1
            chargers.append(Charger(len(chargers) + 1, si, ctype, kw, tid))

    # ---- tenants, depots, fleets
    tenants, depots, fleets = [], [], []
    for ti, (name, _share, plan, _mix, km, fleet_names) in enumerate(TENANTS, start=1):
        tenants.append(Tenant(ti, name, plan, km))
        dep_nodes = _weighted_nodes(g, rng, len(fleet_names), sigma_km=10.0)
        for fi, fname in enumerate(fleet_names):
            node = int(dep_nodes[fi])
            depots.append(Depot(len(depots) + 1, ti, f"{name.split()[0]} {fname} depot",
                                float(g.lat[node]), float(g.lon[node]), node,
                                float(rng.choice([250, 400, 600, 900, 1500]))))
            fleets.append(Fleet(len(fleets) + 1, ti, fname, depots[-1].id))

    # ---- vehicles
    shares = np.array([t[1] for t in TENANTS])
    counts = np.floor(shares * n_vehicles).astype(int)
    counts[0] += n_vehicles - counts.sum()
    vin, model_code, oem, registration, commissioned = [], [], [], [], []
    tenant_id = np.zeros(n_vehicles, dtype=np.int32)
    fleet_id = np.zeros(n_vehicles, dtype=np.int32)
    model_year = np.zeros(n_vehicles, dtype=np.int16)
    home_node = np.zeros(n_vehicles, dtype=np.int32)
    daily_km = np.zeros(n_vehicles, dtype=np.float32)
    serial = {"voltra": 0, "nimbus": 0, "kestrel": 0}
    i = 0
    for ti, (_name, _s, _p, mix, km, _f) in enumerate(TENANTS, start=1):
        tfleets = [f for f in fleets if f.tenant_id == ti]
        codes = list(mix)
        probs = np.array([mix[c] for c in codes])
        for _ in range(counts[ti - 1]):
            code = codes[int(rng.choice(len(codes), p=probs))]
            m = MODELS_BY_CODE[code]
            year = int(rng.choice([2022, 2023, 2024, 2025, 2026], p=[0.1, 0.2, 0.3, 0.3, 0.1]))
            serial[m.oem] += 1
            v = make_vin(WMI[m.oem], VDS[code], YEAR_CODE[year], "ABCDEFGH"[serial[m.oem] % 8], serial[m.oem])
            fl = tfleets[int(rng.integers(len(tfleets)))]
            dep = depots[fl.depot_id - 1]
            vin.append(v)
            model_code.append(code)
            oem.append(m.oem)
            tenant_id[i] = ti
            fleet_id[i] = fl.id
            model_year[i] = year
            start = date(year, 1, 1) + timedelta(days=int(rng.integers(0, 330)))
            commissioned.append(min(start, TODAY - timedelta(days=14)))
            registration.append(f"KA-{prng.randint(1, 60):02d}-{prng.choice('ABCDEFGHJKMNPRSTUVWXYZ')}"
                                f"{prng.choice('ABCDEFGHJKMNPRSTUVWXYZ')}-{prng.randint(1000, 9999)}")
            # vehicles spread around their depot: home node = random node within ~8 km of depot
            home_node[i] = dep.node
            daily_km[i] = rng.uniform(*km)
            i += 1

    # Home nodes: sample near depots (vectorised): pick random node, keep if close to depot.
    near = _weighted_nodes(g, rng, min(g.n, 40_000), sigma_km=9.0)
    home_node = near[rng.integers(0, len(near), size=n_vehicles)].astype(np.int32)

    # ---- hidden battery truth
    age_years = np.array([(TODAY - c).days / 365.25 for c in commissioned])
    lfp = np.array([CHEMISTRY[c] == "LFP" for c in model_code])
    dc_share = np.clip(rng.beta(2, 6, n_vehicles) + (tenant_id == 3) * 0.15, 0, 0.95)
    degr = np.where(lfp, rng.normal(1.3, 0.35, n_vehicles), rng.normal(2.1, 0.5, n_vehicles))
    degr = np.clip(degr * (1 + 0.9 * dc_share) * (daily_km / 120) ** 0.3, 0.4, None)
    defect = rng.random(n_vehicles) < 0.025
    degr = np.where(defect, rng.uniform(9, 18, n_vehicles), degr)
    # defects are recent: they started misbehaving within the last ~8 months
    defect_age = np.minimum(age_years, rng.uniform(0.25, 0.7, n_vehicles))
    soh0 = 100 - np.where(defect, (age_years - defect_age) * 2.0 + defect_age * degr, age_years * degr)
    soh0 = np.clip(soh0 + rng.normal(0, 0.4, n_vehicles), 60, 100)

    # ---- drivers (PII - synthetic) and assignments
    drivers, assignments = [], []
    did = 0
    for idx in range(n_vehicles):
        if rng.random() < 0.6:
            did += 1
            fn, ln = prng.choice(FIRST), prng.choice(LAST)
            drivers.append((did, int(tenant_id[idx]), f"{fn} {ln}", f"{fn.lower()}.{ln.lower()}{did}@example.com",
                            f"+91-9{prng.randint(100000000, 999999999)}",
                            f"KA{prng.randint(1, 60):02d} {prng.randint(2010, 2025)} {prng.randint(1, 9999999):07d}"))
            assignments.append((vin[idx], did))

    return World(tenants, depots, fleets, stations, chargers, tariffs, vin, tenant_id, fleet_id, model_code,
                 model_year, commissioned, registration, oem, home_node, daily_km, soh0.astype(np.float32),
                 degr.astype(np.float32), defect, dc_share.astype(np.float32), drivers, assignments)
