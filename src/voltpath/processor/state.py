"""Per-vehicle streaming state owned by the partition consumer (Kafka Streams-style local state).

Dedup: exact per-VIN sliding window over sequence numbers (max_seq + 64-bit bitmap). Events
within the window are checked exactly; events older than the window are 'too late' for live
state/alerts but still flow to idempotent storage sinks (PK (vin, ts) / ON CONFLICT).
State is checkpointed to Redis with every update, so a new partition owner after a rebalance
or crash resumes with the right dedup window and open trip / charging-session markers.
"""

from __future__ import annotations

from dataclasses import dataclass, field

WINDOW = 64
RESET_AFTER_S = 30.0

NEW, DUPLICATE, TOO_LATE = "new", "duplicate", "too_late"
CELL_DEG = 0.04  # ~4.4 km map-cluster grid


def cell_key(lat: float, lon: float) -> str:
    """O(1) grid cell for map clustering (a geohash-5 costs ~25 bit iterations in Python;
    this runs for every vehicle every 2 s)."""
    return f"{int(lat // CELL_DEG)}:{int(lon // CELL_DEG)}"


def cell_center(key: str) -> tuple[float, float]:
    i, j = key.split(":")
    return (int(i) + 0.5) * CELL_DEG, (int(j) + 0.5) * CELL_DEG


@dataclass
class VehicleState:
    vin: str
    tenant_id: int
    max_seq: int = -1
    seen_mask: int = 0            # bit k set => seq (max_seq - k) seen, k in [0, 63]
    ts: float = 0.0               # event time of the latest applied event (epoch s)
    lat: float = 0.0
    lon: float = 0.0
    speed: float = 0.0
    soc: float = 0.0
    soh: float = 0.0
    temp: float = 0.0
    power: float = 0.0
    odo: float = 0.0
    status: str = "PARKED"
    charger_id: int | None = None
    dtc: list[str] = field(default_factory=list)
    trip: dict | None = None      # open trip: {ts, soc, odo}
    chg: dict | None = None       # open charging session: {ts, soc, charger_id}
    stored_ts: float = 0.0        # last ts written to the warm store (downsampling)
    last_range_check: float = 0.0
    geo_lat: float = 0.0          # position last written to the GEO index (write only on movement)
    geo_lon: float = 0.0

    # ------------------------------------------------------------------ dedup window
    def check_seq(self, seq: int, ts: float | None = None) -> str:
        """Classify a sequence number. A jump far *below* the window while event time moves
        forward is a device sequence reset (telematics unit reboot / OEM re-provisioning): the
        window restarts instead of discarding every future event as late."""
        if seq > self.max_seq:
            shift = seq - self.max_seq
            self.seen_mask = ((self.seen_mask << shift) | 1) & ((1 << WINDOW) - 1) if shift < WINDOW else 1
            self.max_seq = seq
            return NEW
        k = self.max_seq - seq
        if k >= WINDOW:
            if ts is not None and ts > self.ts + RESET_AFTER_S:
                self.max_seq, self.seen_mask = seq, 1
                return NEW
            return TOO_LATE
        bit = 1 << k
        if self.seen_mask & bit:
            return DUPLICATE
        self.seen_mask |= bit
        return NEW

    # ------------------------------------------------------------------ transitions
    def apply(self, e: dict, ts: float) -> list[tuple[str, dict]]:
        """Apply an in-order event; returns lifecycle facts [(kind, payload)] for sinks."""
        facts: list[tuple[str, dict]] = []
        evt = e["evt"]
        if evt == "TRIP_START":
            self.trip = {"ts": ts, "soc": e["soc_pct"], "odo": e["odo_km"]}
            self.status = "DRIVING"
        elif evt == "TRIP_END":
            if self.trip:
                facts.append(("trip", {"start": self.trip, "end": {"ts": ts, "soc": e["soc_pct"], "odo": e["odo_km"]}}))
            self.trip = None
            self.status = "PARKED"
        elif evt == "CHARGE_START":
            self.chg = {"ts": ts, "soc": e["soc_pct"], "charger_id": e.get("charger_id")}
            self.status = "CHARGING"
        elif evt == "CHARGE_STOP":
            if self.chg:
                facts.append(("charge", {"start": self.chg, "end": {"ts": ts, "soc": e["soc_pct"]}}))
            self.chg = None
            self.status = "PARKED"
        elif e["power_kw"] > 0.5:
            self.status = "CHARGING"
        elif e["speed_kmh"] > 1 or self.trip:
            self.status = "DRIVING"
        else:
            self.status = "PARKED"
        self.ts = ts
        self.lat, self.lon = e["lat"], e["lon"]
        self.speed, self.soc, self.soh = e["speed_kmh"], e["soc_pct"], e["soh_pct"]
        self.temp, self.power, self.odo = e["batt_temp_c"], e["power_kw"], e["odo_km"]
        self.charger_id = e.get("charger_id")
        self.dtc = e.get("dtc") or []
        return facts

    # ------------------------------------------------------------------ (de)serialisation
    def moved(self, min_deg: float = 0.0004) -> bool:
        """True (and remembers the position) if the vehicle moved ~>45 m since the last GEO write."""
        if abs(self.lat - self.geo_lat) > min_deg or abs(self.lon - self.geo_lon) > min_deg:
            self.geo_lat, self.geo_lon = self.lat, self.lon
            return True
        return False

    def to_public(self) -> dict:
        """What the API/UI reads (no internal bookkeeping)."""
        return {"vin": self.vin, "ts": self.ts, "lat": round(self.lat, 6), "lon": round(self.lon, 6),
                "speed": round(self.speed, 1), "soc": round(self.soc, 1), "soh": round(self.soh, 2),
                "temp": round(self.temp, 1), "power": round(self.power, 1), "odo": round(self.odo, 1),
                "status": self.status, "charger_id": self.charger_id, "dtc": self.dtc}

    PUBLIC = ("vin", "ts", "lat", "lon", "speed", "soc", "soh", "temp", "power", "odo", "status", "charger_id", "dtc")

    def to_checkpoint(self) -> dict:
        # shallow copy is enough (values are scalars / small JSON-able containers) and ~20x
        # faster than dataclasses.asdict's recursive deepcopy on the hot path
        return dict(self.__dict__)

    @classmethod
    def from_checkpoint(cls, d: dict) -> VehicleState:
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)
