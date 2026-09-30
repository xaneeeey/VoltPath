from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from voltcore.oem import ENCODERS, from_kestrel, normalise
from voltcore.schema import TelemetryEvent, parse_ts

CANON = {
    "vin": "1HGCM82633A004352", "ts": datetime(2026, 9, 25, 10, 15, 2, 120000, tzinfo=UTC), "oem": "x",
    "seq": 88412, "lat": 12.9702, "lon": 77.5911, "speed_kmh": 64.2, "soc_pct": 41.0, "soh_pct": 93.5,
    "batt_temp_c": 31.5, "odo_km": 18234.7, "power_kw": -12.5, "evt": "HARSH_BRAKE", "dtc": ["P0A7E"],
    "charger_id": None,
}


@pytest.mark.parametrize("oem", ["voltra", "nimbus", "kestrel"])
def test_roundtrip_every_oem(oem):
    wire = ENCODERS[oem](CANON)
    ok, bad = normalise(oem, [wire])
    assert not bad
    got = ok[0]
    assert got["oem"] == oem
    for k in ("lat", "lon", "speed_kmh", "soc_pct", "soh_pct", "batt_temp_c", "odo_km"):
        assert got[k] == pytest.approx(CANON[k], abs=0.1), k
    assert got["dtc"] == ["P0A7E"] and got["seq"] == 88412 and got["evt"] == "HARSH_BRAKE"
    assert abs((got["ts"] - CANON["ts"]).total_seconds()) < 0.01
    TelemetryEvent(**got)  # canonical contract holds


def test_charger_id_roundtrip():
    e = dict(CANON, charger_id=42)
    for oem in ("voltra", "nimbus", "kestrel"):
        ok, _ = normalise(oem, [ENCODERS[oem](e)])
        assert ok[0]["charger_id"] == 42


def test_normalise_collects_bad_records():
    ok, bad = normalise("voltra", [{"vin": "X"}, ENCODERS["voltra"](CANON)])
    assert len(ok) == 1 and len(bad) == 1 and "KeyError" in bad[0][1]
    ok, bad = normalise("kestrel", ["garbage|line"])
    assert not ok and "malformed" in bad[0][1]
    with pytest.raises(KeyError):
        normalise("unknown", [])


def test_kestrel_strict_regex():
    line = ENCODERS["kestrel"](CANON)
    assert from_kestrel(line + "\n")["vin"] == CANON["vin"]
    with pytest.raises(ValueError):
        from_kestrel(line.replace("1HGCM82633A004352", "IHGCM82633A004352"))


def test_schema_rejects_bad_values():
    with pytest.raises(ValidationError):
        TelemetryEvent(**dict(CANON, vin="1HGCM82633A004353"))
    with pytest.raises(ValidationError):
        TelemetryEvent(**dict(CANON, soc_pct=140))
    with pytest.raises(ValidationError):
        TelemetryEvent(**dict(CANON, extra_field=1))


def test_schema_wire_and_naive_ts():
    e = TelemetryEvent(**dict(CANON, ts=datetime(2026, 1, 1, 0, 0)))
    assert e.ts.tzinfo is not None
    assert b'"vin":"1HGCM82633A004352"' in e.to_wire()


def test_parse_ts_variants():
    a = parse_ts("2026-09-25T10:15:02.120Z")
    assert parse_ts(int(a.timestamp() * 1000)) == a
    assert parse_ts(a.timestamp()) == a
