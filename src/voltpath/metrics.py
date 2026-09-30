"""Prometheus exposition that works for single- and multi-process (uvicorn workers) servers."""

from __future__ import annotations

import os
import shutil
import tempfile

from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest


def prepare_multiproc(name: str) -> None:
    """Call in the parent BEFORE workers import prometheus_client."""
    d = os.path.join(tempfile.gettempdir(), f"prom_{name}")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = d


def metrics_response() -> PlainTextResponse:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        reg = CollectorRegistry()
        multiprocess.MultiProcessCollector(reg)
        return PlainTextResponse(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
