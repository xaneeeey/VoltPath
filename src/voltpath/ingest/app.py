"""Ingest gateway: OEM cloud webhooks -> canonical events -> Kafka.

Pipeline per batch (1-1000 records):
  1. authenticate  HMAC-SHA256(secret, ts + "." + body), 5-minute replay window, constant-time compare
  2. decode        JSON array (voltra/nimbus) or text lines (kestrel), optional gzip
  3. normalise     OEM adapter -> canonical dict (units, field names, DTC parsing)
  4. validate      schema v1 bounds + VIN check digit + clock-skew guard; failures -> DLQ with reason
  5. dedup         per-worker rotating Bloom filter on (vin, seq) drops exact retransmits early;
                   exact, cross-replica dedup happens in the processor (partition owner)
  6. produce       idempotent producer, key = VIN (per-vehicle ordering), lz4, acks=all
Durability: 202 only after the broker acknowledged every record (acks=all, idempotent); otherwise
503 + Retry-After. Back-pressure: if the producer's in-flight queue exceeds INGEST_MAX_INFLIGHT the
request gets 429 + Retry-After and nothing is enqueued, so the OEM re-sends later (no silent drops).
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import hmac
import logging
import threading
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import orjson
from confluent_kafka import KafkaException, Producer
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import Counter, Gauge, Histogram

from voltcore.oem import ADAPTERS, normalise
from voltcore.schema import SCHEMA_VERSION, EventType
from voltcore.sketches import RotatingBloom
from voltcore.vin import is_valid_vin
from voltpath.config import settings
from voltpath.metrics import metrics_response

log = logging.getLogger("ingest")

EV = Counter("ingest_events_total", "events by outcome", ["oem", "result"])
REQ = Histogram("ingest_request_seconds", "batch handling latency", ["oem"],
                buckets=(.001, .0025, .005, .01, .025, .05, .1, .25, .5, 1))
INFLIGHT = Gauge("ingest_producer_inflight", "messages waiting in the producer queue", multiprocess_mode="livesum")
THROTTLED = Counter("ingest_throttled_total", "batches rejected with 429", ["oem"])
UNACKED = Counter("ingest_unacked_batches_total", "batches answered 503 because the broker did not ack", ["oem"])

EVT_TYPES = frozenset(e.value for e in EventType)
MAX_SKEW_FUTURE = timedelta(minutes=5)
MAX_AGE = timedelta(days=7)


def validate(e: dict, now: datetime) -> str | None:
    """Hot-path validator mirroring voltcore.schema.TelemetryEvent (~1 us vs ~8 us for pydantic).
    Contract tests assert both agree."""
    if not is_valid_vin(e["vin"]):
        return "vin"
    if not (-90 <= e["lat"] <= 90 and -180 <= e["lon"] <= 180):
        return "latlon"
    if not (0 <= e["soc_pct"] <= 100 and 0 <= e["soh_pct"] <= 100):
        return "soc_soh"
    if not (0 <= e["speed_kmh"] <= 300 and -40 <= e["batt_temp_c"] <= 120 and e["odo_km"] >= 0):
        return "range"
    if e["seq"] < 0 or e["evt"] not in EVT_TYPES:
        return "seq_evt"
    ts = e["ts"]
    if ts > now + MAX_SKEW_FUTURE or ts < now - MAX_AGE:
        return "clock_skew"
    return None


PRODUCER_CONF = {
    "enable.idempotence": True,
    "acks": "all",
    "linger.ms": 10,
    "batch.num.messages": 10_000,
    "compression.type": "lz4",
    "queue.buffering.max.kbytes": 1_048_576,
    "message.timeout.ms": 30_000,
}


class _Batch:
    """Tracks broker acknowledgements for one HTTP request's records."""

    __slots__ = ("pending", "failed", "fut", "loop")

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.pending = 0
        self.failed = 0
        self.loop = loop
        self.fut: asyncio.Future = loop.create_future()

    def done(self, err, _msg=None) -> None:  # librdkafka delivery callback (poll thread)
        if err is not None:
            self.failed += 1
        self.pending -= 1
        if self.pending == 0:
            self.loop.call_soon_threadsafe(self._resolve)

    def _resolve(self) -> None:
        if not self.fut.done():
            self.fut.set_result(self.failed)


class Gateway:
    """At-least-once edge contract: 202 is returned only after the broker has durably acknowledged
    every record of the batch (acks=all). Any delivery failure/timeout returns 503 so the OEM cloud
    retries; duplicates from retries are removed by the Bloom filter / processor dedup window."""

    ACK_TIMEOUT_S = 15.0

    def __init__(self, producer: Producer | None = None):
        s = settings()
        self.s = s
        self.secrets = {k: v.encode() for k, v in s.oem_secret_map.items()}
        self._external = producer is not None
        self.producer = producer if producer is not None else self._new_producer()
        self.bloom = RotatingBloom(capacity=2_000_000, fp_rate=1e-3)
        self._fatal = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._poll_loop, daemon=True, name="kafka-poll").start()

    def _new_producer(self) -> Producer:
        return Producer({"bootstrap.servers": self.s.kafka_bootstrap, "error_cb": self._on_error,
                         "queue.buffering.max.messages": max(self.s.ingest_max_inflight * 2, 100_000), **PRODUCER_CONF})

    def _on_error(self, err) -> None:
        if err.fatal():
            # e.g. idempotent sequence desync after a broker lost unflushed data: this producer
            # instance can never recover - replace it (callers already got 503 for affected batches)
            log.error("fatal producer error, recreating producer: %s", err)
            self._fatal = True

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.producer.poll(0.05)
            except Exception:  # noqa: BLE001
                log.exception("producer poll failed")
            if self._fatal and not self._external:
                with self._lock:
                    old, self.producer, self._fatal = self.producer, self._new_producer(), False
                try:
                    old.purge()
                    old.flush(0)   # fails every queued record -> their batches resolve as failed (503)
                except Exception:  # noqa: BLE001
                    pass

    def close(self, timeout: float = 10) -> None:
        self._stop.set()
        self.producer.flush(timeout)

    def verify(self, oem: str, ts: str | None, sig: str | None, body: bytes) -> None:
        secret = self.secrets.get(oem)
        if secret is None:
            raise HTTPException(404, f"unknown OEM {oem}")
        if not ts or not sig:
            raise HTTPException(401, "missing signature")
        try:
            skew = abs(time.time() - int(ts))
        except ValueError as exc:
            raise HTTPException(401, "bad timestamp") from exc
        if skew > 300:
            raise HTTPException(401, "stale request (replay window)")
        expected = hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(401, "bad signature")

    async def handle(self, oem: str, body: bytes) -> dict:
        producer = self.producer
        inflight = len(producer)
        INFLIGHT.set(inflight)
        if inflight > self.s.ingest_max_inflight:
            THROTTLED.labels(oem).inc()
            raise HTTPException(429, "back-pressure: producer queue full", headers={"Retry-After": "1"})
        try:
            payloads = body.decode().splitlines() if oem == "kestrel" else orjson.loads(body)
        except (orjson.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(400, "undecodable body") from exc
        if not isinstance(payloads, list):
            raise HTTPException(400, "expected a JSON array")
        ok, bad = normalise(oem, [p for p in payloads if p])
        now = datetime.now(UTC)
        accepted = dup = 0
        rejected = len(bad)
        topic, dlq = self.s.topic_raw, self.s.topic_dlq
        batch = _Batch(asyncio.get_running_loop())
        batch.pending = 1  # guard: resolved only after the loop below finishes
        cb = batch.done
        keys: list[str] = []
        batch_keys: set[str] = set()
        try:
            for raw, reason in bad:
                batch.pending += 1
                producer.produce(dlq, value=orjson.dumps({"oem": oem, "reason": reason, "payload": raw, "at": now}),
                                 on_delivery=cb)
            for e in ok:
                reason = validate(e, now)
                if reason is not None:
                    rejected += 1
                    batch.pending += 1
                    producer.produce(dlq, value=orjson.dumps({"oem": oem, "reason": reason, "event": e, "at": now}),
                                     on_delivery=cb)
                    continue
                key = f"{e['vin']}:{e['seq']}"
                if key in self.bloom or key in batch_keys:
                    dup += 1
                    continue
                e["v"] = SCHEMA_VERSION
                batch.pending += 1
                producer.produce(topic, key=e["vin"], value=orjson.dumps(e), on_delivery=cb)
                keys.append(key)
                batch_keys.add(key)
                accepted += 1
        except BufferError as exc:
            THROTTLED.labels(oem).inc()
            raise HTTPException(429, "back-pressure", headers={"Retry-After": "1"}) from exc
        except KafkaException as exc:
            self._fatal = True
            raise HTTPException(503, "event log unavailable", headers={"Retry-After": "2"}) from exc
        batch.done(None)  # release the guard
        try:
            failed = await asyncio.wait_for(asyncio.shield(batch.fut), self.ACK_TIMEOUT_S)
        except TimeoutError as exc:
            UNACKED.labels(oem).inc()
            raise HTTPException(503, "broker did not acknowledge in time", headers={"Retry-After": "2"}) from exc
        if failed:
            UNACKED.labels(oem).inc()
            raise HTTPException(503, f"{failed} records not acknowledged by the broker", headers={"Retry-After": "2"})
        for k in keys:   # only now: a failed batch must stay retryable, not be 'seen'
            self.bloom.seen(k)
        EV.labels(oem, "accepted").inc(accepted)
        EV.labels(oem, "rejected").inc(rejected)
        EV.labels(oem, "duplicate").inc(dup)
        return {"accepted": accepted, "rejected": rejected, "duplicates": dup}


def create_app(gateway: Gateway | None = None) -> FastAPI:
    state: dict = {"gw": gateway}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if state["gw"] is not None:
            state["gw"].close(10)   # drain in-flight messages on shutdown (no loss on rollout)

    app = FastAPI(title="VoltPath ingest", version="1.0.0", docs_url="/docs", lifespan=lifespan)

    def gw() -> Gateway:
        if state["gw"] is None:
            state["gw"] = Gateway()
        return state["gw"]

    @app.post("/v1/oem/{oem}/telemetry", status_code=202)
    async def telemetry(oem: str, request: Request):
        t0 = time.perf_counter()
        if oem not in ADAPTERS:
            raise HTTPException(404, f"unknown OEM {oem}")
        body = await request.body()
        g = gw()
        g.verify(oem, request.headers.get("x-timestamp"), request.headers.get("x-signature"), body)
        if request.headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        result = await g.handle(oem, body)
        REQ.labels(oem).observe(time.perf_counter() - t0)
        return result

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        try:
            gw().producer.list_topics(timeout=2)
        except Exception as exc:  # noqa: BLE001
            return Response(status_code=503, content=str(exc))
        return {"status": "ready"}

    @app.get("/metrics")
    def metrics():
        return metrics_response()

    return app
