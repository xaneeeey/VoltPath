"""12-factor configuration: every setting comes from the environment (same image on any cloud)."""

from __future__ import annotations

import json
import logging
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql://voltpath:voltpath@localhost:5432/voltpath"
    app_database_url: str = "postgresql://voltpath_app:voltpath_app@localhost:5432/voltpath"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap: str = "localhost:19092"
    topic_raw: str = "telemetry.raw"
    topic_dlq: str = "telemetry.dlq"
    topic_partitions: int = 12

    s3_endpoint: str = "http://localhost:8333"
    s3_access_key: str = "voltpath"
    s3_secret_key: str = "voltpath-secret"
    s3_region: str = "us-east-1"
    lake_bucket: str = "voltpath-lake"

    graph_path: str = str(ROOT / "data" / "bangalore_graph.npz")
    model_dir: str = str(ROOT / "data" / "models")
    seed: int = 42
    n_vehicles: int = 100_000

    # OEM webhook HMAC secrets (dev defaults; production values come from the secret store).
    oem_secrets: str = json.dumps({"voltra": "dev-voltra-secret", "nimbus": "dev-nimbus-secret",
                                   "kestrel": "dev-kestrel-secret"})

    jwt_private_key_path: str = str(ROOT / "secrets" / "jwt_private.pem")
    jwt_issuer: str = "https://auth.voltpath.local"
    jwt_audience: str = "voltpath-api"
    jwt_ttl_s: int = 3600
    cors_origins: str = "http://localhost:5173,http://localhost:8080"
    rate_limit_per_min: int = 600
    seed_password: str = "voltpath-demo"

    anthropic_api_key: str | None = None
    copilot_model: str = "claude-sonnet-5"

    ingest_max_inflight: int = 400_000
    metrics_port: int = 9100

    @property
    def oem_secret_map(self) -> dict[str, str]:
        return json.loads(self.oem_secrets)


@lru_cache
def settings() -> Settings:
    return Settings()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import orjson

        d = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
             "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)
        for k in ("service", "trace_id", "tenant_id", "vin"):
            if hasattr(record, k):
                d[k] = getattr(record, k)
        return orjson.dumps(d).decode()


def setup_logging(service: str, level: str = "INFO") -> logging.Logger:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    for noisy in ("httpx", "uvicorn.access", "botocore", "urllib3"):
        logging.getLogger(noisy).setLevel("WARNING")
    return logging.getLogger(service)
