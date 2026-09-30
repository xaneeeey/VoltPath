"""Segment noisy GPS/speed traces into trips and stops with a 2-state HMM (Viterbi DP).

Thresholding speed alone flickers on GPS noise and traffic-light stops. The HMM adds a
switching penalty so short blips don't split trips.

States: 0 = STOPPED, 1 = MOVING
Emission log-likelihood from observed speed (km/h): stopped ~ half-normal(sigma=3),
moving ~ normal(mu=30, sigma=20) truncated at 0. Transition: stay with prob 1 - p_switch.
Viterbi: O(N * S^2) = O(N) time and O(N) back-pointers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

STOPPED, MOVING = 0, 1


def _log_emit(speed: float) -> tuple[float, float]:
    s = max(0.0, speed)
    stopped = -0.5 * (s / 3.0) ** 2 + math.log(2 / (3.0 * math.sqrt(2 * math.pi)))
    moving = -0.5 * ((s - 30.0) / 20.0) ** 2 + math.log(1 / (20.0 * math.sqrt(2 * math.pi)))
    return stopped, moving


def viterbi_states(speeds: list[float], p_switch: float = 0.02) -> list[int]:
    if not speeds:
        return []
    stay, switch = math.log(1 - p_switch), math.log(p_switch)
    e0 = _log_emit(speeds[0])
    v = [math.log(0.5) + e0[0], math.log(0.5) + e0[1]]
    back: list[tuple[int, int]] = []
    for s in speeds[1:]:
        e = _log_emit(s)
        prev0 = (v[0] + stay, v[1] + switch)
        prev1 = (v[0] + switch, v[1] + stay)
        b0 = 0 if prev0[0] >= prev0[1] else 1
        b1 = 0 if prev1[0] >= prev1[1] else 1
        v = [prev0[b0] + e[0], prev1[b1] + e[1]]
        back.append((b0, b1))
    state = 0 if v[0] >= v[1] else 1
    path = [state]
    for b in reversed(back):
        state = b[state]
        path.append(state)
    return path[::-1]


@dataclass
class Segment:
    kind: str  # "trip" | "stop"
    start: datetime
    end: datetime
    start_idx: int
    end_idx: int

    @property
    def duration_s(self) -> float:
        return (self.end - self.start).total_seconds()


def segment(points: list[tuple[datetime, float, float, float]], min_stop_s: float = 120,
            p_switch: float = 0.02) -> list[Segment]:
    """points: (ts, lat, lon, speed_kmh) sorted by ts. Stops shorter than min_stop_s are merged
    into the surrounding trip (traffic lights / queues)."""
    if not points:
        return []
    states = viterbi_states([p[3] for p in points], p_switch)
    segs: list[Segment] = []
    i = 0
    while i < len(states):
        j = i
        while j + 1 < len(states) and states[j + 1] == states[i]:
            j += 1
        segs.append(Segment("trip" if states[i] == MOVING else "stop", points[i][0], points[j][0], i, j))
        i = j + 1
    # Merge short interior stops into trips.
    merged: list[Segment] = []
    for s in segs:
        if (s.kind == "stop" and s.duration_s < min_stop_s and merged and merged[-1].kind == "trip"):
            merged[-1] = Segment("trip", merged[-1].start, s.end, merged[-1].start_idx, s.end_idx)
            continue
        if merged and merged[-1].kind == s.kind:
            merged[-1] = Segment(s.kind, merged[-1].start, s.end, merged[-1].start_idx, s.end_idx)
            continue
        merged.append(s)
    return merged
