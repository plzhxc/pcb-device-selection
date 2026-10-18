"""Preset component library: deterministic parts for kinds that need no LLM selection.

The library (``assets/preset_components.json``, built by
``netlist_end_to_end.common_devices.build_presets``) holds two groups of parts:

* **reference-case parts** — resistors, capacitors, inductors, ferrite beads,
  LEDs, small diodes / TVS / MOSFETs / BJTs / fuses with real LCSC C-numbers
  that the 30 reference designs actually used;
* **catalog parts** — whole ``device_parameters`` categories whose members
  differ only in mechanical parameters (connectors, switches, crystals, LEDs,
  fuses, buzzers), each with its distinguishing *variant* string, price and pin
  count.

Two lookups are offered:

``bind_device(props)``
    For one netlist device (``deviceType`` / ``value`` / ``deviceName`` /
    optional ``package``) return the default preset part of the same kind and
    value, or ``None``.  Used by the netlist stage to fill ``supplierPart`` of
    passives after the catalog gate has run.

``match_module(module)``
    For one requirement module return the preset connector / switch / LED that
    the module reduces to, or ``None`` when the module needs real selection
    (an IC is implied, or no rule covers it).  Used by the device-selection
    stage to bypass the LLM evaluation for such modules.

Both are pure functions over the JSON asset; nothing here talks to a database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

ASSETS_DIR = Path(__file__).with_name("assets")
PRESET_PATH = ASSETS_DIR / "preset_components.json"
PRESET_SCHEMA_PATH = ASSETS_DIR / "preset_components.schema.json"

SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6}


# --------------------------------------------------------------------------- #
# Value parsing (shared with the generator)
# --------------------------------------------------------------------------- #
def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _canonical(value: float, units: Sequence[tuple[str, float]], fallback: str) -> str:
    for unit, scale in units:
        if value >= scale * 0.999:
            return f"{value / scale:g}{unit}"
    return f"{value:g}{fallback}"


def parse_resistance(text: str) -> tuple[str, float] | None:
    """``"10kΩ"`` / ``"4.7k"`` / ``"0Ω"`` / ``"4R7"`` → ``("10kΩ", 10000.0)``."""

    m = re.search(r"(\d+(?:\.\d+)?)\s*([kKmM]?)\s*(?:Ω|ohm|R\b)", text, re.I)
    if m is None:
        m = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)\s*([kKM])(?![\w])", text)
        if m is None:
            m2 = re.fullmatch(r"\s*(\d+)[Rr](\d+)\s*", text)
            if m2 is None:
                return None
            value = float(f"{m2.group(1)}.{m2.group(2)}")
            return canonical_resistance(value), value
    unit = m.group(2)
    value = _num(m.group(1)) * SI[unit] if unit else _num(m.group(1))
    return canonical_resistance(value), value


def canonical_resistance(value: float) -> str:
    return _canonical(value, (("MΩ", 1e6), ("kΩ", 1e3), ("Ω", 1.0)), "Ω")


def parse_capacitance(text: str) -> tuple[str, float] | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(p|n|u|µ|μ|m)F", text)
    if m is None:
        return None
    value = _num(m.group(1)) * SI[m.group(2)]
    return canonical_capacitance(value), value


def canonical_capacitance(value: float) -> str:
    return _canonical(value, (("mF", 1e-3), ("uF", 1e-6), ("nF", 1e-9), ("pF", 1e-12)), "F")


def parse_inductance(text: str) -> tuple[str, float] | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(n|u|µ|μ|m)?H\b", text)
    if m is None:
        return None
    value = _num(m.group(1)) * SI[m.group(2) or ""]
    return canonical_inductance(value), value


def canonical_inductance(value: float) -> str:
    return _canonical(value, (("mH", 1e-3), ("uH", 1e-6), ("nH", 1e-9)), "H")


def parse_bead(text: str) -> tuple[str, float] | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*Ω\s*@\s*(\d+(?:\.\d+)?)\s*MHz", text)
    if m is None:
        return None
    return f"{_num(m.group(1)):g}Ω@{_num(m.group(2)):g}MHz", _num(m.group(1))


LED_COLORS = (("红", "red"), ("绿", "green"), ("蓝", "blue"), ("黄", "yellow"), ("暖白", "warm_white"), ("白", "white"), ("橙", "orange"), ("紫", "purple"), ("粉", "pink"))


def parse_led_color(text: str) -> str | None:
    for zh, en in LED_COLORS:
        if zh in text:
            return en
    m = re.search(r"\b(red|green|blue|yellow|white|orange)\b", text, re.I)
    return m.group(1).lower() if m else None


def parse_voltage(text: str) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*V\b", text)
    return f"{_num(m.group(1)):g}V" if m else None


def parse_tolerance(text: str) -> str | None:
    m = re.search(r"±\s*(\d+(?:\.\d+)?)\s*%", text)
    return f"±{m.group(1)}%" if m else None


def parse_power(text: str) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(m?)W\b", text)
    if m is None:
        return None
    watts = _num(m.group(1)) * (1e-3 if m.group(2) else 1.0)
    return f"{watts * 1e3:g}mW" if watts < 1 else f"{watts:g}W"


def parse_dielectric(text: str) -> str | None:
    m = re.search(r"\b(X7R|X5R|C0G|NP0|X6S|X7S|Y5V|X7T)\b", text, re.I)
    return m.group(1).upper() if m else None


VALUE_PARSERS = {
    "resistor": parse_resistance,
    "capacitor": parse_capacitance,
    "capacitor_polar": parse_capacitance,
    "inductor": parse_inductance,
    "ferrite_bead": parse_bead,
}


# --------------------------------------------------------------------------- #
# Library access
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def load_presets() -> dict[str, Any]:
    return json.loads(PRESET_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _index() -> dict[str, Any]:
    """Secondary indexes over ``parts``: by kind, by (kind, value_si), by mpn."""

    by_kind: dict[str, list[dict[str, Any]]] = {}
    by_value: dict[tuple[str, float], list[dict[str, Any]]] = {}
    by_mpn: dict[str, list[dict[str, Any]]] = {}
    by_c: dict[str, dict[str, Any]] = {}
    for part in load_presets()["parts"]:
        by_kind.setdefault(part["kind"], []).append(part)
        if part.get("value_si") is not None:
            by_value.setdefault((part["kind"], round(float(part["value_si"]), 15)), []).append(part)
        mpn = str(part.get("mpn") or "").strip().casefold()
        if mpn:
            by_mpn.setdefault(mpn, []).append(part)
        by_c[part["supplier_part"]] = part
    return {"by_kind": by_kind, "by_value": by_value, "by_mpn": by_mpn, "by_c": by_c}


def kinds() -> Mapping[str, Mapping[str, Any]]:
    return load_presets()["kinds"]


def part_by_c_number(c_number: str) -> dict[str, Any] | None:
    return _index()["by_c"].get(str(c_number).strip().upper())


def _best(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The best-ranked part across variants: rank inside its variant, then usage, then price."""

    if not parts:
        return None
    return dict(
        min(
            parts,
            key=lambda p: (
                p.get("rank", 99),
                0 if p.get("jlc_class") == "basic" else 1,
                -int(p.get("usage", {}).get("cases", 0)),
                -int(p.get("usage", {}).get("instances", 0)),
                p.get("price_cny") if p.get("price_cny") is not None else 1e9,
                p["supplier_part"],
            ),
        )
    )


# --------------------------------------------------------------------------- #
# Netlist device binding
# --------------------------------------------------------------------------- #
_POLAR_HINT = re.compile(r"电解|钽|polymer|tantal|electrolytic|铝", re.I)
_BEAD_HINT = re.compile(r"磁珠|bead|FB\d", re.I)
_LED_HINT = re.compile(r"LED|灯|指示", re.I)
# Colour word → regex over the LCSC ``luminous_color`` word that opens a catalog LED variant.
_LED_CATALOG_COLORS = {
    "red": r"Red\b",
    "green": r"(Emerald|Green)",
    "blue": r"Blue\b",
    "yellow": r"Yellow\b",
    "white": r"White\b",
    "orange": r"Orange\b",
}
_C_NUMBER = re.compile(r"^C\d+$")


def _device_kind(props: Mapping[str, Any]) -> tuple[str | None, str]:
    """Map netlist ``deviceType`` (+ text hints) to a preset kind; returns (kind, text)."""

    device_type = str(props.get("deviceType") or props.get("device_type") or "").strip().lower()
    value = str(props.get("value") or "").strip()
    name = str(props.get("deviceName") or props.get("device_name") or "").strip()
    text = f"{name} {value}".strip()
    if device_type in {"resistance", "resistor"}:
        return "resistor", text
    if device_type in {"capacitance", "capacitor"}:
        parsed = parse_capacitance(text)
        if _POLAR_HINT.search(text) or (parsed is not None and parsed[1] >= 47e-6):
            return "capacitor_polar", text
        return "capacitor", text
    if device_type in {"inductance", "inductor"}:
        return ("ferrite_bead" if _BEAD_HINT.search(text) or parse_bead(text) else "inductor"), text
    if device_type == "diode":
        return ("led" if _LED_HINT.search(text) else "diode"), text
    if device_type in {"tvs_diode", "esd_protection"}:
        return "tvs_esd", text
    if device_type == "mos":
        return "mosfet", text
    if device_type == "triode":
        return "bjt", text
    if device_type in {"switch", "oscillator", "connector", "fuse"}:
        return device_type, text
    return None, text


def _by_mpn(text: str, allowed_kinds: set[str] | None = None) -> dict[str, Any] | None:
    """Exact manufacturer-part match on any whitespace-separated token of ``text``."""

    tokens = {t.strip(",;()（）").casefold() for t in re.split(r"\s+|/", text) if t.strip()}
    tokens.add(text.strip().casefold())
    hits: list[dict[str, Any]] = []
    for token in tokens:
        for part in _index()["by_mpn"].get(token, []):
            if allowed_kinds is None or part["kind"] in allowed_kinds:
                hits.append(part)
    return _best(hits)


_MPN_KINDS = {
    "diode": {"diode_schottky", "diode_switching", "diode_zener", "diode_rectifier", "led", "rgb_led_builtin_ic"},
    "tvs_esd": {"tvs_esd"},
    "mosfet": {"mosfet"},
    "bjt": {"bjt"},
    "switch": {"tactile_switch", "slide_switch", "dip_switch", "button_switch", "toggle_switch", "rotary_encoder"},
    "oscillator": {"passive_crystal_oscillator", "active_crystal_oscillator"},
    "connector": None,  # any catalog connector kind
    "fuse": {"fuse_resettable", "self_resetting_fuse", "disposable_fuse"},
}


def bind_device(props: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return ``{"part": ..., "match": "value+package" | "value" | "mpn", "package_fallback": bool}`` or ``None``."""

    kind, text = _device_kind(props)
    if kind is None:
        return None
    index = _index()
    if kind in VALUE_PARSERS:
        parsed = VALUE_PARSERS[kind](str(props.get("value") or "")) or VALUE_PARSERS[kind](text)
        if parsed is None:
            return None
        candidates = index["by_value"].get((kind, round(parsed[1], 15)), [])
        if not candidates:
            return None
        meta = kinds().get(kind, {})
        wanted = str(props.get("package") or props.get("footprint") or meta.get("default_package") or "").strip()
        if "package" in meta.get("match", []) and wanted:
            exact = [p for p in candidates if str(p.get("package") or "") == wanted]
            if exact:
                return {"part": _best(exact), "match": "value+package", "package_fallback": False}
            return {"part": _best(candidates), "match": "value", "package_fallback": True}
        return {"part": _best(candidates), "match": "value", "package_fallback": False}
    if kind == "led":
        color = parse_led_color(text)
        if color is None:
            hit = _by_mpn(text, _MPN_KINDS["diode"])
            return {"part": hit, "match": "mpn", "package_fallback": False} if hit else None
        meta = kinds().get("led", {})
        wanted = str(props.get("package") or props.get("footprint") or meta.get("default_package") or "").strip()
        # Reference-case LEDs carry ``color`` + ``package``; catalog LEDs carry the
        # LCSC colour word in their variant ("Red / 0603", "Emerald Green / 0603 ...").
        case_leds = [p for p in index["by_kind"].get("led", []) if p.get("color") == color]
        words = _LED_CATALOG_COLORS.get(color)
        catalog_leds = [
            p for p in index["by_kind"].get("light_emitting_diode_led", [])
            if words and re.match(words, str(p.get("variant") or ""), re.I)
        ]
        candidates = case_leds + catalog_leds
        exact = [p for p in candidates if wanted and (str(p.get("package") or "") == wanted or f"/ {wanted}" in str(p.get("variant") or ""))]
        if exact:
            return {"part": _best(exact), "match": "color+package", "package_fallback": False}
        if candidates:
            return {"part": _best(candidates), "match": "color", "package_fallback": True}
        return None
    name = str(props.get("deviceName") or props.get("device_name") or "").strip()
    hit = (_by_mpn(name, _MPN_KINDS.get(kind)) if name else None) or _by_mpn(text, _MPN_KINDS.get(kind))
    return {"part": hit, "match": "mpn", "package_fallback": False} if hit else None


# --------------------------------------------------------------------------- #
# Module routing
# --------------------------------------------------------------------------- #
def _module_text(module: Mapping[str, Any], fields: Sequence[str]) -> str:
    parameters = module.get("parameters") if isinstance(module.get("parameters"), Mapping) else {}
    pieces = [str(module.get("name") or ""), str(module.get("description") or "")]
    pieces.extend(str(parameters.get(field) or "") for field in fields)
    return " ".join(p for p in pieces if p)


def _base_id(module_id: str) -> str:
    return re.split(r"[#\s]", module_id, maxsplit=1)[0]


def _variant_default(kind: str, variant: str | None, prefix: str | None) -> dict[str, Any] | None:
    parts = _index()["by_kind"].get(kind, [])
    if variant:
        exact = [p for p in parts if p.get("variant") == variant]
        if exact:
            return _best(exact)
    if prefix:
        candidates = [p for p in parts if str(p.get("variant") or "").startswith(prefix)]
        if candidates:
            return _best(candidates)
    return None


def match_module(module: Mapping[str, Any]) -> dict[str, Any] | None:
    """Resolve a requirement module to one preset part, or ``None`` when it needs real selection."""

    module_id = _base_id(str(module.get("id") or ""))
    catalog_title = str(module.get("catalog_module") or "").strip()
    for rule in load_presets()["module_presets"]:
        if module_id != rule["module_id"] and not (catalog_title and catalog_title.startswith(rule["title"])):
            continue
        text = _module_text(module, rule.get("parameter_fields", ()))
        block = rule.get("block_if_parameters_match")
        if block and re.search(block, text, re.I):
            return None
        part = None
        matched_on = None
        for variant in rule.get("variants", []):
            if re.search(variant["when"], text, re.I):
                part = _variant_default(rule["kind"], variant.get("variant"), variant.get("variant_prefix"))
                matched_on = variant["when"]
                if part is not None:
                    break
        if part is None and rule.get("fallback_variant"):
            part = _variant_default(rule["kind"], rule["fallback_variant"], None)
            matched_on = "fallback"
        if part is None and not rule.get("variants"):
            part = _best(_index()["by_kind"].get(rule["kind"], []))
            matched_on = "kind default"
        if part is None:
            return None
        return {"rule": rule["module_id"], "kind": rule["kind"], "variant": part.get("variant"), "matched_on": matched_on, "part": part}
    return None


def describe_part(part: Mapping[str, Any]) -> str:
    """One-line human rendering used in reasons and notes."""

    bits = [part.get("mpn") or part["supplier_part"], part.get("variant") or "", part.get("package") or ""]
    usage = part.get("usage") or {}
    if usage.get("cases"):
        bits.append(f"用于 {usage['cases']} 个参考案例")
    if part.get("price_cny") is not None:
        bits.append(f"¥{part['price_cny']:.2f}")
    return " · ".join(str(b) for b in bits if b)


__all__ = [
    "PRESET_PATH",
    "PRESET_SCHEMA_PATH",
    "bind_device",
    "canonical_capacitance",
    "canonical_inductance",
    "canonical_resistance",
    "describe_part",
    "kinds",
    "load_presets",
    "match_module",
    "parse_bead",
    "parse_capacitance",
    "parse_dielectric",
    "parse_inductance",
    "parse_led_color",
    "parse_power",
    "parse_resistance",
    "parse_tolerance",
    "parse_voltage",
    "part_by_c_number",
]
