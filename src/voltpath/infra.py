"""Thin adapters for infrastructure clients (Kafka, S3 lake, JWT keys), shared by services."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from voltpath.config import settings

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ Kafka
def kafka_conf(**extra) -> dict:
    return {"bootstrap.servers": settings().kafka_bootstrap, **extra}


def ensure_topics() -> None:
    from confluent_kafka.admin import AdminClient, NewTopic

    s = settings()
    admin = AdminClient(kafka_conf())
    existing = set(admin.list_topics(timeout=15).topics)
    want = [
        # 12 partitions keyed by VIN: per-vehicle ordering + up to 12 parallel consumers per group.
        NewTopic(s.topic_raw, num_partitions=s.topic_partitions, replication_factor=1,
                 config={"retention.ms": str(24 * 3600 * 1000), "compression.type": "producer",
                         "cleanup.policy": "delete", "write.caching": "false"}),
        NewTopic(s.topic_dlq, num_partitions=3, replication_factor=1,
                 config={"retention.ms": str(7 * 24 * 3600 * 1000), "write.caching": "false"}),
    ]
    todo = [t for t in want if t.topic not in existing]
    if todo:
        for t, f in admin.create_topics(todo).items():
            try:
                f.result()
                log.info("created topic %s", t)
            except Exception as exc:  # noqa: BLE001 - topic may race with another replica
                log.warning("topic %s: %s", t, exc)
    enforce_durability(admin, [t.topic for t in want])


def enforce_durability(admin, topics: list[str]) -> None:
    """acks=all must mean 'fsynced'. Redpanda's dev-container mode enables write caching (ack before
    fsync), which lost acknowledged records in our chaos test when the broker was SIGKILLed. Turn it
    off cluster-wide and per topic. Other Kafka brokers do not know the property: best effort."""
    import httpx
    from confluent_kafka.admin import AlterConfigOpType, ConfigEntry, ConfigResource

    admin_url = os.environ.get("REDPANDA_ADMIN_URL")
    if admin_url:
        try:
            httpx.put(f"{admin_url}/v1/cluster_config", json={"upsert": {"write_caching_default": "false"}, "remove": []},
                      timeout=5).raise_for_status()
            log.info("redpanda: write_caching_default=false")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not set redpanda cluster config: %s", exc)
    res = [ConfigResource("topic", t, incremental_configs=[
        ConfigEntry("write.caching", "false", incremental_operation=AlterConfigOpType.SET)]) for t in topics]
    for r, f in admin.incremental_alter_configs(res).items():
        try:
            f.result()
        except Exception as exc:  # noqa: BLE001
            log.info("write.caching not applied to %s (%s)", r, exc)


# ------------------------------------------------------------------ S3 lake
def s3fs():
    from pyarrow import fs

    s = settings()
    scheme, _, host = s.s3_endpoint.partition("://")
    return fs.S3FileSystem(access_key=s.s3_access_key, secret_key=s.s3_secret_key, endpoint_override=host,
                           scheme=scheme, region=s.s3_region, allow_bucket_creation=True)


def ensure_bucket() -> None:
    from pyarrow import fs

    s = settings()
    f = s3fs()
    info = f.get_file_info(s.lake_bucket)
    if info.type == fs.FileType.NotFound:
        f.create_dir(s.lake_bucket)
        log.info("created bucket %s", s.lake_bucket)


# ------------------------------------------------------------------ JWT signing keys
def ensure_jwt_key() -> Path:
    """Dev convenience: create an RSA key on first boot. In k8s the key is mounted from the
    secret store and this is a no-op."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    p = Path(settings().jwt_private_key_path)
    if p.exists():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    os.chmod(p, 0o600)
    log.info("generated JWT signing key at %s", p)
    return p
