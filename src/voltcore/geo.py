"""Geohash encode/decode/neighbours and haversine distance."""

from __future__ import annotations

import math

_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_B32_IDX = {c: i for i, c in enumerate(_B32)}
EARTH_R_KM = 6371.0088


def encode(lat: float, lon: float, precision: int = 7) -> str:
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out, bits, ch, even = [], 0, 0, True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch, lon_lo = (ch << 1) | 1, mid
            else:
                ch, lon_hi = ch << 1, mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch, lat_lo = (ch << 1) | 1, mid
            else:
                ch, lat_hi = ch << 1, mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_B32[ch])
            bits, ch = 0, 0
    return "".join(out)


def bounds(gh: str) -> tuple[float, float, float, float]:
    """(lat_lo, lat_hi, lon_lo, lon_hi) of a geohash cell."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for c in gh:
        v = _B32_IDX[c]
        for shift in range(4, -1, -1):
            bit = (v >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                lon_lo, lon_hi = (mid, lon_hi) if bit else (lon_lo, mid)
            else:
                mid = (lat_lo + lat_hi) / 2
                lat_lo, lat_hi = (mid, lat_hi) if bit else (lat_lo, mid)
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def decode(gh: str) -> tuple[float, float]:
    """Cell centre (lat, lon)."""
    a, b, c, d = bounds(gh)
    return (a + b) / 2, (c + d) / 2


def neighbours(gh: str) -> list[str]:
    """The 8 surrounding cells (computed by re-encoding offset centres - simple and correct)."""
    lat_lo, lat_hi, lon_lo, lon_hi = bounds(gh)
    dlat, dlon = lat_hi - lat_lo, lon_hi - lon_lo
    clat, clon = (lat_lo + lat_hi) / 2, (lon_lo + lon_hi) / 2
    out = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx or dy:
                out.append(encode(clat + dy * dlat, clon + dx * dlon, len(gh)))
    return out


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def mask_location(lat: float, lon: float, precision: int = 5) -> tuple[float, float]:
    """Privacy masking: snap to the centre of a coarse geohash cell (~4.9 km at precision 5)."""
    return decode(encode(lat, lon, precision))
