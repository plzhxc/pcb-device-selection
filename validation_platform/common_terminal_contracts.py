"""Versioned logical terminal contracts for additional abstract common devices.

These definitions describe chosen abstract devices, never package pin numbering,
ratings, internal conduction in a selected switch state, or reference case wiring.
Every terminal map is supplied explicitly and must cover both sets bijectively.
Unknown subtypes are reported with required parameters instead of guessed roles.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any

from .pin_contracts import decimal_number, evidence_errors

VERSION = "1.2"
DEFINITIONS = {
    "connector": {"parameters": ["contact_count", "contact_functions"], "terminals": "contact_1..contact_N", "role": "passive_terminal",
                  "intrinsic_required_connection": False,
                  "contact_usage": "explicit assigned|spare for every terminal; absent means assigned",
                  "contact_port_bindings": "assigned contacts map to declared external port and zero-based bit; spare contacts have no mapping",
                  "rated_contact_to_contact_voltage_v": "optional positive finite volts: continuous DC insulation rating required of all contact pairs; no active-input voltage range is implied",
                  "connection_policy": "assigned contacts must connect; explicitly spare contacts may remain open after interface coverage validation"},
    "test_point": {"parameters": ["signal_function"], "terminals": ["contact"], "role": "passive_terminal"},
    "touch_pad": {"parameters": ["signal_function"], "terminals": ["contact"], "role": "passive_terminal"},
    "diode": {"parameters": ["polarity"], "polarity": ["unidirectional"], "terminals": ["anode", "cathode"]},
    "led": {"parameters": ["polarity"], "polarity": ["unidirectional"], "terminals": ["anode", "cathode"]},
    "zener_diode": {"parameters": ["polarity"], "polarity": ["unidirectional"], "terminals": ["anode", "cathode"]},
    "tvs_diode": {"parameters": ["polarity"], "polarity": ["unidirectional", "bidirectional"], "terminals": "A/K for unidirectional; a/b for bidirectional"},
    "switch": {"parameters": ["contact_mode", "default_state"], "modes": {"spst": ["a", "b"], "spdt": ["common", "no", "nc"]}},
}
UNSUPPORTED_PARAMETERS = {
    "mosfet": ["channel_type", "terminal_mapping:G/D/S", "body_diode_and_kelvin_source_configuration"],
    "transistor": ["transistor_subtype", "terminal_mapping:B/C/E", "internal_bias_network"],
    "rgb_led": ["common_anode_or_cathode_or_independent_or_addressable", "channel_terminal_mapping", "supply_and_logic_interface"],
    "relay": ["coil_configuration", "internal_suppression_diode", "contact_groups", "terminal_mapping"],
    "optocoupler": ["input_emitter_configuration", "output_subtype", "isolation_domains", "terminal_mapping"],
    "module": ["exact_model", "complete_evidenced_terminal_table"],
    "display": ["exact_model", "interface_mode", "complete_evidenced_terminal_table"],
    "oscillator": ["oscillator_subtype", "power_and_output_configuration", "terminal_mapping"],
    "mechanical": ["electrical_terminal_presence", "complete_terminal_set"],
    "fiducial": ["electrical_terminal_presence", "complete_terminal_set"],
}


def build_common_terminal_contract(device_type: str, pin_ids: Sequence[str], *,
                                   terminal_mapping: Mapping[str, str] | None,
                                   parameters: Mapping[str, Any] | None,
                                   source_evidence: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """Build a logical contract or return explicit unresolved/fail diagnostics."""
    result: dict[str, Any] = {"template_id": f"pcbagent.common_terminal.{device_type}", "version": VERSION,
                              "pin_space": "logical", "pins": {}, "bindings": {}, "diagnostics": []}
    def add(code: str, message: str, *, status: str = "unresolved", **detail: Any) -> None:
        result["diagnostics"].append({"rule": code, "status": status, "message": message, "component_type": device_type, **detail})
    definition = DEFINITIONS.get(device_type)
    if definition is None:
        add("COMMON_SUBTYPE_UNRESOLVED", "No supported generic terminal contract for this subtype",
            required_parameters=UNSUPPORTED_PARAMETERS.get(device_type, ["subtype", "complete_evidenced_terminal_mapping"]))
        return result
    parameters = parameters if isinstance(parameters, Mapping) else {}
    missing = [k for k in definition["parameters"] if k not in parameters]
    if missing:
        add("COMMON_REQUIRED_PARAMETERS", "Required logical-device parameters are absent", required_parameters=missing)
        return result
    source_errors = evidence_errors(source_evidence, allow_input=True)
    if source_errors:
        add("COMMON_SELECTION_EVIDENCE", "Logical subtype/terminal choices lack attributable source evidence", reasons=source_errors)
    roles, functions, contact_usage = {}, {}, {}
    if device_type == "connector":
        count = parameters.get("contact_count")
        if type(count) is not int or not 1 <= count <= 1024:
            add("COMMON_CONTACT_COUNT", "contact_count must be a bounded positive integer", status="fail")
            return result
        roles = {f"contact_{i}": "passive_terminal" for i in range(1, count + 1)}
        functions = parameters.get("contact_functions")
        if not isinstance(functions, Mapping) or set(functions) != set(roles) or any(not isinstance(v, str) or not v.strip() for v in functions.values()):
            add("COMMON_CONTACT_FUNCTIONS", "Every contact needs an explicit intended signal/power function")
            return result
        contact_usage = parameters.get("contact_usage", {terminal: "assigned" for terminal in roles})
        if (not isinstance(contact_usage, Mapping) or set(contact_usage) != set(roles)
                or any(not isinstance(value, str) or value not in {"assigned", "spare"} for value in contact_usage.values())):
            add("COMMON_CONTACT_USAGE", "Every logical contact must explicitly be assigned or spare", status="fail")
            return result
        if "spare" in contact_usage.values() and not isinstance(parameters.get("contact_port_bindings"), Mapping):
            add("COMMON_CONTACT_PORT_BINDINGS", "Spare permission requires explicit bindings for every assigned contact")
        result["contact_usage"] = dict(contact_usage)
        result["contact_port_bindings"] = dict(parameters.get("contact_port_bindings", {})) if isinstance(parameters.get("contact_port_bindings"), Mapping) else {}
    elif device_type in {"test_point", "touch_pad"}:
        function = parameters.get("signal_function")
        if not isinstance(function, str) or not function.strip():
            add("COMMON_SIGNAL_FUNCTION", "Explicit terminal signal function is required")
            return result
        roles, functions = {"contact": "passive_terminal"}, {"contact": function}
    elif device_type == "switch":
        mode, state = parameters.get("contact_mode"), parameters.get("default_state")
        if mode == "spst" and state in {"open", "closed"}:
            roles = {"a": "switch_contact", "b": "switch_contact"}
        elif mode == "spdt" and state in {"no", "nc"}:
            roles = {"common": "switch_common", "no": "switch_no", "nc": "switch_nc"}
        else:
            add("COMMON_SWITCH_MODE", "Unsupported or inconsistent switch contact mode/default state")
            return result
        result["contact_mode"] = mode
        result["default_state"] = state
        result["scope"] = "terminal roles and declared contact mode only; no internal wire collapse or dynamic switch simulation"
    else:
        polarity = parameters.get("polarity")
        if polarity not in definition["polarity"]:
            add("COMMON_POLARITY", "Polarity mode must be explicitly supported")
            return result
        roles = {"anode": "diode_anode", "cathode": "diode_cathode"} if polarity == "unidirectional" else {"a": "passive_terminal", "b": "passive_terminal"}
        result["polarity"] = polarity
    if (not isinstance(terminal_mapping, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in terminal_mapping.items())
            or set(terminal_mapping) != set(pin_ids) or len(pin_ids) != len(set(pin_ids))
            or set(terminal_mapping.values()) != set(roles) or len(terminal_mapping) != len(roles)):
        add("COMMON_TERMINAL_BIJECTION", "Explicit logical pin-to-role map must bijectively cover the complete terminal set")
        return result
    canonical = json.dumps({"version": VERSION, "definition": definition}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    evidence = [{"source_kind": "template", "source_ref": result["template_id"] + "@" + VERSION,
                 "document_sha256": hashlib.sha256(canonical.encode()).hexdigest(), "quote": canonical,
                 "locator": f"validation_platform.common_terminal_contracts.DEFINITIONS.{device_type}"}]
    if device_type == "connector":
        rating = parameters.get("rated_contact_to_contact_voltage_v")
        if rating is not None and (type(rating) not in {int, float} or decimal_number(rating) is None or decimal_number(rating) <= 0):
            add("COMMON_INSULATION_RATING", "Contact-pair continuous DC rating must be a finite positive number", status="fail")
        result["voltage_obligation"] = {
            "kind": "connector_contact_insulation", "parameters": dict(parameters),
            "terminal_mapping": dict(terminal_mapping), "template_evidence": evidence,
        }
    for pid, terminal in terminal_mapping.items():
        role = roles[terminal]
        spare = device_type == "connector" and contact_usage.get(terminal) == "spare"
        result["pins"][pid] = {"semantic_role": role, "electrical_type": "passive", "capabilities": [role],
                               "modes": ["passive"], "resources": [], "required_connection": not spare, "allow_nc": spare,
                               "evidence": evidence, "logical_terminal": terminal}
        result["bindings"][pid] = {"selected_role": role, "usage": role, "mode": "passive", "resources": [],
                                   "signal_function": functions.get(terminal), "evidence": list(source_evidence)}
        if device_type == "connector":
            result["pins"][pid]["intrinsic_required_connection"] = False
            result["bindings"][pid]["contact_usage"] = contact_usage[terminal]
    result["terminal_mapping"] = dict(terminal_mapping)
    return result


def authorize_common_open_pins(spec: Mapping, pin_usage: Sequence[Mapping], ports: Mapping,
                               source_index: Mapping) -> dict[str, Any]:
    """Authorize only explicitly spare abstract connector terminals.

    Source indexes must come from the frozen input preparation stage. Returned
    permissions are conditional on aggregate external-port coverage and later
    verification of each assigned contact against actual compiled networks.
    They cannot authorize a core or a manufacturer's physical package pin.
    """
    result = {"allow_nc": {}, "diagnostics": [], "covered_port_bits": [], "port_bindings": [],
              "scope": "abstract connector contacts; declared plan only, actual-net verification still required"}
    def add(rule: str, message: str, status: str = "unresolved", **detail: Any) -> None:
        result["diagnostics"].append({"rule": rule, "status": status, "message": message, **detail})
    device = spec.get("device", {}) if isinstance(spec, Mapping) else {}
    if not isinstance(device, Mapping) or device.get("type") != "connector":
        return result
    if device.get("is_abstract") is not True or device.get("id") is not None:
        add("COMMON_OPEN_IDENTITY", "Only a declared abstract connector can use generic spare-contact permission", "fail")
        return result
    pins = device.get("pins", [])
    ids = [pin.get("id") for pin in pins if isinstance(pin, Mapping)] if isinstance(pins, list) else []
    if not ids or not all(isinstance(pid, str) and pid for pid in ids) or len(ids) != len(pins):
        add("COMMON_OPEN_PINS", "Complete explicit logical contact identifiers are required", "fail")
        return result
    evidence = []
    refs = spec.get("source_refs", [])
    if not isinstance(refs, list) or not refs:
        add("COMMON_OPEN_SOURCE", "Connector selection must cite the frozen input")
    for ref in refs if isinstance(refs, list) else []:
        indexed = source_index.get(ref) if isinstance(ref, str) else None
        if not isinstance(indexed, Mapping):
            add("COMMON_OPEN_SOURCE", "Unknown connector selection source", source_ref=ref)
            continue
        supplied = indexed.get("evidence")
        if isinstance(supplied, list) and not evidence_errors(supplied, allow_input=True):
            evidence.extend(supplied)
        elif indexed.get("kind") in {"requirement", "user_request"} and indexed.get("value") is not None:
            try:
                value = json.dumps(indexed["value"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            except (TypeError, ValueError):
                add("COMMON_OPEN_SOURCE", "Selection source must be finite JSON", source_ref=ref)
                continue
            evidence.append({"source_kind": "input", "source_ref": ref, "locator": ref,
                             "quote": value, "document_sha256": hashlib.sha256(value.encode()).hexdigest()})
        else:
            add("COMMON_OPEN_SOURCE", "Selection source lacks its actual input value or authenticated evidence", source_ref=ref)
    contract = build_common_terminal_contract("connector", ids, terminal_mapping=spec.get("terminal_mapping"),
        parameters=spec.get("parameters"), source_evidence=evidence)
    result["diagnostics"].extend(contract["diagnostics"])
    if not contract["pins"]:
        return result
    states, bindings = contract.get("contact_usage", {}), contract.get("contact_port_bindings", {})
    assigned = {terminal for terminal, state in states.items() if state == "assigned"}
    if set(bindings) != assigned:
        add("COMMON_ASSIGNED_PORT_COVERAGE", "Port bindings must cover exactly the assigned contacts", "fail")
    usages = {}
    for use in pin_usage:
        if not isinstance(use, Mapping):
            continue
        pid = use.get("pin")
        if not isinstance(pid, str):
            add("COMMON_OPEN_USAGE_PIN", "Contact usage must identify an exact logical pin", "fail")
            continue
        if pid in usages:
            add("COMMON_OPEN_USAGE_DUPLICATE", "Duplicate contact usage", "fail", pin=pid)
        usages[pid] = use
    if set(usages) != set(ids):
        add("COMMON_OPEN_USAGE_COVERAGE", "Usage must cover every connector contact exactly once", "fail")
    reverse = {terminal: pid for pid, terminal in contract["terminal_mapping"].items()}
    for terminal, state in states.items():
        pid = reverse[terminal]; use = usages.get(pid, {})
        if use.get("selected_role") != "passive_terminal":
            add("COMMON_CONTACT_ROLE", "A connector contact is passive; signal purpose is a separate function", "fail", pin=pid)
        if type(use.get("unconnected")) is not bool or use["unconnected"] is not (state == "spare"):
            add("COMMON_CONTACT_STATE", "Selected connectivity conflicts with assigned/spare contact state", "fail", pin=pid)
        if not isinstance(use.get("source_refs"), list) or not use["source_refs"] or any(not isinstance(ref, str) or ref not in source_index for ref in use["source_refs"]):
            add("COMMON_CONTACT_USAGE_SOURCE", "Every selected contact purpose requires attributable source references", pin=pid)
        if state == "spare":
            continue
        endpoint = bindings.get(terminal)
        if (not isinstance(endpoint, Mapping) or set(endpoint) != {"port", "bit"}
                or not isinstance(endpoint.get("port"), str) or type(endpoint.get("bit")) is not int):
            add("COMMON_CONTACT_PORT", "Assigned contact requires a closed port/bit object", "fail", pin=pid)
            continue
        port = ports.get(endpoint["port"], {})
        if (not isinstance(port, Mapping) or port.get("external") is not True
                or type(port.get("width")) is not int or not 0 <= endpoint["bit"] < port["width"]):
            add("COMMON_CONTACT_PORT", "Assigned contact references an unknown external port or bit", "fail", pin=pid)
            continue
        if (not isinstance(port.get("source_refs"), list) or not port["source_refs"]
                or any(not isinstance(ref, str) or ref not in source_index for ref in port["source_refs"])):
            add("COMMON_CONTACT_PORT_SOURCE", "Selected external port purpose requires attributable input references", pin=pid)
        result["port_bindings"].append({"pin": pid, "port": endpoint["port"], "bit": endpoint["bit"]})
        result["covered_port_bits"].append({"port": endpoint["port"], "bit": endpoint["bit"]})
    if not result["diagnostics"]:
        result["allow_nc"] = {pid: contract["pins"][pid]["allow_nc"] for pid in ids}
    return result


def check_common_port_coverage(authorizations: Sequence[Mapping], ports: Mapping) -> list[dict]:
    """Check aggregate declared connector coverage without guessing port names.

    Multiple contacts may intentionally share one supply/ground port. This is
    endpoint coverage only; source-required interface inventory and actual nets
    must still be checked by the caller.
    """
    invalid = [port for port, declaration in ports.items() if isinstance(declaration, Mapping)
               and declaration.get("external") is True and (type(declaration.get("width")) is not int or not 1 <= declaration["width"] <= 1024)]
    if invalid:
        return [{"rule": "COMMON_EXTERNAL_PORT_SCHEMA", "status": "unresolved", "message": "Explicit bounded external port widths are required", "ports": invalid}]
    expected = {(port, bit) for port, declaration in ports.items() if isinstance(declaration, Mapping)
                and declaration.get("external") is True and type(declaration.get("width")) is int
                for bit in range(declaration["width"])}
    covered = {(row.get("port"), row.get("bit")) for authorization in authorizations
               if not authorization.get("diagnostics") for row in authorization.get("covered_port_bits", [])}
    missing = sorted(expected - covered)
    return [] if not missing else [{"rule": "COMMON_EXTERNAL_PORT_COVERAGE", "status": "unresolved",
        "message": "Declared external port bits lack assigned connector contact coverage",
        "missing": [{"port": port, "bit": bit} for port, bit in missing]}]


def check_common_port_nets(authorization: Mapping, *, module: str, device: str,
                           component: str, netlist: Mapping, networks: Sequence[Mapping]) -> list[dict]:
    """Verify declared assigned contacts against compiler ports and real pins."""
    diagnostics = []
    actual = netlist.get(component, {})
    pins = {p.get("id"): p.get("net") for p in actual.get("pins", []) if isinstance(p, Mapping)}
    for binding in authorization.get("port_bindings", []):
        port_nets = {network.get("id") for network in networks if any(
            member.get("kind") == "port" and member.get("module") == module
            and member.get("port") == binding["port"] and member.get("bit") == binding["bit"]
            for member in network.get("members", []) if isinstance(member, Mapping))}
        pin_nets = {network.get("id") for network in networks if any(
            member.get("kind") == "pin" and member.get("module") == module
            and member.get("device") == device and member.get("pin") == binding["pin"]
            for member in network.get("members", []) if isinstance(member, Mapping))}
        actual_net = pins.get(binding["pin"])
        if not actual_net or port_nets != {actual_net} or pin_nets != {actual_net}:
            diagnostics.append({"rule": "COMMON_ASSIGNED_PORT_NET", "status": "fail",
                "message": "Assigned connector contact and declared external port bit must share the actual compiled net",
                "module": module, "device": device, "component": component, **binding,
                "actual_pin_net": actual_net, "manifest_port_nets": sorted(port_nets, key=str),
                "manifest_pin_nets": sorted(pin_nets, key=str)})
    return diagnostics


__all__ = ["VERSION", "DEFINITIONS", "UNSUPPORTED_PARAMETERS", "build_common_terminal_contract",
           "authorize_common_open_pins", "check_common_port_coverage", "check_common_port_nets"]
