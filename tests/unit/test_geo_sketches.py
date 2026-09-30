import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from voltcore import geo
from voltcore.sketches import BloomFilter, CountMinSketch, RotatingBloom


def test_geohash_known_value():
    assert geo.encode(57.64911, 10.40744, 11) == "u4pruydqqvj"


@given(st.floats(-89.9, 89.9), st.floats(-179.9, 179.9), st.integers(1, 9))
def test_geohash_roundtrip_within_cell(lat, lon, p):
    gh = geo.encode(lat, lon, p)
    la0, la1, lo0, lo1 = geo.bounds(gh)
    assert la0 <= lat <= la1 and lo0 <= lon <= lo1


def test_neighbours_are_adjacent_and_distinct():
    gh = geo.encode(12.97, 77.59, 6)
    nb = geo.neighbours(gh)
    assert len(nb) == 8 and gh not in nb and len(set(nb)) == 8
    c = geo.decode(gh)
    for n in nb:
        assert geo.haversine_km(*c, *geo.decode(n)) < 2.0


def test_haversine():
    assert geo.haversine_km(12.9716, 77.5946, 13.0827, 80.2707) == pytest.approx(290, rel=0.02)
    assert geo.haversine_km(1, 1, 1, 1) == 0


def test_mask_location_is_coarse():
    lat, lon = geo.mask_location(12.97161, 77.59463)
    assert geo.haversine_km(12.97161, 77.59463, lat, lon) < 4
    assert geo.mask_location(12.9717, 77.5947) == (lat, lon)  # nearby points collapse


def test_bloom_no_false_negatives_and_low_fp():
    bf = BloomFilter(10_000, 0.01)
    keys = [f"vin{i}" for i in range(10_000)]
    for k in keys:
        bf.add(k)
    assert all(k in bf for k in keys)
    fp = sum(f"other{i}" in bf for i in range(10_000)) / 10_000
    assert fp < 0.03


def test_bloom_add_reports_duplicate():
    bf = BloomFilter(100)
    assert bf.add("a") is False
    assert bf.add("a") is True
    assert bf.add(b"bytes") is False


def test_rotating_bloom_bounded_and_remembers_previous():
    rb = RotatingBloom(capacity=100)
    for i in range(150):
        rb.seen(f"k{i}")
    assert rb.previous is not None
    assert rb.seen("k10") is True   # still remembered via previous generation
    assert rb.seen("new-key") is False


def test_count_min_never_undercounts_and_finds_heavy_hitters():
    cms = CountMinSketch(eps=0.001, delta=0.01, top_k=5)
    rng = random.Random(1)
    truth: dict[str, int] = {}
    heavy = [f"hot{i}" for i in range(5)]
    for _ in range(20_000):
        k = rng.choice(heavy) if rng.random() < 0.5 else f"cold{rng.randint(0, 5000)}"
        truth[k] = truth.get(k, 0) + 1
        cms.add(k)
    for k, c in truth.items():
        assert cms.estimate(k) >= c
        assert cms.estimate(k) <= c + 0.001 * cms.total * 3
    assert {k for k, _ in cms.topk()} == set(heavy)


def test_count_min_merge():
    a, b = CountMinSketch(top_k=3), CountMinSketch(top_k=3)
    for _ in range(10):
        a.add("x")
        b.add("x")
    b.add("y", 3)
    a.merge(b)
    assert a.estimate("x") >= 20 and a.estimate("y") >= 3
    with pytest.raises(ValueError):
        a.merge(CountMinSketch(eps=0.1))
