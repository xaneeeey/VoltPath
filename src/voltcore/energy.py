"""EV energy / range physics shared by the simulator (ground truth) and the baseline predictor.

Consumption (Wh/km) = base * speed_factor(v) * temp_factor(T) * (1 + hvac) * payload
 - speed_factor: U-shaped (stop-go inefficiency at low speed, aero drag ~ v^2 at high speed)
 - temp_factor : cold batteries and cabin heating/cooling cost energy
The baseline range estimate is what most dashboards show: rated_range * SoC * SoH.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class EvModel:
    code: str
    name: str
    oem: str
    capacity_kwh: float
    rated_range_km: float
    max_dc_kw: float
    max_ac_kw: float

    @property
    def base_wh_per_km(self) -> float:
        return self.capacity_kwh * 1000 / self.rated_range_km


# Fictional models (synthetic data only) spanning compact city cars to vans.
MODELS: list[EvModel] = [
    EvModel("VL-AURA", "Voltra Aura", "voltra", 40.5, 315, 50, 7.2),
    EvModel("VL-STRATA", "Voltra Strata", "voltra", 72.0, 460, 150, 11.0),
    EvModel("NB-CIRRUS", "Nimbus Cirrus", "nimbus", 58.0, 410, 120, 11.0),
    EvModel("NB-HAULER", "Nimbus Hauler Van", "nimbus", 90.0, 330, 110, 22.0),
    EvModel("KS-SWIFT", "Kestrel Swift", "kestrel", 30.2, 250, 30, 7.2),
    EvModel("KS-RANGER", "Kestrel Ranger", "kestrel", 60.0, 380, 100, 11.0),
]
MODELS_BY_CODE = {m.code: m for m in MODELS}


def speed_factor(v_kmh):
    v = np.asarray(v_kmh, dtype=float)
    return 1.0 + 0.35 * np.exp(-v / 12.0) + 0.000055 * np.maximum(v - 50, 0) ** 2


def temp_factor(t_c):
    t = np.asarray(t_c, dtype=float)
    return 1.0 + 0.012 * np.maximum(18 - t, 0) + 0.006 * np.maximum(t - 30, 0)


def wh_per_km(model: EvModel, v_kmh, ambient_c, hvac_on, payload_factor=1.0):
    hv = np.where(np.asarray(hvac_on, dtype=bool), 0.08, 0.0)
    return model.base_wh_per_km * speed_factor(v_kmh) * temp_factor(ambient_c) * (1 + hv) * payload_factor


def true_range_km(model: EvModel, soc_pct, soh_pct, avg_speed_kmh, ambient_c, hvac_on, payload_factor=1.0):
    usable_kwh = model.capacity_kwh * np.asarray(soh_pct) / 100 * np.asarray(soc_pct) / 100
    return usable_kwh * 1000 / wh_per_km(model, avg_speed_kmh, ambient_c, hvac_on, payload_factor)


def baseline_range_km(model: EvModel, soc_pct: float, soh_pct: float = 100.0) -> float:
    return model.rated_range_km * soc_pct / 100 * soh_pct / 100
