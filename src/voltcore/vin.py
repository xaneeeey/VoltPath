"""VIN validation (ISO 3779 / FMVSS 115 check digit).

A VIN is 17 characters from [A-HJ-NPR-Z0-9] (no I, O, Q). Position 9 is a check digit
computed from the transliterated values of all characters weighted by position.
"""

from __future__ import annotations

import re
from functools import lru_cache

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

_TRANSLIT = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
_ALPHABET = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"


def check_digit(vin: str) -> str:
    """Return the expected check digit ('0'-'9' or 'X') for a 17-char VIN."""
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS, strict=True))
    rem = total % 11
    return "X" if rem == 10 else str(rem)


@lru_cache(maxsize=262_144)
def is_valid_vin(vin: str) -> bool:
    """Full validation: charset/length regex plus check digit. Cached - the fleet is finite,
    so steady-state validation is an O(1) dict hit instead of 17 multiplications."""
    if not isinstance(vin, str) or not VIN_RE.match(vin):
        return False
    return vin[8] == check_digit(vin)


def make_vin(wmi: str, vds: str, model_year: str, plant: str, serial: int) -> str:
    """Build a valid VIN: WMI(3) + VDS(5) + check(1) + year(1) + plant(1) + serial(6)."""
    body = f"{wmi}{vds}0{model_year}{plant}{serial:06d}"
    if len(body) != 17 or not VIN_RE.match(body):
        raise ValueError(f"cannot build VIN from parts: {body!r}")
    return body[:8] + check_digit(body) + body[9:]


def random_vin(rng, wmi: str = "9VX") -> str:
    """Random but valid VIN (rng: random.Random or numpy Generator-compatible choice)."""
    vds = "".join(rng.choice(_ALPHABET) for _ in range(5))
    year = rng.choice("RSTV")  # 2024-2027
    plant = rng.choice("ABCDEFGH")
    serial = int(rng.random() * 999_999)
    return make_vin(wmi, vds, year, plant, serial)
