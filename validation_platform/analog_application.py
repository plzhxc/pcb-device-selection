"""Reviewed application topology predicates, separate from analog performance.

Device identities, terminal numbers and applicability are source-profile data.
This consumer neither supplies a circuit template nor awards equivalence credit.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

SCHEMA = "pcbagent.analog_device_electrical.v1"
_PIN = {"type": "string", "minLength": 1}
_PINS = {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True, "items": _PIN}
_PART = {"type": "string", "pattern": "^[CS][0-9]+$"}
_SIDE = {"type": "object", "additionalProperties": False,
    "required": ["output_pin", "ground_pins", "supply_pins"],
    "properties": {"output_pin": _PIN, "ground_pins": _PINS, "supply_pins": _PINS}}
_RECEIVER = {"type": "object", "additionalProperties": False,
    "required": ["supplier_part", "positive_pin", "negative_pin", "ground_pins", "supply_pins", "reference_filter_pins"],
    "properties": {"supplier_part": _PART, "positive_pin": _PIN, "negative_pin": _PIN,
        "ground_pins": _PINS, "supply_pins": _PINS, "reference_filter_pins": _PINS}}
ANALOG_DEVICE_ELECTRICAL_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["schema", "supplier_part", "document_sha256", "analog_applications"],
    "properties": {"schema": {"const": SCHEMA}, "supplier_part": _PART,
        "document_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "analog_applications": {"type": "array", "minItems": 1, "maxItems": 1, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "scope", "effect", "evidence", "review"],
            "properties": {"id": _PIN, "scope": {"type": "object", "additionalProperties": False,
                "required": ["supplier_part"], "properties": {"supplier_part": _PART}},
                "effect": {"type": "object", "additionalProperties": False,
                    "required": ["kind", "topology", "producer", "receiver", "intended_configuration", "receiver_supply_v"],
                    "properties": {"kind": {"const": "analog_application"},
                        "topology": {"const": "single_ended_ac_ground"}, "producer": _SIDE, "receiver": _RECEIVER,
                        "dc_interface_class": {"const": "independently_biased_ac_codec_input"},
                        "receiver_supply_v": {"type": "number", "exclusiveMinimum": 0},
                        "intended_configuration": {"type": "array", "minItems": 1, "maxItems": 16,
                            "items": {"type": "object", "additionalProperties": False, "required": ["field", "value"],
                                "properties": {"field": {"type": "string", "pattern": "^configuration\\.[A-Za-z][A-Za-z0-9_]*$"},
                                    "value": {"type": ["string", "number", "boolean"]}}}}}},
                "evidence": {"type": "array", "minItems": 2, "maxItems": 32, "items": {
                    "type": "object", "required": ["source_supplier_part", "source_ref", "quote"],
                    "properties": {"source_supplier_part": _PART}}},
                "review": {"type": "object", "required": ["method", "reviewer", "rule_sha256", "evidence_sha256"]}}}}}}

# Preserve the original microphone effect verbatim. The receiver variant is a
# separately reviewed source obligation, not a microphone permission or a claim
# of selected mode, signal limits, gain, capacitor rating or supply compliance.
_APPLICATION_PROPERTIES = ANALOG_DEVICE_ELECTRICAL_SCHEMA["properties"]["analog_applications"]["items"]["properties"]
_APPLICATION_PROPERTIES["effect"] = {"oneOf": [_APPLICATION_PROPERTIES["effect"], {
    "type": "object", "additionalProperties": False,
    "required": ["kind", "topology", "input_pins", "ground_pins", "endpoint_policy", "applicability"],
    "properties": {"kind": {"const": "analog_application"},
        "topology": {"const": "receiver_series_rc_input"}, "input_pins": _PINS, "ground_pins": _PINS,
        "endpoint_policy": {"const": "analog_output_or_receiver_ground"},
        "applicability": {"const": "all_documented_input_modes"}}}]}


def is_receiver_rc_profile(profile: Any) -> bool:
    """Dispatch only; full schema, frozen membership and source checks follow."""
    rules = _map(profile).get("analog_applications")
    return (_map(profile).get("schema") == SCHEMA and isinstance(rules, list) and len(rules) == 1
            and _map(_map(rules[0]).get("effect")).get("topology") == "receiver_series_rc_input")


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _status(rows: list[dict]) -> str:
    return "fail" if any(r["status"] == "fail" for r in rows) else "unresolved" if any(r["status"] == "unresolved" for r in rows) else "pass"


def trace_ac_branch(graph: Any, start: tuple, target: tuple, *, ground_end: bool = False) -> dict:
    """A unique unbranched populated path with one C and at most eight zero Rs.

    Ownership is not conduction. Only an explicitly valued ideal zero resistor
    can be traversed, and the capacitor is recorded as a DC barrier. Extra
    signal peers, NC endpoints, active devices, loops and parallel paths reject
    this particular profile. The shared ground endpoint may have other loads.
    """
    from .electrical_values import normalized_value
    if start not in graph.pins or target not in graph.pins:
        return {"status": "fail", "reason": "nonexistent physical path endpoint"}
    current, visited, components, capacitors = start, set(), [], []
    for _ in range(10):
        net_key = graph.pins[current].net
        if net_key is None:
            return {"status": "fail", "reason": "unconnected/NC endpoint is not a wire"}
        if net_key in visited:
            return {"status": "fail", "reason": "loop or DC bypass is outside the application path"}
        visited.add(net_key)
        peers = set(graph.nets[net_key].pins) - {current}
        if target in peers:
            if (not ground_end and peers != {target}) or len(capacitors) != 1:
                return {"status": "fail", "reason": "one isolated DC barrier and no additional signal branch are required"}
            return {"status": "pass", "components": components, "capacitors": capacitors,
                    "nets": [key[1] for key in sorted(visited)], "proof": "one_series_dc_barrier"}
        if len(peers) != 1:
            return {"status": "fail", "reason": "missing or additional signal branch"}
        entry = next(iter(peers)); device = graph.components[entry[:2]]
        pins = graph.pin_map(entry[:2])
        if device.is_core or device.is_abstract is not True or device.type not in {"capacitor", "resistor"} or len(pins) != 2:
            return {"status": "fail", "reason": "only populated ideal capacitor/zero-resistor series elements are supported"}
        dimension, value = normalized_value(device.value)
        if dimension in {"missing", "text"}:
            return {"status": "unresolved", "reason": "explicit dimensioned nominal component value is required"}
        if not isinstance(value, Decimal) or not value.is_finite():
            return {"status": "unresolved", "reason": "finite explicit nominal component value is required"}
        if device.type == "capacitor":
            if dimension != "f" or value <= 0 or capacitors:
                return {"status": "fail", "reason": "exactly one positive series capacitor is supported"}
            capacitors.append(entry[1])
        elif dimension != "ohm" or value != 0:
            return {"status": "fail", "reason": "resistor is not an explicit ideal zero-ohm jumper"}
        components.append(entry[1])
        current = next(pin.key for pin in pins.values() if pin.key != entry)
    return {"status": "unresolved", "reason": "bounded series path search exhausted"}


def trace_receiver_rc_branch(graph: Any, start: tuple, input_pins: set[tuple]) -> dict:
    """Receiver-outward R then C; zero-ohm jumpers never count as input R.

    Trace only a bounded unambiguous actual network. Endpoint identity is checked
    separately, so absent voltage/role facts cannot hide a structurally missing
    required series element. No actual graph or component value is changed.
    """
    from .electrical_values import normalized_value
    current, visited, components, candidates, values, sequence = start, set(), [], [], {}, []
    excluded_shunts: set[tuple] = set()

    def result(status: str, reason: str, **detail: Any) -> dict:
        return {"status": status, "reason": reason, "components": components,
                "series_candidates": candidates, "nominal_values": values,
                "excluded_shunts": [list(key) for key in sorted(excluded_shunts)], **detail}

    def passive(pin: tuple) -> tuple:
        device = graph.components[pin[:2]]; pins = graph.pin_map(pin[:2])
        valid = (not device.is_core and device.is_abstract is True
                 and device.type in {"resistor", "capacitor"} and len(pins) == 2)
        return device, pins, normalized_value(device.value) if valid else (None, None), valid

    for _ in range(12):
        if current not in graph.pins:
            return result("unresolved", "actual receiver terminal is absent")
        net = graph.pins[current].net
        if net is None:
            return result("fail", "required receiver branch is open")
        # Contract only explicit ideal jumpers for this traversal; retain their
        # identities so independent branches cannot silently share them.
        closure, todo, jumpers = {net}, [net], set()
        while todo:
            for pin in sorted(graph.nets[todo.pop()].pins):
                device, pins, (dimension, value), valid = passive(pin)
                if not valid or device.type != "resistor" or dimension != "ohm" or value != 0:
                    continue
                jumpers.add(pin[:2])
                if len(jumpers) > 8:
                    return result("unresolved", "bounded zero-ohm jumper traversal exhausted")
                for other in pins.values():
                    if other.net is None:
                        return result("unresolved", "an additional zero-ohm branch is open")
                    if other.net not in closure:
                        closure.add(other.net); todo.append(other.net)
        if closure & visited:
            return result("fail", "required branch has a loop or bypass")
        visited.update(closure)
        components.extend(key[1] for key in sorted(jumpers) if key[1] not in components)
        frontier = {p for n in closure for p in graph.nets[n].pins if p != current and p[:2] not in jumpers}
        if frontier & (input_pins - {start}):
            return result("fail", "independent receiver inputs share the same branch node")
        # A shorted extra passive is outside this profile, not proof that a
        # required element on the remaining path was bypassed. Ignore its
        # zero-length detour, then diagnose missing R/C at the actual endpoint.
        shunts = {p[:2] for p in frontier if passive(p)[3]
                  and all(other.net in closure for other in graph.pin_map(p[:2]).values())}
        excluded_shunts.update(shunts)
        frontier = {p for p in frontier if p[:2] not in shunts}
        active = {p for p in frontier if not passive(p)[3]}
        if active:
            if not {"resistor", "capacitor"} <= set(sequence):
                return result("fail", "wire reaches a terminal without both required positive R and C")
            if sequence != ["resistor", "capacitor"]:
                return result("unresolved", "complete series network has an order or multiplicity outside the reviewed profile",
                              endpoint_pins=[list(p) for p in sorted(active)], endpoint_net=list(net))
            return result("pass" if frontier == active and not excluded_shunts else "unresolved",
                          "endpoint needs independent source identity" if frontier == active and not excluded_shunts else "additional branch is outside this bounded profile",
                          endpoint_pins=[list(p) for p in sorted(active)], endpoint_net=list(net),
                          endpoint_extra_branches=frontier != active)
        if not frontier:
            return result("fail", "required receiver branch has no source endpoint")
        series_devices = {p[:2] for p in frontier if passive(p)[3]}
        expected = "resistor" if not sequence else "capacitor" if sequence == ["resistor"] else None
        candidate_devices = series_devices if len(series_devices) == 1 else {
            key for key in series_devices if graph.components[key].type == expected}
        if len(candidate_devices) == 1:
            candidates.append(next(iter(candidate_devices))[1])
        if len(frontier) != 1:
            return result("unresolved", "additional or parallel branch requires a separate reviewed profile")
        entry = next(iter(frontier)); device, pins, (dimension, value), _ = passive(entry)
        if dimension in {"missing", "text"} or not isinstance(value, Decimal) or not value.is_finite():
            return result("unresolved", "finite dimensioned component value is unknown")
        if dimension != ("ohm" if device.type == "resistor" else "f") or value <= 0:
            return result("fail", "required series R/C must have a positive value in its physical unit")
        sequence.append(device.type); components.append(entry[1]); values[device.type] = str(value)
        current = next(p.key for p in pins.values() if p.key != entry)
    return result("unresolved", "bounded receiver series path traversal exhausted")


def _receiver_rc_checks(graph: Any, contracts: Mapping, cid: str, rule: Mapping) -> tuple[list[dict], dict]:
    from .pin_contracts import GROUND_ROLES
    from .device_electrical import _rail_potential
    effect = rule["effect"]
    starts = {(graph.source, cid, pid) for pid in effect["input_pins"]}
    branches = {p[2]: trace_receiver_rc_branch(graph, p, starts) for p in sorted(starts)}
    used = [c for b in branches.values() for c in set(b["components"] + b["series_candidates"])]
    shared = sorted({c for c in used if used.count(c) > 1})
    endpoint_rows = []
    for pid, branch in branches.items():
        if branch["status"] != "pass" and not branch.get("endpoint_extra_branches"):
            continue
        endpoints = [tuple(p) for p in branch.get("endpoint_pins", [])]
        outputs = [p for p in endpoints if not _pin_evidence(graph, contracts, p[1], p[2], "electrical_type", {"analog_output"})]
        grounds = [p for p in endpoints if not _pin_evidence(graph, contracts, p[1], p[2], "semantic_role", set(GROUND_ROLES))]
        kind, issues, status = None, [], "unresolved"
        if len(endpoints) == len(outputs) == 1:
            kind, status = "analog_output", "pass"
        elif grounds and not outputs:
            kind = "receiver_ground"
            net = graph.pins[grounds[0]].net
            wrong_ground = False
            for gp in effect["ground_pins"]:
                identity_issues = _pin_evidence(graph, contracts, cid, gp, "semantic_role", set(GROUND_ROLES))
                issues.extend(identity_issues)
                if graph.pins[(graph.source, cid, gp)].net != net:
                    issues.append("source endpoint is not the receiver's actual ground network")
                    wrong_ground = wrong_ground or not identity_issues
            potential, voltage_issues = _rail_potential(graph, contracts, graph.pins[grounds[0]])
            issues.extend(voltage_issues)
            if potential is None or potential["minimum_v"] != 0 or potential["maximum_v"] != 0:
                issues.append("actual zero reference is not independently established")
            status = "fail" if wrong_ground else "unresolved" if issues else "pass"
            if status == "pass" and branch.get("endpoint_extra_branches") and not branch.get("excluded_shunts"):
                # Other loads may share the independently authenticated ground
                # endpoint. This exception never applies to an internal RC node
                # or an analog source whose additional branches are unreviewed.
                branch["status"] = "pass"
                branch["reason"] = "independent R then C ends at the authenticated shared ground"
        else:
            issues.append("unique source-authenticated analog output or actual receiver ground is missing")
        endpoint_rows.append({"pin": pid, "status": status, "kind": kind, "reasons": issues})
    topology = "fail" if shared else _status(list(branches.values()))
    rows = [{"rule": "ANALOG_RECEIVER_RC_TOPOLOGY", "component": cid, "status": topology,
             "message": "Each source-declared input requires an independent positive R then C path",
             "branches": branches, "shared_components": shared, "source_rule_id": rule["id"]}]
    endpoint_status = _status(endpoint_rows) if len(endpoint_rows) == len(starts) else "unresolved"
    if endpoint_status == "pass" and not any(r["kind"] == "analog_output" for r in endpoint_rows):
        endpoint_status = "fail"
    rows.append({"rule": "ANALOG_RECEIVER_RC_ENDPOINTS", "component": cid, "status": endpoint_status,
        "message": "Actual analog source and any ground-reference endpoint require their own source facts; no rail is inferred",
        "checks": endpoint_rows, "source_rule_id": rule["id"]})
    return rows, {"component": cid, "component_type": graph.components[(graph.source, cid)].type,
        "status": _status(rows), "application_topology": topology, "endpoints": endpoint_status,
        "discharged_obligations": ["independent_series_rc_topology"] if topology == "pass" else [],
        "numerical_signal_envelope": "not_inferred", "nominal_value_equivalence_credit": False,
        "capacitor_rating_checked_separately": True,
        "remaining_separate_obligations": ["selected_mode", "static_bias_application", "source_supply_compatibility", "signal_stress"]}


def _pin_evidence(graph: Any, contracts: Mapping, cid: str, pid: str, field: str, allowed: set,
                  *, conditions: Mapping | None = None) -> list[str]:
    from .pin_contracts import component_contract
    from .device_electrical import _source_errors
    cc = component_contract(contracts, cid); component = graph.components[(graph.source, cid)]
    fact = _map(_map(cc.get("pins")).get(pid))
    if pid not in graph.pin_map(component.key) or not component.is_core or cc.get("supplier_part") != component.anchor_id:
        return ["actual physical terminal identity is incomplete"]
    errors = []
    if field == "electrical_type" and fact.get("conditional_rules"):
        from .conditional_pin_rules import evaluate_pin_rules
        rules = fact.get("conditional_rules")
        if not isinstance(rules, list):
            return ["conditional terminal mode rules must be a list"]
        modes = [rule for rule in rules if _map(_map(rule).get("effect")).get("kind") == "operating_pin_mode"]
        for rule in modes:
            errors.extend(_source_errors(contracts, component.anchor_id, _map(rule).get("evidence"), ["electrical_type", "modes"]))
        # Use conditions reconstructed from the frozen input, never a planner's
        # free-text claim. Other pin obligations remain with their own checks.
        evaluated = evaluate_pin_rules({key: fact.get(key) for key in
            ("electrical_type", "modes", "evidence")} | {"field_evidence": dict(_map(fact.get("field_evidence"))), "conditional_rules": modes},
            _map(conditions), supplier_part=component.anchor_id, pin_number=pid)
        errors.extend("conditional terminal mode is not established: " + str(row.get("rule_id", row.get("rule")))
                      for row in evaluated["diagnostics"] if row.get("status") in {"fail", "unresolved"})
        fact = evaluated["fact"]
    refs = _map(fact.get("field_evidence")).get(field, fact.get("evidence"))
    value = fact.get(field)
    return errors + (["source terminal " + field + " does not match required role"]
                     if not isinstance(value, str) or value not in allowed else []) + _source_errors(contracts, component.anchor_id, refs, [field])


def _supply_checks(graph: Any, contracts: Mapping, cid: str, side: Mapping, ground_net: Any,
                   nominal: Any = None) -> list[dict]:
    from .pin_contracts import component_contract, GROUND_ROLES, POWER_ROLES
    from .device_electrical import _rail_potential, _source_errors, _number
    from .conditional_pin_rules import unconditional_claim_errors
    rows = []; cc = component_contract(contracts, cid)
    for pid in side["ground_pins"]:
        errors = _pin_evidence(graph, contracts, cid, pid, "semantic_role", set(GROUND_ROLES))
        pin = graph.pins.get((graph.source, cid, pid))
        status = "unresolved" if errors else "pass" if pin and pin.net == ground_net and ground_net else "fail"
        rows.append({"status": status, "pin": pid, "check": "actual_common_ground", "reasons": errors})
    for pid in side["supply_pins"]:
        errors = _pin_evidence(graph, contracts, cid, pid, "semantic_role", set(POWER_ROLES) - set(GROUND_ROLES))
        pin = graph.pins.get((graph.source, cid, pid))
        potential, issues = _rail_potential(graph, contracts, pin) if pin else (None, ["missing supply pin"])
        errors.extend(issues); fact = _map(_map(cc.get("pins")).get(pid))
        low, high = _number(fact.get("voltage_min_v")), _number(fact.get("voltage_max_v"))
        for field in ("voltage_min_v", "voltage_max_v"):
            refs = _map(fact.get("field_evidence")).get(field, fact.get("evidence"))
            errors.extend(_source_errors(contracts, cc.get("supplier_part"), refs, [field]))
            errors.extend(unconditional_claim_errors(field, fact.get(field), refs))
        if fact.get("conditional_rules"):
            errors.append("conditional supply ranges need an independently evaluated operating profile")
        if low is None or high is None or low > high:
            errors.append("finite source-backed recommended supply bounds are required")
        status = "unresolved"
        if not errors:
            status = "pass" if (potential["reference_net"] == ground_net[1]
                and low <= potential["minimum_v"] <= potential["maximum_v"] <= high
                and (nominal is None or potential["minimum_v"] == potential["maximum_v"] == _number(nominal))) else "fail"
        rows.append({"status": status, "pin": pid, "check": "source_supply_conditions", "reasons": errors,
                     "basis": "source_bound_static_supply_envelope" if contracts.get("_static_voltage_context") is not None else "actual_declared_ideal_supply_rail", "signal_voltage_inferred": False})
    return rows


def check_analog_applications(graph: Any, contracts: Mapping) -> dict:
    from jsonschema import Draft202012Validator
    from .certificate_facts import digest
    from .conditional_pin_rules import _review_errors, bind_frozen_input_conditions, evaluate_condition
    from .device_electrical import _conditions, _rail_potential, _source_errors
    from .pin_contracts import component_contract
    from datasheet_automation.knowledge.evidence import primary_document_sha256
    diagnostics, coverage, handled, covered_pins = [], [], [], []
    for key, component in graph.components.items():
        cid, part = key[1], component.anchor_id
        cc = component_contract(contracts, cid); binding = _map(cc.get("device_electrical")); profile = _map(binding.get("profile"))
        if profile.get("schema") != SCHEMA:
            continue
        receiver_rc = is_receiver_rc_profile(profile)
        handled.append(cid)
        def add(rule: str, status: str, message: str, **detail: Any) -> None:
            diagnostics.append({"rule": rule, "status": status, "message": message, "component": cid, **detail})
        issues = [e.message for e in Draft202012Validator(ANALOG_DEVICE_ELECTRICAL_SCHEMA).iter_errors(profile)]
        frozen = _map(_map(contracts.get("source_bundles")).get(part))
        try:
            source_record = _map(binding.get("source_record"))
            intact = (binding.get("profile_sha256") == digest(profile)
                and digest(source_record) == frozen.get("knowledge_record_sha256")
                and _map(source_record.get("contracts")).get("device_electrical") == profile)
        except (TypeError, ValueError):
            intact = False
        if ((component.type != "microphone" and not receiver_rc) or not component.is_core or cc.get("supplier_part") != part
            or profile.get("supplier_part") != part or not intact
            or profile.get("document_sha256") != primary_document_sha256(_map(frozen.get("bundle")))
            or binding.get("source_knowledge_sha256") != frozen.get("knowledge_record_sha256")
            or binding.get("origin") != "frozen_source_and_explicit_input_selections"
            or binding.get("input_sha256") != contracts.get("input_sha256")):
            issues.append("profile must originate in this exact device's frozen source contract")
        if issues:
            add("ANALOG_APPLICATION_SOURCE", "unresolved", "Complete reviewed source profile required", reasons=issues)
            continue
        rule = profile["analog_applications"][0]; effect = rule["effect"]
        if receiver_rc:
            issues.extend(_review_errors(rule))
            declared = [*effect["input_pins"], *effect["ground_pins"]]
            if rule["scope"] != {"supplier_part": part} or any(ref.get("source_supplier_part") != part for ref in rule["evidence"]):
                issues.append("receiver profile needs its exact original source owner")
            issues.extend(_source_errors(contracts, part, rule["evidence"], ["application_interface_profile"]))
            if len(set(declared)) != len(declared) or not set(declared) <= set(graph.pin_map(key)):
                issues.append("receiver input/ground categories must be distinct actual physical terminals")
            if issues:
                add("ANALOG_APPLICATION_SOURCE", "unresolved", "Independent receiver application source review is required", reasons=issues)
                continue
            add("ANALOG_APPLICATION_SOURCE", "pass", "Exact frozen receiver application profile and separate field-scoped review")
            rc_rows, rc_coverage = _receiver_rc_checks(graph, contracts, cid, rule)
            diagnostics.extend(rc_rows); coverage.append(rc_coverage)
            # Do not append covered_pin_obligations: topology alone does not
            # discharge the existing complete analog interface obligations.
            continue
        producer, receiver = effect["producer"], effect["receiver"]
        receiver_part = receiver["supplier_part"]
        issues.extend(_review_errors(rule))
        if rule["scope"] != {"supplier_part": part} or receiver_part == part:
            issues.append("independent producer/receiver source scope is required")
        for source_part in {part, receiver_part}:
            refs = [ref for ref in rule["evidence"] if ref.get("source_supplier_part") == source_part]
            issues.extend(_source_errors(contracts, source_part, refs, ["application_interface_profile"]))
        if any(ref.get("source_supplier_part") not in {part, receiver_part} for ref in rule["evidence"]):
            issues.append("application citation has an unrelated source owner")
        candidates = [k[1] for k, c in graph.components.items() if c.is_core and c.anchor_id == receiver_part]
        if len(candidates) != 1:
            issues.append("receiver physical instance is absent or ambiguous")
        if issues:
            add("ANALOG_APPLICATION_SOURCE", "unresolved", "Source review and unique receiver are required", reasons=issues)
            continue
        rid = candidates[0]
        _, input_issues = _conditions(binding, contracts, part)
        board = _map(_map(binding.get("input_snapshot")).get("payload"))
        index = {"requirement:" + row["id"]: {"kind": "requirement", "value": row} for row in board.get("requirements", []) if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
        selection = bind_frozen_input_conditions(board_input=board, supplier_part=receiver_part, source_index=index)
        receiver_cc = _map(_map(contracts.get("components")).get(rid))
        if receiver_cc.get("operating_selection_alias") is not None:
            from .operating_selections import rebuild_bound_operating_conditions
            # The producer's choice never grants a receiver's operating mode.
            # Rebuild the unique receiver instance against its own source option.
            selection = rebuild_bound_operating_conditions(contracts, receiver_cc["operating_selection_alias"], component_id=rid)
        conditions = selection["conditions"] if not input_issues and not selection["diagnostics"] else {}
        identities = []
        all_producer = [producer["output_pin"], *producer["ground_pins"], *producer["supply_pins"]]
        all_receiver = [receiver["positive_pin"], receiver["negative_pin"], *receiver["ground_pins"], *receiver["supply_pins"], *receiver["reference_filter_pins"]]
        if (len(set(all_producer)) != len(all_producer) or set(all_producer) != set(graph.pin_map(key))
            or len(set(all_receiver)) != len(all_receiver) or not set(all_receiver) <= set(graph.pin_map((graph.source, rid)))):
            identities.append("profile terminal categories must be distinct actual physical pins; microphone inventory must be complete")
        identities.extend(_pin_evidence(graph, contracts, cid, producer["output_pin"], "electrical_type", {"analog_output"}))
        for pid in (receiver["positive_pin"], receiver["negative_pin"]):
            identities.extend(_pin_evidence(graph, contracts, rid, pid, "electrical_type", {"analog_input"}, conditions=conditions))
        add("ANALOG_APPLICATION_SOURCE", "unresolved" if identities else "pass", "Reviewed exact analog endpoint identities", receiver=rid, reasons=identities)
        if identities:
            continue
        selected_fields = [item["field"] for item in effect["intended_configuration"]]
        if len(set(selected_fields)) != len(selected_fields):
            input_issues.append("intended configuration fields must be unique")
        condition = evaluate_condition({"all": [{**item, "op": "eq"} for item in effect["intended_configuration"]]}, selection["conditions"])
        config_status = "unresolved" if input_issues or selection["diagnostics"] or condition["status"] not in {"true", "false"} else "pass" if condition["status"] == "true" else "fail"
        add("ANALOG_INTENDED_CONFIGURATION", config_status, "Explicit frozen receiver configuration; firmware execution is not asserted", required=effect["intended_configuration"], reasons=input_issues + selection["diagnostics"], condition=condition)
        ground_pin = graph.pins[(graph.source, cid, producer["ground_pins"][0])]
        ground, ground_issues = _rail_potential(graph, contracts, ground_pin)
        if ground_issues or ground is None or ground["minimum_v"] != 0 or ground["maximum_v"] != 0:
            add("ANALOG_APPLICATION_SUPPLIES", "unresolved", "Actual source-authenticated common zero ground is required", reasons=ground_issues)
            continue
        supplies = _supply_checks(graph, contracts, cid, producer, ground_pin.net)
        supplies += _supply_checks(graph, contracts, rid, receiver, ground_pin.net, effect["receiver_supply_v"])
        add("ANALOG_APPLICATION_SUPPLIES", _status(supplies), "Actual supply/reference nets checked independently of audio waveform", checks=supplies)
        branches = {
            "positive": trace_ac_branch(graph, (graph.source, cid, producer["output_pin"]), (graph.source, rid, receiver["positive_pin"])),
            "negative": trace_ac_branch(graph, (graph.source, rid, receiver["negative_pin"]), ground_pin.key, ground_end=True)}
        for pid in receiver["reference_filter_pins"]:
            branches["filter:" + pid] = trace_ac_branch(graph, (graph.source, rid, pid), ground_pin.key, ground_end=True)
        topology_status = _status(list(branches.values()))
        used = [c for result in branches.values() for c in result.get("components", [])]
        if len(set(used)) != len(used):
            topology_status = "fail"
        add("ANALOG_APPLICATION_TOPOLOGY", topology_status, "Separate source-profile AC barriers and filter returns; extra signal branches reject applicability", branches=branches)
        dc_status = "unresolved"
        if effect.get("dc_interface_class") == "independently_biased_ac_codec_input":
            # This is a separately reviewed application inference, not a numeric
            # bias/stress interval. An old topology receipt cannot authorize the
            # additional field. Recheck each owner's claim against frozen assets.
            dc_errors = []
            from .conditional_pin_rules import evidence_field_scope_errors
            for source_part in {part, receiver_part}:
                refs = [ref for ref in rule["evidence"] if ref.get("source_supplier_part") == source_part
                        and not evidence_field_scope_errors([ref], ["analog_static_application_class"])]
                dc_errors.extend(_source_errors(contracts, source_part, refs, ["analog_static_application_class"]))
            dc_status = _status([{"status": value} for value in
                (config_status, _status(supplies), topology_status, "unresolved" if dc_errors else "pass")])
            add("ANALOG_STATIC_DC_APPLICABILITY", dc_status,
                "Source-reviewed independently biased codec/microphone application with separate actual supplies and a DC barrier; no numerical signal envelope is inferred",
                receiver=rid, reasons=dc_errors, source_rule_id=rule["id"],
                claim_scope="steady_state_dc_application_admissibility",
                capacitor_rating_checked_separately=True, nominal_value_equivalence_credit=False)
            if dc_status == "pass":
                for owner, pin_ids in ((cid, [producer["output_pin"]]),
                                       (rid, [receiver["positive_pin"], receiver["negative_pin"]])):
                    covered_pins.extend({"component": owner, "pin": pid, "status": "pass",
                        "source_rule_id": rule["id"], "producer_component": cid,
                        "obligations": ["selected_mode", "dc_isolation", "static_bias_application", "source_supply_compatibility"],
                        "claim_scope": "steady_state_dc_application_admissibility"} for pid in pin_ids)
        else:
            add("ANALOG_STATIC_SIGNAL_STRESS", "unresolved", "The reviewed topology does not yet qualify the actual analog bias/stress applicability; a source-reviewed compatible bias class or applicable signal limits are still required", receiver=rid)
        coverage.append({"component": cid, "component_type": component.type,
            "status": _status([{"status": value} for value in (config_status, _status(supplies), topology_status, dc_status)]),
            "application_topology": topology_status, "intended_configuration": config_status,
            "supplies": _status(supplies), "static_dc_applicability": dc_status, "receiver": rid,
            "numerical_signal_envelope": "not_inferred", "capacitor_rating_checked_separately": True,
            "discharged_obligations": [name for name, state in (("dc_isolation_topology", topology_status),
                ("intended_analog_configuration", config_status), ("source_supply_compatibility", _status(supplies)),
                ("static_bias_application", dc_status)) if state == "pass"],
            "outside_scope_not_additional_acceptance_gates": ["acoustic_SNR", "full_scale_distortion", "layout", "thermal_simulation"],
            "nominal_value_equivalence_credit": False})
    return {"diagnostics": diagnostics, "coverage": coverage, "handled_components": handled,
            "covered_pin_obligations": covered_pins, "status": _status(diagnostics),
            "scope": "reviewed static application obligations and ideal supply conditions; capacitor ratings remain separate"}


__all__ = ["SCHEMA", "ANALOG_DEVICE_ELECTRICAL_SCHEMA", "trace_ac_branch", "trace_receiver_rc_branch", "is_receiver_rc_profile", "check_analog_applications"]
