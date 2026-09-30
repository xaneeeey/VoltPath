"""Vectorised vehicle physics for one simulator shard.

State lives in numpy arrays (one slot per vehicle) so a 25K-vehicle shard ticks in tens of
milliseconds; only routes (ragged) are per-vehicle Python objects. The simulator is the
ground truth: it knows each pack's true degradation rate and defects, the platform does not.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from voltcore.charging_dp import taper_kw
from voltcore.energy import MODELS_BY_CODE, wh_per_km
from voltcore.graph import RoadGraph
from voltpath.fleetgen import World

PARKED, DRIVING, CHARGING = 0, 1, 2
STATUS = {PARKED: "PARKED", DRIVING: "DRIVING", CHARGING: "CHARGING"}


@dataclass
class ActiveRoute:
    cum: np.ndarray   # cumulative metres at each node
    lat: np.ndarray
    lon: np.ndarray
    dest_node: int
    purpose: str      # "trip" | "charge"
    station_id: int | None = None


def ambient_c(ts: datetime) -> float:
    """Bengaluru-like diurnal temperature: ~19 C at 5am, ~30 C at 3pm."""
    h = ts.astimezone(UTC).hour + 5.5 + ts.minute / 60  # IST
    return 24.5 + 5.5 * math.sin((h - 9) / 24 * 2 * math.pi)


def traffic_factor(ts: datetime) -> float:
    h = (ts.astimezone(UTC).hour + 5.5) % 24
    if 8 <= h < 11 or 17 <= h < 21:
        return 0.55  # rush hour
    if h >= 23 or h < 6:
        return 1.35
    return 1.0


class Shard:
    def __init__(self, world: World, graph: RoadGraph, idx: np.ndarray, seed: int, now: datetime):
        self.w, self.g, self.idx = world, graph, idx
        n = len(idx)
        self.n = n
        self.rng = np.random.default_rng(seed)
        self.prng = random.Random(seed)
        self.vins = [world.vin[i] for i in idx]
        self.oem = [world.oem[i] for i in idx]
        models = [MODELS_BY_CODE[world.model_code[i]] for i in idx]
        self.models = models
        self.cap = np.array([m.capacity_kwh for m in models])
        self.max_dc = np.array([m.max_dc_kw for m in models])
        self.max_ac = np.array([m.max_ac_kw for m in models])
        self.soh = world.soh0[idx].astype(np.float64).copy()
        self.degr = world.degr_per_year[idx].astype(np.float64)
        self.defect = world.defect[idx]
        self.dc_pref = world.dc_share[idx] > 0.3
        self.state = np.full(n, PARKED, dtype=np.int8)
        self.node = world.home_node[idx].astype(np.int64).copy()
        self.lat = graph.lat[self.node].copy()
        self.lon = graph.lon[self.node].copy()
        self.soc = self.rng.uniform(25, 95, n)
        self.odo = np.array([max(1000.0, (now.date() - world.commissioned[i]).days * float(world.daily_km[i]) * 0.9)
                             for i in idx])
        self.temp = np.full(n, ambient_c(now))
        self.speed = np.zeros(n)
        self.power = np.zeros(n)
        self.trip_speed = np.zeros(n)
        self.s = np.zeros(n)  # metres along route
        # stagger departures over the first ~15 minutes so the fleet ramps up naturally
        self.until = now.timestamp() + self.rng.exponential(420, n)
        self.charge_threshold = self.rng.uniform(12, 35, n)
        self.charge_threshold[self.rng.random(n) < 0.06] = 6  # range-optimistic drivers -> alerts
        self.charge_target = self.rng.uniform(80, 95, n)
        self.charger = np.full(n, -1, dtype=np.int64)
        self.charger_kw = np.zeros(n)
        # epoch-based start keeps sequences monotonic across simulator restarts
        self.seq = (int(now.timestamp()) * 4 + self.rng.integers(0, 1_000, n)).astype(np.int64)
        self.hot_until = np.zeros(n)  # defect heat episodes
        self.routes: list[ActiveRoute | None] = [None] * n
        self.emit_acc = self.rng.random(n)
        # charging infrastructure lookups
        self.station_by_node: dict[int, int] = {s.node: s.id for s in world.stations}
        self.connectors: dict[int, list[tuple[int, str, float]]] = {}
        for c in world.chargers:
            self.connectors.setdefault(c.station_id, []).append((c.id, c.connector_type, c.max_kw))
        self.poi = graph.indices[self.rng.integers(0, len(graph.indices), 4000)]  # destinations

    # ------------------------------------------------------------------ routing helpers
    def _route_to(self, i: int, dest: int, purpose: str, station_id: int | None = None) -> bool:
        r = self.g.astar(int(self.node[i]), int(dest))
        if r is None or len(r.nodes) < 2:
            return False
        nodes = np.asarray(r.nodes)
        la, lo = self.g.lat[nodes], self.g.lon[nodes]
        seg = np.hypot(np.diff(la) * 111_000, np.diff(lo) * 111_000 * math.cos(math.radians(la[0])))
        # scale geometric segment lengths to routed metres (edge weights are haversine-summed)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        if cum[-1] > 0:
            cum *= r.dist_m / cum[-1]
        self.routes[i] = ActiveRoute(cum, la, lo, int(dest), purpose, station_id)
        self.s[i] = 0.0
        return True

    def _pick_destination(self, i: int) -> int:
        """Urban trips are mostly 2-10 km: sample POIs and keep the first in that band."""
        cand = self.poi[self.rng.integers(len(self.poi), size=12)]
        d = np.hypot((self.g.lat[cand] - self.lat[i]) * 111, (self.g.lon[cand] - self.lon[i]) * 108)
        ok = np.nonzero((d > 2) & (d < 10))[0]
        return int(cand[ok[0]] if len(ok) else cand[np.argmin(np.abs(d - 5))])

    def _pick_charger(self, i: int) -> tuple[int, int] | None:
        found = self.g.nearest_targets(int(self.node[i]), self.station_by_node, k=3, cutoff_m=25_000)
        if not found:
            return None
        station_id, route = self.prng.choice(found)
        return station_id, route.nodes[-1]

    # ------------------------------------------------------------------ tick
    def tick(self, now: datetime, dt: float) -> list[tuple[int, str]]:
        """Advance physics by dt seconds; returns state-change events [(i, evt)]."""
        t = now.timestamp()
        events: list[tuple[int, str]] = []
        amb = ambient_c(now)
        traffic = traffic_factor(now)

        # ---- departures
        due = np.nonzero((self.state == PARKED) & (self.until <= t))[0]
        for i in due:
            if self.soc[i] < self.charge_threshold[i]:
                pick = self._pick_charger(i)
                ok = pick is not None and self._route_to(i, pick[1], "charge", pick[0])
            else:
                ok = self._route_to(i, self._pick_destination(i), "trip")
            if not ok:
                self.until[i] = t + 60
                continue
            self.state[i] = DRIVING
            self.trip_speed[i] = self.rng.uniform(18, 42)
            events.append((i, "TRIP_START"))

        # ---- driving
        drv = np.nonzero(self.state == DRIVING)[0]
        if len(drv):
            target = self.trip_speed[drv] * traffic
            noise = self.rng.normal(0, 3, len(drv))
            stop = self.rng.random(len(drv)) < 0.03          # traffic lights / congestion
            new_speed = np.where(stop, 0.0, np.clip(0.7 * self.speed[drv] + 0.3 * target + noise, 0, 110))
            harsh = (self.speed[drv] - new_speed) > 18
            self.speed[drv] = new_speed
            dist_m = new_speed / 3.6 * dt
            hvac = amb > 27
            wh = np.array([wh_per_km(self.models[i], new_speed[k], amb, hvac) for k, i in enumerate(drv)]) \
                if len(drv) < 64 else self._wh_vec(drv, new_speed, amb, hvac)
            kwh = wh * dist_m / 1000 / 1000
            usable = self.cap[drv] * self.soh[drv] / 100
            self.soc[drv] = np.maximum(0.0, self.soc[drv] - kwh / usable * 100)
            self.power[drv] = -(wh * new_speed / 1000)
            self.temp[drv] += (amb + 6 + new_speed / 25 - self.temp[drv]) * 0.01
            self.odo[drv] += dist_m / 1000
            self.s[drv] += dist_m
            for k, i in enumerate(drv):
                if harsh[k]:
                    events.append((i, "HARSH_BRAKE"))
                r = self.routes[i]
                if r is None:
                    continue
                if self.soc[i] <= 0.0:
                    self._tow(i, events)
                    continue
                if self.s[i] >= r.cum[-1]:
                    self._arrive(i, t, events)
                else:
                    j = int(np.searchsorted(r.cum, self.s[i]))
                    f = (self.s[i] - r.cum[j - 1]) / max(1e-6, r.cum[j] - r.cum[j - 1])
                    self.lat[i] = r.lat[j - 1] + f * (r.lat[j] - r.lat[j - 1])
                    self.lon[i] = r.lon[j - 1] + f * (r.lon[j] - r.lon[j - 1])

        # ---- charging
        chg = np.nonzero(self.state == CHARGING)[0]
        if len(chg):
            kw = np.minimum(self.charger_kw[chg], taper_kw(self.soc[chg], self.charger_kw[chg]))
            usable = self.cap[chg] * self.soh[chg] / 100
            self.soc[chg] = np.minimum(100.0, self.soc[chg] + kw * dt / 3600 * 0.92 / usable * 100)
            self.power[chg] = kw
            heat = np.where(self.charger_kw[chg] > 25, 16.0, 5.0)
            self.temp[chg] += (amb + heat - self.temp[chg]) * 0.004
            self.speed[chg] = 0.0
            done = chg[self.soc[chg] >= self.charge_target[chg]]
            for i in done:
                self.state[i] = PARKED
                self.power[i] = 0.0
                self.until[i] = t + self.rng.uniform(120, 1200)
                events.append((i, "CHARGE_STOP"))
                self.charger[i] = -1

        # ---- parked: cool towards ambient
        pk = self.state == PARKED
        self.temp[pk] += (amb - self.temp[pk]) * 0.002
        self.power[pk] = 0.0
        self.speed[pk] = 0.0

        # ---- defect packs: heat episodes + fast SoH loss; everyone ages (accelerated x365 for demo)
        hot_start = self.defect & (self.hot_until < t) & (self.rng.random(self.n) < 1 / 5400)
        self.hot_until[hot_start] = t + self.rng.uniform(60, 240, int(hot_start.sum()))
        hot = self.hot_until > t
        self.temp[hot] = np.maximum(self.temp[hot], 57 + self.rng.normal(0, 2, int(hot.sum())))
        self.soh -= self.degr / (365 * 86_400) * dt  # real-time ageing at each pack's true yearly rate
        return events

    def _wh_vec(self, drv, speed, amb, hvac):
        base = np.array([self.models[i].base_wh_per_km for i in drv])
        from voltcore.energy import speed_factor, temp_factor

        return base * speed_factor(speed) * temp_factor(amb) * (1.08 if hvac else 1.0)

    def _tow(self, i: int, events: list[tuple[int, str]]) -> None:
        """Battery flat: roadside assistance tows the car to the nearest station (AC top-up)."""
        pick = self._pick_charger(i)
        self.routes[i] = None
        self.speed[i] = 0.0
        events.append((i, "TRIP_END"))
        if pick is None:
            self.state[i] = PARKED
            self.until[i] = float("inf")
            return
        station_id, node = pick
        self.node[i] = node
        self.lat[i], self.lon[i] = self.g.lat[node], self.g.lon[node]
        cid, _ctype, kw = self.connectors[station_id][0]
        self.state[i] = CHARGING
        self.charger[i] = cid
        self.charger_kw[i] = min(kw, self.max_ac[i])
        events.append((i, "CHARGE_START"))

    def _arrive(self, i: int, t: float, events: list[tuple[int, str]]) -> None:
        r = self.routes[i]
        self.node[i] = r.dest_node
        self.lat[i], self.lon[i] = r.lat[-1], r.lon[-1]
        self.speed[i] = 0.0
        self.routes[i] = None
        events.append((i, "TRIP_END"))
        if r.purpose == "charge" and r.station_id is not None:
            conns = self.connectors[r.station_id]
            dc = [c for c in conns if c[1] != "TYPE2"]
            pool = dc if (dc and (self.dc_pref[i] or self.soc[i] < 15)) else conns
            cid, ctype, kw = self.prng.choice(pool)
            limit = self.max_dc[i] if ctype != "TYPE2" else self.max_ac[i]
            self.state[i] = CHARGING
            self.charger[i] = cid
            self.charger_kw[i] = min(kw, limit)
            events.append((i, "CHARGE_START"))
        else:
            self.state[i] = PARKED
            h = (datetime.fromtimestamp(t, UTC).hour + 5.5) % 24
            long_park = h >= 21 or h < 6
            self.until[i] = t + (self.rng.uniform(1800, 5400) if long_park else self.rng.exponential(900) + 120)

    # ------------------------------------------------------------------ emission
    def select_periodic(self, rate_per_vehicle: float) -> np.ndarray:
        self.emit_acc += rate_per_vehicle
        due = self.emit_acc >= 1.0
        self.emit_acc[due] -= 1.0
        return np.nonzero(due)[0]

    def canonical(self, i: int, now: datetime, evt: str) -> dict:
        self.seq[i] += 1
        dtc = []
        if self.temp[i] > 55:
            dtc.append("P0A7E")
        if self.defect[i] and self.soh[i] < 80:
            dtc.append("P0A7F")
        return {
            "vin": self.vins[i], "ts": now, "seq": int(self.seq[i]),
            "lat": float(self.lat[i]), "lon": float(self.lon[i]), "speed_kmh": float(self.speed[i]),
            "soc_pct": float(self.soc[i]), "soh_pct": float(self.soh[i]), "batt_temp_c": float(self.temp[i]),
            "odo_km": float(self.odo[i]), "power_kw": float(self.power[i]),
            "evt": "FAULT" if (dtc and evt == "PERIODIC" and self.temp[i] > 55) else evt, "dtc": dtc,
            "charger_id": int(self.charger[i]) if self.charger[i] >= 0 else None,
        }
