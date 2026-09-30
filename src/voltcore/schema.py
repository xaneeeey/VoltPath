"""Canonical telemetry schema (v1) shared by every service.

Every OEM format is normalised into `TelemetryEvent` at the edge (ingest gateway). Downstream
services depend only on this contract (see tests/contract).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .vin import is_valid_vin

SCHEMA_VERSION = 1


class EventType(StrEnum):
    PERIODIC = "PERIODIC"
    TRIP_START = "TRIP_START"
    TRIP_END = "TRIP_END"
    CHARGE_START = "CHARGE_START"
    CHARGE_STOP = "CHARGE_STOP"
    HARSH_BRAKE = "HARSH_BRAKE"
    FAULT = "FAULT"


class VehicleStatus(StrEnum):
    DRIVING = "DRIVING"
    PARKED = "PARKED"
    CHARGING = "CHARGING"
    OFFLINE = "OFFLINE"


class TelemetryEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int = SCHEMA_VERSION
    vin: str
    ts: datetime
    oem: str
    seq: int = Field(ge=0)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=300)
    soc_pct: float = Field(ge=0, le=100)
    soh_pct: float = Field(ge=0, le=100)
    batt_temp_c: float = Field(ge=-40, le=120)
    odo_km: float = Field(ge=0)
    power_kw: float = 0.0  # +ve = charging, -ve = discharging
    evt: EventType = EventType.PERIODIC
    dtc: list[str] = Field(default_factory=list)
    charger_id: int | None = None

    @field_validator("vin")
    @classmethod
    def _vin(cls, v: str) -> str:
        if not is_valid_vin(v):
            raise ValueError("invalid VIN (charset/length/check digit)")
        return v

    @field_validator("ts")
    @classmethod
    def _ts(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            v = v.replace(tzinfo=UTC)
        return v

    def to_wire(self) -> bytes:
        return orjson.dumps(self.model_dump(mode="json"))


def event_to_dict(e: TelemetryEvent) -> dict:
    d = e.model_dump(mode="json")
    return d


def wire_to_dict(raw: bytes) -> dict:
    """Fast path for consumers: trust the contract (validated at ingest), skip pydantic."""
    return orjson.loads(raw)


def parse_ts(value: str | int | float) -> datetime:
    if isinstance(value, int | float):
        # heuristic: ms vs s epochs
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=UTC)
    s = value.replace("Z", "+00:00")
    return datetime.fromisoformat(s)
