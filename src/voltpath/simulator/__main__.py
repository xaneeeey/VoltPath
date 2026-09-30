"""Fleet simulator: 100K EVs across N processes, pushing signed OEM webhook batches to ingest.

Realism knobs (env):
  SIM_RATE        aggregate periodic events/sec (100000 = every vehicle at 1 Hz)
  SIM_PROCS       worker processes (vehicles are sharded vin-index % procs)
  SIM_DUP_PCT     duplicate re-sends (at-least-once OEM clouds)            default 0.5
  SIM_LATE_PCT    events delayed 5-30 s (out-of-order)                       default 1.0
  SIM_BAD_PCT     corrupt records (bad VIN / out-of-range / malformed)       default 0.05
  SIM_OUTAGE      enable connectivity outages: 2 % of a shard offline 1-3 min, then backlog burst
  SIM_BURST_AT    unix ts: 3x rate for SIM_BURST_S seconds (shift-start burst)
Back-pressure: on HTTP 429 the sender honours Retry-After; when the send queue is full the
tick loop blocks, so the simulator naturally slows instead of dropping data.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import multiprocessing as mp
import os
import queue
import random
import signal
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime

import httpx
import numpy as np
import orjson
from prometheus_client import Counter, Gauge, Histogram, start_http_server

from voltcore.graph import RoadGraph
from voltcore.oem import ENCODERS
from voltpath.config import settings, setup_logging
from voltpath.fleetgen import generate
from voltpath.simulator.world import Shard

EVENTS = Counter("sim_events_total", "events produced", ["kind"])
SENT = Counter("sim_http_requests_total", "webhook POSTs", ["oem", "status"])
QDEPTH = Gauge("sim_send_queue_depth", "pending batches", ["proc"])
POST_LAT = Histogram("sim_post_seconds", "webhook POST latency", buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5))
TICK = Histogram("sim_tick_seconds", "tick compute time", buckets=(.01, .025, .05, .1, .25, .5, 1))

BATCH_MAX = 1000


def env_f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def sign(secret: str, ts: str, body: bytes) -> str:
    return hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


class Sender:
    """Thread pool that POSTs encoded batches; retries with Retry-After on 429/5xx."""

    def __init__(self, base_url: str, proc: int, threads: int = 4, maxsize: int = 200):
        self.q: queue.Queue = queue.Queue(maxsize=maxsize)
        self.base = base_url.rstrip("/")
        self.secrets = settings().oem_secret_map
        self.proc = str(proc)
        self.log = logging.getLogger("sim.sender")
        for _ in range(threads):
            threading.Thread(target=self._run, daemon=True).start()

    def submit(self, oem: str, body: bytes, ctype: str) -> None:
        self.q.put((oem, body, ctype))  # blocks when full -> back-pressure into the tick loop
        QDEPTH.labels(self.proc).set(self.q.qsize())

    def _run(self) -> None:
        client = httpx.Client(timeout=10.0, limits=httpx.Limits(max_keepalive_connections=4))
        while True:
            oem, body, ctype = self.q.get()
            backoff = 0.2
            while True:
                ts = str(int(time.time()))
                try:
                    t0 = time.perf_counter()
                    r = client.post(f"{self.base}/v1/oem/{oem}/telemetry", content=body,
                                    headers={"Content-Type": ctype, "X-Timestamp": ts,
                                             "X-Signature": sign(self.secrets[oem], ts, body)})
                    POST_LAT.observe(time.perf_counter() - t0)
                    SENT.labels(oem, str(r.status_code)).inc()
                    if r.status_code in (429, 502, 503, 504):
                        time.sleep(float(r.headers.get("Retry-After", backoff)))
                        backoff = min(backoff * 2, 5)
                        continue
                    if r.status_code >= 400:
                        self.log.warning("ingest rejected batch: %s %s", r.status_code, r.text[:200])
                    break
                except httpx.HTTPError as exc:
                    SENT.labels(oem, "error").inc()
                    self.log.warning("post failed (%s), retrying in %.1fs", exc, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 5)
            QDEPTH.labels(self.proc).set(self.q.qsize())


def corrupt(ev: dict, rng: random.Random) -> dict:
    ev = dict(ev)
    kind = rng.randrange(3)
    if kind == 0:
        v = ev["vin"]
        ev["vin"] = v[:8] + ("0" if v[8] != "0" else "1") + v[9:]   # breaks the check digit
    elif kind == 1:
        ev["soc_pct"] = 140.0
    else:
        ev["lat"] = 123.0
    return ev


def run_shard(proc: int, procs: int, stop: mp.Event) -> None:
    log = setup_logging(f"sim-{proc}")
    s = settings()
    world = generate(s.seed, s.n_vehicles)
    graph = RoadGraph.load(s.graph_path)
    idx = np.arange(proc, len(world.vin), procs)
    now = datetime.now(UTC)
    shard = Shard(world, graph, idx, seed=s.seed * 1000 + proc, now=now)
    sender = Sender(os.environ.get("INGEST_URL", "http://localhost:8001"), proc)
    rng = random.Random(proc)

    rate = env_f("SIM_RATE", 100_000)
    dup_p = env_f("SIM_DUP_PCT", 0.5) / 100
    late_p = env_f("SIM_LATE_PCT", 1.0) / 100
    bad_p = env_f("SIM_BAD_PCT", 0.05) / 100
    outage = os.environ.get("SIM_OUTAGE", "1") == "1"
    burst_at = env_f("SIM_BURST_AT", 0)
    burst_s = env_f("SIM_BURST_S", 300)

    vin_index = {v: k for k, v in enumerate(shard.vins)}
    start_http_server(settings().metrics_port + 1 + proc)  # one scrape target per shard process
    delayed: list[tuple[float, int, dict]] = []     # (release_ts, index, event) for late delivery
    offline_until = np.zeros(shard.n)
    backlog: dict[int, list[dict]] = defaultdict(list)
    log.info("shard %d/%d: %d vehicles, target %.0f ev/s total", proc, procs, shard.n, rate)

    tick_s = 1.0
    next_tick = time.time()
    while not stop.is_set():
        t0 = time.perf_counter()
        now = datetime.now(UTC)
        changes = shard.tick(now, tick_s)
        mult = 3.0 if burst_at and burst_at <= now.timestamp() < burst_at + burst_s else 1.0
        per_vehicle = min(1.0, rate * mult / len(world.vin))
        periodic = shard.select_periodic(per_vehicle) if per_vehicle < 1.0 else np.arange(shard.n)
        batch: list[dict] = [shard.canonical(i, now, evt) for i, evt in changes]
        batch += [shard.canonical(int(i), now, "PERIODIC") for i in periodic]
        if mult > 1.0 and per_vehicle >= 1.0:  # 3x burst when already at 1 Hz: extra samples
            extra = shard.rng.choice(shard.n, size=int(shard.n * (mult - 1)), replace=True)
            batch += [shard.canonical(int(i), now, "PERIODIC") for i in extra]
        EVENTS.labels("generated").inc(len(batch))

        # connectivity outages: buffer at the "OEM cloud", flush as a burst on reconnect
        t = now.timestamp()
        if outage and rng.random() < 1 / 120:
            victims = shard.rng.choice(shard.n, size=max(1, shard.n // 50), replace=False)
            offline_until[victims] = t + shard.rng.uniform(60, 180, len(victims))
        out: list[tuple[int, dict]] = []   # (shard index, event) - index survives VIN corruption
        for ev in batch:
            k = vin_index[ev["vin"]]
            if offline_until[k] > t:
                backlog[k].append(ev)
                continue
            if k in backlog:
                out.extend((k, b) for b in backlog.pop(k))
                EVENTS.labels("backlog_flush").inc()
            r = rng.random()
            if r < late_p:
                delayed.append((t + rng.uniform(5, 30), k, ev))
                EVENTS.labels("late").inc()
                continue
            out.append((k, ev))
            if r < late_p + dup_p:
                out.append((k, ev))
                EVENTS.labels("duplicate").inc()
            elif r < late_p + dup_p + bad_p:
                out[-1] = (k, corrupt(ev, rng))
                EVENTS.labels("corrupt").inc()
        if delayed:
            out.extend((k, e) for ts_, k, e in delayed if ts_ <= t)
            delayed = [d for d in delayed if d[0] > t]
        rng.shuffle(out)  # OEM clouds do not preserve order across vehicles

        by_oem: dict[str, list] = defaultdict(list)
        for k, ev in out:
            oem = shard.oem[k]
            by_oem[oem].append(ENCODERS[oem](ev))
        for oem, items in by_oem.items():
            for j in range(0, len(items), BATCH_MAX):
                chunk = items[j:j + BATCH_MAX]
                if oem == "kestrel":
                    body = ("\n".join(chunk)).encode()
                    if rng.random() < bad_p * 20:
                        body += b"\nthis-is-not|a-valid|kestrel-line"
                    sender.submit(oem, body, "text/plain")
                else:
                    sender.submit(oem, orjson.dumps(chunk), "application/json")
        TICK.observe(time.perf_counter() - t0)
        next_tick += tick_s
        sleep = next_tick - time.time()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_tick = time.time()  # running behind: don't accumulate debt


def main() -> None:
    log = setup_logging("simulator")
    procs = int(os.environ.get("SIM_PROCS", "4"))
    generate(settings().seed, settings().n_vehicles)  # warm cache before fork
    stop = mp.Event()
    ctx = mp.get_context("fork")
    workers = [ctx.Process(target=run_shard, args=(p, procs, stop), daemon=True) for p in range(procs)]
    for w in workers:
        w.start()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    log.info("simulator started with %d shards", procs)
    try:
        while any(w.is_alive() for w in workers):
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
