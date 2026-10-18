"""Strict, dimension-aware nominal value comparison; never used for pairing."""
from __future__ import annotations

import re
from decimal import Decimal

_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_VALUE = re.compile(rf"({_NUMBER})\s*([pPnNuUmMkKMGgµμ]?)(Ω|Ω|[Oo][Hh][Mm][Ss]?|[FfHhVvAaWw]|[Hh][Zz])")
_PREFIX = {"": 0, "p": -12, "P": -12, "n": -9, "N": -9, "u": -6, "U": -6,
           "µ": -6, "μ": -6, "m": -3, "k": 3, "K": 3, "M": 6, "G": 9, "g": 9}


def normalized_value(value: str | None) -> tuple[str, Decimal | str | None]:
    """Parse a complete quantity, preserving qualifiers as literal text.

    For example, 600Ω@100MHz must not silently become a 600Ω resistor.
    Unitless shorthand is kept as text because common_device has no R/C/L
    subtype and its designator is not reliable evidence of physical units.
    """
    if value is None:
        return ("missing", None)
    match = _VALUE.fullmatch(value.strip())
    if not match:
        return ("text", value.strip())
    number, prefix, unit = match.groups()
    dimension = "ohm" if unit in {"Ω", "Ω"} or unit.lower().startswith("ohm") else unit.lower()
    # Construct the SI decimal exactly, independent of ambient decimal precision.
    parts = Decimal(number).as_tuple()
    return dimension, Decimal((parts.sign, parts.digits, parts.exponent + _PREFIX[prefix]))


def compare_values(reference: str | None, generated: str | None) -> dict[str, object]:
    ref = normalized_value(reference)
    gen = normalized_value(generated)
    if reference is None and generated is None:
        status, equal = "not_applicable", None
    elif reference is None or generated is None:
        status, equal = "missing_value", False
    else:
        equal = ref == gen
        status = ("text_equal" if ref[0] == "text" else "equal") if equal else "different"
    return {
        "reference": reference, "generated": generated, "status": status, "equal": equal,
        "reference_normalized": {"dimension": ref[0], "value": str(ref[1]) if ref[1] is not None else None},
        "generated_normalized": {"dimension": gen[0], "value": str(gen[1]) if gen[1] is not None else None},
    }
