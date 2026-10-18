"""Instantiate explicit physical pins through the existing bounded SCL compiler."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import keyword
from typing import Any

from ..netlist.scl import SCLSource, compile_scl
from .applicability import assess
from .models import Fragment, Target, canonical_digest


def instantiate_fragment(
    fragment: Fragment, target: Target, context: Mapping[str, Any], *,
    module: str, device_alias: str, port_bindings: Mapping[str, str],
    accepted_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Create connection statements without adding devices, values or NC states.

    The caller owns the frozen catalog, target and context. A catalog digest
    establishes payload integrity, not that an annotation is factually correct.
    Every boundary port must be bound explicitly, with no implicit net merging.
    """
    decision = assess(fragment, target, accepted_digests=accepted_digests)
    result = {"ok": False, "assessment": decision, "source": None,
              "target_sha256": canonical_digest(target.model_dump(mode="json")),
              "context_sha256": None, "diagnostics": []}
    if decision["status"] != "applicable":
        return result
    errors = result["diagnostics"]
    try:
        result["context_sha256"] = canonical_digest(context)
    except (TypeError, ValueError, OverflowError):
        errors.append("context must contain finite JSON values")
        return result
    if (not isinstance(context, Mapping) or not isinstance(context.get("modules"), Mapping)
            or not isinstance(module, str) or not isinstance(device_alias, str)
            or not isinstance(port_bindings, Mapping)):
        errors.append("context modules and boundary bindings must be objects with string module/device IDs")
        return result
    raw_module = context["modules"].get(module)
    if (not isinstance(raw_module, Mapping) or not isinstance(raw_module.get("devices"), Mapping)
            or not isinstance(raw_module.get("ports"), Mapping)):
        errors.append("selected module must declare device and port objects")
        return result
    device = raw_module["devices"].get(device_alias)
    if not isinstance(device, Mapping):
        errors.append("selected device must exist in the frozen module")
        return result
    if (not isinstance(device_alias, str) or not device_alias.isidentifier()
            or keyword.iskeyword(device_alias) or device_alias.startswith("_")
            or device_alias in {"pin", "module_port", "connect", "voltage_domain", "voltage_domain_ref"}):
        errors.append("invalid or reserved device alias")
    if device.get("name") != target.device.model:
        errors.append("frozen context device name must equal the target model")
    raw_pins = device.get("pins", [])
    if (not isinstance(raw_pins, list)
            or any(not isinstance(p, Mapping) or not isinstance(p.get("id"), str)
                   or not isinstance(p.get("name"), str) for p in raw_pins)
            or len({p.get("id") for p in raw_pins}) != len(raw_pins)
            or {p.get("id"): p.get("name") for p in raw_pins} != target.pin_catalog):
        errors.append("complete frozen context pin catalog differs from target")
    if (set(port_bindings) != set(fragment.ports)
            or any(not isinstance(p, str) or not p for p in port_bindings.values())
            or len(set(port_bindings.values())) != len(port_bindings)):
        errors.append("boundary ports require a complete one-to-one explicit binding")
    ports = raw_module.get("ports", {})
    for port in port_bindings.values():
        if (not isinstance(port, str) or not isinstance(ports.get(port), Mapping)
                or type(ports[port].get("width")) is not int or ports[port]["width"] != 1):
            errors.append("every bound boundary must exist as a scalar module port")
    if errors:
        return result
    pin_map = {pin.logical: pin.physical for pin in fragment.pins}
    quote = lambda text: json.dumps(text, ensure_ascii=False)

    def expression(endpoint: str) -> str:
        kind, name = endpoint.split(":", 1)
        if kind == "pin":
            return f"pin({device_alias}, {quote(pin_map[name])})"
        return f"module_port({quote(module)}, {quote(port_bindings[name])})"

    lines, lineage = [], []
    for connection in fragment.connections:
        first, *rest = connection.endpoints
        for endpoint in rest:
            lines.append(f"connect({expression(first)}, {expression(endpoint)})")
            lineage.append({"line": len(lines), "connection_id": connection.id,
                            "kind": connection.kind, "evidence": list(connection.evidence)})
    result.update(ok=True, source={"name": f"application:{fragment.id}", "module": module,
                                   "text": "\n".join(lines) + "\n"},
                  physical_pin_mapping=pin_map, port_bindings=dict(port_bindings), lineage=lineage)
    return result


def _connectivity_errors(fragment: Fragment, compilation: Mapping, *, module: str,
                         device_alias: str, port_bindings: Mapping[str, str]) -> list[str]:
    """Independently read the exported compiler partition for split/short errors."""
    networks = {}
    for network in compilation["manifest"]["networks"]:
        for endpoint in network["members"]:
            if endpoint["kind"] == "pin":
                key = ("pin", endpoint["module"], endpoint["device"], endpoint["pin"])
            elif endpoint["kind"] == "port":
                key = ("port", endpoint["module"], endpoint["port"], endpoint["bit"])
            else:
                continue
            networks[key] = network["id"]
    pins = {p.logical: p.physical for p in fragment.pins}
    errors, previous = [], {}
    for connection in fragment.connections:
        groups = set()
        for endpoint in connection.endpoints:
            kind, name = endpoint.split(":", 1)
            key = (("pin", module, device_alias, pins[name]) if kind == "pin" else
                   ("port", module, port_bindings[name], 0))
            groups.add(networks.get(key))
        if None in groups or len(groups) != 1:
            errors.append(f"connection {connection.id} is absent or split in compiled partition")
        else:
            group = next(iter(groups))
            if group in previous:
                errors.append(f"independent connections {previous[group]} and {connection.id} were shorted")
            previous[group] = connection.id
    return errors


def compile_application(
    fragment: Fragment, target: Target, context: Mapping[str, Any], *,
    module: str, device_alias: str, port_bindings: Mapping[str, str],
    accepted_digests: Mapping[str, str], support_sources: Sequence[SCLSource] = (),
) -> dict[str, Any]:
    """Compile a partial application in caller-owned context, with readback.

    Pins outside the fragment still require caller-supplied connections or
    explicit context states. This function never invents those states. Support
    sources may complete a circuit but may not short independent fragment nets.
    """
    result = instantiate_fragment(fragment, target, context, module=module,
        device_alias=device_alias, port_bindings=port_bindings, accepted_digests=accepted_digests)
    result.update(scope="partial_application_fragment", electrical_validation="not_performed",
                  source_semantic_review="caller_supplied_annotations", compilation=None)
    if not result["ok"]:
        return result
    source = SCLSource(**result["source"])
    compiled = compile_scl([source, *support_sources], context).to_dict()
    result["compilation"] = compiled
    if not compiled["ok"]:
        result["ok"] = False
        result["diagnostics"].extend(compiled["diagnostics"])
        return result
    errors = _connectivity_errors(fragment, compiled, module=module, device_alias=device_alias,
                                  port_bindings=port_bindings)
    if errors:
        result["ok"] = False
        result["diagnostics"].extend(errors)
        # A valid SCL program can still violate the selected fragment partition.
        # Do not expose that netlist as a ready result.
        compiled["netlist"] = None
        compiled["ok"] = False
        compiled["manifest"]["status"] = "fragment_connectivity_rejected"
    result["connectivity_readback"] = "pass" if not errors else "fail"
    return result
