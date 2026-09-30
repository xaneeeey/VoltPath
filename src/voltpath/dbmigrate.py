"""Minimal forward-only SQL migration runner (ordered files, tracked in schema_migrations).

Plain SQL (not an ORM autogenerator) because TimescaleDB / pgvector / RLS DDL is the point of
the schema and should be reviewed as written. Each file runs in its own transaction and a
Postgres advisory lock prevents two replicas migrating concurrently.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import psycopg

from voltpath.config import ROOT, settings, setup_logging

MIGRATIONS = ROOT / "db" / "migrations"
LOCK_ID = 0x766F6C74  # 'volt'


def wait_for_db(url: str, timeout_s: float = 90) -> None:
    deadline = time.time() + timeout_s
    while True:
        try:
            with psycopg.connect(url, connect_timeout=3):
                return
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(1.5)


def migrate(url: str | None = None, directory: Path = MIGRATIONS) -> list[str]:
    log = setup_logging("migrate")
    url = url or settings().database_url
    wait_for_db(url)
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, "
                         "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for f in sorted(directory.glob("*.sql")):
                if f.name in done:
                    continue
                log.info("applying %s", f.name)
                with conn.transaction():
                    conn.execute(f.read_text())
                    conn.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (f.name,))
                applied.append(f.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    log.info("migrations up to date (%d applied now)", len(applied))
    return applied


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else None)
