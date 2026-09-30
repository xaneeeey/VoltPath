import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta

import orjson
import pytest
from fastapi.testclient import TestClient

from voltcore.oem import ENCODERS
from voltcore.schema import TelemetryEvent
from voltpath.ingest.app import Gateway, create_app, validate

VIN = "1HGCM82633A004352"


class FakeProducer:
    def __init__(self, fail_after: int | None = None):
        self.msgs: list[tuple[str, bytes | None, bytes]] = []
        self.fail_after = fail_after
        self.queue_len = 0

    def produce(self, topic, key=None, value=None, on_delivery=None):
        if self.fail_after is not None and len(self.msgs) >= self.fail_after:
            raise BufferError("queue full")
        self.msgs.append((topic, key, value))

    def poll(self, _t):
        return 0

    def __len__(self):
        return self.queue_len

    def list_topics(self, timeout=None):
        return {}

    def flush(self, _t=None):
        return 0


def canon(seq=1, **kw):
    d = {"vin": VIN, "ts": datetime.now(UTC), "oem": "voltra", "seq": seq, "lat": 12.97, "lon": 77.59,
         "speed_kmh": 40.0, "soc_pct": 55.0, "soh_pct": 96.0, "batt_temp_c": 31.0, "odo_km": 1200.0,
         "power_kw": -8.0, "evt": "PERIODIC", "dtc": [], "charger_id": None}
    d.update(kw)
    return d


def sign(secret: str, body: bytes, ts: str | None = None):
    ts = ts or str(int(time.time()))
    return {"X-Timestamp": ts, "X-Signature": hmac.new(secret.encode(), ts.encode() + b"." + body,
                                                       hashlib.sha256).hexdigest()}


@pytest.fixture
def client():
    prod = FakeProducer()
    gw = Gateway(producer=prod)
    return TestClient(create_app(gw)), gw, prod


def test_validate_agrees_with_pydantic_schema():
    now = datetime.now(UTC)
    cases = [canon(), canon(vin="1HGCM82633A004353"), canon(soc_pct=101), canon(lat=95), canon(speed_kmh=400),
             canon(evt="NOPE"), canon(seq=-1), canon(ts=now + timedelta(hours=1)), canon(batt_temp_c=-50)]
    for c in cases:
        fast_ok = validate(c, now) is None
        try:
            TelemetryEvent(**c)
            slow_ok = c["ts"] <= now + timedelta(minutes=5)
        except Exception:  # noqa: BLE001
            slow_ok = False
        assert fast_ok == slow_ok, c


def test_accepts_signed_batch_all_oems(client):
    c, gw, prod = client
    for oem in ("voltra", "nimbus", "kestrel"):
        items = [ENCODERS[oem](canon(seq=i)) for i in range(1, 4)]
        body = "\n".join(items).encode() if oem == "kestrel" else orjson.dumps(items)
        r = c.post(f"/v1/oem/{oem}/telemetry", content=body, headers=sign(gw.s.oem_secret_map[oem], body))
        assert r.status_code == 202, r.text
        assert r.json()["accepted"] == 3 or r.json()["duplicates"] == 3
    keys = {k for t, k, _ in prod.msgs if t == "telemetry.raw"}
    assert keys == {VIN}
    msg = orjson.loads(prod.msgs[0][2])
    assert msg["v"] == 1 and msg["oem"] == "voltra"


def test_rejects_bad_signature_and_replay(client):
    c, gw, _ = client
    body = orjson.dumps([ENCODERS["voltra"](canon())])
    assert c.post("/v1/oem/voltra/telemetry", content=body, headers=sign("wrong", body)).status_code == 401
    old = str(int(time.time()) - 3600)
    assert c.post("/v1/oem/voltra/telemetry", content=body,
                  headers=sign(gw.s.oem_secret_map["voltra"], body, old)).status_code == 401
    assert c.post("/v1/oem/voltra/telemetry", content=body).status_code == 401
    assert c.post("/v1/oem/tesla/telemetry", content=body).status_code == 404


def test_invalid_records_go_to_dlq_and_duplicates_dropped(client):
    c, gw, prod = client
    items = [ENCODERS["voltra"](canon(seq=1)), ENCODERS["voltra"](canon(seq=1)),
             ENCODERS["voltra"](canon(seq=2, soc_pct=140)), {"garbage": True}]
    body = orjson.dumps(items)
    r = c.post("/v1/oem/voltra/telemetry", content=body, headers=sign(gw.s.oem_secret_map["voltra"], body))
    assert r.json() == {"accepted": 1, "rejected": 2, "duplicates": 1}
    dlq = [orjson.loads(v) for t, _, v in prod.msgs if t == "telemetry.dlq"]
    assert {d["reason"].split(":")[0] for d in dlq} == {"soc_soh", "KeyError"}


def test_back_pressure_returns_429(client):
    c, gw, prod = client
    prod.queue_len = gw.s.ingest_max_inflight + 1
    body = orjson.dumps([ENCODERS["voltra"](canon())])
    r = c.post("/v1/oem/voltra/telemetry", content=body, headers=sign(gw.s.oem_secret_map["voltra"], body))
    assert r.status_code == 429 and r.headers["retry-after"] == "1"


def test_buffer_error_mid_batch_is_429():
    prod = FakeProducer(fail_after=1)
    gw = Gateway(producer=prod)
    c = TestClient(create_app(gw))
    body = orjson.dumps([ENCODERS["voltra"](canon(seq=i)) for i in range(1, 4)])
    r = c.post("/v1/oem/voltra/telemetry", content=body, headers=sign(gw.s.oem_secret_map["voltra"], body))
    assert r.status_code == 429


def test_bad_body(client):
    c, gw, _ = client
    body = b"{not json"
    assert c.post("/v1/oem/voltra/telemetry", content=body,
                  headers=sign(gw.s.oem_secret_map["voltra"], body)).status_code == 400
    body = orjson.dumps({"not": "a list"})
    assert c.post("/v1/oem/voltra/telemetry", content=body,
                  headers=sign(gw.s.oem_secret_map["voltra"], body)).status_code == 400


def test_health_and_metrics(client):
    c, _, _ = client
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").status_code == 200
    assert b"ingest_events_total" in c.get("/metrics").content
