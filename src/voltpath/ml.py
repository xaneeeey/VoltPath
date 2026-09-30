"""Remaining-range model: features, loading and inference (training lives in voltpath.batch.train).

Why ML beats the dashboard baseline (rated_range x SoC x SoH): real consumption depends on
speed profile, temperature/HVAC and - most of all - the individual vehicle's recent Wh/km
(payload, driver style). The model learns that interaction; the baseline ignores it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from voltcore.energy import EvModel, baseline_range_km
from voltpath.config import settings

log = logging.getLogger(__name__)
FEATURES = ["soc", "soh", "ambient_c", "avg_speed", "hvac", "capacity_kwh", "rated_range_km", "recent_wh_per_km"]
MODEL_FILE = "range_model.joblib"


def features(model: EvModel, soc: float, soh: float, ambient_c: float, avg_speed: float, hvac: bool,
             recent_wh_per_km: float | None) -> list[float]:
    return [soc, soh, ambient_c, avg_speed, float(hvac), model.capacity_kwh, model.rated_range_km,
            recent_wh_per_km if recent_wh_per_km else model.base_wh_per_km]


def load_range_model(path: str | None = None):
    p = Path(path or settings().model_dir) / MODEL_FILE
    if not p.exists():
        log.warning("no range model at %s - serving baseline only", p)
        return None
    import joblib

    return joblib.load(p)


def predict_range(bundle, model: EvModel, soc: float, soh: float, ambient_c: float, avg_speed: float,
                  hvac: bool, recent_wh_per_km: float | None) -> dict:
    base = baseline_range_km(model, soc, soh)
    out = {"baseline_km": round(base, 1), "ml_km": None, "model_version": None}
    if bundle is not None:
        x = np.array([features(model, soc, soh, ambient_c, avg_speed, hvac, recent_wh_per_km)])
        out["ml_km"] = round(float(max(0.0, bundle["model"].predict(x)[0])), 1)
        out["model_version"] = bundle.get("version")
    out["range_km"] = out["ml_km"] if out["ml_km"] is not None else out["baseline_km"]
    return out
