"""Algorithm micro-benchmarks -> docs/bench.json (cited by docs/algorithms.md).

    uv run python tools/bench_algorithms.py
"""

from __future__ import annotations

import json
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voltcore.charging_dp import TariffWindow, plan_charging, slot_prices  # noqa: E402
from voltcore.geo import encode  # noqa: E402
from voltcore.graph import RoadGraph  # noqa: E402
from voltcore.segment import viterbi_states  # noqa: E402
from voltcore.sketches import BloomFilter, CountMinSketch  # noqa: E402
from voltcore.vin import is_valid_vin, random_vin  # noqa: E402


def timeit(fn, n):
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return (time.perf_counter() - t0) / n


def main():
    out = {}
    g = RoadGraph.load(ROOT / "data" / "bangalore_graph.npz")
    rng = random.Random(7)
    pairs = []
    while len(pairs) < 60:
        s, t = rng.randrange(g.n), rng.randrange(g.n)
        km = g.h_m(s, t) / 1000
        if 3 < km < 15:
            pairs.append((s, t))
    a_exp, d_exp, a_ms, d_ms = [], [], [], []
    for s, t in pairs:
        t0 = time.perf_counter()
        a = g.astar(s, t)
        a_ms.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        d = g.dijkstra(s, t)
        d_ms.append((time.perf_counter() - t0) * 1000)
        assert abs(a.dist_m - d.dist_m) < 1e-3 * d.dist_m
        a_exp.append(a.stats.expanded)
        d_exp.append(d.stats.expanded)
    stations = {n: i for i, n in enumerate(rng.sample(range(g.n), 1200))}
    mt = [timeit(lambda s=s: g.nearest_targets(s, stations, k=3, cutoff_m=40_000), 1) * 1000 for s, _ in pairs]
    out["routing"] = {
        "graph": {"nodes": g.n, "edges": int(len(g.indices) // 2)},
        "pairs": len(pairs),
        "astar_median_expanded": int(statistics.median(a_exp)),
        "dijkstra_median_expanded": int(statistics.median(d_exp)),
        "expansion_ratio": round(statistics.median(d) / statistics.median(a), 1) if (d := d_exp) and (a := a_exp) else None,
        "astar_median_ms": round(statistics.median(a_ms), 2),
        "dijkstra_median_ms": round(statistics.median(d_ms), 2),
        "multi_target_k3_median_ms": round(statistics.median(mt), 2),
    }
    windows = [TariffWindow(22 * 60, 6 * 60, 6.5), TariffWindow(6 * 60, 10 * 60, 10.5), TariffWindow(18 * 60, 22 * 60, 14.2)]
    start = datetime(2026, 9, 30, 18, 0)
    prices = slot_prices(windows, start, 52, 10.5)
    res = []
    for kw in (7.4, 11, 22, 60):
        t0 = time.perf_counter()
        p = plan_charging(capacity_kwh=72, soc_pct=20, target_soc_pct=90, charger_kw=kw, prices=prices, start=start)
        res.append({"charger_kw": kw, "ms": round((time.perf_counter() - t0) * 1000, 1), "cost": p.cost,
                    "charge_now_cost": p.baseline_cost, "saving_pct": round(p.savings / p.baseline_cost * 100, 1)})
    out["charging_dp"] = res
    bf = BloomFilter(2_000_000, 1e-3)
    keys = [f"VIN{i}:{i}" for i in range(200_000)]
    t = timeit(lambda: [bf.add(k) for k in keys[:20_000]], 1) / 20_000
    fp = sum(f"other{i}" in bf for i in range(100_000)) / 100_000
    out["bloom"] = {"bits_mb": round(bf.m / 8 / 1e6, 2), "k": bf.k, "add_us": round(t * 1e6, 2),
                    "measured_fp_rate_at_1pct_load": fp}
    cms = CountMinSketch(eps=0.002, delta=0.01, top_k=15)
    zipf = np.random.default_rng(1).zipf(1.3, 200_000) % 1200
    t0 = time.perf_counter()
    for z in zipf:
        cms.add(str(z))
    true = np.bincount(zipf, minlength=1200)
    top_true = set(np.argsort(-true)[:10].astype(str))
    top_est = {k for k, _ in cms.topk()[:10]}
    out["count_min"] = {"width": cms.w, "depth": cms.d, "add_us": round((time.perf_counter() - t0) / len(zipf) * 1e6, 2),
                        "top10_recall": len(top_true & top_est) / 10}
    vins = [random_vin(rng) for _ in range(20_000)]
    is_valid_vin.cache_clear()
    cold = timeit(lambda: [is_valid_vin(v) for v in vins], 1) / len(vins)
    warm = timeit(lambda: [is_valid_vin(v) for v in vins], 1) / len(vins)
    out["vin"] = {"cold_us": round(cold * 1e6, 2), "cached_us": round(warm * 1e6, 3)}
    out["geohash_encode_us"] = round(timeit(lambda: encode(12.9716, 77.5946, 7), 20_000) * 1e6, 2)
    speeds = list(np.clip(np.random.default_rng(2).normal(30, 15, 8640), 0, 120))
    out["viterbi_8640_points_ms"] = round(timeit(lambda: viterbi_states(speeds), 5) * 1000, 1)
    (ROOT / "docs" / "bench.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
