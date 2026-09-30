"""Road graph in CSR form with Dijkstra, multi-target Dijkstra and A*.

Nodes carry (lat, lon); edge weights are metres. Undirected (one-ways ignored - documented).
CSR (indptr/indices/weights numpy arrays) keeps a 60K-node city graph in a few MB and gives
cache-friendly neighbour scans.

Complexities (V nodes, E edges, binary heap):
  dijkstra / nearest_targets : O((V + E) log V) worst case, early-exit on cutoff / k targets
  astar                      : same worst case; with the admissible haversine heuristic it
                               expands far fewer nodes on point-to-point queries (see docs).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .geo import EARTH_R_KM


@dataclass
class SearchStats:
    expanded: int = 0


@dataclass
class Route:
    nodes: list[int]
    dist_m: float
    stats: SearchStats = field(default_factory=SearchStats)


class RoadGraph:
    def __init__(self, lat: np.ndarray, lon: np.ndarray, indptr: np.ndarray, indices: np.ndarray,
                 weights: np.ndarray):
        self.lat = lat.astype(np.float64)
        self.lon = lon.astype(np.float64)
        self.indptr = indptr.astype(np.int64)
        self.indices = indices.astype(np.int32)
        self.weights = weights.astype(np.float32)
        self._lat_r = np.radians(self.lat)
        self._lon_r = np.radians(self.lon)
        self._cos = np.cos(self._lat_r)
        # Coarse grid index for nearest-node lookup (~1 km cells).
        self._cell = 0.01
        self._grid: dict[tuple[int, int], np.ndarray] = {}
        keys = np.floor(self.lat / self._cell).astype(int) * 100_000 + np.floor(self.lon / self._cell).astype(int)
        order = np.argsort(keys, kind="stable")
        uniq, starts = np.unique(keys[order], return_index=True)
        bounds_ = list(starts) + [len(order)]
        for i, k in enumerate(uniq):
            self._grid[(int(k // 100_000), int(k % 100_000))] = order[bounds_[i]:bounds_[i + 1]]

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_edges(cls, lat, lon, edges: list[tuple[int, int, float]]) -> RoadGraph:
        n = len(lat)
        src, dst, w = [], [], []
        for a, b, d in edges:
            src += [a, b]
            dst += [b, a]
            w += [d, d]
        src_a = np.asarray(src, dtype=np.int64)
        order = np.argsort(src_a, kind="stable")
        indptr = np.zeros(n + 1, dtype=np.int64)
        np.add.at(indptr, src_a + 1, 1)
        indptr = np.cumsum(indptr)
        return cls(np.asarray(lat), np.asarray(lon), indptr, np.asarray(dst)[order], np.asarray(w)[order])

    @classmethod
    def load(cls, path: str | Path) -> RoadGraph:
        z = np.load(path)
        return cls(z["lat"], z["lon"], z["indptr"], z["indices"], z["weights"])

    def save(self, path: str | Path) -> None:
        np.savez_compressed(path, lat=self.lat, lon=self.lon, indptr=self.indptr, indices=self.indices,
                            weights=self.weights)

    @property
    def n(self) -> int:
        return len(self.lat)

    def neighbours(self, u: int):
        s, e = self.indptr[u], self.indptr[u + 1]
        return self.indices[s:e], self.weights[s:e]

    # ------------------------------------------------------------------ geometry
    def h_m(self, u: int, v: int) -> float:
        """Haversine metres between nodes - admissible A* heuristic (edges >= straight line)."""
        dp = self._lat_r[v] - self._lat_r[u]
        dl = self._lon_r[v] - self._lon_r[u]
        a = math.sin(dp / 2) ** 2 + self._cos[u] * self._cos[v] * math.sin(dl / 2) ** 2
        return 2000 * EARTH_R_KM * math.asin(min(1.0, math.sqrt(a)))

    def nearest_node(self, lat: float, lon: float) -> int:
        ci, cj = math.floor(lat / self._cell), math.floor(lon / self._cell)
        for r in range(0, 30):
            cand = [self._grid[(ci + di, cj + dj)] for di in range(-r, r + 1) for dj in range(-r, r + 1)
                    if max(abs(di), abs(dj)) == r and (ci + di, cj + dj) in self._grid]
            if cand:
                idx = np.concatenate(cand)
                d2 = (self.lat[idx] - lat) ** 2 + ((self.lon[idx] - lon) * math.cos(math.radians(lat))) ** 2
                return int(idx[np.argmin(d2)])
        return int(np.argmin((self.lat - lat) ** 2 + (self.lon - lon) ** 2))

    def polyline(self, nodes: list[int]) -> list[list[float]]:
        return [[round(float(self.lon[u]), 6), round(float(self.lat[u]), 6)] for u in nodes]

    # ------------------------------------------------------------------ search
    @staticmethod
    def _path(prev: dict[int, int], target: int) -> list[int]:
        out = [target]
        while out[-1] in prev:
            out.append(prev[out[-1]])
        return out[::-1]

    def dijkstra(self, source: int, target: int) -> Route | None:
        """Point-to-point Dijkstra (baseline for A*)."""
        dist = {source: 0.0}
        prev: dict[int, int] = {}
        pq = [(0.0, source)]
        stats = SearchStats()
        done: set[int] = set()
        while pq:
            d, u = heapq.heappop(pq)
            if u in done:
                continue
            done.add(u)
            stats.expanded += 1
            if u == target:
                return Route(self._path(prev, target), d, stats)
            nbrs, ws = self.neighbours(u)
            for v, w in zip(nbrs.tolist(), ws.tolist(), strict=True):
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return None

    def astar(self, source: int, target: int) -> Route | None:
        """A* with haversine heuristic. Consistent heuristic => each node expanded once."""
        g = {source: 0.0}
        prev: dict[int, int] = {}
        pq = [(self.h_m(source, target), 0.0, source)]
        stats = SearchStats()
        done: set[int] = set()
        while pq:
            _, d, u = heapq.heappop(pq)
            if u in done:
                continue
            done.add(u)
            stats.expanded += 1
            if u == target:
                return Route(self._path(prev, target), d, stats)
            nbrs, ws = self.neighbours(u)
            for v, w in zip(nbrs.tolist(), ws.tolist(), strict=True):
                nd = d + w
                if nd < g.get(v, math.inf):
                    g[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd + self.h_m(v, target), nd, v))
        return None

    def nearest_targets(self, source: int, targets: dict[int, object], k: int = 3,
                        cutoff_m: float = math.inf) -> list[tuple[object, Route]]:
        """Multi-target Dijkstra: the k closest targets by road distance within cutoff.

        One search instead of one A* per charger: O((V+E) log V) bounded by the cutoff ball,
        independent of the number of chargers. `targets` maps node -> payload (charger ids).
        """
        dist = {source: 0.0}
        prev: dict[int, int] = {}
        pq = [(0.0, source)]
        stats = SearchStats()
        done: set[int] = set()
        found: list[tuple[object, Route]] = []
        while pq and len(found) < k:
            d, u = heapq.heappop(pq)
            if u in done:
                continue
            if d > cutoff_m:
                break
            done.add(u)
            stats.expanded += 1
            if u in targets:
                found.append((targets[u], Route(self._path(prev, u), d, stats)))
            nbrs, ws = self.neighbours(u)
            for v, w in zip(nbrs.tolist(), ws.tolist(), strict=True):
                nd = d + w
                if nd < dist.get(v, math.inf):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return found


# ---------------------------------------------------------------------- components
class DisjointSet:
    """Union-find with path halving + union by size: near-O(1) amortised (inverse Ackermann)."""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x: int) -> int:
        p = self.parent
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True


def connected_components(n: int, edges) -> list[list[int]]:
    """Components of an undirected graph, largest first. O(E alpha(V))."""
    ds = DisjointSet(n)
    for a, b, *_ in edges:
        ds.union(a, b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(ds.find(i), []).append(i)
    return sorted(groups.values(), key=len, reverse=True)


def proximity_clusters(points: list[tuple[float, float]], radius_m: float, min_size: int = 3) -> list[list[int]]:
    """Cluster points whose pairwise distance <= radius (single linkage) via a grid + union-find.

    Used to find groups of parked vehicles at unapproved depots. Grid bucketing makes it
    ~O(n) for uniformly spread points instead of O(n^2) pairwise checks.
    """
    cell_deg = radius_m / 111_000
    grid: dict[tuple[int, int], list[int]] = {}
    for i, (la, lo) in enumerate(points):
        grid.setdefault((int(la // cell_deg), int(lo // cell_deg)), []).append(i)
    ds = DisjointSet(len(points))
    r2 = radius_m ** 2
    for (ci, cj), members in grid.items():
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                other = grid.get((ci + di, cj + dj))
                if not other:
                    continue
                for a in members:
                    la, lo = points[a]
                    k = math.cos(math.radians(la)) * 111_000
                    for b in other:
                        if b <= a:
                            continue
                        lb, ob = points[b]
                        if ((la - lb) * 111_000) ** 2 + ((lo - ob) * k) ** 2 <= r2:
                            ds.union(a, b)
    groups: dict[int, list[int]] = {}
    for i in range(len(points)):
        groups.setdefault(ds.find(i), []).append(i)
    return sorted((g for g in groups.values() if len(g) >= min_size), key=len, reverse=True)
