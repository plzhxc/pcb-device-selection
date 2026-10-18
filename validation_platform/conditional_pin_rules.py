"""Bounded, evidence-bearing rules for a selected static operating scenario.

This module executes reviewed device facts, not Python expressions or model
judgements. A rule's independent review covers its complete immutable payload;
source attribution by itself is insufficient to approve a conditional claim.
Unknown conditions never enable an effect. No service or database is accessed.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import re
from typing import Any

from datasheet_automation.knowledge.evidence import normalize_source_text
from .pin_contracts import decimal_number, evidence_errors, evidence_field_scope_errors, PAPER_ROLES, PROJECT_ROLES

SCHEMA = "pcbagent.conditional_pin_rules.v1"
OPERATING_PIN_MODES = frozenset({"power_in", "power_out", "analog_input", "analog_output"})
CONDITIONAL_RULE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["id", "scope", "when", "effect", "evidence"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "scope": {"type": "object", "additionalProperties": False, "required": ["supplier_part", "pin_number"],
                  "properties": {"supplier_part": {"type": "string", "pattern": "^[CS][0-9]+$"},
                                 "pin_number": {"type": "string", "minLength": 1}}},
        "when": {"type": "object", "additionalProperties": False, "required": ["all"],
                 "properties": {"all": {"type": "array", "maxItems": 32, "items": {
                     "type": "object", "additionalProperties": False, "required": ["field", "op", "value"],
                     "oneOf": [
                         {"properties": {"field": {"type": "string", "not": {"const": "terminal_voltage_v"}},
                                         "op": {"enum": ["eq", "in", "gte", "lte"]},
                                         "value": {"type": ["string", "number", "boolean", "array"]}}},
                         {"properties": {"field": {"const": "terminal_voltage_v"}, "op": {"const": "within"},
                                         "value": {"type": "object", "additionalProperties": False,
                                             "required": ["positive_pin", "reference_pin", "minimum_v", "maximum_v"],
                                             "properties": {"positive_pin": {"type": "string", "minLength": 1},
                                                 "reference_pin": {"type": "string", "minLength": 1},
                                                 "minimum_v": {"type": "number"}, "maximum_v": {"type": "number"}}}}}],
                     "properties": {"field": {}, "op": {}, "value": {}}}}}},
        "effect": {"oneOf": [
            {"type": "object", "additionalProperties": False,
             "required": ["kind", "capability", "semantic_role"],
             "properties": {"kind": {"const": "pin_function_role"},
                 "capability": {"type": "string", "minLength": 1},
                 "semantic_role": {"enum": sorted(PAPER_ROLES | PROJECT_ROLES)}}},
            {"type": "object", "additionalProperties": False,
             "required": ["kind", "capability", "allowed_modes", "requires"],
             "properties": {"kind": {"const": "pin_selection_constraint"},
                 "capability": {"type": "string", "minLength": 1},
                 "allowed_modes": {"type": "array", "minItems": 1, "maxItems": 32, "uniqueItems": True,
                                   "items": {"type": "string", "minLength": 1}},
                 "requires": {"type": "object", "additionalProperties": False, "required": ["all"],
                     "properties": {"all": {"type": "array", "maxItems": 32, "items": {
                         "type": "object", "additionalProperties": False, "required": ["field", "op", "value"],
                         "properties": {"field": {"type": "string", "pattern": "^(configuration|requirements)\\.[A-Za-z][A-Za-z0-9_.-]{0,100}$"},
                                        "op": {"enum": ["eq", "in"]},
                                        "value": {"type": ["string", "number", "boolean", "array"]}}}}}}}},
            {"type": "object", "additionalProperties": False, "required": ["kind"], "minProperties": 2,
             "properties": {"kind": {"const": "connection_policy"}, "allow_nc": {"type": "boolean"},
                            "required_connection": {"type": "boolean"},
                            "intrinsic_policy": {"enum": ["output_only_open_allowed", "input_only_must_not_float"]}}},
            {"type": "object", "additionalProperties": False, "required": ["kind", "basis", "minimum", "maximum"],
             "properties": {"kind": {"const": "voltage_range"}, "basis": {"const": "recommended_operating"},
                            "minimum": {"$ref": "#/$defs/voltage"}, "maximum": {"$ref": "#/$defs/voltage"}}},
            {"type": "object", "additionalProperties": False, "required": ["kind", "check"],
             "properties": {"kind": {"const": "not_applicable"}, "check": {"enum": ["resources", "gpio_remap_safe"]}}},
            *[{"type": "object", "additionalProperties": False, "required": ["kind", "relation", "pins"],
               "properties": {"kind": {"const": "pin_relation"}, "relation": {"const": relation},
                              "pins": {"type": "array", "minItems": minimum, "maxItems": 32, "uniqueItems": True,
                                       "items": {"type": "string", "minLength": 1}}}}
              for relation, minimum in (("same_net", 2), ("unconnected", 1))],
            {"type": "object", "additionalProperties": False, "required": ["kind", "electrical_type", "modes"],
             "properties": {"kind": {"const": "operating_pin_mode"}, "electrical_type": {"enum": sorted(OPERATING_PIN_MODES)},
                            "modes": {"type": "array", "minItems": 1, "maxItems": 1, "items": {"enum": sorted(OPERATING_PIN_MODES)}}}},
            {"type": "object", "additionalProperties": False, "required": ["kind", "basis", "direction", "levels"],
             "properties": {"kind": {"const": "logic_levels"}, "basis": {"const": "documented_operating_logic"},
                            "direction": {"enum": ["input", "output"]}, "maximum_output_current_a": {"type": ["number", "string"]},
                            "levels": {"type": "object", "additionalProperties": False,
                                       "properties": {name: {"$ref": "#/$defs/voltage"} for name in
                                           ("vih_min_v", "vil_max_v", "voh_min_v", "vol_max_v", "operating_min_v", "operating_max_v")}}}},
            {"type": "object", "additionalProperties": False,
             "required": ["kind", "profile", "direction", "capability", "supply", "reference_supply", "supply_min_v", "supply_max_v"],
             "properties": {"kind": {"const": "application_interface_profile"}, "profile": {"const": "same_supply_direct_spi"},
                 "direction": {"enum": ["input", "output"]},
                 **{key: {"type": "string", "minLength": 1} for key in ("capability", "supply", "reference_supply")},
                 **{key: {"type": ["number", "string"]} for key in ("supply_min_v", "supply_max_v")}}}]},
        "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
        "review": {"type": "object", "properties": {
            "method": {"enum": ["independent_source_review", "bounded_source_parser", "proposed"]},
            "reviewer": {"type": "string"}, "rule_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "evidence_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}}},
    "$defs": {"voltage": {"oneOf": [
        {"type": "object", "additionalProperties": False, "required": ["constant_v"],
         "properties": {"constant_v": {"type": ["number", "string"]}}},
        {"type": "object", "additionalProperties": False, "required": ["supply", "scale", "offset_v"],
         "properties": {"supply": {"type": "string"}, "scale": {"type": ["number", "string"]},
                        "offset_v": {"type": ["number", "string"]}}}]}}
}
_FIELDS = {"required_connection", "allow_nc", "voltage_min_v", "voltage_max_v"}
_CONDITIONAL = re.compile(r"\b(?:if|when|unless|provided|only|except|depending|unused)\b|\bnot\s+used\b|\boperation\s+mode\b|"
                          r"如果|若|当|仅|只有|除非|不用|未使用|未用|悬空时|模式", re.I)
_FORBIDDEN_OPEN = re.compile(r"(?:not\s+(?:allowed|permitted)|must\s+not|shall\s+not|do\s+not|"
                             r"cannot|can't|unstable|禁止|不得|不允许|不能|不可)", re.I)
_ABSOLUTE = re.compile(r"absolute\s+maximum|stress\s+rating|绝对最大|极限参数", re.I)
_TERMINAL_DIFFERENTIAL = re.compile(
    r"\bV[\s_{}()]*[GD][\s_{}()]*S\b|\b(?:gate|drain)[\s\-–—]*source\s+(?:voltage|breakdown)\b|"
    r"栅[极]?\s*[\-–—]?\s*源[极]?\s*电压|漏[极]?\s*[\-–—]?\s*源[极]?\s*电压", re.I)
_RESET_STATE = re.compile(r"\b(?:during|after|on)\s+(?:(?:and|power[- ]?on)\s+)?(?:after\s+)?reset\b|\breset\s+state\b|复位(?:时|后|状态)", re.I)
_FLOATING_STATE = re.compile(r"floating\s+inputs?|high[- ]impedance|高阻|浮空输入", re.I)
_EXPLICIT_OPEN_PERMISSION = re.compile(r"(?:may|can)\s+(?:be\s+)?(?:left\s+)?(?:open|unconnected)|leave\s+(?:\w+\s+){0,3}unconnected|可(?:以)?(?:不接|悬空)", re.I)
_PATH = re.compile(r"^(configuration|requirements)\.([A-Za-z][A-Za-z0-9_.-]{0,100})$")


def rule_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _normal(text: Any) -> str:
    return normalize_source_text(str(text))


def source_clause(quote: str, source_text: str) -> str | None:
    """Recover complete surrounding clauses, including a truncated negation.

    Ambiguous repeated substrings are rejected. Whitespace and NFKC changes are
    allowed; omission of words/table columns is not. Decimal points are retained.
    """
    text, fragment = _normal(source_text), _normal(quote)
    if not fragment or text.count(fragment) != 1:
        return None
    start = text.index(fragment); end = start + len(fragment)
    boundaries = [m.end() for m in re.finditer(r"(?<!\d)[.!?](?!\d)|[。！？;；]", text)]
    left = max((p for p in boundaries if p <= start), default=0)
    right = min((p for p in boundaries if p >= end), default=len(text))
    return text[left:right].strip()


def unconditional_claim_errors(field: str, value: Any, evidence: Any, *,
                               source_pages: Mapping[str, str] | None = None) -> list[str]:
    """Conservative guard, not a general natural-language entailment proof.

    The extractor should pass original-page text to detect omitted surrounding
    conditions/negations. Without it this guard can inspect only supplied quotes.
    A guard rejection clears the unconditional field, never invents a new rule.
    """
    if field not in _FIELDS or value is None:
        return []
    errors = []
    for index, row in enumerate(evidence if isinstance(evidence, list) else []):
        row = _map(row); quote = _normal(row.get("quote", ""))
        if field in {"allow_nc", "required_connection"} and row.get("intrinsic_connection_policies"):
            errors.append(f"evidence[{index}]: intrinsic guide permission requires an exact-pin reviewed policy rule, not a raw scalar")
        clause = quote
        if source_pages is not None:
            page = source_pages.get(row.get("source_ref"))
            clause = source_clause(quote, page) if isinstance(page, str) else None
            if clause is None:
                errors.append(f"evidence[{index}]: complete unique original-source clause unavailable")
                continue
        if _CONDITIONAL.search(clause):
            errors.append(f"evidence[{index}]: conditional source cannot establish an unconditional fact")
        if _RESET_STATE.search(clause):
            errors.append(f"evidence[{index}]: a reset-state description is not an unconditional operating obligation")
        if field == "allow_nc" and value is True and _FORBIDDEN_OPEN.search(clause):
            errors.append(f"evidence[{index}]: negation or unstable-open warning requires explicit source review")
        if field == "allow_nc" and value is True and _FLOATING_STATE.search(clause) and not _EXPLICIT_OPEN_PERMISSION.search(clause):
            errors.append(f"evidence[{index}]: an internal floating-input/high-impedance state does not license an external NC")
        if field.startswith("voltage_") and _ABSOLUTE.search(clause):
            errors.append(f"evidence[{index}]: absolute maximum is not an operating range")
        if field.startswith("voltage_") and _TERMINAL_DIFFERENTIAL.search(clause):
            errors.append(f"evidence[{index}]: a terminal-pair voltage requires explicit endpoints and an operating scenario; it is not a scalar pin operating range")
    return list(dict.fromkeys(errors))


def parse_unused_interface_permission(*, feature: str, pin_name: str, supplier_part: str,
                                      pin_number: str, evidence: Mapping, source_text: str) -> dict | None:
    """Parse one explicit named output permission; never expand grouped pins.

    Accepted language is exactly ``If X is not used, the pin(s) Y can be left
    open.``. The caller must separately establish Y's physical-pin identity from
    its authenticated pin table. Multi-interface OR and mux aliases require an
    independent review and are deliberately not inferred by this parser.
    """
    if not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", x) for x in (feature, pin_name)):
        return None
    if not re.fullmatch(r"[CS][0-9]+", supplier_part) or not isinstance(pin_number, str) or not pin_number:
        return None
    if evidence_errors([evidence]) or evidence.get("source_authentication_version") != 2 or evidence.get("provenance") != "original_pdf_native":
        return None
    quote = _normal(evidence.get("quote", "")); clause = source_clause(quote, source_text)
    pattern = rf"(?:\d+\.\s*)?If {re.escape(feature)} is not used, the pins? {re.escape(pin_name)} can be left open\."
    if clause is None or not re.fullmatch(pattern, quote, re.I) or not re.fullmatch(pattern, clause, re.I):
        return None
    rule = {"id": f"unused_{feature}_{pin_name}",
            "scope": {"supplier_part": supplier_part, "pin_number": pin_number},
            "when": {"all": [{"field": f"configuration.{feature}_used", "op": "eq", "value": False}]},
            "effect": {"kind": "connection_policy", "allow_nc": True, "required_connection": False},
            "evidence": [deepcopy(dict(evidence))]}
    rule["review"] = {"method": "bounded_source_parser", "reviewer": "validation_platform.conditional_pin_rules.parse_unused_interface_permission",
                      "parser_version": "1.0", "matched_clause": clause,
                      "rule_sha256": rule_digest(rule), "evidence_sha256": rule_digest(rule["evidence"])}
    return rule


def _scalar(value: Any) -> bool:
    return isinstance(value, (str, bool)) or type(value) in {int, float} and decimal_number(value) is not None


def bind_input_selection(field: str, value: Any, source_refs: Any, *, board_input: Mapping,
                         supplier_part: str, source_index: Mapping) -> dict:
    """Authenticate one condition against the exact immutable input selection.

    Shared by the pre-compilation device pool and post-compilation adapter. This
    never selects a value from prose or resolves individual identical instances.
    """
    diagnostics = []
    unknown = {"value": None, "evidence": []}
    def reject(code: str, message: str, status: str = "unresolved") -> dict:
        return {"selection": unknown, "diagnostics": [{"rule": code, "status": status, "message": message, "field": field}]}
    if not isinstance(field, str) or not _PATH.fullmatch(field) or not _scalar(value):
        return reject("BINDING_CONDITION_SCHEMA", "A supported condition path and explicit scalar are required")
    rows = board_input.get("requirements", [])
    matches = []
    for raw in rows if isinstance(rows, list) else []:
        requirement = _map(raw); choice = _map(requirement.get("selection"))
        if choice.get("field") == field and choice.get("board_id") == board_input.get("board_id") and choice.get("device_id") == supplier_part:
            matches.append((requirement, choice))
    if not matches or any(choice.get("applies_to") != "all_instances" for _, choice in matches):
        return reject("BINDING_CONDITION_INPUT", "Condition lacks an explicit frozen-input selection for this board and supplier part")
    if any(type(choice.get("value")) is not type(value) or choice.get("value") != value for _, choice in matches):
        return reject("BINDING_CONDITION_VALUE", "Planned condition conflicts with a frozen-input value or type", "fail")
    refs = source_refs if isinstance(source_refs, list) else []
    selected = [(requirement, f"requirement:{requirement.get('id')}") for requirement, _ in matches
                if f"requirement:{requirement.get('id')}" in refs and f"requirement:{requirement.get('id')}" in source_index]
    if not selected:
        return reject("BINDING_CONDITION_SOURCE", "Condition must cite the exact structured input requirement")
    sha = rule_digest(dict(board_input))
    evidence = [{"source_kind": "input", "source_ref": str(board_input.get("board_id")),
                 "document_sha256": sha, "locator": ref,
                 "quote": json.dumps(requirement, ensure_ascii=False, sort_keys=True)} for requirement, ref in selected]
    return {"selection": {"value": deepcopy(value), "evidence": evidence,
            "selection_authentication": {"method": "exact_frozen_input_selection", "field": field,
                "supplier_part": supplier_part, "board_id": board_input.get("board_id"), "applies_to": "all_instances", "input_sha256": sha}},
            "diagnostics": diagnostics}


def bind_frozen_input_conditions(*, board_input: Mapping, supplier_part: str,
                                 source_index: Mapping, planned: Mapping | None = None,
                                 fields: list[str] | None = None) -> dict:
    """Bind explicit all-instance input selections without model repetition.

    ``planned`` contains the original pin-usage configuration/requirements maps.
    When ``fields`` is provided, only those rule predicates and explicit planned
    assertions are considered. Conflicting planned values remain unresolved and
    fail; the original assertion is retained for audit. No prose is interpreted.
    """
    planned = _map(planned)
    assertions = {f"{collection}.{name}": _map(selection)
                  for collection in ("configuration", "requirements")
                  for name, selection in _map(planned.get(collection)).items()}
    choices = {}
    for raw in board_input.get("requirements", []) if isinstance(board_input.get("requirements"), list) else []:
        requirement = _map(raw); selection = _map(requirement.get("selection")); field = selection.get("field")
        if (isinstance(field, str) and _PATH.fullmatch(field)
                and selection.get("board_id") == board_input.get("board_id")
                and selection.get("device_id") == supplier_part):
            choices.setdefault(field, []).append((requirement, selection))
    wanted = set(choices) if fields is None else {field for field in fields if isinstance(field, str) and _PATH.fullmatch(field)}
    wanted.update(assertions)
    conditions, diagnostics = {}, []
    for field in sorted(wanted):
        matches = choices.get(field, [])
        if matches:
            value = matches[0][1].get("value")
            refs = [f"requirement:{requirement.get('id')}" for requirement, _ in matches]
            checked = bind_input_selection(field, value, refs, board_input=board_input,
                                           supplier_part=supplier_part, source_index=source_index)
            if field in assertions and (type(assertions[field].get("value")) is not type(value)
                                        or assertions[field].get("value") != value):
                checked = {"selection": {"value": None, "evidence": []}, "diagnostics": [{
                    "rule": "BINDING_CONDITION_VALUE", "status": "fail", "field": field,
                    "message": "Planned condition conflicts with the explicit frozen-input selection"}]}
            elif field in assertions:
                checked = bind_input_selection(field, value, assertions[field].get("source_refs"),
                    board_input=board_input, supplier_part=supplier_part, source_index=source_index)
        elif field in assertions:
            assertion = assertions[field]
            checked = bind_input_selection(field, assertion.get("value"), assertion.get("source_refs"),
                board_input=board_input, supplier_part=supplier_part, source_index=source_index)
        else:
            # Absent requirements remain absent. The conditional evaluator reports
            # the applicable missing predicate, rather than inventing a default.
            continue
        match = _PATH.fullmatch(field)
        if match is None:
            diagnostics.extend(checked["diagnostics"])
            continue
        conditions.setdefault(match[1], {})[match[2]] = checked["selection"]
        diagnostics.extend(checked["diagnostics"])
    return {"conditions": conditions, "diagnostics": diagnostics,
            "planned_conditions": {key: deepcopy(planned[key]) for key in ("configuration", "requirements") if key in planned}}


def _value(binding: Mapping, field: str) -> tuple[Any, list[str]]:
    if field in {"mode", "usage"}:
        value, evidence = binding.get(field), binding.get("evidence")
    elif match := _PATH.fullmatch(field):
        item = _map(_map(binding.get(match[1])).get(match[2]))
        value, evidence = item.get("value"), item.get("evidence")
    else:
        return None, ["unsupported condition field"]
    issues = evidence_errors(evidence, allow_input=True)
    if not _scalar(value):
        issues.append("explicit scalar selected value required")
    return value, issues


def has_terminal_voltage_predicate(rules: Any) -> bool:
    return isinstance(rules, list) and any(
        _map(p).get("field") == "terminal_voltage_v"
        for rule in rules for p in (_map(_map(rule).get("when")).get("all")
            if isinstance(_map(_map(rule).get("when")).get("all"), list) else []))


def rule_condition_fields(rules: Any) -> list[str]:
    """Names needed by the existing frozen input/internal-option binder."""
    fields = []
    for rule in rules if isinstance(rules, list) else []:
        conditions = [_map(rule).get("when")]
        effect = _map(_map(rule).get("effect"))
        if effect.get("kind") == "pin_selection_constraint":
            conditions.append(effect.get("requires"))
        for condition in conditions:
            predicates = _map(condition).get("all")
            for predicate in predicates if isinstance(predicates, list) else []:
                field = _map(predicate).get("field")
                if isinstance(field, str):
                    fields.append(field)
    return fields


def _selection_constraint_errors(rule: Mapping, fact: Mapping) -> list[str]:
    effect = _map(rule.get("effect")); modes = effect.get("allowed_modes")
    required = _map(effect.get("requires")); predicates = required.get("all")
    if (set(effect) != {"kind", "capability", "allowed_modes", "requires"}
            or not isinstance(effect.get("capability"), str) or not effect["capability"]
            or not isinstance(modes, list) or not 1 <= len(modes) <= 32
            or any(not isinstance(mode, str) or not mode for mode in modes)
            or len(set(modes)) != len(modes)):
        return ["selection constraint requires one capability and a bounded distinct mode list"]
    if rule.get("when") != {"all": []}:
        return ["selection constraints must use unconditional outer applicability; put required configuration in effect.requires"]
    if (not isinstance(fact.get("capabilities"), list) or effect["capability"] not in fact["capabilities"]
            or not isinstance(fact.get("modes"), list) or any(not isinstance(mode, str) for mode in fact["modes"])
            or not set(modes) <= set(fact["modes"])):
        return ["selection constraint may only restrict independently supported capability/mode facts"]
    if set(required) != {"all"} or not isinstance(predicates, list) or len(predicates) > 32:
        return ["requires.all must contain at most 32 frozen configuration/requirement predicates"]
    for p in predicates:
        if (not isinstance(p, Mapping) or set(p) != {"field", "op", "value"}
                or not isinstance(p.get("field"), str) or not _PATH.fullmatch(p["field"])
                or p.get("op") not in {"eq", "in"}
                or p["op"] == "eq" and not _scalar(p["value"])
                or p["op"] == "in" and (not isinstance(p["value"], list) or not 1 <= len(p["value"]) <= 32
                    or not all(_scalar(value) for value in p["value"]))):
            return ["selection requirements accept only bounded sourced configuration/requirements eq/in predicates"]
    return []


class TerminalVoltageContext:
    """Graph-derived evaluator; never deserialize this object from a binding.

    The independent binder owns the received generation/input/knowledge roots.
    Rebuild their derived association before resolving any physical voltage.
    """
    def __init__(self, graph, contracts):
        from .regulator_transfer import _generation_errors, attach_regulator_transfers
        from .static_voltages import contexts_for, StaticVoltageContext
        self.graph, self.contracts = graph, contracts
        self.errors = _generation_errors(graph, contracts)
        contexts = attach_regulator_transfers(contexts_for(contracts), graph, contracts)
        selected = contracts.get("_static_voltage_context")
        # The scenario selector is not a supplied expression/space authority.
        candidates = [c for c in contexts if isinstance(selected, StaticVoltageContext)
                      and c.space.scenario_id == selected.space.scenario_id]
        if selected is None and len(contexts) == 1:
            candidates = contexts
        self.context = candidates[0] if len(candidates) == 1 else None
        if self.context is None or self.context.binding is None:
            self.errors.append("one independently bound explicit source scenario is required; nominal-only rails are insufficient")
        elif self.context.errors:
            self.errors.extend(self.context.errors)

    def evaluate(self, predicate, *, component_id, pin_number, rule):
        from .certificate_facts import certificate_source_errors, EFFECT_FIELDS
        from .regulator_transfer import _original_fact
        from .static_voltages import terminal_difference, predicate_gte
        from fractions import Fraction
        errors = list(self.errors)
        value = _map(predicate.get("value"))
        if (set(predicate) != {"field", "op", "value"} or predicate.get("field") != "terminal_voltage_v"
                or predicate.get("op") != "within" or set(value) != {"positive_pin", "reference_pin", "minimum_v", "maximum_v"}
                or any(type(value.get(k)) is not str or not value[k] for k in ("positive_pin", "reference_pin"))
                or value.get("positive_pin") == value.get("reference_pin")
                or any(type(value.get(k)) not in (int, float) or decimal_number(value[k]) is None for k in ("minimum_v", "maximum_v"))):
            errors.append("closed terminal_voltage_v/within predicate with distinct pins and finite JSON bounds required")
        if not errors and value["minimum_v"] > value["maximum_v"]:
            errors.append("ordered terminal voltage bounds required")
        component = self.graph.components.get((self.graph.source, component_id))
        cc = _map(_map(self.contracts.get("components")).get(component_id))
        part = cc.get("supplier_part")
        original = _original_fact(self.contracts, part, pin_number)
        instances = _map(_map(_map(self.contracts.get("generation_binding_snapshot")).get("compilation")).get("manifest")).get("instances", [])
        identities = [_map(i).get("identity") for i in instances if _map(i).get("component") == component_id]
        original_identity = (component is not None and component.supplier_id == part) or (
            component is not None and component.supplier_id is None and len(identities) == 1
            and _map(identities[0]).get("supplier_part") == part and cc.get("fixed_identity") == identities[0]
            and _map(self.contracts.get("authenticated_identity_anchors")).get(component_id) == part)
        if (component is None or not component.is_core or component.anchor_id != part or not original_identity
                or rule not in original.get("conditional_rules", [])):
            errors.append("complete reviewed rule must be an unchanged member of this fixed instance's original source record")
        if errors:
            return {"field": "terminal_voltage_v", "status": "unresolved", "errors": errors}
        actual = self.graph.pin_map(component.key)
        record = _map(_map(self.contracts.get("regulator_source_records")).get(part))
        original_pins = record.get("pins", [])
        numbers = [p.get("pin_number") for p in original_pins if isinstance(p, Mapping)]
        if (len(numbers) != len(set(numbers)) or set(numbers) != set(actual)
                or _map(record.get("device")).get("pin_count") != len(numbers)):
            errors.append("complete original physical pin inventory must equal actual same-instance pins")
        for endpoint, electrical in (("positive_pin", "power_in"), ("reference_pin", "ground")):
            pid = value[endpoint]; fact = _original_fact(self.contracts, part, pid)
            if pid not in actual or fact.get("electrical_type") != electrical:
                errors.append("voltage endpoints require source-authenticated power input and ground pins")
                continue
            errors.extend(certificate_source_errors(self.contracts, part,
                _map(fact.get("field_evidence")).get("electrical_type", fact.get("evidence")), ["electrical_type"]))
        fields = EFFECT_FIELDS.get(_map(rule.get("effect")).get("kind"), []) + ["terminal_voltage_v"]
        errors.extend(certificate_source_errors(self.contracts, part, rule.get("evidence"), fields))
        if errors:
            return {"field": "terminal_voltage_v", "status": "unresolved", "errors": errors}
        pins = [actual[value[k]] for k in ("positive_pin", "reference_pin")]
        nets = [p.net[1] if p.net else None for p in pins]
        potentials = [self.context.net(n) for n in nets]
        # Same-wire identity proves zero difference, never a public ground.
        reference = potentials[1]
        if (reference.expression is None or reference.reference != reference.net
                or predicate_gte(reference.expression, self.context.space.constant(0)) != "true"
                or predicate_gte(self.context.space.constant(0), reference.expression) != "true"):
            return {"field": "terminal_voltage_v", "status": "unresolved", "errors": ["actual source ground reference is unestablished"]}
        difference = terminal_difference(*potentials)
        low = predicate_gte(difference.expression, self.context.space.constant(Fraction(decimal_number(value["minimum_v"]))))
        high = predicate_gte(self.context.space.constant(Fraction(decimal_number(value["maximum_v"]))), difference.expression)
        status = "true" if low == high == "true" else "false" if "false" in (low, high) else "unresolved"
        return {"field": "terminal_voltage_v", "status": status,
                "errors": [] if status != "unresolved" else ["full actual terminal envelope does not establish the operating window"],
                "positive_pin": value["positive_pin"], "reference_pin": value["reference_pin"],
                "actual_nets": nets, "scenario_id": self.context.space.scenario_id,
                "static_context_sha256": self.context.space.context_id, "difference_basis": difference.basis,
                "lower_predicate": low, "upper_predicate": high}


def evaluate_condition(when: Any, binding: Mapping, *, terminal_evaluator=None) -> dict:
    """Kleene conjunction over explicit, sourced selections (never free text)."""
    if not isinstance(when, Mapping) or set(when) != {"all"} or not isinstance(when["all"], list) or len(when["all"]) > 32:
        return {"status": "unresolved", "errors": ["when.all must contain at most 32 predicates"]}
    rows = []
    for predicate in when["all"]:
        if not isinstance(predicate, Mapping) or set(predicate) != {"field", "op", "value"} or not isinstance(predicate.get("field"), str):
            rows.append({"status": "unresolved", "errors": ["closed predicate object required"]}); continue
        if predicate["field"] == "terminal_voltage_v":
            rows.append(terminal_evaluator(predicate) if terminal_evaluator is not None else {
                "field": "terminal_voltage_v", "status": "unresolved", "errors": ["authenticated actual graph/source context unavailable"]})
            continue
        op, expected = predicate["op"], predicate["value"]
        if op not in {"eq", "in", "gte", "lte"} or (op == "eq" and not _scalar(expected)) or (op == "in" and
                (not isinstance(expected, list) or not expected or len(expected) > 32 or not all(_scalar(x) for x in expected))):
            rows.append({"status": "unresolved", "errors": ["bounded eq/in scalar predicate required"]}); continue
        actual, issues = _value(binding, predicate["field"])
        # JSON boolean and integer are intentionally distinct (False is not 0).
        equals = lambda value: ((decimal_number(value) == decimal_number(actual))
                                if type(value) in {int, float} and type(actual) in {int, float}
                                else type(value) is type(actual) and value == actual)
        if op in {"gte", "lte"}:
            if type(expected) not in {int, float} or type(actual) not in {int, float} or decimal_number(expected) is None or decimal_number(actual) is None:
                issues.append("numeric comparison requires explicit finite JSON numbers, excluding booleans")
                matches = False
            else:
                matches = decimal_number(actual) >= decimal_number(expected) if op == "gte" else decimal_number(actual) <= decimal_number(expected)
        else:
            matches = equals(expected) if op == "eq" else any(equals(x) for x in expected)
        rows.append({"field": predicate["field"], "status": "unresolved" if issues else "true" if matches else "false", "errors": issues})
    status = "false" if any(r["status"] == "false" for r in rows) else "unresolved" if any(r["status"] == "unresolved" for r in rows) else "true"
    return {"status": status, "predicates": rows}


def _review_errors(rule: Mapping) -> list[str]:
    review = _map(rule.get("review"))
    if review.get("method") not in {"independent_source_review", "bounded_source_parser"} or not review.get("reviewer"):
        return ["independent source review or bounded source parser receipt required"]
    try:
        digest = rule_digest({k: v for k, v in rule.items() if k != "review"})
    except (TypeError, ValueError):
        return ["rule payload must be finite JSON"]
    if review.get("rule_sha256") != digest or review.get("evidence_sha256") != rule_digest(rule.get("evidence")):
        return ["source review is not bound to the complete current rule/evidence payload"]
    return []


def _intrinsic_policy_errors(rule: Mapping, fact: Mapping, part: str | None, pin: str | None) -> list[str]:
    """Join an exact reviewed guide policy to this pin's original fixed direction.

    Physical no-peer/use contradictions are still checked by pin_contracts.
    This adds no functional-completeness claim or configuration selection.
    """
    effect = _map(rule.get("effect")); policy = effect.get("intrinsic_policy")
    refs = rule.get("evidence") if isinstance(rule.get("evidence"), list) else []
    guides = [e for e in refs if _map(e).get("intrinsic_connection_policies")]
    if policy is None:
        return ["page-scoped guide policy requires its explicit intrinsic effect marker"] if guides else []
    from datasheet_automation.knowledge.secondary_pdf import INTRINSIC_POLICY_VALUES
    values = {k: effect[k] for k in ("allow_nc", "required_connection") if k in effect}
    if (effect.get("kind") != "connection_policy" or not isinstance(policy, str) or policy not in INTRINSIC_POLICY_VALUES
            or rule.get("when") != {"all": []} or not values or not guides
            or any(type(v) is not bool or v is not INTRINSIC_POLICY_VALUES[policy][k] for k, v in values.items())):
        return ["intrinsic policy requires reviewed Boolean values and no erased operating conditions"]
    output = policy == "output_only_open_allowed"
    modes = fact.get("modes")
    valid_modes = {"output", "push_pull_output", "open_drain", "open_collector"} if output else {"input"}
    if (fact.get("semantic_role") not in ({"logic_out", "clock_out"} if output else {"logic_in", "clock_in"})
            or not isinstance(modes, list) or not modes or not all(isinstance(m, str) for m in modes)
            or not set(modes) <= valid_modes):
        return ["intrinsic policy requires an independently documented fixed digital direction"]
    for evidence in guides:
        if (evidence.get("source_kind") != "manufacturer_application_note"
                or evidence.get("provenance") != "reviewed_original_pdf_transcription"
                or evidence.get("source_authentication_version") != 3):
            return ["intrinsic policy requires the authenticated exact guide-page review"]
        policies = evidence["intrinsic_connection_policies"]
        if not isinstance(policies, list):
            return ["intrinsic guide policy proof must be a bounded review list"]
        matches = [p for p in policies if isinstance(p, Mapping)
            and p.get("intrinsic_policy") == policy and p.get("supplier_part") == part
            and pin in p.get("pin_numbers", []) and p.get("page") == evidence.get("page")
            and p.get("source_pdf_sha256") == evidence.get("document_sha256")
            and all(_map(p.get("fields")).get(k) is v for k, v in values.items())
            and _normal(p.get("quote")) == _normal(evidence.get("quote"))]
        if len(matches) != 1:
            return ["intrinsic effect differs from its source page, value, policy or physical-pin scope"]
        for field in ("semantic_role", "modes"):
            direction_refs = _map(fact.get("field_evidence")).get(field, fact.get("evidence"))
            if (evidence_errors(direction_refs) or evidence_field_scope_errors(direction_refs, [field])
                    or any(_map(e).get("source_kind") != "datasheet"
                           or _map(e).get("document_sha256") != matches[0]["primary_pdf_sha256"]
                           for e in direction_refs)):
                return ["intrinsic direction requires this owner's independent primary-datasheet evidence"]
    return []


def _voltage(expression: Any, *, supplies: Mapping, domains: Mapping,
             pin_nets: Mapping[str, str | None], component_bindings: Mapping,
             reference: Any) -> tuple[Decimal | None, list[str]]:
    if not isinstance(expression, Mapping):
        return None, ["voltage expression object required"]
    if set(expression) == {"constant_v"}:
        value = decimal_number(expression["constant_v"])
        return value, [] if value is not None else ["finite constant voltage required"]
    if set(expression) != {"supply", "scale", "offset_v"} or not isinstance(expression.get("supply"), str):
        return None, ["only constant_v or affine named-supply expressions are supported"]
    scale, offset = decimal_number(expression["scale"]), decimal_number(expression["offset_v"])
    supply = _map(supplies.get(expression["supply"]))
    domain = _map(domains.get(supply.get("domain")))
    pin = supply.get("pin")
    errors = evidence_errors(supply.get("evidence"), allow_input=True) + evidence_errors(domain.get("evidence"), allow_input=True)
    if not isinstance(pin, str) or pin not in pin_nets or not pin_nets.get(pin) or pin_nets[pin] != domain.get("net"):
        errors.append("named supply must bind an existing physical pin to the actual domain net")
    if _map(component_bindings.get(pin)).get("domain") != supply.get("domain"):
        errors.append("named supply conflicts with the source pin's selected domain")
    ground_reference = (supply.get("domain") == reference and domain.get("reference") is None
                        and decimal_number(domain.get("voltage_v")) == 0)
    if not isinstance(reference, str) or not reference or reference not in domains or (domain.get("reference") != reference and not ground_reference):
        errors.append("relative voltage expressions require the same explicit reference domain")
    voltage = decimal_number(domain.get("voltage_v"))
    if scale is None or offset is None or voltage is None:
        errors.append("finite supply voltage, scale and offset required")
    return (None, errors) if errors else (voltage * scale + offset, [])


def evaluate_pin_rules(fact: Mapping, binding: Mapping, *, domains: Mapping | None = None,
                       supplies: Mapping | None = None, pin_nets: Mapping | None = None,
                       component_bindings: Mapping | None = None, supplier_part: str | None = None,
                       pin_number: str | None = None, terminal_voltage_context: TerminalVoltageContext | None = None,
                       component_id: str | None = None) -> dict:
    """Derive an effective pin contract without mutating any stored source fact.

    A review receipt is created outside generation by an independent source
    reviewer/parser and bound to the entire rule. The caller must protect that
    trusted contract store; a self-asserted model 'review' is not authentication.
    Only the explicitly implemented effects are accepted. Overlapping
    contradictory effects remain unresolved instead of choosing a favorable one.
    """
    effective = deepcopy(dict(fact)); diagnostics = []; derived = {}; not_applicable = []
    for field in _FIELDS:
        refs = _map(fact.get("field_evidence")).get(field, fact.get("evidence"))
        if issues := unconditional_claim_errors(field, fact.get(field), refs):
            effective[field] = None
            diagnostics.append({"rule": "CONDITIONAL_UNCONDITIONAL_CLAIM", "status": "unresolved", "field": field, "reasons": issues})
    rules = fact.get("conditional_rules", [])
    if not isinstance(rules, list) or len(rules) > 64:
        return {"fact": effective, "diagnostics": diagnostics + [{"rule": "CONDITIONAL_RULE_SCHEMA", "status": "unresolved"}], "derived_fields": {}, "not_applicable": []}
    seen = set(); conflicts = set()
    for raw in rules:
        rule = _map(raw); rid = rule.get("id")
        issues = []
        if not isinstance(rid, str) or not rid or rid in seen:
            issues.append("unique nonempty rule id required")
        if isinstance(rid, str): seen.add(rid)
        if set(rule) - {"id", "scope", "when", "effect", "evidence", "review"}:
            issues.append("unknown conditional rule properties")
        scope = _map(rule.get("scope"))
        if not supplier_part or not pin_number or scope != {"supplier_part": supplier_part, "pin_number": pin_number}:
            issues.append("reviewed rule scope does not match the exact supplier part and physical pin")
        effect = _map(rule.get("effect"))
        if effect.get("kind") == "pin_function_role":
            if (set(effect) != {"kind", "capability", "semantic_role"}
                    or not isinstance(effect.get("capability"), str) or not effect["capability"]
                    or not isinstance(effect.get("semantic_role"), str)
                    or effect["semantic_role"] not in PAPER_ROLES | PROJECT_ROLES
                    or rule.get("when") != {"all": []}
                    or not isinstance(fact.get("capabilities"), list)
                    or effect["capability"] not in fact["capabilities"]):
                issues.append("function role requires an independently supported capability, a closed role and unconditional outer applicability")
        if effect.get("kind") == "pin_selection_constraint":
            issues.extend(_selection_constraint_errors(rule, fact))
        if effect.get("kind") == "pin_relation":
            relation, pins = effect.get("relation"), effect.get("pins")
            minimum = 2 if relation == "same_net" else 1
            if (set(effect) != {"kind", "relation", "pins"} or relation not in ("same_net", "unconnected")
                    or not isinstance(pins, list) or not minimum <= len(pins) <= 32
                    or not all(isinstance(p, str) and p for p in pins) or len(set(pins)) != len(pins)):
                issues.append("bounded pin relation requires distinct explicit physical pins (same_net:2..32; unconnected:1..32)")
            elif relation == "unconnected" and pin_number not in pins:
                issues.append("reviewed relation must include its scoped physical pin")
        issues.extend(_intrinsic_policy_errors(rule, fact, supplier_part, pin_number))
        issues.extend(evidence_errors(rule.get("evidence")))
        if not issues:
            effect = _map(rule.get("effect"))
            fields = ([k for k in ("allow_nc", "required_connection") if k in effect] if effect.get("kind") == "connection_policy" else
                      ["logic_levels"] if effect.get("kind") == "logic_levels" else
                      ["application_interface_profile"] if effect.get("kind") == "application_interface_profile" else
                      ["voltage_min_v", "voltage_max_v"] if effect.get("kind") == "voltage_range" else
                      ["electrical_type", "modes"] if effect.get("kind") == "operating_pin_mode" else
                      ["capabilities", "semantic_role"] if effect.get("kind") == "pin_function_role" else
                      ["capabilities", "modes"] if effect.get("kind") == "pin_selection_constraint" else
                      ["required_connection"] if effect.get("kind") == "pin_relation" else
                      [effect.get("check")] if effect.get("kind") == "not_applicable" else ["unsupported_effect"])
            if has_terminal_voltage_predicate([rule]):
                fields.append("terminal_voltage_v")
            issues.extend(evidence_field_scope_errors(rule["evidence"], fields))
            for e in rule["evidence"]:
                pdf = e.get("source_kind") == "datasheet" and e.get("source_authentication_version") == 2 and e.get("provenance") == "original_pdf_native"
                html = e.get("source_kind") == "manufacturer_page" and e.get("source_authentication_version") == 3 and e.get("provenance") == "original_manufacturer_html"
                reviewed_pdf = e.get("source_kind") in {"datasheet", "manufacturer_application_note"} and e.get("source_authentication_version") == 3 and e.get("provenance") == "reviewed_original_pdf_transcription"
                secondary = e.get("source_kind") == "manufacturer_application_note" and e.get("source_authentication_version") == 3 and e.get("provenance") == "original_secondary_pdf_native"
                if not (pdf or html or reviewed_pdf or secondary) or e.get("claim_scope") == "pin_number_and_label_only":
                    issues.append("conditional rules require authenticated original electrical-fact sources; pinout identity alone is insufficient")
        issues.extend(_review_errors(rule))
        terminal_evaluator = (lambda p: terminal_voltage_context.evaluate(p, component_id=component_id,
            pin_number=pin_number, rule=rule)) if isinstance(terminal_voltage_context, TerminalVoltageContext) else None
        condition = evaluate_condition(rule.get("when"), binding, terminal_evaluator=terminal_evaluator)
        row = {"rule": "CONDITIONAL_PIN_RULE", "rule_id": rid, "condition": condition,
               "effect_kind": _map(rule.get("effect")).get("kind"), "status": "unresolved"}
        if _map(rule.get("effect")).get("kind") == "pin_relation":
            row["relation"] = _map(rule.get("effect")).get("relation")
        diagnostics.append(row)
        if issues:
            row["reasons"] = issues; continue
        if condition["status"] == "false":
            row["status"] = "not_applicable"; continue
        if condition["status"] != "true":
            row["reasons"] = ["selected operating condition is unknown"]; continue
        effect = _map(rule.get("effect")); kind = effect.get("kind"); values = {}
        from .static_voltages import domains_have_profiles
        proven_constant_range = (kind == "voltage_range"
            and condition["status"] == "true" and all(set(_map(effect.get(k))) == {"constant_v"} for k in ("minimum", "maximum")))
        if domains_have_profiles(domains) and kind in {"voltage_range", "logic_levels", "application_interface_profile"} and not proven_constant_range:
            row["reasons"] = ["STATIC_PROFILE_UNSUPPORTED: this conditional numeric consumer does not yet evaluate joint affine source limits; nominal fallback is forbidden"]
            continue
        if kind == "pin_function_role":
            row["capability"] = effect["capability"]
            usage = binding.get("usage")
            if not isinstance(usage, str) or not usage:
                row["reasons"] = ["selected capability is unknown"]; continue
            if usage != effect["capability"]:
                row.update(status="not_applicable", applicability="role targets a different selected capability"); continue
            # Function selection is a design intention, not evidence that
            # firmware ran, that a mode is valid, or that NC is permitted.
            values = {"semantic_role": effect["semantic_role"]}
        elif kind == "pin_selection_constraint":
            row.update(capability=effect["capability"], allowed_modes=deepcopy(effect["allowed_modes"]))
            usage, mode = binding.get("usage"), binding.get("mode")
            if not isinstance(usage, str) or not usage:
                row["reasons"] = ["selected capability is unknown"]; continue
            if usage != effect["capability"]:
                row.update(status="not_applicable", applicability="constraint targets a different selected capability"); continue
            if not isinstance(mode, str) or not mode:
                row["reasons"] = ["selected mode is unknown"]; continue
            if mode not in effect["allowed_modes"]:
                row.update(status="fail", selected_mode=mode, reasons=["selected mode is incompatible with the selected capability"]); continue
            required = evaluate_condition(effect["requires"], binding)
            row["required_condition"] = required
            if required["status"] != "true":
                row.update(status="fail" if required["status"] == "false" else "unresolved",
                    reasons=["required selected configuration is false" if required["status"] == "false"
                             else "required selected configuration is unknown"])
                continue
            # This is a restriction on an independently documented choice. It
            # grants no field, NC permission, source fact or execution claim.
        elif kind == "connection_policy":
            if set(effect) - {"kind", "allow_nc", "required_connection", "intrinsic_policy"} or len(effect) < 2 or any(type(v) is not bool for k, v in effect.items() if k not in {"kind", "intrinsic_policy"}):
                row["reasons"] = ["connection policy requires explicit boolean effects"]; continue
            values = {k: v for k, v in effect.items() if k not in {"kind", "intrinsic_policy"}}
            if values.get("allow_nc") is True and values.get("required_connection") is True:
                row["reasons"] = ["contradictory connection policy"]; continue
        elif kind == "voltage_range":
            if set(effect) != {"kind", "basis", "minimum", "maximum"} or effect.get("basis") != "recommended_operating":
                row["reasons"] = ["only a documented recommended operating range is supported"]; continue
            domain = _map(_map(domains).get(binding.get("domain")))
            kwargs = dict(supplies=_map(supplies), domains=_map(domains), pin_nets=_map(pin_nets),
                          component_bindings=_map(component_bindings), reference=domain.get("reference"))
            low, low_errors = _voltage(effect.get("minimum"), **kwargs)
            high, high_errors = _voltage(effect.get("maximum"), **kwargs)
            if low_errors or high_errors or low is None or high is None or low > high:
                row["reasons"] = low_errors + high_errors + (["invalid operating voltage interval"] if low is None or high is None or low > high else []); continue
            values = {"voltage_min_v": str(low), "voltage_max_v": str(high)}
        elif kind == "pin_relation":
            pins = effect["pins"]
            row["pins"] = pins
            if not isinstance(pin_nets, Mapping):
                row["reasons"] = ["physical graph unavailable for this same-instance relation"]; continue
            if any(p not in pin_nets for p in pins):
                row.update(status="fail", reasons=["relation references a nonexistent physical pin"]); continue
            if effect["relation"] == "unconnected":
                # Mandatory selected-scenario isolation, not optional NC permission
                # and not a declaration that the terminal is inherently an NC pin.
                # Even a singleton named net is an explicit external-wire assignment.
                if any(pin_nets[p] is not None for p in pins):
                    row.update(status="fail", reasons=["required isolated physical pins have external wire assignments"]); continue
            elif any(pin_nets[p] is None for p in pins) or len({pin_nets[p] for p in pins}) != 1:
                row.update(status="fail", reasons=["required physical pins do not share a connected wire net"]); continue
        elif kind == "operating_pin_mode":
            electrical = effect.get("electrical_type")
            if set(effect) != {"kind", "electrical_type", "modes"} or not isinstance(electrical, str) or electrical not in OPERATING_PIN_MODES or effect.get("modes") != [electrical]:
                row["reasons"] = ["operating-pin mode requires one reviewed power or analog direction state"]; continue
            values = {"electrical_type": electrical, "modes": [electrical]}
        elif kind == "logic_levels":
            levels = _map(effect.get("levels")); direction = effect.get("direction")
            required = {"vih_min_v", "vil_max_v"} if direction == "input" else {"voh_min_v", "vol_max_v"}
            allowed = required | {"operating_min_v", "operating_max_v"}
            if (set(effect) - {"kind", "basis", "direction", "levels", "maximum_output_current_a"}
                    or effect.get("basis") != "documented_operating_logic" or direction not in {"input", "output"}
                    or not required <= set(levels) or not set(levels) <= allowed
                    or ("maximum_output_current_a" in effect and direction != "output")):
                row["reasons"] = ["closed direction-specific documented logic guarantees required"]; continue
            evaluated = {}; errors = []
            domain = _map(_map(domains).get(binding.get("domain")))
            for name, expression in levels.items():
                value, issues = _voltage(expression, supplies=_map(supplies), domains=_map(domains), pin_nets=_map(pin_nets),
                    component_bindings=_map(component_bindings), reference=domain.get("reference"))
                errors.extend(issues)
                if value is not None:
                    evaluated[name] = str(value)
            high, low = ("vih_min_v", "vil_max_v") if direction == "input" else ("voh_min_v", "vol_max_v")
            if high in evaluated and low in evaluated and decimal_number(evaluated[high]) <= decimal_number(evaluated[low]):
                errors.append("logic high guarantee must exceed logic low guarantee")
            if ("operating_min_v" in evaluated and "operating_max_v" in evaluated and
                    decimal_number(evaluated["operating_min_v"]) > decimal_number(evaluated["operating_max_v"])):
                errors.append("documented operating minimum exceeds maximum")
            current = decimal_number(effect.get("maximum_output_current_a"))
            if "maximum_output_current_a" in effect and (current is None or current < 0):
                errors.append("nonnegative finite output current bound required")
            if errors:
                row["reasons"] = errors; continue
            values = {"logic_levels": {"direction": direction, **evaluated,
                "maximum_output_current_a": str(current) if current is not None else None,
                "evidence": deepcopy(rule["evidence"]), "source_rule_id": rid}}
        elif kind == "application_interface_profile":
            from .logic_interfaces import logic_supply_context
            expected = {"kind", "profile", "direction", "capability", "supply", "reference_supply", "supply_min_v", "supply_max_v"}
            direction = effect.get("direction")
            low, high = decimal_number(effect.get("supply_min_v")), decimal_number(effect.get("supply_max_v"))
            if (set(effect) != expected or effect.get("profile") != "same_supply_direct_spi"
                    or direction not in {"input", "output"} or low is None or high is None or not 0 < low <= high
                    or any(not isinstance(effect.get(k), str) or not effect[k] for k in ("capability", "supply", "reference_supply"))):
                row["reasons"] = ["closed source-reviewed direct SPI profile required"]; continue
            if binding.get("usage") != effect["capability"]:
                row["reasons"] = ["selected capability does not match reviewed interface profile"]; continue
            domain = _map(_map(domains).get(binding.get("domain"))); reference = domain.get("reference")
            supply = _map(_map(supplies).get(effect["supply"]))
            ground = _map(_map(supplies).get(effect["reference_supply"]))
            voltage, errors = _voltage({"supply": effect["supply"], "scale": 1, "offset_v": 0},
                supplies=_map(supplies), domains=_map(domains), pin_nets=_map(pin_nets),
                component_bindings=_map(component_bindings), reference=reference)
            zero, ground_errors = _voltage({"supply": effect["reference_supply"], "scale": 1, "offset_v": 0},
                supplies=_map(supplies), domains=_map(domains), pin_nets=_map(pin_nets),
                component_bindings=_map(component_bindings), reference=reference)
            errors.extend(ground_errors)
            # The reviewed rule selects the supply symbol and its actual pin.
            # A signal-domain declaration can be separate metadata; it is not
            # itself a supply wire and is never selected by voltage equality.
            _, context_errors = logic_supply_context(binding.get("domain"), supply.get("domain"), reference, _map(domains))
            errors.extend(context_errors)
            if ground.get("domain") != reference or zero != 0:
                errors.append("direct interface requires the actual same supply and zero-reference physical pins")
            if voltage is not None and not low <= voltage <= high:
                errors.append("actual supply lies outside the reviewed application range")
            if errors:
                row["reasons"] = errors; continue
            values = {"application_interface_profile": {**deepcopy(dict(effect)), "signal_domain": binding["domain"],
                "supply_domain": supply["domain"],
                "reference_domain": reference, "supply_voltage_v": str(voltage),
                "physical_supply_pin": supply["pin"], "physical_reference_pin": ground["pin"],
                "evidence": deepcopy(rule["evidence"]), "source_rule_id": rid}}
        elif kind == "not_applicable":
            # Identity, pin sets, role and safety obligations cannot be disabled.
            if set(effect) != {"kind", "check"} or effect.get("check") not in {"resources", "gpio_remap_safe"}:
                row["reasons"] = ["only resources/remapping applicability can be declared by this effect"]; continue
            check = effect["check"]
            if check == "resources" and binding.get("resources") not in (None, []):
                row["reasons"] = ["selected resources conflict with not-applicable effect"]; continue
            not_applicable.append({"check": check, "rule_id": rid})
        else:
            row["reasons"] = ["unsupported conditional effect"]; continue
        for field, value in values.items():
            old = effective.get(field)
            same = old == value if field in {"allow_nc", "required_connection", "semantic_role", "electrical_type", "modes", "logic_levels", "application_interface_profile"} else decimal_number(old) == decimal_number(value)
            # Source modes enumerate supported states. An applicable reviewed
            # mode rule may select a member, without rewriting that source set.
            # A second active rule cannot re-expand or replace an earlier choice.
            if (kind == "operating_pin_mode" and field == "modes" and field not in derived
                    and isinstance(old, list) and all(isinstance(mode, str) for mode in old)
                    and len(old) == len(set(old)) and set(value) <= set(old)):
                same = True
            if field in conflicts or old is not None and not same:
                conflicts.add(field); effective[field] = None
                row.setdefault("reasons", []).append(f"conflicting active or unconditional values for {field}")
            else:
                effective[field] = value
                derived.setdefault(field, []).append(rid)
                effective.setdefault("field_evidence", {})[field] = deepcopy(rule["evidence"])
        row["status"] = "unresolved" if row.get("reasons") else "pass"
        row["derived_fields"] = sorted(values)
    for field in conflicts: derived.pop(field, None)
    for row in diagnostics:
        if row.get("rule") == "CONDITIONAL_UNCONDITIONAL_CLAIM" and row.get("field") in derived:
            row.update(status="not_applicable", resolution="Unconditional claim discarded; a reviewed applicable rule supplies the effective field")
    return {"fact": effective, "diagnostics": diagnostics, "derived_fields": derived,
            "not_applicable": not_applicable,
            "scope": "reviewed structured facts evaluated against an explicit sourced static scenario"}


__all__ = ["SCHEMA", "CONDITIONAL_RULE_SCHEMA", "rule_digest", "source_clause", "unconditional_claim_errors",
           "parse_unused_interface_permission", "bind_input_selection", "bind_frozen_input_conditions", "rule_condition_fields", "evaluate_condition", "evaluate_pin_rules"]
