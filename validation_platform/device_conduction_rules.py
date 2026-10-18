"""Local-device conduction rules used by connect abstraction.

The connect metric walks from an IC pin through nets and local devices until it
reaches another IC pin, GND, power, or system IO.  This file is the fixed place
for package-level rules that decide which pins inside a local device are
conductively connected for that walk.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ConductiveGroup:
    key: str
    pins: Tuple[str, ...]


def conductive_groups(dev: Dict[str, Any]) -> List[ConductiveGroup]:
    """Return conductive pin groups for a local/path device.

    Most R/C/L/SW/D/Q devices are represented as one local body.  Resistor
    arrays are the important exception: one physical RN package contains
    multiple independent resistors, so each resistor must become a separate
    conductive group.
    """
    pins = _sorted_pins(dev)
    if not pins:
        return []
    dtype = str(dev.get("type") or "").upper()
    if dtype == "R":
        groups = _resistor_array_groups(dev, pins)
        if groups:
            return groups
    return [ConductiveGroup("MAIN", tuple(pins))]


def _resistor_array_groups(dev: Dict[str, Any], pins: List[str]) -> List[ConductiveGroup]:
    if len(pins) < 4 or len(pins) % 2:
        return []

    props = dev.get("props") or {}
    designator = str(props.get("designator") or props.get("Designator") or "").strip().upper()
    notes = " ".join(
        str(props.get(key) or "")
        for key in ("deviceNotes", "description", "deviceName", "DeviceName", "value")
    )
    text = notes.upper()
    resistor_count = _extract_int_field(notes, ("电阻器数", "RESISTOR COUNT", "RESISTORS"))
    pin_count = _extract_int_field(notes, ("引脚数", "PIN COUNT", "PINS"))
    looks_like_array = (
        designator.startswith("RN")
        or "电阻器数" in notes
        or "RESISTOR ARRAY" in text
        or "RESISTOR NETWORK" in text
        or bool(re.search(r"\b\d+D\d+\b", text))
    )

    if resistor_count and pin_count:
        if pin_count == len(pins) and pin_count == resistor_count * 2:
            return _mirrored_groups(pins)
        return []
    if looks_like_array:
        return _mirrored_groups(pins)
    return []


def _mirrored_groups(pins: List[str]) -> List[ConductiveGroup]:
    return [
        ConductiveGroup(f"R{idx + 1}", (pins[idx], pins[-idx - 1]))
        for idx in range(len(pins) // 2)
    ]


def _sorted_pins(dev: Dict[str, Any]) -> List[str]:
    pins = [str(pin) for pin in (dev.get("pins") or {})]
    if all(pin.isdigit() for pin in pins):
        return sorted(pins, key=lambda item: int(item))
    return sorted(pins, key=_token_key)


def _extract_int_field(text: str, names: Iterable[str]) -> Optional[int]:
    for name in names:
        pattern = re.escape(name)
        match = re.search(pattern + r"\s*[:：=]?\s*(\d+)", text, flags=re.I)
        if match:
            return int(match.group(1))
    return None


def _token_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(text or "").upper()).strip("_")
