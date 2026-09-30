"""Batch CLI / scheduler.

    python -m voltpath.batch history|rollup|forecast|energy|train|kb|all
    python -m voltpath.batch scheduler      # container default: bootstrap once, then periodic
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from voltpath.batch import jobs
from voltpath.batch.kb import load_kb
from voltpath.batch.train import train
from voltpath.config import settings, setup_logging
from voltpath.ml import MODEL_FILE


def run_all(force: bool = True) -> None:
    jobs.history()
    jobs.rollup()
    if force or not jobs.forecast_is_fresh():
        jobs.forecast()
        jobs.energy_rollup()
    if not (Path(settings().model_dir) / MODEL_FILE).exists():
        train()


def scheduler() -> None:
    log = setup_logging("batch")
    run_all(force=False)  # container restarts must not redo a fresh 100K-vehicle forecast
    last_forecast = time.time()
    while True:
        time.sleep(600)
        try:
            jobs.rollup()
            if time.time() - last_forecast > 6 * 3600:
                jobs.forecast()
                jobs.energy_rollup()
                last_forecast = time.time()
        except Exception:  # noqa: BLE001 - keep the scheduler alive; next run retries
            log.exception("scheduled batch run failed")


if __name__ == "__main__":
    setup_logging("batch")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    {"history": jobs.history, "rollup": jobs.rollup, "forecast": jobs.forecast, "energy": jobs.energy_rollup,
     "train": train, "kb": load_kb, "all": run_all, "scheduler": scheduler}[cmd]()
