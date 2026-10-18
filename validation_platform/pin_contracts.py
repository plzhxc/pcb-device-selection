"""Evidence-bearing, frozen pin contracts and deterministic static checks.

Contracts are supplied by the caller; this module never fetches or writes data.
``components`` is keyed by graph component ID (gge...), each containing
``supplier_part``, ``pins`` keyed by exact physical pin number, and ``bindings``
keyed by the same number. Pin facts may specify semantic_role, capabilities,
electrical_type, modes, required_connection, allow_nc, voltage_min_v,
voltage_max_v, and evidence. Bindings specify usage, mode, domain, resources.
``domains`` maps IDs to {net, voltage_v, evidence}. Evidence includes source_kind,
source_ref, document_sha256, quote and a one-based page or locator. A valid
reference means attributable supplied evidence, not independent PDF verification.
Missing facts are unresolved; passing these checks is not functional simulation.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from .netlist_graph import NetlistGraph

SCHEMA = "pcbagent.pin_contracts.v1"
PAPER_ROLES = frozenset("supply_vdd supply_gnd primary_vdd primary_gnd secondary_vdd secondary_gnd "
    "sense_plus sense_minus out out_plus out_minus logic_in logic_out mosfet_gate mosfet_drain "
    "mosfet_source mosfet_kelvin_source buck_vin buck_gnd buck_sw buck_fb buck_en buck_boot "
    "halfbridge_hb halfbridge_hs gate_ho gate_lo xfmr_primary xfmr_secondary passive_terminal "
    "diode_anode diode_cathode".split())
PROJECT_ROLES = frozenset({"logic_io", "clock_in", "clock_out", "oscillator_in", "oscillator_out", "no_connect",
                           "switch_contact", "switch_common", "switch_no", "switch_nc",
                           "bjt_base", "bjt_collector", "bjt_emitter",
                           "feedback_input", "parameter_control"})
ELECTRICAL_TYPES = frozenset({"input", "push_pull_output", "open_drain", "open_source", "tri_state", "bidirectional", "passive", "power_in", "power_out", "ground", "no_connect", "analog_input", "analog_output"})
POWER_ROLES = frozenset({"supply_vdd", "supply_gnd", "primary_vdd", "primary_gnd", "secondary_vdd", "secondary_gnd", "buck_vin", "buck_gnd"})
GROUND_ROLES = frozenset({"supply_gnd", "primary_gnd", "secondary_gnd", "buck_gnd"})
_SHA = re.compile(r"^[0-9a-f]{64}$")
# Generic output records direction only; it never establishes a driver subtype.
_OUTPUT_MODES = {"output", "push_pull_output", "open_drain", "open_source", "tri_state"}


def role_mode_compatible(role: Any, mode: Any) -> bool | None:
    """Small explicit role/mode matrix; no inferred driver type for other roles."""
    if role in {"logic_in", "clock_in"}:
        return mode == "input"
    if role in {"logic_out", "clock_out"}:
        return mode in _OUTPUT_MODES
    if role == "logic_io":
        return mode in _OUTPUT_MODES | {"input", "bidirectional", "high_impedance", "quasi_bidirectional"}
    if role == "no_connect":
        return mode == "no_connect"
    return None


def decimal_number(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def evidence_errors(value: Any, *, allow_input: bool = False) -> list[str]:
    """Validate source references only; callers own provenance authentication."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        return ["nonempty evidence array required"]
    errors = []
    kinds = {"datasheet", "manufacturer_application_note", "manufacturer_page", "manufacturer_image", "template", "manufacturer_datasheet_revision"}
    if allow_input:
        kinds.add("input")
    for index, row in enumerate(value):
        prefix = f"evidence[{index}]"
        if not isinstance(row, Mapping):
            errors.append(f"{prefix}: object required")
            continue
        if row.get("source_kind") not in kinds:
            errors.append(f"{prefix}: unsupported source kind")
        for key in ("source_ref", "quote"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                errors.append(f"{prefix}: {key} required")
        if not _SHA.fullmatch(str(row.get("document_sha256", ""))):
            errors.append(f"{prefix}: document_sha256 required")
        if row.get("source_kind") == "manufacturer_datasheet_revision":
            if (row.get("source_authentication_version") != 3
                    or row.get("document_role") != "manufacturer_datasheet_revision"
                    or row.get("claim_scope") != "explicitly_reviewed_fields"
                    or not isinstance(row.get("reviewed_claim_scopes"), list) or not row["reviewed_claim_scopes"]
                    or any(not _SHA.fullmatch(str(row.get(key, ""))) for key in
                        ("source_selection_sha256", "applicability_receipt_sha256", "canonical_primary_document_sha256"))):
                errors.append(f"{prefix}: explicit revision selection and scoped auth3 provenance required")
        if row.get("source_kind") in {"datasheet", "manufacturer_application_note", "manufacturer_datasheet_revision"}:
            if type(row.get("page")) is not int or row["page"] < 1:
                errors.append(f"{prefix}: one-based page required")
        elif not isinstance(row.get("locator"), str) or not row["locator"].strip():
            errors.append(f"{prefix}: locator required")
    return errors


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def component_contract(contracts: Mapping[str, Any] | None, component_id: str) -> Mapping[str, Any]:
    return _mapping(_mapping(_mapping(contracts).get("components")).get(component_id))


def pin_fact(contracts: Mapping[str, Any] | None, component_id: str, pin_id: str) -> Mapping[str, Any]:
    return _mapping(_mapping(component_contract(contracts, component_id).get("pins")).get(pin_id))


def pin_binding(contracts: Mapping[str, Any] | None, component_id: str, pin_id: str) -> Mapping[str, Any]:
    return _mapping(_mapping(component_contract(contracts, component_id).get("bindings")).get(pin_id))


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and x for x in value) and len(value) == len(set(value))


def evidence_field_scope_errors(refs: Any, fields: list[str]) -> list[str]:
    """Keep independently reviewed image claims within their explicit scope."""
    if not isinstance(fields, list) or not all(isinstance(field, str) and field for field in fields):
        return ["explicit field names are required for source-scope validation"]
    issues = []
    for raw in refs if isinstance(refs, list) else []:
        evidence = _mapping(raw)
        image_fields = (evidence.get("source_kind") == "manufacturer_image"
            and evidence.get("provenance") == "reviewed_manufacturer_image_fields"
            and evidence.get("source_authentication_version") == 3
            and evidence.get("claim_scope") == "explicitly_reviewed_fields"
            and _strings(evidence.get("reviewed_claim_scopes"))
            and bool(evidence["reviewed_claim_scopes"])
            and set(evidence["reviewed_claim_scopes"]) <= {"semantic_role", "capabilities"}
            and all(isinstance(evidence.get(k), str) and _SHA.fullmatch(evidence[k])
                for k in ("review_receipt_sha256", "parent_html_sha256", "transcript_sha256", "catalog_identity_sha256"))
            and evidence.get("source_ref") == f"manufacturer_image:{evidence.get('document_sha256')}:review:{evidence.get('review_receipt_sha256')}")
        if ((evidence.get("source_kind") in {"manufacturer_image", "local_symbol"} and not image_fields)
                or evidence.get("claim_scope") == "pin_number_and_label_only") and not set(fields) <= {"pin_number", "pin_name"}:
            issues.append("pin-number/label evidence cannot authorize electrical facts")
        if evidence.get("source_kind") == "manufacturer_application_note":
            if (evidence.get("source_authentication_version") != 3
                    or evidence.get("claim_scope") != "explicitly_reviewed_fields"
                    or not evidence.get("document_role") or not _mapping(evidence.get("applicability"))):
                issues.append("application note requires authenticated device applicability and explicit reviewed fields")
        if evidence.get("provenance") == "reviewed_original_pdf_transcription" or evidence.get("claim_scope") == "explicitly_reviewed_fields":
            scope = evidence.get("reviewed_claim_scopes")
            if (evidence.get("source_authentication_version") != 3
                    or evidence.get("claim_scope") != "explicitly_reviewed_fields"
                    or not _strings(scope) or not set(fields) <= set(scope)
                    or evidence.get("provenance") == "reviewed_original_pdf_transcription" and any(
                        not isinstance(evidence.get(k), str) or not re.fullmatch(r"[0-9a-f]{64}", evidence[k])
                        for k in ("review_receipt_sha256", "rendered_image_sha256"))):
                issues.append("reviewed source does not authorize the requested field scope")
    return issues


def pin_evidence_scope_errors(fact: Mapping) -> list[str]:
    """Pinout-image and CAD identity cannot grant electrical capabilities."""
    issues = []
    for field in ("semantic_role", "electrical_type", "capabilities", "modes", "required_connection", "allow_nc", "voltage_min_v", "voltage_max_v", "resources", "gpio_remap_safe", "logic_levels", "application_interface_profile"):
        if fact.get(field) is None:
            continue
        refs = _mapping(fact.get("field_evidence")).get(field, fact.get("evidence"))
        issues.extend(f"{field}: {issue}" for issue in evidence_field_scope_errors(refs, [field]))
    return issues


def _check_pin_contracts_single(graph: NetlistGraph, contracts: Mapping[str, Any] | None) -> dict[str, Any]:
    """Check full physical pins, roles/modes, NC, obligations and voltage domains.

``constraints`` optionally supplies fixed same_net/different_net/connected rules
with endpoints [{component, pin}], evidence, and applies=True. Unknown predicates
or applicability remain unresolved. Modes describe a selected static scenario;
simultaneously active push-pull outputs on one net fail, unknown modes do not pass.
"""
    from .conditional_pin_rules import evaluate_pin_rules, has_terminal_voltage_predicate, TerminalVoltageContext
    from .logic_interfaces import documented_logic_direction, check_external_logic
    from .passive_voltage import check_passive_voltage

    contracts = _mapping(contracts)
    rows: list[dict[str, Any]] = []

    def add(rule: str, status: str, message: str, *, component: str | None = None,
            pin: str | None = None, **detail: Any) -> None:
        rows.append({"rule": rule, "status": status, "message": message,
                     "component": component, "pin": pin, **detail})

    if contracts.get("schema") != SCHEMA:
        add("CONTRACT_SCHEMA", "unresolved", "Expected frozen pcbagent.pin_contracts.v1 schema")
    for diagnostic in contracts.get("binding_diagnostics", []):
        if isinstance(diagnostic, Mapping):
            rows.append({**dict(diagnostic), "status": diagnostic.get("status") if diagnostic.get("status") in {"fail", "unresolved", "pass", "not_applicable"} else "unresolved"})
    domains = _mapping(contracts.get("domains"))
    terminal_voltage_context = (TerminalVoltageContext(graph, contracts) if any(
        has_terminal_voltage_predicate(_mapping(pin).get("conditional_rules"))
        for cc in _mapping(contracts.get("components")).values()
        for pin in _mapping(_mapping(cc).get("pins")).values()) else None)
    valid_facts: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    valid_bindings: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    valid_directions: dict[tuple[str, str, str], str] = {}
    unused_outputs = set()
    passive_voltage_components = []
    for key, component in sorted(graph.components.items()):
        cid = key[1]
        contract = component_contract(contracts, cid)
        facts = _mapping(contract.get("pins"))
        actual = graph.pin_map(key)
        component_voltage_check = (component.is_abstract is True and
            contract.get("pin_space") == "logical" and component.type in {"capacitor", "connector"})
        if component_voltage_check:
            passive_voltage_components.append((component, actual, contract))
        if not contract:
            add("COMPONENT_CONTRACT", "unresolved", "No contract for component", component=cid)
        elif component.type == "module" and contract.get("fixed_identity") and not component.is_core:
            add("FUNCTIONAL_IDENTITY_ANCHOR", "unresolved", "Apply the authenticated functional identity overlay before checking physical module contracts", component=cid)
            facts = {}
        elif component.is_core and (not component.anchor_id or contract.get("supplier_part") != component.anchor_id):
            add("COMPONENT_IDENTITY", "fail", "Contract identity differs from physical part", component=cid)
            facts = {}
        else:
            if contract.get("component_type") and contract["component_type"] != component.type:
                add("COMPONENT_TYPE", "fail", "Contract component type differs", component=cid)
                facts = {}
            missing, extra = sorted(facts.keys() - actual.keys()), sorted(actual.keys() - facts.keys())
            pin_set_status = "fail" if missing or (extra and contract.get("pins_complete") is True) else "unresolved" if extra else "pass"
            add("LOGICAL_TERMINAL_SET" if contract.get("pin_space") == "logical" else "PHYSICAL_PIN_SET", pin_set_status,
                "Pin or terminal set must equal the complete contract", component=cid,
                missing_in_netlist=missing, absent_from_contract=extra)
        for pid, pin in sorted(actual.items()):
            fact = _mapping(facts.get(pid))
            binding = pin_binding(contracts, cid, pid)
            conditional = evaluate_pin_rules(fact, binding, domains=domains,
                supplies=_mapping(contract.get("supplies")), component_bindings=_mapping(contract.get("bindings")),
                supplier_part=component.anchor_id, pin_number=pid,
                terminal_voltage_context=terminal_voltage_context, component_id=cid,
                pin_nets={n: p.net[1] if p.net else None for n, p in actual.items()})
            fact = conditional["fact"]
            reviewed_ground_tie = any(
                diagnostic.get("effect_kind") == "pin_relation" and diagnostic.get("relation") == "same_net"
                and diagnostic.get("status") == "pass" and pid in diagnostic.get("pins", []) and any(
                    other != pid and _mapping(facts.get(other)).get("semantic_role") in GROUND_ROLES
                    and _mapping(facts.get(other)).get("electrical_type") == "ground"
                    and not evidence_errors(_mapping(facts.get(other)).get("evidence"))
                    for other in diagnostic.get("pins", []))
                for diagnostic in conditional["diagnostics"])
            has_peer = bool(graph.connected_pins(pin.key)) or (pin.net is not None and binding.get("external_endpoint") is True)
            permitted_unconnected = not has_peer and fact.get("allow_nc") is True and fact.get("required_connection") is not True and binding.get("unconnected") is not False
            for diagnostic in conditional["diagnostics"]:
                # Only a valid reviewed rule with an unknown *condition* can
                # become irrelevant through the actual physical state. Invalid
                # scope/review/source/schema diagnostics remain unresolved.
                if diagnostic.get("reasons") == ["selected operating condition is unknown"] and (
                        has_peer and diagnostic.get("effect_kind") == "connection_policy" or
                        permitted_unconnected and diagnostic.get("effect_kind") == "voltage_range"):
                    diagnostic = {**diagnostic, "status": "not_applicable",
                                  "applicability": "Actual connection state excludes this check; its unknown fact is not accepted"}
                rows.append({"component": cid, "pin": pid, "message": "Conditional device fact evaluated in the selected operating scenario", **diagnostic})
            evidence = evidence_errors(fact.get("evidence")) + pin_evidence_scope_errors(fact)
            if evidence:
                add("PIN_EVIDENCE", "unresolved", "Pin facts lack attributable evidence", component=cid, pin=pid, reasons=evidence)
                continue
            role = fact.get("semantic_role")
            if role not in PAPER_ROLES | PROJECT_ROLES:
                add("PIN_ROLE", "unresolved", "Unknown or unsupported role", component=cid, pin=pid, semantic_role=role)
                continue
            valid_facts[pin.key] = fact
            add("PIN_ROLE", "pass", "Role has supplied evidence", component=cid, pin=pid)
            supported = fact.get("capabilities")
            logic_direction = documented_logic_direction(fact)
            unused_output = (permitted_unconnected and binding.get("unconnected") is True and
                             role in {"logic_out", "clock_out"} and _strings(supported) and binding.get("usage") in supported)
            if unused_output:
                unused_outputs.add(pin.key)
                add("PIN_ELECTRICAL_TYPE", "not_applicable", "A proven unused output drives no external network; no driver subtype is inferred", component=cid, pin=pid)
            elif logic_direction and fact.get("electrical_type") is None:
                add("PIN_ELECTRICAL_TYPE", "not_applicable", "Reviewed fixed logic direction and level guarantees support static interface checks; driver subtype remains unknown", component=cid, pin=pid)
            elif fact.get("electrical_type") not in ELECTRICAL_TYPES:
                add("PIN_ELECTRICAL_TYPE", "unresolved", "Electrical type is unknown or unsupported", component=cid, pin=pid)
            usage, mode = binding.get("usage"), binding.get("mode")
            selected_role = binding.get("selected_role")
            if "selected_role" in binding:
                if selected_role not in PAPER_ROLES | PROJECT_ROLES:
                    add("PIN_SELECTED_ROLE", "unresolved", "Selected role is unknown or outside the supported vocabulary", component=cid, pin=pid)
                else:
                    compatible = selected_role == role or (role == "logic_io" and selected_role in {"logic_in", "logic_out"})
                    add("PIN_SELECTED_ROLE", "pass" if compatible else "fail", "Selected role must refine a supported factual role", component=cid, pin=pid)
            modes = fact.get("modes")
            if not usage or not _strings(supported) or not supported:
                add("PIN_USAGE", "unresolved", "Usage or supported capabilities missing", component=cid, pin=pid)
            else:
                add("PIN_USAGE", "pass" if usage in supported else "fail", "Selected use must be supported", component=cid, pin=pid)
            if unused_output and not (mode and _strings(modes) and modes and mode not in modes):
                add("PIN_MODE", "not_applicable", "Explicitly unused output has an applicable NC permission; unknown drive mode remains unknown", component=cid, pin=pid)
            elif logic_direction and mode is None and modes is None and _strings(supported) and usage in supported:
                valid_directions[pin.key] = logic_direction
                add("PIN_MODE", "not_applicable", "Fixed documented direction does not require guessing an unknown output driver subtype for the bounded static interface check", component=cid, pin=pid)
            elif not mode or not _strings(modes) or not modes:
                add("PIN_MODE", "unresolved", "Selected mode or supported modes missing", component=cid, pin=pid)
            else:
                status = "pass" if mode in modes else "fail"
                add("PIN_MODE", status, "Selected static mode must be supported", component=cid, pin=pid)
                if status == "pass" and _strings(supported) and usage in supported:
                    valid_bindings[pin.key] = binding
                    if mode == "output" and role_mode_compatible(selected_role or role, mode) is True:
                        valid_directions[pin.key] = "output"
                role_compatible = role_mode_compatible(role, mode)
                if role_compatible is not None:
                    add("PIN_ROLE_MODE", "pass" if role_compatible else "fail", "Selected role and mode must agree", component=cid, pin=pid)
                selected_compatible = role_mode_compatible(selected_role, mode)
                if selected_compatible is False:
                    add("PIN_SELECTED_ROLE_MODE", "fail", "Selected role and mode conflict", component=cid, pin=pid)
            resources = binding.get("resources")
            supported_resources = fact.get("resources")
            if any(r["check"] == "resources" for r in conditional["not_applicable"]):
                add("PIN_RESOURCES", "not_applicable", "Reviewed selected-scenario rule excludes shared resources", component=cid, pin=pid)
            elif unused_output and resources in (None, []):
                add("PIN_RESOURCES", "not_applicable", "A proven unused output with no external peer and no selected resource makes no mux allocation claim; source resource facts remain unchanged", component=cid, pin=pid)
            elif (logic_direction or reviewed_ground_tie) and _strings(supported) and len(supported) == 1 and resources in (None, []) and supported_resources is None:
                add("PIN_RESOURCES", "not_applicable", "A single fixed documented function claims no mux-resource allocation", component=cid, pin=pid)
            elif role in POWER_ROLES and fact.get("electrical_type") in {"ground", "power_in", "power_out"} and mode in {"ground", "power_in", "power_out"} and resources in (None, []) and supported_resources is None:
                add("PIN_RESOURCES", "not_applicable", "A selected supply/reference connection claims no digital mux resource; unknown resource facts are retained", component=cid, pin=pid)
            elif resources is not None or supported_resources is not None:
                if not _strings(resources) or not _strings(supported_resources):
                    add("PIN_RESOURCES", "unresolved", "Resource facts or assignments are incomplete", component=cid, pin=pid)
                else:
                    add("PIN_RESOURCES", "pass" if set(resources) <= set(supported_resources) else "fail", "Selected resources must be supported", component=cid, pin=pid)
            required, allow_nc = fact.get("required_connection"), fact.get("allow_nc")
            connected = has_peer
            permitted_open = permitted_unconnected
            if binding.get("unconnected") is False and not connected:
                add("PIN_DECLARED_USE", "fail", "A pin explicitly selected for connection has no peer or external endpoint", component=cid, pin=pid)
            if binding.get("unconnected") is True and connected:
                add("PIN_DECLARED_UNUSED", "fail", "A pin declared unconnected participates in an actual network", component=cid, pin=pid)
            if required is True and allow_nc is True:
                add("PIN_CONNECTION_POLICY", "fail", "Contract simultaneously requires a connection and permits NC", component=cid, pin=pid)
            elif pin.net is not None and (role == "no_connect" or fact.get("electrical_type") == "no_connect"):
                add("PIN_NC", "fail", "Reserved NC pin has a network", component=cid, pin=pid)
            elif connected:
                add("PIN_REQUIRED", "pass", "Actual peer or declared external endpoint satisfies either possible presence obligation", component=cid, pin=pid)
                add("PIN_NC", "not_applicable", "The pin is connected; no unused-state permission is claimed", component=cid, pin=pid)
            elif required is True or allow_nc is False:
                add("PIN_NC", "fail", "Pin lacks a peer despite an explicit connection obligation or NC prohibition", component=cid, pin=pid)
            elif permitted_open:
                add("PIN_NC", "pass", "Explicit applicable device permission allows this unconnected state", component=cid, pin=pid)
            else:
                add("PIN_CONNECTION_POLICY", "unresolved", "Unconnected pin has no established applicable NC permission", component=cid, pin=pid)
            if permitted_open:
                add("VOLTAGE_RANGE", "not_applicable", "A permitted open pin has no externally applied static net voltage to check", component=cid, pin=pid)
                continue
            if component_voltage_check and fact.get("electrical_type") == "passive":
                add("VOLTAGE_RANGE", "not_applicable", "Passive terminal has no active-input operating window; component terminal-difference/insulation obligation is checked separately", component=cid, pin=pid)
                continue
            domain_id = binding.get("domain")
            if not domain_id:
                if role in POWER_ROLES or fact.get("electrical_type") in {"input", "push_pull_output", "open_drain", "open_source", "bidirectional", "tri_state", "power_in", "power_out", "ground", "analog_input", "analog_output"}:
                    add("PIN_DOMAIN", "unresolved", "Domain binding is missing", component=cid, pin=pid)
                continue
            domain = _mapping(domains.get(domain_id))
            if evidence_errors(domain.get("evidence"), allow_input=True):
                add("PIN_DOMAIN", "unresolved", "Domain lacks attributable evidence", component=cid, pin=pid)
                continue
            if role in POWER_ROLES or fact.get("electrical_type") in {"power_in", "power_out", "ground"}:
                if not domain.get("net") or pin.net is None:
                    add("POWER_DOMAIN", "unresolved", "Power domain net binding is incomplete", component=cid, pin=pid)
                else:
                    add("POWER_DOMAIN", "pass" if pin.net[1] == domain["net"] else "fail", "Power pin must bind to the declared rail", component=cid, pin=pid)
            voltage = decimal_number(domain.get("voltage_v"))
            if (reviewed_ground_tie and fact.get("electrical_type") == "passive" and mode in {"passive", "ground"}
                    and _strings(modes) and mode in modes
                    and voltage == 0 and domain.get("reference") is None
                    and pin.net is not None and pin.net[1] == domain.get("net")):
                add("GROUND_CONNECTION_OBLIGATION", "pass", "A reviewed physical-pin relation proves the required tie to the actual zero reference", component=cid, pin=pid)
                add("VOLTAGE_RANGE", "not_applicable", "A source-required ground tie needs no invented passive-pin operating range", component=cid, pin=pid)
                continue
            if role in GROUND_ROLES and fact.get("electrical_type") == "ground":
                if voltage is not None and voltage != 0:
                    add("GROUND_REFERENCE", "fail", "Ground role is bound to a nonzero reference potential", component=cid, pin=pid)
                    continue
                if (voltage == 0 and "reference" in domain and domain["reference"] is None and
                        pin.net is not None and pin.net[1] == domain.get("net") and
                        mode == "ground" and _strings(modes) and "ground" in modes):
                    add("GROUND_REFERENCE", "pass", "Evidenced ground terminal is wired to the explicitly declared zero reference", component=cid, pin=pid)
                    add("VOLTAGE_RANGE", "not_applicable", "A reference datum does not require invented device minimum/maximum voltages", component=cid, pin=pid)
                    continue
            low, high = decimal_number(fact.get("voltage_min_v")), decimal_number(fact.get("voltage_max_v"))
            if logic_direction:
                add("VOLTAGE_RANGE", "not_applicable", "Logic domain is a supply context; actual signal guarantees are checked at the interface", component=cid, pin=pid)
                continue
            if fact.get("electrical_type") in {"analog_input", "analog_output"}:
                add("VOLTAGE_RANGE", "unresolved", "Analog domain is a supply context; actual signal bias/range and receiver loading require a separate interface proof", component=cid, pin=pid)
                continue
            if contracts.get("_static_voltage_context") is not None:
                from .static_voltages import rail_potential
                potential = rail_potential(pin.net[1] if pin.net else None, domains, context=contracts["_static_voltage_context"])
                minimum, maximum = decimal_number(potential.get("minimum_v")), decimal_number(potential.get("maximum_v"))
                if potential["status"] != "pass" or low is None or high is None or low > high:
                    add("VOLTAGE_RANGE", "unresolved", "Full source envelope or applicable source limits are incomplete", component=cid, pin=pid, potential=potential)
                else:
                    add("VOLTAGE_RANGE", "pass" if low <= minimum <= maximum <= high else "fail", "Entire declared static envelope checked against source limits", component=cid, pin=pid,
                        actual_minimum_v=str(minimum), actual_maximum_v=str(maximum))
                continue
            if voltage is None or low is None or high is None or low > high:
                add("VOLTAGE_RANGE", "unresolved", "Voltage or allowed range is incomplete", component=cid, pin=pid)
            else:
                add("VOLTAGE_RANGE", "pass" if low <= voltage <= high else "fail", "Declared voltage checked against allowed range", component=cid, pin=pid)

    for cid in sorted(_mapping(contracts.get("components")).keys() - {k[1] for k in graph.components}):
        add("MISSING_COMPONENT", "fail", "Contract instance is absent from graph", component=cid)
    rail_nets: dict[str, list[str]] = {}
    for domain_id, raw in domains.items():
        domain = _mapping(raw)
        if domain.get("net") and not evidence_errors(domain.get("evidence"), allow_input=True):
            rail_nets.setdefault(domain["net"], []).append(domain_id)
    for net_name, domain_ids in rail_nets.items():
        for i, a in enumerate(domain_ids):
            for b in domain_ids[i + 1:]:
                av, bv = decimal_number(domains[a].get("voltage_v")), decimal_number(domains[b].get("voltage_v"))
                if av is not None and bv is not None and av != bv:
                    add("DOMAIN_SHORT", "fail", "Distinct declared voltages share a wire net", net=net_name, domains=[a, b])
    for net_key, net in sorted(graph.nets.items()):
        outputs = [p for p in net.pins if p in valid_bindings and
                   valid_bindings[p].get("mode") == "push_pull_output"]
        if len(outputs) > 1:
            add("DRIVER_CONFLICT", "fail", "Multiple simultaneously selected push-pull outputs", net=net_key[1], pins=[list(p[1:]) for p in sorted(outputs)])
        if outputs and net_key[1] in rail_nets:
            add("OUTPUT_RAIL_SHORT", "fail", "Push-pull output is wired directly to a declared power rail", net=net_key[1], pins=[list(p[1:]) for p in sorted(outputs)])
        uncertain_outputs = [p for p in net.pins if valid_directions.get(p) == "output"]
        if uncertain_outputs and (len(outputs) + len(uncertain_outputs) > 1 or net_key[1] in rail_nets):
            add("DRIVER_SUBTYPE_REQUIRED", "unresolved", "Shared-output or rail-connected topology requires driver behavior beyond fixed direction", net=net_key[1])
        if len(net.pins) > 1 and any(p not in valid_facts or (p not in valid_bindings and p not in valid_directions) for p in net.pins):
            add("NET_ROLE_COVERAGE", "unresolved", "Not every endpoint has a known role and selected mode", net=net_key[1])
    logic_net_proofs = {}
    for pin_key, fact in valid_facts.items():
        if not documented_logic_direction(fact) or pin_key in unused_outputs:
            continue
        cid, pid = pin_key[1:]
        pin = graph.pins[pin_key]
        binding = pin_binding(contracts, cid, pid)
        others = []
        for peer in graph.connected_pins(pin_key):
            peer_component = graph.components[(graph.source, peer[1])]
            # Connector contacts and test points are direct logical endpoints.
            # Arbitrary passive branches need separate loading analysis.
            if peer_component.type not in {"connector", "test_point"} or _mapping(valid_facts.get(peer)).get("semantic_role") != "passive_terminal":
                others.append(list(peer[1:]))
        for diagnostic in check_external_logic(fact, binding, domains=domains,
                supplier_part=component_contract(contracts, cid).get("supplier_part"),
                input_sha256=contracts.get("input_sha256"), pin_net=pin.net[1] if pin.net else None,
                other_active_pins=others):
            rows.append({"component": cid, "pin": pid, **diagnostic})
            if diagnostic.get("rule") == "LOGIC_PASSIVE_INTERCONNECT" and diagnostic.get("status") == "pass":
                logic_net_proofs.setdefault(diagnostic["net"], []).append({"component": cid, "pin": pid, **diagnostic})
    for component, actual, contract in passive_voltage_components:
        rows.extend(check_passive_voltage(component, actual, contract, domains, logic_net_proofs=logic_net_proofs,
            static_context=contracts.get("_static_voltage_context")))
    constraints = contracts.get("constraints", [])
    if not isinstance(constraints, list):
        add("CONSTRAINT_SCHEMA", "fail", "constraints must be a list")
        constraints = []
    for index, raw in enumerate(constraints):
        constraint = _mapping(raw)
        kind = constraint.get("kind")
        detail = {"constraint_index": index, "kind": kind}
        if constraint.get("applies") is False:
            add("EXPLICIT_CONSTRAINT", "not_applicable", "Frozen applicability excludes rule", **detail)
            continue
        if constraint.get("applies") is not True or evidence_errors(constraint.get("evidence"), allow_input=True):
            add("EXPLICIT_CONSTRAINT", "unresolved", "Applicability or evidence missing", **detail)
            continue
        endpoints = constraint.get("endpoints")
        if not isinstance(endpoints, list) or not endpoints:
            add("EXPLICIT_CONSTRAINT", "fail", "Explicit endpoints required", **detail)
            continue
        pins = [graph.pins.get((graph.source, str(_mapping(e).get("component", "")), str(_mapping(e).get("pin", "")))) for e in endpoints]
        if any(p is None for p in pins):
            add("EXPLICIT_CONSTRAINT", "fail", "Constraint references nonexistent physical pin", **detail)
        elif kind not in {"connected", "same_net", "different_net"}:
            add("EXPLICIT_CONSTRAINT", "unresolved", "Constraint predicate is not implemented", **detail)
        elif any(p.net is None for p in pins):
            add("EXPLICIT_CONSTRAINT", "fail", "Required endpoint is unconnected", **detail)
        else:
            nets = {p.net for p in pins}
            good = (all(graph.connected_pins(p.key) for p in pins) if kind == "connected" else
                    len(nets) == 1 if kind == "same_net" else len(nets) == len(pins))
            add("EXPLICIT_CONSTRAINT", "pass" if good else "fail", "Frozen endpoint constraint evaluated", **detail)
    from .device_electrical import check_device_electrical
    device_electrical = check_device_electrical(graph, contracts)
    rows.extend(device_electrical["diagnostics"])
    from .planning_obligations import consume_obligations
    rows.extend(consume_obligations(graph, contracts, rows))
    counts = Counter(row["status"] for row in rows)
    status = "fail" if counts["fail"] else "unresolved" if counts["unresolved"] else "pass"
    return {"schema": "pcbagent.pin_contract_check.v1", "status": status, "pass": status == "pass",
            "device_electrical_coverage": device_electrical["coverage"],
            "counts": dict(counts), "coverage": {"pins": len(graph.pins), "pins_with_evidenced_roles": len(valid_facts),
            "pins_with_selected_supported_modes": len(valid_bindings), "pins_with_documented_fixed_directions": len(valid_directions),
            "pins_with_proven_unused_output_permission": len(unused_outputs)}, "diagnostics": rows,
            "scope": "static supplied contracts; no waveform simulation or source authenticity verification"}


def check_pin_contracts(graph: NetlistGraph, contracts: Mapping[str, Any] | None) -> dict[str, Any]:
    from .static_voltages import contexts_for, has_profiles
    contracts = _mapping(contracts)
    if not has_profiles(contracts):
        return _check_pin_contracts_single(graph, contracts)
    from .regulator_transfer import attach_regulator_transfers
    contexts = attach_regulator_transfers(contexts_for(contracts), graph, contracts)
    results = []; rows = []; scenario_statuses = []
    for context in contexts:
        start = len(rows)
        result = _check_pin_contracts_single(graph, {**contracts, "_static_voltage_context": context})
        results.append(result)
        if context.binding is not None:
            scenario = next(s for s in context.binding["environment"]["required_scenarios"] if s["id"] == context.space.scenario_id)
            if scenario.get("external_configuration"):
                rows.append({"rule": "STATIC_SCENARIO_CONFIGURATION_UNSUPPORTED", "status": "unresolved",
                    "message": "Per-scenario operating choices need a migrated source-conditional consumer; global nominal assumptions cannot substitute",
                    "scenario_id": context.space.scenario_id, "component": None, "pin": None})
            for did, declaration in context.domains.items():
                if _mapping(declaration).get("net") and context.domain(did).expression is None:
                    rows.append({"rule": "STATIC_DOMAIN_ENVELOPE", "status": "unresolved", "domain": did,
                        "message": "Actual domain has no established source envelope in this required scenario; internal setpoints, absent sources and signal labels are not guarantees",
                        "scenario_id": context.space.scenario_id, "component": None, "pin": None})
        rows.extend({**d, "scenario_id": context.space.scenario_id, "static_context_sha256": context.space.context_id}
                    for d in result["diagnostics"])
        rows.extend({"rule": "STATIC_CONTEXT", "status": "unresolved", "message": error,
                     "component": None, "pin": None, "scenario_id": context.space.scenario_id} for error in context.errors)
        states = {d["status"] for d in rows[start:]}
        scenario_statuses.append("fail" if "fail" in states else "unresolved" if "unresolved" in states else "pass")
    valid = all(not c.errors for c in contexts)
    rows.append({"rule": "STATIC_SCENARIO_COVERAGE", "status": "pass" if valid else "unresolved",
                 "component": None, "pin": None, "message": "Every required scenario is checked on the same graph and value inventory",
                 "scenarios": [c.space.scenario_id for c in contexts], "metric_denominators_changed": False})
    counts = Counter(d["status"] for d in rows)
    status = "fail" if counts["fail"] else "unresolved" if counts["unresolved"] else "pass"
    return {**results[0], "status": status, "pass": status == "pass", "counts": dict(counts), "diagnostics": rows,
            "static_scenarios": [{"id": c.space.scenario_id, "status": status, "context_sha256": c.space.context_id}
                for c,status in zip(contexts,scenario_statuses)],
            "device_electrical_coverage": [{**d, "scenario_id": c.space.scenario_id} for c,r in zip(contexts,results)
                for d in r["device_electrical_coverage"]]}


__all__ = ["SCHEMA", "PAPER_ROLES", "PROJECT_ROLES", "check_pin_contracts", "evidence_errors"]
