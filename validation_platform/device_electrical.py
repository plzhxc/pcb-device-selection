"""Source-qualified device obligations beyond individual pin ERC.

The bounded MOS checker evaluates signed terminal differences in an explicit
static scenario. Absolute stress bounds, recommended ranges and guaranteed
test points remain separate. A stress result cannot prove transistor switching,
analog transfer or RF installation correctness.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import re
from typing import Any

SCHEMA = "pcbagent.device_electrical.v1"
SUPPORTED_TYPES = frozenset({"mosfet", "microphone", "antenna"})
LIMIT_SCOPES = {"absolute_maximum": "terminal_voltage_limits",
                "recommended_operating": "recommended_terminal_voltage_limits",
                "guaranteed_test_point": "guaranteed_terminal_voltage_test_point"}
TERMINAL_ROLES = {"gate": "mosfet_gate", "source": "mosfet_source", "drain": "mosfet_drain"}
CONTEXT_FIELDS = ("configuration.mosfet_state", "requirements.operating_temperature_min_c",
                  "requirements.operating_temperature_max_c", "requirements.drain_current_min_a",
                  "requirements.drain_current_max_a")
DEVICE_ELECTRICAL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["schema", "supplier_part", "document_sha256", "terminal_map", "polarity", "terminal_limits"],
    "properties": {
        "schema": {"const": SCHEMA}, "supplier_part": {"type": "string", "pattern": "^[CS][0-9]+$"},
        "document_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "terminal_map": {"type": "object", "additionalProperties": False, "required": ["gate", "source", "drain"],
                         "properties": {key: {"type": ["string", "null"]} for key in TERMINAL_ROLES}},
        "polarity": {"oneOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
            "required": ["id", "scope", "effect", "evidence"], "properties": {
                "id": {"type": "string", "minLength": 1}, "scope": {"type": "object", "additionalProperties": False,
                    "required": ["supplier_part"], "properties": {"supplier_part": {"type": "string", "pattern": "^[CS][0-9]+$"}}},
                "effect": {"type": "object", "additionalProperties": False, "required": ["kind", "value"],
                    "properties": {"kind": {"const": "mosfet_polarity"}, "value": {"enum": ["n_channel", "p_channel"]}}},
                "evidence": {"$ref": "#/$defs/evidence"}, "review": {"$ref": "#/$defs/review"}}}]},
        "terminal_limits": {"type": "array", "maxItems": 32, "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "scope", "when", "effect", "evidence"],
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "scope": {"type": "object", "additionalProperties": False, "required": ["supplier_part", "positive_pin", "negative_pin"],
                    "properties": {"supplier_part": {"type": "string", "pattern": "^[CS][0-9]+$"},
                                   "positive_pin": {"type": "string", "minLength": 1}, "negative_pin": {"type": "string", "minLength": 1}}},
                "when": {"type": "object", "additionalProperties": False, "required": ["all"], "properties": {
                    "all": {"type": "array", "maxItems": 32, "items": {"type": "object", "additionalProperties": False,
                        "required": ["field", "op", "value"], "properties": {
                            "field": {"type": "string", "pattern": "^(configuration|requirements)\\.[A-Za-z][A-Za-z0-9_]*$"},
                            "op": {"enum": ["eq", "in", "gte", "lte"]},
                            "value": {"type": ["string", "number", "boolean", "array"]}}}}}},
                "effect": {"type": "object", "additionalProperties": False,
                    "required": ["kind", "quantity", "limit_class", "min_v", "max_v"], "properties": {
                        "kind": {"const": "terminal_voltage_limit"}, "quantity": {"enum": ["gate_source", "drain_source"]},
                        "limit_class": {"enum": list(LIMIT_SCOPES)}, "min_v": {"type": ["number", "null"]},
                        "max_v": {"type": ["number", "null"]}}},
                "evidence": {"$ref": "#/$defs/evidence"}, "review": {"$ref": "#/$defs/review"}}}}},
    "$defs": {"evidence": {"type": "array", "minItems": 1, "maxItems": 32, "items": {"type": "object"}},
              "review": {"type": "object", "required": ["method", "reviewer", "rule_sha256", "evidence_sha256"],
                  "properties": {"method": {"enum": ["independent_source_review", "bounded_source_parser", "proposed"]},
                                 "reviewer": {"type": "string", "minLength": 1},
                                 "rule_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                                 "evidence_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}}}

MOS_DEVICE_ELECTRICAL_SCHEMA = DEVICE_ELECTRICAL_SCHEMA
from .analog_application import ANALOG_DEVICE_ELECTRICAL_SCHEMA
from .regulator_transfer import REGULATOR_SCHEMA
DEVICE_ELECTRICAL_SCHEMA = {"oneOf": [MOS_DEVICE_ELECTRICAL_SCHEMA, ANALOG_DEVICE_ELECTRICAL_SCHEMA, REGULATOR_SCHEMA],
                          "$defs": MOS_DEVICE_ELECTRICAL_SCHEMA["$defs"]}


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any) -> Decimal | None:
    from .pin_contracts import decimal_number
    return decimal_number(value) if type(value) in {int, float} else None


def bind_device_electrical(record: Mapping, supplier_part: str, *, board_input: Mapping,
                           source_index: Mapping) -> dict:
    """Copy only frozen source rules and independently authenticated input choices.

    No generated prose, pin mode or declared function can create a source rule.
    Authentication of every claim is repeated by the checker against the bound
    original-source bundle; missing profiles are deliberately represented.
    """
    from .certificate_facts import digest
    from .conditional_pin_rules import bind_frozen_input_conditions
    profile = deepcopy(_map(_map(record.get("contracts")).get("device_electrical")))
    conditions = bind_frozen_input_conditions(board_input=board_input, supplier_part=supplier_part,
                                              source_index=source_index)
    result = {"profile": dict(profile), "profile_sha256": digest(profile),
            "source_knowledge_sha256": digest(record), "conditions": conditions["conditions"],
            "input_sha256": digest(board_input), "binding_diagnostics": conditions["diagnostics"],
            "input_snapshot": {"sha256": digest(board_input), "payload": deepcopy(dict(board_input))},
            "origin": "frozen_source_and_explicit_input_selections"}
    if profile.get("schema") in {"pcbagent.analog_device_electrical.v1", "pcbagent.regulator_device_electrical.v1"}:
        # Permit the consumer to reconstruct the profile's membership in the
        # independently frozen knowledge record, rather than trusting a new
        # profile hash or a self-rehashed application review.
        result["source_record"] = deepcopy(dict(record))
    return result


def _source_errors(contracts: Mapping, part: str, refs: Any, fields: list[str]) -> list[str]:
    from .certificate_facts import certificate_source_errors
    return certificate_source_errors(contracts, part, refs, fields)


def _claim_errors(claim: Any, contracts: Mapping, part: str, fields: list[str]) -> list[str]:
    from .conditional_pin_rules import _review_errors
    if not isinstance(claim, Mapping):
        return ["source-reviewed structured claim is missing"]
    errors = _review_errors(claim)
    if not isinstance(claim.get("id"), str) or not claim["id"]:
        errors.append("reviewed claim needs an explicit ID")
    errors.extend(_source_errors(contracts, part, claim.get("evidence"), fields))
    return errors


def _role_is_sourced(graph: Any, contracts: Mapping, pin_key: Any, roles: set[str]) -> bool:
    from .pin_contracts import component_contract
    component = graph.components[pin_key[:2]]
    cc = component_contract(contracts, pin_key[1])
    fact = _map(_map(cc.get("pins")).get(pin_key[2]))
    refs = _map(fact.get("field_evidence")).get("semantic_role", fact.get("evidence"))
    return (component.is_core and cc.get("supplier_part") == component.anchor_id
            and fact.get("semantic_role") in roles
            and not _source_errors(contracts, component.anchor_id, refs, ["semantic_role"]))


def _rail_potential(graph: Any, contracts: Mapping, pin: Any) -> tuple[dict | None, list[str]]:
    """Shared arithmetic with the existing stronger actual-source policy."""
    from .pin_contracts import GROUND_ROLES, POWER_ROLES
    from .static_voltages import current_context, rail_potential
    if pin is None or pin.net is None:
        return None, ["terminal has no actual network"]
    net = graph.nets.get(pin.net)
    if not net or not any(_role_is_sourced(graph, contracts, p, set(POWER_ROLES)) for p in net.pins):
        return None, ["a domain label is insufficient: actual net lacks a source-authenticated supply/ground terminal"]
    ctx = current_context(contracts)
    if ctx is None:
        return None, ["an explicit required static scenario is necessary"]
    potential = rail_potential(pin.net[1], _map(contracts.get("domains")), context=ctx)
    if potential["status"] != "pass":
        return None, [potential.get("reason", "actual source envelope unresolved")]
    reference = graph.nets.get((graph.source, potential["reference_net"]))
    if not reference or not any(_role_is_sourced(graph, contracts, p, set(GROUND_ROLES)) for p in reference.pins):
        return None, ["actual zero reference lacks a source-authenticated ground terminal"]
    # Retain the legacy device policy: no chained or floating zero references.
    domains = _map(contracts.get("domains"))
    for d in domains.values():
        if _map(d).get("net") == pin.net[1] and d.get("reference") is not None:
            ref = _map(domains.get(d["reference"]))
            if ref.get("reference") is not None or _number(ref.get("voltage_v")) != 0:
                return None, ["device rail requires one actual zero reference; chained reference is unsupported"]
    return {**potential, "minimum_v": Decimal(potential["minimum_v"]),
            "maximum_v": Decimal(potential["maximum_v"]), "basis": potential["proof_path"]}, []


def terminal_voltage_difference(graph: Any, contracts: Mapping, component: str,
                                positive_pin: str, negative_pin: str) -> dict:
    """Return V(positive)-V(negative), or explicit unresolved evidence needs."""
    pins = [graph.pins.get((graph.source, component, pid)) for pid in (positive_pin, negative_pin)]
    if any(pin is None for pin in pins):
        return {"status": "fail", "reasons": ["relative constraint references a nonexistent actual pin"]}
    if pins[0].net is not None and pins[0].net == pins[1].net:
        return {"status": "pass", "minimum_v": "0", "maximum_v": "0",
                "basis": "same_actual_wire", "net": pins[0].net[1]}
    plus, plus_errors = _rail_potential(graph, contracts, pins[0])
    minus, minus_errors = _rail_potential(graph, contracts, pins[1])
    if plus_errors or minus_errors:
        return {"status": "unresolved", "reasons": plus_errors + minus_errors}
    if plus["reference_net"] != minus["reference_net"]:
        return {"status": "unresolved", "reasons": ["terminal voltages do not have the same actual zero reference"]}
    from .static_voltages import current_context, resolved_difference
    return resolved_difference(current_context(contracts), pins[0].net[1], pins[1].net[1])


def _conditions(binding: Mapping, contracts: Mapping, part: str) -> tuple[dict, list[str]]:
    from .pin_contracts import evidence_errors
    from .certificate_facts import digest
    from .conditional_pin_rules import bind_frozen_input_conditions
    result, errors = {}, []
    snapshot = _map(binding.get("input_snapshot")); payload = _map(snapshot.get("payload"))
    try:
        intact = snapshot.get("sha256") == contracts.get("input_sha256") == digest(payload)
    except (TypeError, ValueError):
        intact = False
    if not intact:
        return {}, ["complete device-scoped board-input snapshot is missing or changed"]
    source_index = {"requirement:" + row["id"]: {"kind": "requirement", "value": row}
                    for row in payload.get("requirements", []) if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
    expected = bind_frozen_input_conditions(board_input=payload, supplier_part=part, source_index=source_index)
    if expected["diagnostics"] or expected["conditions"] != binding.get("conditions"):
        errors.append("selected device conditions differ from the actual frozen input requirements")
    for collection, raw in _map(binding.get("conditions")).items():
        if collection not in {"configuration", "requirements"} or not isinstance(raw, Mapping):
            errors.append("only explicit configuration/requirements selections are supported"); continue
        for name, selected in raw.items():
            selected = _map(selected); authentication = _map(selected.get("selection_authentication"))
            if (authentication.get("method") != "exact_frozen_input_selection"
                    or authentication.get("field") != collection + "." + str(name)
                    or authentication.get("supplier_part") != part or authentication.get("applies_to") != "all_instances"
                    or authentication.get("input_sha256") != contracts.get("input_sha256")
                    or evidence_errors(selected.get("evidence"), allow_input=True)):
                errors.append("device condition is not an authenticated selection from the exact input snapshot"); continue
            result.setdefault(collection, {})[name] = selected
    if not errors and binding.get("operating_selection_alias") is not None:
        from .operating_selections import rebuild_bound_operating_conditions
        alias = binding["operating_selection_alias"]
        candidates = [cc for cc in _map(contracts.get("components")).values()
                      if _map(cc).get("device") == alias and _map(cc).get("supplier_part") == part
                      and _map(cc).get("device_electrical") == binding]
        if len(candidates) != 1:
            errors.append("internal selection does not belong to this exact device profile instance")
        else:
            rebuilt = rebuild_bound_operating_conditions(contracts, alias)
            errors.extend(row["message"] for row in rebuilt["diagnostics"])
            result = rebuilt["conditions"]
    return ({} if errors else result), errors


def check_device_electrical(graph: Any, contracts: Mapping) -> dict:
    from jsonschema import Draft202012Validator
    from .certificate_facts import digest
    from .conditional_pin_rules import evaluate_condition
    from .pin_contracts import component_contract
    from datasheet_automation.knowledge.evidence import primary_document_sha256
    from .analog_application import check_analog_applications
    analog = check_analog_applications(graph, contracts)
    rows, coverage = list(analog["diagnostics"]), list(analog["coverage"])
    from .regulator_transfer import check_regulator_transfers
    regulators = check_regulator_transfers(graph, contracts)
    rows.extend(regulators["diagnostics"]); coverage.extend(regulators["coverage"])
    analog_obligations = {"selected_mode", "dc_isolation", "static_bias_application", "source_supply_compatibility"}
    covered_analog_pins = {(row["component"], row["pin"]) for row in analog["covered_pin_obligations"]
                          if row["status"] == "pass" and analog_obligations <= set(row["obligations"])}
    def add(rule: str, status: str, message: str, cid: str, **details: Any) -> None:
        rows.append({"rule": rule, "status": status, "message": message, "component": cid, **details})
    for key, component in graph.components.items():
        cid, part = key[1], component.anchor_id
        cc = component_contract(contracts, cid)
        analog_pins = [pid for pid, fact in _map(cc.get("pins")).items()
                       if _map(fact).get("electrical_type") in {"analog_input", "analog_output"}
                       and (cid, pid) not in covered_analog_pins]
        if analog_pins:
            add("ANALOG_INTERFACE_COVERAGE", "unresolved", "Analog direction alone cannot prove selected receiver mode, bias, loading or required DC isolation", cid, pins=analog_pins)
        if component.type not in SUPPORTED_TYPES:
            continue
        if component.type in {"microphone", "antenna"}:
            if cid in analog["handled_components"]:
                continue
            obligation = "analog transfer and required DC-blocking path" if component.type == "microphone" else "RF feed and applicable manufacturer installation/matching profile"
            add("DEVICE_FUNCTION_COVERAGE", "unresolved", "Required device-level predicate is not implemented: " + obligation, cid)
            coverage.append({"component": cid, "component_type": component.type, "status": "unresolved", "required": [obligation]})
            continue
        binding = _map(cc.get("device_electrical")); profile = _map(binding.get("profile"))
        schema_issues = [issue.message for issue in Draft202012Validator(MOS_DEVICE_ELECTRICAL_SCHEMA).iter_errors(profile)]
        if schema_issues:
            add("DEVICE_ELECTRICAL_SCHEMA", "unresolved", "A complete typed source profile is needed before device obligations can be checked", cid, reasons=schema_issues)
            coverage.append({"component": cid, "component_type": "mosfet", "status": "unresolved", "required": ["source_electrical_profile", "static_function"]})
            continue
        frozen = _map(_map(contracts.get("source_bundles")).get(part)); bundle = _map(frozen.get("bundle"))
        errors = []
        try:
            intact = binding.get("profile_sha256") == digest(profile)
        except (TypeError, ValueError):
            intact = False
        if (not component.is_core or not part or cc.get("supplier_part") != part
                or binding.get("origin") != "frozen_source_and_explicit_input_selections" or not intact
                or binding.get("input_sha256") != contracts.get("input_sha256")
                or binding.get("source_knowledge_sha256") != frozen.get("knowledge_record_sha256")
                or profile.get("schema") != SCHEMA or profile.get("supplier_part") != part
                or profile.get("document_sha256") != primary_document_sha256(bundle)
                or set(profile) != {"schema", "supplier_part", "document_sha256", "terminal_map", "polarity", "terminal_limits"}):
            errors.append("complete frozen source electrical profile and exact input binding are required")
        terminals = _map(profile.get("terminal_map"))
        if (set(terminals) != set(TERMINAL_ROLES) or any(not isinstance(p, str) or not p for p in terminals.values())
                or len(set(terminals.values())) != 3 or set(terminals.values()) != set(graph.pin_map(key))):
            errors.append("this bounded MOS model requires exactly three distinct real gate/source/drain terminals")
        else:
            for name, pid in terminals.items():
                if not _role_is_sourced(graph, contracts, (*key, pid), {TERMINAL_ROLES[name]}):
                    errors.append("terminal map disagrees with an independently sourced actual " + name + " role")
        if errors:
            add("DEVICE_TERMINAL_IDENTITY", "unresolved", "MOS terminal identity/profile is incomplete", cid, reasons=errors)
        else:
            add("DEVICE_TERMINAL_IDENTITY", "pass", "Complete physical terminal map agrees with independently sourced roles", cid)
        polarity = _map(profile.get("polarity")); effect = _map(polarity.get("effect"))
        polarity_errors = _claim_errors(polarity, contracts, part, ["mosfet_polarity"])
        if (set(polarity) != {"id", "scope", "effect", "evidence", "review"}
                or polarity.get("scope") != {"supplier_part": part}
                or set(effect) != {"kind", "value"} or effect.get("kind") != "mosfet_polarity"
                or effect.get("value") not in {"n_channel", "p_channel"}):
            polarity_errors.append("an independently reviewed N/P channel claim is required")
        add("MOSFET_POLARITY", "unresolved" if polarity_errors else "pass", "Channel polarity requires its own source-reviewed claim", cid, reasons=polarity_errors)
        selected, selection_errors = _conditions(binding, contracts, part)
        context_values = {}
        for field in CONTEXT_FIELDS:
            collection, name = field.split(".", 1)
            context_values[field] = _map(_map(selected.get(collection)).get(name)).get("value")
        state = context_values[CONTEXT_FIELDS[0]]
        numeric = [_number(context_values[field]) for field in CONTEXT_FIELDS[1:]]
        complete_context = (not selection_errors and isinstance(state, str) and state in {"on", "off"} and all(n is not None for n in numeric)
                            and numeric[0] <= numeric[1] and numeric[2] <= numeric[3])
        add("MOSFET_STATIC_CONTEXT", "pass" if complete_context else "unresolved",
            "Selected on/off state, bounded operating temperature and signed drain-current load must be explicit input guarantees", cid,
            missing_or_unknown=[field for field, value in context_values.items() if value is None], reasons=selection_errors)
        rules = profile.get("terminal_limits", [])
        if not isinstance(rules, list) or len(rules) > 32:
            rules = []; add("TERMINAL_LIMIT_SCHEMA", "unresolved", "At most 32 reviewed terminal limits are supported", cid)
        statuses = {pair: [] for pair in ("gate_source", "drain_source")}
        seen = set()
        for rule in rules:
            rule = _map(rule); effect = _map(rule.get("effect")); scope = _map(rule.get("scope"))
            rid, limit_class, quantity = rule.get("id"), effect.get("limit_class"), effect.get("quantity")
            required_scope = LIMIT_SCOPES.get(limit_class, "unsupported_terminal_limit_class") if isinstance(limit_class, str) else "unsupported_terminal_limit_class"
            issues = _claim_errors(rule, contracts, part, [required_scope])
            minimum, maximum = _number(effect.get("min_v")), _number(effect.get("max_v"))
            if (set(rule) != {"id", "scope", "when", "effect", "evidence", "review"}
                    or set(effect) != {"kind", "quantity", "limit_class", "min_v", "max_v"}
                    or effect.get("kind") != "terminal_voltage_limit" or not isinstance(limit_class, str) or limit_class not in LIMIT_SCOPES
                    or not isinstance(quantity, str) or quantity not in statuses or not isinstance(rid, str) or rid in seen
                    or set(scope) != {"supplier_part", "positive_pin", "negative_pin"} or scope.get("supplier_part") != part
                    or scope.get("positive_pin") != terminals.get("gate" if quantity == "gate_source" else "drain")
                    or scope.get("negative_pin") != terminals.get("source")
                    or effect.get("min_v") is not None and minimum is None
                    or effect.get("max_v") is not None and maximum is None
                    or minimum is None and maximum is None or minimum is not None and maximum is not None and minimum > maximum):
                issues.append("closed reviewed class-specific terminal limit and its physical signed endpoints are required")
            if isinstance(rid, str): seen.add(rid)
            predicates = _map(rule.get("when")).get("all")
            if (not isinstance(predicates, list) or any(not re.fullmatch(r"(?:configuration|requirements)\.[A-Za-z][A-Za-z0-9_]*", str(_map(p).get("field", ""))) for p in predicates)):
                issues.append("terminal-limit conditions must name explicit typed input selections")
            condition = evaluate_condition(rule.get("when"), selected)
            detail = {"rule_id": rid, "limit_class": limit_class, "quantity": quantity, "condition": condition,
                      "specified_bounds": {"minimum": minimum is not None, "maximum": maximum is not None}}
            if issues or errors or polarity_errors or selection_errors:
                add("TERMINAL_VOLTAGE_LIMIT", "unresolved", "Terminal limit cannot be used without its complete source/identity/selection proof", cid, reasons=issues + errors + polarity_errors + selection_errors, **detail)
                continue
            if condition["status"] != "true":
                add("TERMINAL_VOLTAGE_LIMIT", "not_applicable" if condition["status"] == "false" else "unresolved",
                    "Source condition is false or unknown in the selected operating scenario", cid, **detail)
                continue
            difference = terminal_voltage_difference(graph, contracts, cid, scope["positive_pin"], scope["negative_pin"])
            status = difference["status"]
            if status == "pass":
                lo, hi = Decimal(difference["minimum_v"]), Decimal(difference["maximum_v"])
                if minimum is not None and lo < minimum or maximum is not None and hi > maximum:
                    status = "fail"
            add("TERMINAL_VOLTAGE_LIMIT", status, "Signed terminal difference checked only against the stated bounds and their class", cid,
                difference=difference, minimum_v=str(minimum) if minimum is not None else None,
                maximum_v=str(maximum) if maximum is not None else None, **detail)
            if limit_class == "absolute_maximum": statuses[quantity].append(status)
        for quantity, current in statuses.items():
            status = "fail" if "fail" in current else "pass" if current and all(s == "pass" for s in current) else "unresolved"
            add("MOSFET_STRESS_COVERAGE", status, "An applicable source-qualified stress constraint is required for this terminal pair", cid, quantity=quantity)
        add("DEVICE_FUNCTION_COVERAGE", "unresolved", "Stress/range/test-point checks do not establish the requested switching function, available gate drive or current/load performance; that device-level proof is not implemented", cid)
        coverage.append({"component": cid, "component_type": "mosfet", "status": "unresolved",
                         "required": ["terminal_identity", "polarity", "gate_source_stress", "drain_source_stress", "static_context", "static_function"],
                         "functional_proof_implemented": False})
    status = "fail" if any(r["status"] == "fail" for r in rows) else "unresolved" if any(r["status"] == "unresolved" for r in rows) else "pass"
    return {"schema": "pcbagent.device_electrical_check.v1", "status": status, "diagnostics": rows, "coverage": coverage,
            "scope": "declared ideal static circuit; no transistor transfer, waveform, analog or RF-performance certification"}


__all__ = ["SCHEMA", "DEVICE_ELECTRICAL_SCHEMA", "LIMIT_SCOPES", "CONTEXT_FIELDS", "bind_device_electrical", "terminal_voltage_difference", "check_device_electrical"]
