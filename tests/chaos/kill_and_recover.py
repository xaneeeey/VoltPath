"""Chaos test: kill the Kafka broker and a stream processor mid-stream, prove recovery without loss.

    uv run python tests/chaos/kill_and_recover.py [--vins 60 --events 250]

1. Streams N uniquely-identifiable events for a sample of fleet VINs through the real ingest
   gateway (signed webhooks, retrying on 429/5xx/connection errors like an OEM cloud would).
2. While streaming: `docker kill` the Redpanda broker (restart after 15 s) and one processor
   replica (restart after 10 s).
3. Waits for consumer lag to reach zero, then checks the warm store: every (vin, ts) sent must
   be present exactly once (PK guarantees no duplicates; gaps would mean loss).
Writes tests/chaos/results/<timestamp>.json. Exit code 1 on any loss.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

INGEST = "http://localhost:8001/v1/oem/kestrel/telemetry"
SECRET = "dev-kestrel-secret"
PG = "postgresql://voltpath:voltpath@localhost:5432/voltpath"
PROM = "http://localhost:9090"


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=False).stdout.strip()


def post(client: httpx.Client, body: bytes, log: list[str]) -> int:
    backoff = 0.25
    attempts = 0
    while True:
        attempts += 1
        ts = str(int(time.time()))
        sig = hmac.new(SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        try:
            r = client.post(INGEST, content=body, headers={"X-Timestamp": ts, "X-Signature": sig,
                                                          "Content-Type": "text/plain"})
            if r.status_code == 202:
                return attempts
            log.append(f"{time.strftime('%X')} HTTP {r.status_code}")
        except httpx.HTTPError as exc:
            log.append(f"{time.strftime('%X')} {type(exc).__name__}")
        time.sleep(backoff)
        backoff = min(backoff * 2, 4)


def lag() -> float:
    try:
        r = httpx.get(f"{PROM}/api/v1/query", params={"query": "sum(processor_consumer_lag)"}, timeout=3).json()
        res = r["data"]["result"]
        return float(res[0]["value"][1]) if res else 0.0
    except Exception:  # noqa: BLE001
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vins", type=int, default=60)
    ap.add_argument("--events", type=int, default=250)
    args = ap.parse_args()
    from voltpath.fleetgen import generate

    vins = generate().vin[5000:5000 + args.vins]
    base = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
    tag = 500_000 + int(time.time()) % 100_000   # unique odometer value marks this run's rows
    # event i for every VIN at base + 31 s * i (31 s > warm-store downsampling interval)
    batches = []
    seq0 = int(time.time()) * 8 + 9_000_000
    for i in range(args.events):
        ts = (base + timedelta(seconds=31 * i)).timestamp()
        lines = [f"{v}|{ts:.3f}|12.970000|77.590000|10.0|55.00|95.00|30.0|{tag}.00|-3.00||PERIODIC|{seq0 + i}|"
                 for v in vins]
        batches.append("\n".join(lines).encode())
    expected = args.events * len(vins)
    errors: list[str] = []
    events: list[str] = []
    retries = 0

    def chaos():
        time.sleep(6)
        events.append(f"{time.strftime('%X')} kill redpanda")
        sh("docker", "kill", "hackathon-redpanda-1")
        time.sleep(3)
        events.append(f"{time.strftime('%X')} kill processor-1")
        sh("docker", "kill", "hackathon-processor-1")
        time.sleep(12)
        events.append(f"{time.strftime('%X')} start redpanda")
        sh("docker", "start", "hackathon-redpanda-1")
        time.sleep(5)
        events.append(f"{time.strftime('%X')} start processor-1")
        sh("docker", "start", "hackathon-processor-1")

    t0 = time.time()
    th = threading.Thread(target=chaos)
    th.start()
    with httpx.Client(timeout=10) as client:
        for b in batches:
            retries += post(client, b, errors) - 1
            time.sleep(0.12)
    th.join()
    sent_s = time.time() - t0
    print(f"sent {expected} events in {sent_s:.0f}s ({retries} retries); waiting for pipeline to drain ...")

    deadline = time.time() + 300
    while time.time() < deadline:
        with psycopg.connect(PG) as c:
            got = c.execute("SELECT count(*) FROM telemetry WHERE vin = ANY(%s) AND odo_km = %s", (vins, tag)).fetchone()[0]
        if got >= expected:
            break
        time.sleep(5)
    with psycopg.connect(PG) as c:
        per_vin = dict(c.execute(
            "SELECT vin, count(*) FROM telemetry WHERE vin = ANY(%s) AND odo_km = %s GROUP BY vin", (vins, tag)).fetchall())
    got = sum(per_vin.values())
    missing = {v: args.events - per_vin.get(v, 0) for v in vins if per_vin.get(v, 0) != args.events}
    res = {"expected": expected, "stored": got, "missing_by_vin": missing, "duplicates_possible": False,
           "passed": got == expected and not missing, "send_seconds": round(sent_s, 1),
           "recovery_seconds_total": round(time.time() - t0, 1), "client_retries": retries,
           "chaos_timeline": events, "client_errors_sample": errors[:15]}
    out = ROOT / "tests" / "chaos" / "results"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
