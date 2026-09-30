"""Ingest gateway: OEM cloud webhooks -> canonical events -> Kafka.

Pipeline per batch (1-1000 records):
  1. authenticate  HMAC-SHA256(secret, ts + "." + body), 5-minute replay window, constant-time compare
  2. decode        JSON array (voltra/nimbus) or text lines (kestrel), optional gzip
  3. normalise     OEM adapter -> canonical dict (units, field names, DTC parsing)
  4. validate      schema v1 bounds + VIN check digit + clock-skew guard; failures -> DLQ with reason
  5. dedup         per-worker rotating Bloom filter on (vin, seq) drops exact retransmits early;
                   exact, cross-replica dedup happens in the processor (partition owner)
  6. produce       idempotent producer, key = VIN (per-vehicle ordering), lz4, acks=all
Back-pressure: if the producer's in-flight queue exceeds INGEST_MAX_INFLIGHT the request gets
429 + Retry-After and nothing is enqueued, so the OEM re-sends later (no silent drops).
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import orjson
from confluent_kafka import Producer
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


class Gateway:
    def __init__(self, producer: Producer | None = None):
        s = settings()
        self.s = s
        self.secrets = {k: v.encode() for k, v in s.oem_secret_map.items()}
        self.producer = producer if producer is not None else Producer({
            "bootstrap.servers": s.kafka_bootstrap,
            "enable.idempotence": True,
            "acks": "all",
            "linger.ms": 20,
            "batch.num.messages": 10_000,
            "compression.type": "lz4",
            "queue.buffering.max.messages": max(s.ingest_max_inflight * 2, 100_000),
            "queue.buffering.max.kbytes": 1_048_576,
        })
        self.bloom = RotatingBloom(capacity=2_000_000, fp_rate=1e-4)
        self.delivery_errors = 0

    def _on_delivery(self, err, _msg) -> None:
        if err is not None:
            self.delivery_errors += 1
            log.error("delivery failed: %s", err)

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

    def handle(self, oem: str, body: bytes) -> dict:
        self.producer.poll(0)
        inflight = len(self.producer)
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
        produce = self.producer.produce
        for raw, reason in bad:
            produce(dlq, value=orjson.dumps({"oem": oem, "reason": reason, "payload": raw, "at": now}))
        for e in ok:
            reason = validate(e, now)
            if reason is not None:
                rejected += 1
                produce(dlq, key=None, value=orjson.dumps({"oem": oem, "reason": reason, "event": e, "at": now}))
                continue
            if self.bloom.seen(f"{e['vin']}:{e['seq']}"):
                dup += 1
                continue
            e["v"] = SCHEMA_VERSION
            try:
                produce(topic, key=e["vin"], value=orjson.dumps(e), on_delivery=self._on_delivery)
            except BufferError as exc:
                # Local queue full mid-batch: what we enqueued is fine; ask OEM to retry the batch.
                # Retried records are dropped as duplicates by the Bloom filter / processor.
                THROTTLED.labels(oem).inc()
                raise HTTPException(429, "back-pressure", headers={"Retry-After": "1"}) from exc
            accepted += 1
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
            state["gw"].producer.flush(10)   # drain in-flight messages on shutdown (no loss on rollout)

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
        result = g.handle(oem, body)
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
