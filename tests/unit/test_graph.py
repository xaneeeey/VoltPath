import math
import random
from pathlib import Path

import pytest

from voltcore.graph import RoadGraph, connected_components, proximity_clusters

GRAPH = Path(__file__).resolve().parents[2] / "data" / "bangalore_graph.npz"


@pytest.fixture(scope="module")
def grid():
    """5x5 grid, ~100 m spacing (edges 1 % longer than straight line, like real roads), plus a slow diagonal shortcut that must NOT be used."""
    lat, lon, edges = [], [], []
    for i in range(5):
        for j in range(5):
            lat.append(12.9 + i * 0.0009)
            lon.append(77.5 + j * 0.00092)
    idx = lambda i, j: i * 5 + j  # noqa: E731
    for i in range(5):
        for j in range(5):
            if j < 4:
                edges.append((idx(i, j), idx(i, j + 1), 100.0 * 1.01))
            if i < 4:
                edges.append((idx(i, j), idx(i + 1, j), 100.0 * 1.01))
    edges.append((idx(0, 0), idx(4, 4), 5000.0))
    return RoadGraph.from_edges(lat, lon, edges)


def test_dijkstra_and_astar_agree_on_grid(grid):
    d = grid.dijkstra(0, 24)
    a = grid.astar(0, 24)
    assert d.dist_m == pytest.approx(808) and a.dist_m == pytest.approx(808)
    assert a.nodes[0] == 0 and a.nodes[-1] == 24 and len(a.nodes) == 9
    assert a.stats.expanded <= d.stats.expanded


def test_same_node_route(grid):
    assert grid.astar(3, 3).dist_m == 0
    assert grid.dijkstra(3, 3).nodes == [3]


def test_unreachable_returns_none():
    g = RoadGraph.from_edges([0.0, 0.0, 1.0], [0.0, 0.1, 1.0], [(0, 1, 10.0)])
    assert g.astar(0, 2) is None and g.dijkstra(0, 2) is None


def test_nearest_targets_orders_by_road_distance(grid):
    found = grid.nearest_targets(0, {24: "far", 2: "near", 10: "mid"}, k=2)
    assert [p for p, _ in found] == ["near", "mid"]
    assert found[0][1].dist_m == pytest.approx(202)


def test_nearest_targets_cutoff(grid):
    assert grid.nearest_targets(0, {24: "far"}, k=1, cutoff_m=500) == []


def test_nearest_node_and_polyline(grid):
    assert grid.nearest_node(12.9 + 4 * 0.0009, 77.5 + 4 * 0.00092) == 24
    assert grid.polyline([0, 1])[1] == [pytest.approx(77.50092), pytest.approx(12.9)]


def test_save_load_roundtrip(tmp_path, grid):
    p = tmp_path / "g.npz"
    grid.save(p)
    g2 = RoadGraph.load(p)
    assert g2.n == grid.n and g2.astar(0, 24).dist_m == pytest.approx(808)


def test_heuristic_admissible_on_grid(grid):
    for u in range(grid.n):
        r = grid.dijkstra(u, 24)
        assert grid.h_m(u, 24) <= r.dist_m + 1e-6


@pytest.mark.skipif(not GRAPH.exists(), reason="road graph not built")
def test_real_graph_astar_matches_dijkstra():
    g = RoadGraph.load(GRAPH)
    rng = random.Random(3)
    for _ in range(5):
        s, t = rng.randrange(g.n), rng.randrange(g.n)
        a, d = g.astar(s, t), g.dijkstra(s, t)
        assert math.isclose(a.dist_m, d.dist_m, rel_tol=1e-4)
        assert a.stats.expanded <= d.stats.expanded


def test_connected_components():
    comps = connected_components(6, [(0, 1), (1, 2), (3, 4)])
    assert [sorted(c) for c in comps] == [[0, 1, 2], [3, 4], [5]]


def test_proximity_clusters():
    pts = [(12.9, 77.5), (12.9001, 77.5001), (12.9002, 77.5), (13.0, 77.6), (13.0001, 77.6), (12.5, 77.0)]
    clusters = proximity_clusters(pts, radius_m=50, min_size=2)
    assert sorted(map(sorted, clusters)) == [[0, 1, 2], [3, 4]]
