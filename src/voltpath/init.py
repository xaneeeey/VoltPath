"""One-shot bootstrap job: migrations, seed data, Kafka topics, lake bucket, signing key.
Every step is idempotent so the job is safe to re-run (compose `init` / k8s pre-install Job)."""

from voltpath.config import setup_logging
from voltpath.dbmigrate import migrate
from voltpath.infra import ensure_bucket, ensure_jwt_key, ensure_topics
from voltpath.seed import seed


def main() -> None:
    log = setup_logging("init")
    migrate()
    seed()
    ensure_topics()
    ensure_bucket()
    ensure_jwt_key()
    try:
        from voltpath.batch.kb import load_kb

        load_kb()
    except Exception as exc:  # noqa: BLE001 - KB is optional for the pipeline
        log.warning("knowledge base not loaded: %s", exc)
    log.info("init complete")


if __name__ == "__main__":
    main()
