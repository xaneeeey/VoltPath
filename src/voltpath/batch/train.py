"""Train the remaining-range model and evaluate it against the dashboard baseline.

Training data: trips labelled with realised range from the simulator's physics (with hidden
per-vehicle payload and driving-style factors the baseline cannot see, plus measurement noise).
The feature that carries the hidden factors is `recent_wh_per_km` - observable in production
from the last few trips. Honest caveat (docs/algorithms.md): the labels come from our own
simulator, so the absolute error is optimistic; the *relative* improvement over the baseline is
the meaningful result.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import train_test_split

from voltcore.energy import MODELS, wh_per_km
from voltpath.config import settings
from voltpath.ml import FEATURES, MODEL_FILE

log = logging.getLogger("train")


def make_dataset(n: int = 150_000, seed: int = 11):
    rng = np.random.default_rng(seed)
    X, y, base = [], [], []
    mi = rng.integers(0, len(MODELS), n)
    soc = rng.uniform(5, 100, n)
    soh = rng.uniform(72, 100, n)
    amb = rng.uniform(14, 40, n)
    spd = rng.uniform(8, 95, n)
    hvac = (amb > 27) & (rng.random(n) < 0.85)
    style = rng.normal(1.0, 0.09, n)                       # hidden: driver aggressiveness
    payload = np.where(rng.random(n) < 0.35, rng.uniform(1.0, 1.25, n), 1.0)  # hidden: load
    for i in range(n):
        m = MODELS[mi[i]]
        factor = style[i] * payload[i]
        true_whkm = wh_per_km(m, spd[i], amb[i], hvac[i], factor)
        usable = m.capacity_kwh * soh[i] / 100 * soc[i] / 100
        target = usable * 1000 / true_whkm * rng.normal(1.0, 0.03)
        # the observable proxy: consumption on recent trips (different speed / temp, noisy)
        recent = wh_per_km(m, max(5.0, spd[i] + rng.normal(0, 10)), amb[i] + rng.normal(0, 3), hvac[i], factor) \
            * rng.normal(1.0, 0.04)
        X.append([soc[i], soh[i], amb[i], spd[i], float(hvac[i]), m.capacity_kwh, m.rated_range_km, recent])
        y.append(target)
        base.append(m.rated_range_km * soc[i] / 100 * soh[i] / 100)
    return np.array(X), np.array(y), np.array(base)


def metrics(y, pred) -> dict:
    err = pred - y
    return {"mae_km": round(float(np.abs(err).mean()), 2), "rmse_km": round(float(np.sqrt((err ** 2).mean())), 2),
            "mape_pct": round(float((np.abs(err) / np.maximum(y, 1)).mean() * 100), 2),
            "p90_abs_err_km": round(float(np.percentile(np.abs(err), 90)), 2),
            "overestimate_rate": round(float((err > 0).mean()), 3)}


def train(out_dir: str | None = None) -> dict:
    t0 = time.time()
    X, y, base = make_dataset()
    Xtr, Xte, ytr, yte, _btr, bte = train_test_split(X, y, base, test_size=0.2, random_state=0)
    model = HistGradientBoostingRegressor(max_iter=400, learning_rate=0.08, max_leaf_nodes=48,
                                          l2_regularization=0.1, random_state=0)
    model.fit(Xtr, ytr)
    lin = LinearRegression().fit(Xtr, ytr)
    result = {
        "version": time.strftime("%Y%m%d-%H%M%S"),
        "features": FEATURES,
        "n_train": len(Xtr), "n_test": len(Xte),
        "baseline_rated_x_soc_x_soh": metrics(yte, bte),
        "linear_regression": metrics(yte, lin.predict(Xte)),
        "gradient_boosting": metrics(yte, model.predict(Xte)),
        "train_seconds": round(time.time() - t0, 1),
    }
    b, g = result["baseline_rated_x_soc_x_soh"]["mae_km"], result["gradient_boosting"]["mae_km"]
    result["mae_improvement_vs_baseline_pct"] = round((b - g) / b * 100, 1)
    d = Path(out_dir or settings().model_dir)
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "version": result["version"], "metrics": result}, d / MODEL_FILE)
    (d / "range_model_metrics.json").write_text(json.dumps(result, indent=2))
    log.info("range model trained: %s", json.dumps(result))
    return result
