"""OBD-II Diagnostic Trouble Code parsing.

Format: one letter system (P powertrain, C chassis, B body, U network) + 4 hex-ish digits,
e.g. P0A7E. OEM payloads embed codes in lists, ';'/','-separated strings or free text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DTC_RE = re.compile(r"\b([PCBU])([0-3])([0-9A-F])([0-9A-F]{2})\b")

SYSTEMS = {"P": "powertrain", "C": "chassis", "B": "body", "U": "network"}

# Subset of codes relevant to EV battery/charging (SAE J2012 hybrid/EV range P0Axx/P0Bxx).
KNOWN = {
    "P0A7E": ("Hybrid/EV battery pack over temperature", "critical"),
    "P0A7F": ("Hybrid/EV battery pack deterioration", "warning"),
    "P0A80": ("Replace hybrid/EV battery pack", "critical"),
    "P0AA6": ("Hybrid/EV battery voltage system isolation fault", "critical"),
    "P0B24": ("Hybrid/EV battery cell voltage imbalance", "warning"),
    "P0D27": ("Battery charger input voltage too low", "info"),
    "P0CE6": ("Battery charging port temperature sensor", "warning"),
    "P0301": ("Cylinder 1 misfire detected", "warning"),
    "U0100": ("Lost communication with ECM/PCM", "warning"),
    "C0035": ("Left front wheel speed sensor circuit", "warning"),
}


@dataclass(frozen=True, slots=True)
class Dtc:
    code: str
    system: str
    generic: bool  # second char 0/2 = SAE generic, 1/3 = manufacturer specific
    description: str
    severity: str


def parse_dtcs(raw) -> list[str]:
    """Extract unique, normalised DTC codes from list/str/None, preserving first-seen order."""
    if raw is None:
        return []
    text = " ".join(map(str, raw)) if isinstance(raw, list | tuple) else str(raw)
    seen: dict[str, None] = {}
    for m in DTC_RE.finditer(text.upper().replace(";", " ").replace(",", " ")):
        seen.setdefault("".join(m.groups()), None)
    return list(seen)


def describe(code: str) -> Dtc:
    m = DTC_RE.fullmatch(code)
    if not m:
        raise ValueError(f"invalid DTC {code!r}")
    desc, sev = KNOWN.get(code, ("Unknown / manufacturer-specific code", "info"))
    return Dtc(code, SYSTEMS[code[0]], code[1] in "02", desc, sev)
