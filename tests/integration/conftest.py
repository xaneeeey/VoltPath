"""Integration fixtures: real TimescaleDB(+pgvector), Redis and Redpanda via Testcontainers.

The containers are started once per session; the schema is created by the real migration runner
and a small deterministic world (3,000 vehicles) is seeded, so tests exercise the same SQL, RLS
policies, Kafka semantics and Redis structures as production.
"""

from __future__ import annotations

import os
import tempfile

import pytest

pytestmark = pytest.mark.integration

PG_IMAGE = "timescale/timescaledb-ha:pg16"
RP_IMAGE = "redpandadata/redpanda:v24.2.7"


@pytest.fixture(scope="session")
def stack():
    from testcontainers.kafka import RedpandaContainer
    from testcontainers.postgres import PostgresContainer
    from testcontainers.redis import RedisContainer

    pg = PostgresContainer(PG_IMAGE, username="voltpath", password="voltpath", dbname="voltpath", driver=None)
    rd = RedisContainer("redis:7.4-alpine")
    rp = RedpandaContainer(RP_IMAGE)
    pg.start()
    rd.start()
    rp.start(timeout=120)   # default 10 s is too tight on a busy laptop / CI runner
    host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
    env = {
        "DATABASE_URL": f"postgresql://voltpath:voltpath@{host}:{port}/voltpath",
        "APP_DATABASE_URL": f"postgresql://voltpath_app:voltpath_app@{host}:{port}/voltpath",
        "REDIS_URL": f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/0",
        "KAFKA_BOOTSTRAP": rp.get_bootstrap_server(),
        "N_VEHICLES": "3000",
        "JWT_PRIVATE_KEY_PATH": os.path.join(tempfile.mkdtemp(), "jwt.pem"),
        "MODEL_DIR": tempfile.mkdtemp(),
        "TOPIC_RAW": "it.telemetry.raw",
        "TOPIC_DLQ": "it.telemetry.dlq",
        "ANTHROPIC_API_KEY": "",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    from voltpath.config import settings
    from voltpath.fleetgen import generate

    settings.cache_clear()
    generate.cache_clear()
    from voltpath.dbmigrate import migrate
    from voltpath.infra import ensure_topics
    from voltpath.seed import seed

    migrate(env["DATABASE_URL"])
    seed(env["DATABASE_URL"])
    ensure_topics()
    from voltpath.batch.kb import load_kb

    load_kb(env["DATABASE_URL"])
    yield env
    for c in (rp, rd, pg):
        c.stop()
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    settings.cache_clear()
    generate.cache_clear()


@pytest.fixture(scope="session")
def world(stack):
    from voltpath.fleetgen import generate

    return generate(42, 3000)
