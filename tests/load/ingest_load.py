"""Ingest load test: signed OEM webhook batches at a target event rate, with an optional burst.

    uv run python tests/load/ingest_load.py --rate 100000 --duration 120 --burst 3

Client side  : events/s offered and accepted, HTTP latency p50/p95/p99, 429 back-pressure count.
Server side  : Prometheus (if reachable) for ingest-accepted rate, processor-applied rate,
               consumer lag and ingest->state p95 during the run.
Writes tests/load/results/<timestamp>.json + .md.

Payloads use real fleet VINs (so the processor applies them) with monotonically increasing
sequence numbers (so dedup does not discard them). Kestrel pipe-text is used because it is the
cheapest dialect to generate - the generator must not be the bottleneck.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import multiprocessing as mp
import random
import statistics
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

BATCH = 1000


def worker(idx: int, n_workers: int, url: str, secret: str, rate: float, duration: float, burst: float,
           burst_at: float, vins: list[str], out: mp.Queue):
    rng = random.Random(idx)
    my_vins = vins[idx::n_workers]
    seq0 = int(time.time()) * 8 + 5_000_000
    seq = {v: seq0 for v in my_vins}
    client = httpx.Client(timeout=15, limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))
    lat: list[float] = []
    sent = accepted = throttled = errors = 0
    lock = threading.Lock()
    t_start = time.time()
    per_worker = rate / n_workers
    next_t = t_start
    pending: list[threading.Thread] = []
    vi = 0

    def post(body: bytes, n: int):
        nonlocal accepted, throttled, errors
        ts = str(int(time.time()))
        sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        t0 = time.perf_counter()
        try:
            r = client.post(url, content=body, headers={"Content-Type": "text/plain", "X-Timestamp": ts,
                                                       "X-Signature": sig})
            dt = time.perf_counter() - t0
            with lock:
                lat.append(dt)
                if r.status_code == 202:
                    accepted += r.json()["accepted"]
                elif r.status_code == 429:
                    throttled += 1
                else:
                    errors += 1
        except httpx.HTTPError:
            with lock:
                errors += 1

    while (now := time.time()) - t_start < duration:
        mult = burst if burst_at <= now - t_start < burst_at + 300 else 1.0
        lines = []
        for _ in range(BATCH):
            v = my_vins[vi % len(my_vins)]
            vi += 1
            seq[v] += 1
            lines.append(f"{v}|{now:.3f}|{12.9 + rng.random() * 0.15:.6f}|{77.5 + rng.random() * 0.2:.6f}|"
                         f"{rng.random() * 60:.1f}|{20 + rng.random() * 70:.2f}|{90 + rng.random() * 9:.2f}|"
                         f"{28 + rng.random() * 8:.1f}|{10000 + rng.random() * 90000:.2f}|-5.00||PERIODIC|{seq[v]}|")
        body = "\n".join(lines).encode()
        t = threading.Thread(target=post, args=(body, BATCH), daemon=True)
        t.start()
        pending.append(t)
        pending = [p for p in pending if p.is_alive()]
        while len(pending) > 16:            # bounded concurrency per worker
            pending[0].join()
            pending = [p for p in pending if p.is_alive()]
        sent += BATCH
        next_t += BATCH / (per_worker * mult)
        sleep = next_t - time.time()
        if sleep > 0:
            time.sleep(sleep)
    for p in pending:
        p.join()
    out.put({"sent": sent, "accepted": accepted, "throttled": throttled, "errors": errors, "lat": lat,
             "elapsed": time.time() - t_start})


PROM = "http://localhost:9090"


def prom(q: str) -> float | None:
    try:
        r = httpx.get(f"{PROM}/api/v1/query?query={quote(q)}", timeout=3).json()
        res = r["data"]["result"]
        return float(res[0]["value"][1]) if res else 0.0
    except Exception:  # noqa: BLE001 - Prometheus optional
        return None


def sample_server(stop: threading.Event, samples: list[dict]):
    while not stop.is_set():
        samples.append({
            "t": time.time(),
            "ingest_eps": prom('sum(rate(ingest_events_total{result="accepted"}[15s]))'),
            "processed_eps": prom('sum(rate(processor_events_total{result="applied"}[15s]))'),
            "lag": prom("sum(processor_consumer_lag)"),
            "age_p95_s": prom("histogram_quantile(0.95, sum(rate(processor_event_age_seconds_bucket[30s])) by (le))"),
        })
        stop.wait(5)


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8001/v1/oem/kestrel/telemetry")
    ap.add_argument("--secret", default="dev-kestrel-secret")
    ap.add_argument("--rate", type=float, default=100_000)
    ap.add_argument("--duration", type=float, default=120)
    ap.add_argument("--burst", type=float, default=1.0, help="multiplier applied for 5 min starting at --burst-at")
    ap.add_argument("--burst-at", type=float, default=30)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--prom", default="http://localhost:9090")
    args = ap.parse_args()
    global PROM
    PROM = args.prom

    from voltpath.fleetgen import generate

    vins = generate().vin
    ctx = mp.get_context("spawn")  # never fork after starting the sampler thread (inherited locks deadlock)
    q = ctx.Queue()
    stop = threading.Event()
    samples: list[dict] = []
    threading.Thread(target=sample_server, args=(stop, samples), daemon=True).start()
    procs = [ctx.Process(target=worker, args=(i, args.workers, args.url, args.secret, args.rate, args.duration,
                                             args.burst, args.burst_at, vins, q)) for i in range(args.workers)]
    print(f"offering {args.rate:,.0f} ev/s for {args.duration:.0f}s (burst x{args.burst} at t={args.burst_at}s) "
          f"with {args.workers} workers ...")
    for p in procs:
        p.start()
    parts = [q.get() for _ in procs]
    for p in procs:
        p.join()
    time.sleep(6)
    stop.set()

    lat = [x for p in parts for x in p["lat"]]
    elapsed = max(p["elapsed"] for p in parts)
    sent = sum(p["sent"] for p in parts)
    acc = sum(p["accepted"] for p in parts)
    srv = [s for s in samples if s["ingest_eps"] is not None]
    res = {
        "offered_rate": args.rate, "duration_s": round(elapsed, 1), "burst": args.burst,
        "events_sent": sent, "events_accepted": acc,
        "client_offered_eps": round(sent / elapsed), "client_accepted_eps": round(acc / elapsed),
        "requests": len(lat), "http_429": sum(p["throttled"] for p in parts), "errors": sum(p["errors"] for p in parts),
        "batch_latency_ms": {"p50": round(statistics.median(lat) * 1000, 1) if lat else None,
                             "p95": round(pct(lat, 0.95) * 1000, 1) if lat else None,
                             "p99": round(pct(lat, 0.99) * 1000, 1) if lat else None},
        "server": {
            "peak_ingest_eps": round(max((s["ingest_eps"] for s in srv), default=0)),
            "peak_processed_eps": round(max((s["processed_eps"] or 0 for s in srv), default=0)),
            "max_consumer_lag": round(max((s["lag"] or 0 for s in srv), default=0)),
            "final_consumer_lag": round(srv[-1]["lag"] or 0) if srv else None,
            "max_ingest_to_state_p95_s": round(max((s["age_p95_s"] or 0 for s in srv), default=0), 2),
        } if srv else "prometheus unreachable",
        "samples": samples,
    }
    out = ROOT / "tests" / "load" / "results"
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}.json").write_text(json.dumps(res, indent=2))
    summary = {k: v for k, v in res.items() if k != "samples"}
    (out / f"{stamp}.md").write_text("# Ingest load test\n\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
