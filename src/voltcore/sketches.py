"""Probabilistic streaming structures: Bloom filter and Count-Min Sketch with top-K."""

from __future__ import annotations

import hashlib
import heapq
import math

import numpy as np


def _hashes(key: bytes, k: int, m: int) -> list[int]:
    # Kirsch-Mitzenmacher double hashing: h_i = h1 + i*h2 (mod m) from one 128-bit digest.
    d = hashlib.blake2b(key, digest_size=16).digest()
    h1 = int.from_bytes(d[:8], "little")
    h2 = int.from_bytes(d[8:], "little") | 1
    return [(h1 + i * h2) % m for i in range(k)]


class BloomFilter:
    """Space-efficient set membership with no false negatives.

    Sized from expected items n and false-positive rate p: m = -n ln p / (ln 2)^2 bits,
    k = (m/n) ln 2 hashes. add/contains are O(k).
    """

    def __init__(self, capacity: int, fp_rate: float = 0.001):
        self.capacity = capacity
        self.m = max(8, int(-capacity * math.log(fp_rate) / (math.log(2) ** 2)))
        self.k = max(1, round(self.m / capacity * math.log(2)))
        self.bits = np.zeros((self.m + 7) // 8, dtype=np.uint8)
        self.count = 0

    def add(self, key: str | bytes) -> bool:
        """Insert; returns True if the key was (probably) already present."""
        kb = key.encode() if isinstance(key, str) else key
        present = True
        for h in _hashes(kb, self.k, self.m):
            byte, bit = divmod(h, 8)
            if not (self.bits[byte] >> bit) & 1:
                present = False
                self.bits[byte] |= 1 << bit
        if not present:
            self.count += 1
        return present

    def __contains__(self, key: str | bytes) -> bool:
        kb = key.encode() if isinstance(key, str) else key
        return all((self.bits[h // 8] >> (h % 8)) & 1 for h in _hashes(kb, self.k, self.m))

    @property
    def saturated(self) -> bool:
        return self.count >= self.capacity


class RotatingBloom:
    """Two-generation Bloom filter so memory stays bounded on an infinite stream:
    when the active filter is full it becomes 'previous' and a fresh one starts."""

    def __init__(self, capacity: int, fp_rate: float = 0.001):
        self._cap, self._fp = capacity, fp_rate
        self.active = BloomFilter(capacity, fp_rate)
        self.previous: BloomFilter | None = None

    def seen(self, key: str) -> bool:
        if self.previous is not None and key in self.previous:
            self.active.add(key)
            return True
        dup = self.active.add(key)
        if self.active.saturated:
            self.previous, self.active = self.active, BloomFilter(self._cap, self._fp)
        return dup


class CountMinSketch:
    """Frequency estimates in O(1/eps * ln(1/delta)) memory; never under-counts.

    width w = ceil(e/eps), depth d = ceil(ln(1/delta)). estimate <= true + eps*N w.p. 1-delta.
    Keeps a bounded min-heap of candidate heavy hitters for top-K queries.
    """

    def __init__(self, eps: float = 0.001, delta: float = 0.01, top_k: int = 20):
        self.w = math.ceil(math.e / eps)
        self.d = math.ceil(math.log(1 / delta))
        self.table = np.zeros((self.d, self.w), dtype=np.int64)
        self.total = 0
        self.top_k = top_k
        self._heap: list[tuple[int, str]] = []
        self._in_heap: dict[str, int] = {}

    def add(self, key: str, count: int = 1) -> int:
        kb = key.encode()
        idx = _hashes(kb, self.d, self.w)
        rows = np.arange(self.d)
        self.table[rows, idx] += count
        self.total += count
        est = int(self.table[rows, idx].min())
        self._track(key, est)
        return est

    def estimate(self, key: str) -> int:
        idx = _hashes(key.encode(), self.d, self.w)
        return int(self.table[np.arange(self.d), idx].min())

    def _track(self, key: str, est: int) -> None:
        if key in self._in_heap:
            self._in_heap[key] = est  # lazy: heap entry refreshed on rebuild
            return
        if len(self._in_heap) < self.top_k:
            self._in_heap[key] = est
            heapq.heappush(self._heap, (est, key))
            return
        # Rebuild heap lazily with fresh estimates, then compare with the current minimum.
        self._heap = [(self._in_heap[k], k) for _, k in self._heap]
        heapq.heapify(self._heap)
        if est > self._heap[0][0]:
            _, evicted = heapq.heapreplace(self._heap, (est, key))
            del self._in_heap[evicted]
            self._in_heap[key] = est

    def topk(self) -> list[tuple[str, int]]:
        return sorted(((k, self.estimate(k)) for k in self._in_heap), key=lambda kv: -kv[1])

    def merge(self, other: CountMinSketch) -> None:
        if self.table.shape != other.table.shape:
            raise ValueError("sketch dimensions differ")
        self.table += other.table
        self.total += other.total
        for k in other._in_heap:
            self._track(k, self.estimate(k))
