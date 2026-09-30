"""End-to-end stream: signed OEM webhook -> ingest gateway -> Redpanda -> processor -> Redis + Timescale."""

import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime, timedelta

import orjson
import psycopg
import pytest
import redis
from confluent_kafka import Consumer
from fastapi.testclient import TestClient

from voltcore.oem import ENCODERS

pytestmark = pytest.mark.integration


def signed(secret: str, body: bytes) -> dict:
    ts = str(int(time.time()))
    return {"X-Timestamp": ts, "X-Signature": hmac.new(secret.encode(), ts.encode() + b"." + body,
                                                       hashlib.sha256).hexdigest()}


def event(vin, seq, ts, **kw):
    d = {"vin": vin, "ts": ts, "seq": seq, "lat": 12.97, "lon": 77.59, "speed_kmh": 30.0, "soc_pct": 60.0,
         "soh_pct": 95.0, "batt_temp_c": 31.0, "odo_km": 1000.0 + seq, "power_kw": -6.0, "evt": "PERIODIC",
         "dtc": [], "charger_id": None}
    d.update(kw)
    return d


def consume_all(stack, n_expected: int, timeout: float = 30):
    c = Consumer({"bootstrap.servers": stack["KAFKA_BOOTSTRAP"], "group.id": f"it-{uuid.uuid4()}",
                  "auto.offset.reset": "earliest", "enable.auto.commit": False})
    c.subscribe([stack["TOPIC_RAW"]])
    out, deadline = [], time.time() + timeout
    while len(out) < n_expected and time.time() < deadline:
        out += [m for m in c.consume(500, timeout=1) if not m.error()]
    c.close()
    return out


def test_webhook_to_state_and_warm_store(stack, world):
    from voltpath.ingest.app import Gateway, create_app
    from voltpath.processor.__main__ import Processor

    gw = Gateway()
    client = TestClient(create_app(gw))
    vin = world.vin[10]
    tenant = int(world.tenant_id[10])
    now = datetime.now(UTC)
    evs = [event(vin, s, now - timedelta(seconds=40 * (6 - s))) for s in range(1, 6)]
    # duplicate + out-of-order + overheating event
    evs += [evs[2], event(vin, 6, now, batt_temp_c=58.0)]
    body = orjson.dumps([ENCODERS["voltra"](e) for e in reversed(evs)])
    r = client.post("/v1/oem/voltra/telemetry", content=body, headers=signed(gw.s.oem_secret_map["voltra"], body))
    assert r.status_code == 202, r.text
    assert r.json() == {"accepted": 6, "rejected": 0, "duplicates": 1}   # 202 means broker-acknowledged

    msgs = consume_all(stack, 6)
    assert len(msgs) == 6 and {m.key().decode() for m in msgs} == {vin}

    p = Processor()
    p.process(msgs)
    p.process(msgs)   # replay after a "crash": must be idempotent

    rd = redis.Redis.from_url(stack["REDIS_URL"])
    st = orjson.loads(rd.hget(f"vs:{tenant}", vin))
    assert st["max_seq"] == 6 and st["temp"] == 58.0
    with psycopg.connect(stack["DATABASE_URL"]) as c:
        rows = c.execute("SELECT count(*) FROM telemetry WHERE vin = %s", (vin,)).fetchone()[0]
        alerts = c.execute("SELECT type_code FROM alert WHERE vin = %s", (vin,)).fetchall()
    assert rows == 6                                         # 5 old (30 s apart -> all kept) + latest, no dups
    assert [a[0] for a in alerts] == ["BATTERY_OVERTEMP"]    # cooldown: replay did not re-alert
    assert rd.exists(f"cd:BATTERY_OVERTEMP:{vin}")


def test_invalid_records_reach_dlq(stack, world):
    from voltpath.ingest.app import Gateway, create_app

    gw = Gateway()
    client = TestClient(create_app(gw))
    bad = event(world.vin[11], 1, datetime.now(UTC), soc_pct=150.0)
    body = ("\n".join([ENCODERS["kestrel"](bad), "not|a|kestrel|line"])).encode()
    r = client.post("/v1/oem/kestrel/telemetry", content=body, headers=signed(gw.s.oem_secret_map["kestrel"], body))
    assert r.json() == {"accepted": 0, "rejected": 2, "duplicates": 0}
    c = Consumer({"bootstrap.servers": stack["KAFKA_BOOTSTRAP"], "group.id": f"it-{uuid.uuid4()}",
                  "auto.offset.reset": "earliest"})
    c.subscribe([stack["TOPIC_DLQ"]])
    reasons, deadline = [], time.time() + 20
    while len(reasons) < 2 and time.time() < deadline:
        reasons += [orjson.loads(m.value())["reason"] for m in c.consume(10, timeout=1) if not m.error()]
    c.close()
    assert any(r.startswith("KestrelParseError") for r in reasons) and "soc_soh" in reasons


def test_trip_and_charging_session_facts(stack, world):
    from voltpath.processor.__main__ import Processor

    class Msg:
        def __init__(self, e):
            self._v = orjson.dumps(e)

        def value(self):
            return self._v

        def partition(self):
            return 0

        def error(self):
            return None

    vin = world.vin[20]
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    seq = iter(range(1, 100))
    evs = [event(vin, next(seq), t0, evt="TRIP_START", soc_pct=40.0, odo_km=500.0),
           event(vin, next(seq), t0 + timedelta(minutes=10), evt="TRIP_END", soc_pct=30.0, odo_km=512.0),
           event(vin, next(seq), t0 + timedelta(minutes=11), evt="CHARGE_START", soc_pct=30.0, charger_id=5,
                 power_kw=7.0),
           event(vin, next(seq), t0 + timedelta(minutes=25), evt="CHARGE_STOP", soc_pct=70.0, charger_id=5)]
    for e in evs:
        e["ts"] = e["ts"].isoformat()
    p = Processor()
    p.process([Msg(e) for e in evs])
    with psycopg.connect(stack["DATABASE_URL"]) as c:
        trip = c.execute("SELECT distance_km, start_soc, end_soc FROM trip WHERE vin = %s", (vin,)).fetchone()
        sess = c.execute("SELECT charger_id, energy_kwh, cost FROM charging_session WHERE vin = %s", (vin,)).fetchone()
    assert float(trip[0]) == pytest.approx(12.0) and float(trip[1]) == 40.0
    assert sess[0] == 5 and float(sess[1]) > 0 and float(sess[2]) > 0
