import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from voltcore.dtc import describe, parse_dtcs
from voltcore.vin import check_digit, is_valid_vin, make_vin, random_vin


@pytest.mark.parametrize("vin", ["1HGCM82633A004352", "1M8GDM9AXKP042788", "11111111111111111"])
def test_known_valid_vins(vin):
    assert is_valid_vin(vin)


@pytest.mark.parametrize("vin", [
    "1HGCM82633A004353",   # wrong check digit
    "1HGCM82633A00435",    # 16 chars
    "1HGCM82633A0043521",  # 18 chars
    "IHGCM82633A004352",   # contains I
    "1HGCM82O33A004352",   # contains O
    "1hgcm82633a004352",   # lower case
    "",
])
def test_invalid_vins(vin):
    assert not is_valid_vin(vin)


def test_non_string_is_invalid():
    assert not is_valid_vin(None)  # type: ignore[arg-type]


def test_check_digit_x():
    # Construct VINs until one needs 'X' and make sure it validates.
    rng = random.Random(7)
    for _ in range(500):
        v = random_vin(rng)
        if v[8] == "X":
            assert is_valid_vin(v)
            return
    pytest.fail("no X check digit generated")


@given(st.integers(min_value=0, max_value=999_999))
def test_make_vin_always_valid(serial):
    assert is_valid_vin(make_vin("9VX", "AB12C", "S", "A", serial))


def test_make_vin_rejects_bad_parts():
    with pytest.raises(ValueError):
        make_vin("9VQ", "AB12C", "S", "A", 1)


def test_check_digit_matches_position_9():
    v = "1HGCM82633A004352"
    assert check_digit(v) == v[8]


def test_parse_dtcs_formats():
    assert parse_dtcs(["P0A7E", "p0301"]) == ["P0A7E", "P0301"]
    assert parse_dtcs("P0A7E;U0100,C0035") == ["P0A7E", "U0100", "C0035"]
    assert parse_dtcs("fault P0A7E then P0A7E again") == ["P0A7E"]
    assert parse_dtcs(None) == []
    assert parse_dtcs("X1234 P9999 P0A7") == []  # bad letter, bad 2nd digit, too short


def test_describe():
    d = describe("P0A7E")
    assert d.system == "powertrain" and d.severity == "critical" and d.generic
    assert describe("P1E00").description.startswith("Unknown")
    assert not describe("P1E00").generic
    with pytest.raises(ValueError):
        describe("Z0000")
