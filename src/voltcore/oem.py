"""OEM payload adapters -> canonical dict.

VoltPath is device-free: OEM clouds push batches to us, each in its own dialect.
  voltra  : nested camelCase JSON, metric units, ISO timestamps
  nimbus  : flat PascalCase JSON, imperial units, epoch-ms, fractions, ';'-joined DTCs
  kestrel : pipe-delimited text lines (legacy telematics), epoch seconds

Adapters are pure functions registered by name, so onboarding an OEM = one function + tests.
Encoding (the reverse direction) lives here too so the simulator and contract tests share it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from .dtc import parse_dtcs
from .schema import parse_ts

MI_TO_KM = 1.609344

Canonical = dict
Adapter = Callable[[object], Canonical]
ADAPTERS: dict[str, Adapter] = {}
ENCODERS: dict[str, Callable[[Canonical], object]] = {}


def adapter(name: str):
    def deco(fn):
        ADAPTERS[name] = fn
        return fn
    return deco


def encoder(name: str):
    def deco(fn):
        ENCODERS[name] = fn
        return fn
    return deco


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- voltra
@adapter("voltra")
def from_voltra(p: dict) -> Canonical:
    b, pos = p["battery"], p["position"]
    return {
        "vin": p["vin"], "ts": parse_ts(p["timestamp"]), "oem": "voltra", "seq": int(p["sequence"]),
        "lat": float(pos["lat"]), "lon": float(pos["lng"]), "speed_kmh": float(p["speedKph"]),
        "soc_pct": float(b["socPercent"]), "soh_pct": float(b["sohPercent"]),
        "batt_temp_c": float(b["tempC"]), "odo_km": float(p["odometerKm"]),
        "power_kw": float(b.get("powerKw", 0.0)), "evt": p.get("eventType", "PERIODIC"),
        "dtc": parse_dtcs(p.get("faults")), "charger_id": p.get("chargerId"),
    }


@encoder("voltra")
def to_voltra(e: Canonical) -> dict:
    return {
        "vin": e["vin"], "timestamp": _iso(e["ts"]), "sequence": e["seq"],
        "position": {"lat": round(e["lat"], 6), "lng": round(e["lon"], 6)},
        "speedKph": round(e["speed_kmh"], 1), "odometerKm": round(e["odo_km"], 2),
        "battery": {"socPercent": round(e["soc_pct"], 2), "sohPercent": round(e["soh_pct"], 2),
                    "tempC": round(e["batt_temp_c"], 1), "powerKw": round(e["power_kw"], 2)},
        "eventType": e["evt"], "faults": e["dtc"], "chargerId": e["charger_id"],
    }


# ---------------------------------------------------------------- nimbus
@adapter("nimbus")
def from_nimbus(p: dict) -> Canonical:
    evse = p.get("EVSE")
    return {
        "vin": p["VehicleId"], "ts": parse_ts(int(p["EpochMs"])), "oem": "nimbus", "seq": int(p["Seq"]),
        "lat": float(p["Lat"]), "lon": float(p["Lon"]), "speed_kmh": float(p["SpeedMph"]) * MI_TO_KM,
        "soc_pct": float(p["StateOfCharge"]) * 100, "soh_pct": float(p["BatteryHealth"]) * 100,
        "batt_temp_c": (float(p["BattTempF"]) - 32) * 5 / 9, "odo_km": float(p["OdometerMiles"]) * MI_TO_KM,
        "power_kw": float(p.get("PowerKw", 0.0)), "evt": p.get("Evt", "PERIODIC"),
        "dtc": parse_dtcs(p.get("Dtc")), "charger_id": int(evse) if evse not in (None, "") else None,
    }


@encoder("nimbus")
def to_nimbus(e: Canonical) -> dict:
    return {
        "VehicleId": e["vin"], "EpochMs": int(e["ts"].timestamp() * 1000), "Seq": e["seq"],
        "Lat": round(e["lat"], 6), "Lon": round(e["lon"], 6), "SpeedMph": round(e["speed_kmh"] / MI_TO_KM, 2),
        "StateOfCharge": round(e["soc_pct"] / 100, 4), "BatteryHealth": round(e["soh_pct"] / 100, 4),
        "BattTempF": round(e["batt_temp_c"] * 9 / 5 + 32, 1), "OdometerMiles": round(e["odo_km"] / MI_TO_KM, 2),
        "PowerKw": round(e["power_kw"], 2), "Evt": e["evt"], "Dtc": ";".join(e["dtc"]),
        "EVSE": "" if e["charger_id"] is None else str(e["charger_id"]),
    }


# ---------------------------------------------------------------- kestrel
KESTREL_RE = re.compile(
    r"^(?P<vin>[A-HJ-NPR-Z0-9]{17})\|(?P<ts>\d{9,11}(?:\.\d+)?)\|(?P<lat>-?\d+\.\d+)\|(?P<lon>-?\d+\.\d+)\|"
    r"(?P<spd>\d+(?:\.\d+)?)\|(?P<soc>\d+(?:\.\d+)?)\|(?P<soh>\d+(?:\.\d+)?)\|(?P<tmp>-?\d+(?:\.\d+)?)\|"
    r"(?P<odo>\d+(?:\.\d+)?)\|(?P<pwr>-?\d+(?:\.\d+)?)\|(?P<dtc>[A-Z0-9,]*)\|(?P<evt>[A-Z_]+)\|(?P<seq>\d+)\|"
    r"(?P<chg>\d*)$"
)


class KestrelParseError(ValueError):
    pass


@adapter("kestrel")
def from_kestrel(line: str) -> Canonical:
    m = KESTREL_RE.match(line.strip())
    if not m:
        raise KestrelParseError(f"malformed kestrel line: {line[:60]!r}")
    g = m.groupdict()
    return {
        "vin": g["vin"], "ts": parse_ts(float(g["ts"])), "oem": "kestrel", "seq": int(g["seq"]),
        "lat": float(g["lat"]), "lon": float(g["lon"]), "speed_kmh": float(g["spd"]),
        "soc_pct": float(g["soc"]), "soh_pct": float(g["soh"]), "batt_temp_c": float(g["tmp"]),
        "odo_km": float(g["odo"]), "power_kw": float(g["pwr"]), "evt": g["evt"],
        "dtc": parse_dtcs(g["dtc"]), "charger_id": int(g["chg"]) if g["chg"] else None,
    }


@encoder("kestrel")
def to_kestrel(e: Canonical) -> str:
    return "|".join([
        e["vin"], f"{e['ts'].timestamp():.3f}", f"{e['lat']:.6f}", f"{e['lon']:.6f}", f"{e['speed_kmh']:.1f}",
        f"{e['soc_pct']:.2f}", f"{e['soh_pct']:.2f}", f"{e['batt_temp_c']:.1f}", f"{e['odo_km']:.2f}",
        f"{e['power_kw']:.2f}", ",".join(e["dtc"]), e["evt"], str(e["seq"]),
        "" if e["charger_id"] is None else str(e["charger_id"]),
    ])


def normalise(oem: str, payloads: Iterable) -> tuple[list[Canonical], list[tuple[object, str]]]:
    """Normalise a batch; returns (ok, rejected[(payload, reason)]). Never raises per-record."""
    fn = ADAPTERS.get(oem)
    if fn is None:
        raise KeyError(f"unknown OEM {oem!r}")
    ok, bad = [], []
    for p in payloads:
        try:
            ok.append(fn(p))
        except (KeyError, ValueError, TypeError) as exc:
            bad.append((p, f"{type(exc).__name__}: {exc}"))
    return ok, bad
