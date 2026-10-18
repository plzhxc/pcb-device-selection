"""Shared parsing helpers for formal netlist metrics.

Retired topology/device-count/value-distribution implementations are outside
the active package.  Compare code keeps only the type and R/C/L value
normalization needed by the formal three metrics.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .taxonomy import compare_type_for_props


def normalize_type(props: Dict[str, Any]) -> str:
    """Infer comparison type such as C/R/L/D_OR_LED/Q/XTAL/SW/CON/IC/OTHER."""
    return compare_type_for_props(props)


_VALUE_NUM_RE = re.compile(r"([\d.]+)\s*([a-zA-ZμµΩω]+)?")


def parse_value(value: str, kind: str) -> Optional[float]:
    """Convert common R/C/L strings to SI values."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower() in {"nc", "none", "tbd"}:
        return None
    match = _VALUE_NUM_RE.match(text)
    if not match:
        return None
    try:
        num = float(match.group(1))
    except ValueError:
        return None

    unit = (match.group(2) or "").strip()
    unit_norm = (
        unit.replace("Ω", "ohm")
        .replace("ω", "ohm")
        .replace("µ", "u")
        .replace("μ", "u")
    )
    unit_low = unit_norm.lower()
    kind_norm = (kind or "").upper()

    if kind_norm == "R":
        if not unit_low or unit_low in {"r", "ohm"}:
            return num
        if unit_low in {"k", "kr", "kohm"}:
            return num * 1e3
        if unit_norm.startswith("M") or unit_low in {"meg", "mega"}:
            return num * 1e6
        if unit_low in {"m", "mohm"}:
            return num * 1e-3
        if unit_low in {"g", "gohm"}:
            return num * 1e9
        return None

    if kind_norm == "C":
        if not unit_low or unit_low == "f":
            return num
        mult = {
            "pf": 1e-12,
            "p": 1e-12,
            "nf": 1e-9,
            "n": 1e-9,
            "uf": 1e-6,
            "u": 1e-6,
            "mf": 1e-3,
            "m": 1e-3,
        }.get(unit_low)
        return num * mult if mult is not None else None

    if kind_norm == "L":
        if not unit_low or unit_low == "h":
            return num
        mult = {
            "nh": 1e-9,
            "n": 1e-9,
            "uh": 1e-6,
            "u": 1e-6,
            "mh": 1e-3,
            "m": 1e-3,
        }.get(unit_low)
        return num * mult if mult is not None else None

    return None
