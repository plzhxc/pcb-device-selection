"""Resolve logical primitive terminals against one immutable builtin definition.

Names and array order never supply a pin mapping. The only implicit mappings are
identical template port IDs and the explicitly supplied versioned 1=a, 2=b
convention; an invalid explicit map never falls back to either convention.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence


def primitive_terminal_mapping(definition: Mapping, pin_ids: Sequence[str], declared: Mapping | None,
                               *, numeric_ab: Mapping | None = None,
                               parameters: Mapping | None = None) -> tuple[dict, list[dict]]:
    ports = definition.get("ports", [])
    canonical = [p.get("id") for p in ports if isinstance(p, Mapping)] if isinstance(ports, list) else []
    details = {"template_id": definition.get("template_id"), "version": definition.get("version"),
               "actual_pin_ids": list(pin_ids), "expected_terminal_ids": canonical,
               "declared_terminal_mapping": dict(declared) if isinstance(declared, Mapping) else declared}
    def reject(message):
        return {}, [{"code": "COMMON_TERMINAL_MAPPING", "message": message, **details}]
    if (not canonical or len(canonical) != len(ports)
            or any(not isinstance(p, str) or not p for p in canonical) or len(set(canonical)) != len(canonical)):
        return reject("This primitive requires a complete unique template port inventory")
    vector = any("width_parameter" in p or p.get("width", 1) != 1 for p in ports)
    if vector:
        from .compiler import _endpoints
        from .errors import CommonDeviceError
        if parameters is not None and not isinstance(parameters, Mapping):
            return reject("Vector terminal widths require explicit parameter values")
        try:
            canonical = [terminal for _, _, terminal in _endpoints(definition, parameters or {})]
        except (CommonDeviceError, TypeError, KeyError) as exc:
            return reject(str(exc))
        details["expected_terminal_ids"] = canonical
        if declared is None:
            return reject("Vector ports require an explicit complete logical-pin to port[index] mapping; no channel pairing is inferred")
    elif any(type(p.get("width", 1)) is not int or p.get("width", 1) != 1 for p in ports):
        return reject("Scalar template widths must be integer one")
    if (any(not isinstance(pin, str) or not pin for pin in pin_ids)
            or len(pin_ids) != len(set(pin_ids))):
        return reject("Logical device pin IDs must be nonempty and unique")
    mapping = declared
    if mapping is None:
        if set(pin_ids) == set(canonical):
            mapping = {pin: pin for pin in pin_ids}
        elif (set(canonical) == {"a", "b"} and set(pin_ids) == {"1", "2"}
              and numeric_ab == {"1": "a", "2": "b"}):
            mapping = numeric_ab
        else:
            return reject("Supply an explicit logical-pin to canonical-template-terminal mapping; names/order are not mappings")
    if (not isinstance(mapping, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k,v in mapping.items())
            or set(mapping) != set(pin_ids) or set(mapping.values()) != set(canonical)
            or len(mapping) != len(canonical) or len(set(mapping.values())) != len(mapping)):
        return reject("terminal_mapping keys must exactly cover device.pins[].id and values must bijectively cover the template terminal IDs; do not reverse the mapping or use display names")
    return dict(mapping), []


__all__ = ["primitive_terminal_mapping"]
