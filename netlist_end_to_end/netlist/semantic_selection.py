"""Mechanical planner constraints derived from authenticated device contracts."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .prompts import load_prompt

from datasheet_automation.knowledge.evidence import primary_source_registry

from validation_platform.conditional_pin_rules import evaluate_pin_rules
from validation_platform.contract_binding import _authenticated_references, supply_pin_numbers
from validation_platform.pin_contracts import POWER_ROLES, role_mode_compatible
from validation_platform.operating_selections import resolve_operating_selections
from validation_platform.planning_obligations import CANDIDATE_MODE


def resolve_plan_operating_selections(plan: Mapping, pool: Mapping, authority: Mapping | None) -> dict:
    """Resolve a local/final plan against caller-owned complete fixed facts."""
    declared = any(m.get("operating_selections") for m in plan.get("modules", []))
    if not declared:
        return {"schema": "pcbagent.operating_selections.v1", "receipts": [], "conditions_by_alias": {}, "diagnostics": []}
    if not isinstance(authority, Mapping):
        return {"receipts": [], "conditions_by_alias": {}, "diagnostics": [{"rule": "OPERATING_SELECTION_CONTEXT",
            "status": "fail", "message": "Source-reviewed operating selections require the caller's frozen input, inventory and knowledge"}]}
    return resolve_operating_selections(plan, authority["inventory"], authority["knowledge"],
        board_input=authority["board_input"], source_index=authority["source_index"], active_aliases=set(pool))


def fixed_device_part(specification: Mapping) -> str:
    """Read internal fixed identity without putting a C/S ID on abstract modules."""
    identity = specification.get("identity") or {}
    return identity.get("supplier_part") or specification["device"]["id"]


def authenticated_selection_constraints(record: Mapping, part: str, binding: Mapping) -> dict:
    """Expose only source-authenticated closed choices and canonical supply pins.

    Unknown facts stay absent. Conditional mode choices use the same reviewed
    rule evaluator and exact frozen input bindings as post-generation checking.
    """
    contract = record.get("contracts") or {}
    result = {"pins": {}, "supply_symbols": {}}
    if contract.get("provenance", {}).get("source_authentication_version") not in {2, 3}:
        return result
    for number, fact in (contract.get("pins") or {}).items():
        if not isinstance(fact, Mapping):
            continue
        checked = evaluate_pin_rules(fact, binding, supplier_part=part, pin_number=str(number))
        effective = checked["fact"]; choices = {}
        for field in ("semantic_role", "capabilities", "modes", "electrical_type"):
            value = effective.get(field)
            if value is None:
                continue
            ids = set(checked["derived_fields"].get(field, []))
            refs = ([e for rule in fact.get("conditional_rules", []) if rule.get("id") in ids for e in rule.get("evidence", [])]
                    if ids else (fact.get("field_evidence") or {}).get(field, fact.get("evidence")))
            if _authenticated_references(record, part, refs, [field]):
                choices[field] = deepcopy(value)
        selection_rules, source_errors = [], []
        for rule in fact.get("conditional_rules") or []:
            if not isinstance(rule, Mapping) or not isinstance(rule.get("effect"), Mapping) or rule["effect"].get("kind") not in {"pin_selection_constraint", "pin_function_role"}:
                continue
            fields = ["capabilities", "semantic_role"] if rule["effect"]["kind"] == "pin_function_role" else ["capabilities", "modes"]
            has_original = (rule["effect"]["kind"] != "pin_function_role" or
                            bool(primary_source_registry({**record, "supplier_part": part})))
            if not has_original or not _authenticated_references(record, part, rule.get("evidence"), fields):
                source_errors.append({"rule_id": rule.get("id"), "reason": "selection constraint lacks exact original-source field authentication"})
            else:
                selection_rules.append(deepcopy(dict(rule)))
        if selection_rules or source_errors:
            choices.update(pin_selection_rules=selection_rules, pin_selection_source_errors=source_errors,
                pin_selection_supplier_part=part,
                # Only caller-rebuilt input/independent internal-option selections
                # enter this context. A pin_usage configuration is not authority.
                pin_selection_conditions={key: deepcopy(binding[key]) for key in ("configuration", "requirements") if key in binding})
        if choices:
            result["pins"][str(number)] = choices
    actual_numbers = {pin.get("pin_number") for pin in record.get("pins", [])}
    for symbol, fact in (contract.get("supply_pins") or {}).items():
        if not isinstance(symbol, str) or not symbol or not isinstance(fact, Mapping):
            continue
        numbers = supply_pin_numbers(fact)
        if (isinstance(numbers, list) and numbers and all(isinstance(pin, str) and pin in actual_numbers for pin in numbers)
                and len(set(numbers)) == len(numbers)
                and _authenticated_references(record, part, fact.get("evidence"), ["pin_number", "pin_name"])):
            result["supply_symbols"][symbol] = {"pin_numbers": sorted(numbers), "evidence": deepcopy(fact["evidence"])}
    return result


def validate_machine_selection(module: Mapping, pool: Mapping, domains: Mapping, *,
                               planning_validation_mode: str = "strict") -> list[dict]:
    """Reject known-invalid selections before generating any SCL."""
    diagnostics = []
    def add(code: str, message: str, use: Mapping, **details: Any) -> None:
        diagnostics.append({"severity": "error", "code": code, "message": message,
            "module": module["id"], "device": use["device"], "pin": use["pin"], **details})
    for use in module["pin_usage"]:
        if use["device"] not in pool:
            continue
        choices = pool[use["device"]].get("selection_constraints", {}).get(use["pin"], {})
        for error in choices.get("pin_selection_source_errors", []):
            add("PIN_SELECTION_CONSTRAINT", "Source-backed selection constraint is unresolved", use,
                status="unresolved", constraint=deepcopy(error))
        if choices.get("pin_selection_rules"):
            conditional = evaluate_pin_rules({**choices, "conditional_rules": choices["pin_selection_rules"]},
                {**deepcopy(choices.get("pin_selection_conditions", {})),
                 "usage": use.get("capability"), "mode": use.get("selected_mode")},
                supplier_part=choices.get("pin_selection_supplier_part"), pin_number=use["pin"])
            choices = conditional["fact"]
            for diagnostic in conditional["diagnostics"]:
                if diagnostic.get("status") not in {"pass", "not_applicable"}:
                    add("PIN_SELECTION_CONSTRAINT", "Selected capability, mode and source-required configuration must be compatible", use,
                        status=diagnostic["status"], constraint=diagnostic)
        role = choices.get("semantic_role")
        selected = use["selected_role"]
        if role and role != "unknown" and not (selected == role or role == "logic_io" and selected in {"logic_in", "logic_out"}):
            add("PIN_SELECTED_ROLE_CONTRACT", "selected_role must preserve the authenticated role; an unused output remains an output, unconnected=true expresses the approved open state",
                use, expected_role=role, selected_role=selected)
        for field, available in (("capability", choices.get("capabilities")), ("selected_mode", choices.get("modes"))):
            missing_open_selection = (planning_validation_mode == CANDIDATE_MODE and use.get("unconnected") is True
                                      and field in use and use[field] is None)
            if isinstance(available, list) and available and use.get(field) not in available and not missing_open_selection:
                add("PIN_MACHINE_SELECTION", f"{field} must be one of this pin's authenticated machine tokens; usage is explanatory prose",
                    use, field=field, allowed=available, selected=use.get(field))
        compatible = role_mode_compatible(selected, use.get("selected_mode"))
        if use.get("selected_mode") is not None and compatible is False:
            add("PIN_ROLE_MODE_CONTRACT", "selected role and mode are incompatible", use)
        if use.get("domain") is not None and use["domain"] not in domains:
            add("PIN_UNKNOWN_DOMAIN", "pin domain must be a declared global supply/logic context, not an individual signal, oscillator or capacitor node name",
                use, selected_domain=use["domain"], allowed_domains=sorted(domains),
                repair_hint=load_prompt("selection_unknown_domain.md"))
        # A documented generic input direction does not make feedback, sensing
        # or oscillator nodes digital supply-domain interfaces. Consult the
        # authenticated role, never the model's selected_role. This only removes
        # an inapplicable planning obligation; actual node voltage, source limits
        # and application requirements remain independent electrical checks.
        signal_node_input = (choices.get("electrical_type") == "input" and role in {
            "buck_fb", "feedback_input", "parameter_control", "sense_plus", "sense_minus", "oscillator_in", "oscillator_out"})
        needs_domain = (role in POWER_ROLES or choices.get("electrical_type") in {
            "push_pull_output", "open_drain", "open_source", "tri_state", "bidirectional"}
            or choices.get("electrical_type") == "input" and not signal_node_input)
        if not use["unconnected"] and needs_domain and domains and use.get("domain") is None:
            add("PIN_DOMAIN_SELECTION", "this evidenced connected pin requires an explicit power/logic domain selection; a logic domain does not short the signal to a supply rail",
                use, allowed_domains=sorted(domains))
    return diagnostics


__all__ = ["authenticated_selection_constraints", "validate_machine_selection", "fixed_device_part", "resolve_plan_operating_selections"]
