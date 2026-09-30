"""Consumer-driven contract tests (no infrastructure needed).

telemetry.raw v1 : ingest (provider) -> processor / lake writer (consumers). The JSON schema in
                   contracts/ is the agreement; changing the pydantic model without regenerating
                   and reviewing the contract fails CI.
web-api v1       : the React UI (consumer) -> API (provider). Every path/method the UI calls must
                   exist in the provider's OpenAPI document.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from voltcore.oem import ENCODERS
from voltcore.schema import TelemetryEvent

ROOT = Path(__file__).resolve().parents[2]
EVENT_CONTRACT = ROOT / "contracts" / "telemetry.raw.v1.schema.json"
WEB_CONTRACT = ROOT / "contracts" / "web-api.v1.json"
VIN = "1HGCM82633A004352"


def sample(**kw):
    d = {"vin": VIN, "ts": datetime.now(UTC), "oem": "voltra", "seq": 7, "lat": 12.97, "lon": 77.59,
         "speed_kmh": 30.0, "soc_pct": 55.0, "soh_pct": 95.0, "batt_temp_c": 31.0, "odo_km": 100.0,
         "power_kw": -3.0, "evt": "CHARGE_START", "dtc": ["P0A7E"], "charger_id": 12}
    d.update(kw)
    return d


def test_event_contract_matches_provider_model():
    stored = json.loads(EVENT_CONTRACT.read_text())
    current = TelemetryEvent.model_json_schema()
    stored.pop("$id", None)
    assert stored == json.loads(json.dumps(current, sort_keys=True)), \
        "TelemetryEvent changed: bump the schema version and regenerate contracts/telemetry.raw.v1.schema.json"


@pytest.mark.parametrize("oem", ["voltra", "nimbus", "kestrel"])
def test_provider_messages_satisfy_contract(oem):
    """What the ingest gateway puts on the topic validates against the contract model."""
    import sys

    sys.path.insert(0, str(ROOT / "tests" / "unit"))
    from test_ingest import FakeProducer  # noqa: E402

    from voltpath.ingest.app import Gateway

    prod = FakeProducer()
    gw = Gateway(producer=prod)
    wire = ENCODERS[oem](sample())
    body = wire.encode() if oem == "kestrel" else orjson.dumps([wire])
    import asyncio

    asyncio.run(gw.handle(oem, body))
    msgs = [orjson.loads(v) for t, _, v in prod.msgs if t == gw.s.topic_raw]
    assert len(msgs) == 1
    TelemetryEvent(**msgs[0])            # strict model: extra="forbid", VIN check digit, bounds
    assert msgs[0]["v"] == 1


def test_consumer_accepts_contract_messages():
    """The processor's state machine only relies on contract fields."""
    from voltpath.processor.state import VehicleState

    msg = orjson.loads(TelemetryEvent(**sample()).to_wire())
    st = VehicleState(VIN, 1)
    st.apply(msg, 1.0)
    assert st.status == "CHARGING" and st.charger_id == 12 and st.dtc == ["P0A7E"]


def test_consumer_tolerates_unknown_future_fields():
    from voltpath.processor.state import VehicleState

    msg = orjson.loads(TelemetryEvent(**sample()).to_wire()) | {"new_field_v2": 1}
    VehicleState(VIN, 1).apply(msg, 1.0)


def test_web_api_contract():
    from voltpath.api.app import create_app

    spec = create_app().openapi()
    contract = json.loads(WEB_CONTRACT.read_text())
    missing = [f"{i['method'].upper()} {i['path']}" for i in contract["interactions"]
               if i["method"] not in spec["paths"].get(i["path"], {})]
    assert not missing, f"provider broke the web contract: {missing}"


def test_live_state_contract_fields():
    from voltpath.api.services import PUBLIC_FIELDS
    from voltpath.processor.state import VehicleState

    contract = json.loads(WEB_CONTRACT.read_text())["live_state_fields"]
    assert list(PUBLIC_FIELDS) == contract
    assert set(contract) <= set(VehicleState.__dataclass_fields__) | {"speed", "soc", "soh", "temp", "power", "odo"}
