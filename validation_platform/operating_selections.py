"""Source-reviewed internal operating choices, separate from input guarantees.

The plan chooses an option already present in a frozen device contract.  Neither
an option ID nor a self-computed hash creates source authority.  Every use is
rebuilt from the exact input, fixed inventory, plan and original source record.
This first version intentionally excludes interface-disable, host, environment,
voltage/rating, firmware-execution and equivalence-permission assertions.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .certificate_facts import certificate_source_errors, digest, frozen_source_bundle
from .conditional_pin_rules import bind_frozen_input_conditions, evaluate_condition, evaluate_pin_rules

SCHEMA = "pcbagent.operating_selections.v1"
OPTION_SCHEMA = "pcbagent.internal_operating_option.v1"
METHOD = "frozen_source_supported_plan_selection"
# A reviewed option cannot expand this authority boundary through its field name.
INTERNAL_FIELDS = frozenset({"configuration.operation_mode", "configuration.operating_mode",
    "configuration.codec_input_mode", "configuration.analog_capture_mode", "configuration.clock_role",
    "configuration.analog_capture_enabled"})
_OPTION_KEYS = frozenset({"schema", "id", "scope", "kind", "field", "value", "when",
                          "required_capability_guards", "evidence", "review"})
_CHOICE_KEYS = frozenset({"device", "option_id", "field", "value", "source_refs", "reason"})


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _same(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def option_ref(part: str, option_id: str) -> str:
    return f"operating-option:{part}:{option_id}"


def _diag(rule: str, message: str, **details: Any) -> dict:
    return {"rule": rule, "status": "fail", "message": message, **details}


def _source_contract(record: Mapping, part: str) -> dict:
    return {"source_bundles": {part: frozen_source_bundle(record, part)}, "components": {
        "fixed": {"supplier_part": part, "source_registry": _map(record.get("contracts")).get("source_registry")}}}


def reviewed_operating_options(record: Mapping, part: str) -> dict:
    """Return usable options only after exact source/field and review checks."""
    contract = _map(record.get("contracts")); raw = contract.get("selection_options", [])
    accepted, diagnostics = {}, []
    if not isinstance(raw, list):
        return {"options": {}, "diagnostics": [_diag("OPERATING_OPTION_SCHEMA", "selection_options must be an array")]}
    counts = Counter(_map(row).get("id") for row in raw if isinstance(_map(row).get("id"), str))
    for item in raw:
        row = _map(item); oid = row.get("id"); issues = []
        if (set(row) != _OPTION_KEYS or row.get("schema") != OPTION_SCHEMA or
                row.get("scope") != {"supplier_part": part} or row.get("kind") != "internal_operating_mode" or
                not isinstance(oid, str) or not oid or counts.get(oid) != 1):
            issues.append("closed, unique, exact-part internal operating option required")
        field, value = row.get("field"), row.get("value")
        if not isinstance(field, str) or field not in INTERNAL_FIELDS or (field == "configuration.analog_capture_enabled" and value is not True) or (
                field != "configuration.analog_capture_enabled" and (not isinstance(value, str) or not value.strip())):
            issues.append("selector is outside the internal-mode authority boundary")
        when = _map(row.get("when")); guards = row.get("required_capability_guards")
        if (set(when) != {"all"} or not isinstance(when.get("all"), list) or len(when["all"]) > 32 or
                any(not isinstance(p, Mapping) or set(p) != {"field", "op", "value"} or
                    not isinstance(p.get("field"), str) or not p["field"].startswith(("configuration.", "requirements."))
                    or p["field"] == field for p in when.get("all", []) if isinstance(when.get("all"), list))):
            issues.append("preconditions must be explicit independent input predicates, without self-selection")
        if (not isinstance(guards, list) or not 1 <= len(guards) <= 64 or any(
                not isinstance(g, Mapping) or set(g) != {"pin", "capability", "input_field", "input_value"} or
                any(not isinstance(g.get(k), str) or not g[k] for k in ("pin", "capability", "input_field")) or
                g.get("input_value") is not True or not g.get("input_field", "").startswith("configuration.") or
                g.get("input_field") == field for g in (guards or []))):
            issues.append("at least one source capability guarded by an explicit required input interface is necessary")
        review = _map(row.get("review"))
        try:
            if (set(review) != {"method", "reviewer", "rule_sha256", "evidence_sha256"} or
                    review.get("method") != "independent_source_review" or not isinstance(review.get("reviewer"), str) or
                    not review["reviewer"].strip() or review.get("rule_sha256") != digest({k:v for k,v in row.items() if k != "review"}) or
                    review.get("evidence_sha256") != digest(row.get("evidence"))):
                issues.append("the complete internal-option body and evidence require unchanged independent review")
            if _map(contract.get("provenance")).get("source_authentication_version") not in {2, 3}:
                issues.append("source contract has no supported authentication provenance")
            issues.extend(certificate_source_errors(_source_contract(record, part), part, row.get("evidence"), ["operating_selection"]))
        except (TypeError, ValueError, KeyError):
            issues.append("option source or hash data is malformed")
        if issues:
            diagnostics.append(_diag("OPERATING_OPTION_SOURCE", "Internal option is not source-authorized", option_id=oid, reasons=issues))
        else:
            accepted[oid] = deepcopy(dict(row))
    return {"options": accepted, "diagnostics": diagnostics}


def _input_index(board: Mapping) -> dict:
    return {"requirement:" + row["id"]: {"kind": "requirement", "value": deepcopy(dict(row))}
            for row in board.get("requirements", []) if isinstance(row, Mapping) and isinstance(row.get("id"), str)}


def selection_input(board: Mapping) -> dict:
    """Exact generation-visible input projection; never claim the unseen case ID."""
    return {"board_id": board.get("board_id"), "label": board.get("label"),
            "request": board.get("request"), "requirements": deepcopy(board.get("requirements", [])),
            **{key: deepcopy(board.get(key, [])) for key in ("core_devices", "functional_devices", "physical_devices")}}


def resolve_operating_selections(plan: Mapping, inventory: Mapping, knowledge: Mapping, *,
                                  board_input: Mapping, source_index: Mapping,
                                  active_aliases: set[str] | None = None) -> dict:
    """Rebuild all declared choices without trusting a plan-supplied receipt.

    ``active_aliases`` bounds a local peripheral planner.  Its provisional
    receipt is never exported; the merged final plan is resolved again.
    """
    board_input = selection_input(board_input)
    diagnostics, receipts, conditions = [], [], {}
    aliases = set(inventory) if active_aliases is None else set(active_aliases)
    assignments, declarations, usages = {}, [], {}
    for module in plan.get("modules", []):
        mid = _map(module).get("id")
        for alias in _map(module).get("core_instances", []):
            assignments.setdefault(alias, []).append(mid)
        for use in _map(module).get("pin_usage", []):
            key = (mid, _map(use).get("device"), _map(use).get("pin"))
            usages.setdefault(key, []).append(use)
        for choice in _map(module).get("operating_selections", []):
            declarations.append((mid, _map(choice)))
    if not declarations:
        return {"schema": SCHEMA, "receipts": [], "conditions_by_alias": {}, "diagnostics": []}
    if any(source_index.get(key) != entry for key, entry in _input_index(board_input).items()):
        diagnostics.append(_diag("OPERATING_INPUT_INDEX", "Input source-index entries must preserve the exact structured requirement payload"))
    if set(assignments) != aliases or any(len(mids) != 1 for mids in assignments.values()):
        diagnostics.append(_diag("OPERATING_INSTANCE_COVERAGE", "Every selected fixed instance must be assigned exactly once"))
    seen = set(); source_cache = {}
    for mid, choice in declarations:
        alias, field, oid = choice.get("device"), choice.get("field"), choice.get("option_id")
        errors = []; key = (alias, field)
        if (set(choice) != _CHOICE_KEYS or alias not in aliases or assignments.get(alias) != [mid] or key in seen or
                not isinstance(choice.get("reason"), str) or not choice["reason"].strip()):
            errors.append("selection must occur once per field and fixed instance in its owning module")
        seen.add(key)
        spec = _map(inventory.get(alias)); identity = _map(spec.get("identity")); part = identity.get("supplier_part")
        record = _map(knowledge.get(part)); catalog = _map(record.get("device"))
        pins = record.get("pins", []); spec_pins = spec.get("pins", [])
        expected_pins = [{"id": str(p.get("pin_number")), "name": p.get("pin_name")} for p in pins if isinstance(p, Mapping)]
        candidates = [r for collection in ("core_devices", "functional_devices", "physical_devices")
                      for r in board_input.get(collection, []) if _map(r).get("device_id") == part]
        input_row = _map(candidates[0]) if len(candidates) == 1 else {}
        idx = identity.get("instance_index"); qty = input_row.get("quantity")
        if (not part or catalog.get("supplier_part") != part or identity.get("catalog_model") != catalog.get("device_name") or
                identity.get("input_model") != input_row.get("model") or type(idx) is not int or type(qty) is not int or
                not 1 <= idx <= qty or identity.get("catalog_pin_count") != len(expected_pins) or
                sorted(spec_pins, key=lambda x:x["id"]) != sorted(expected_pins, key=lambda x:x["id"])):
            errors.append("fixed instance identity and complete physical pins differ from exact input/catalog")
        # Even identical parts have independent choices: aliases cannot point to
        # the same fixed instance index or manufacture an additional instance.
        if sum(_map(_map(s).get("identity")).get("supplier_part") == part and
               _map(_map(s).get("identity")).get("instance_index") == idx for s in inventory.values()) != 1:
            errors.append("fixed instance identity is duplicated")
        if part not in source_cache:
            source_cache[part] = reviewed_operating_options(record, part)
        approved = source_cache[part]; option = _map(approved["options"].get(oid))
        if not option:
            errors.append("option is absent from the independently reviewed frozen source contract")
        if option and (field != option["field"] or not _same(choice.get("value"), option["value"])):
            errors.append("selected field/value differs from the reviewed option")
        ref = option_ref(str(part), str(oid))
        if choice.get("source_refs") != [ref] or _map(source_index.get(ref)) != {
                "kind": "source_operating_option", "device_id": part, "option_id": oid,
                "sha256": digest(option)}:
            errors.append("selection must cite its exact frozen source option index entry")
        inputs = bind_frozen_input_conditions(board_input=board_input, supplier_part=part, source_index=source_index)
        input_conditions = inputs["conditions"]
        errors.extend(d["message"] for d in inputs["diagnostics"])
        input_fact = _map(_map(input_conditions.get(str(field).split(".", 1)[0])).get(str(field).split(".", 1)[-1]))
        if input_fact and not _same(input_fact.get("value"), choice.get("value")):
            errors.append("explicit input selection takes precedence over the proposed internal mode")
        if option:
            checked = evaluate_condition(option["when"], input_conditions)
            if checked["status"] != "true":
                errors.append("option preconditions are not established by explicit input alone")
            for guard in option["required_capability_guards"]:
                test = evaluate_condition({"all": [{"field": guard["input_field"], "op": "eq", "value": True}]}, input_conditions)
                rows = usages.get((mid, alias, guard["pin"]), [])
                fact = _map(_map(_map(record.get("contracts")).get("pins")).get(guard["pin"]))
                evaluated = evaluate_pin_rules(fact, input_conditions, supplier_part=part, pin_number=guard["pin"])
                capabilities = evaluated["fact"].get("capabilities")
                derived = set(evaluated.get("derived_fields", {}).get("capabilities", []))
                refs = ([r for rule in fact.get("conditional_rules", []) if rule.get("id") in derived for r in rule.get("evidence", [])]
                        if derived else _map(fact.get("field_evidence")).get("capabilities", fact.get("evidence")))
                source_errors = certificate_source_errors(_source_contract(record, part), part, refs, ["capabilities"])
                if (test["status"] != "true" or len(rows) != 1 or _map(rows[0]).get("unconnected") is not False or
                        _map(rows[0]).get("capability") != guard["capability"] or
                        not isinstance(capabilities, list) or guard["capability"] not in capabilities or source_errors):
                    errors.append("required interface capability must already be input-required, source-authenticated and allocated to a connected pin")
        if errors:
            diagnostics.append(_diag("OPERATING_SELECTION_AUTHORITY", "Internal selection is not authorized",
                module=mid, device=alias, field=field, option_id=oid, reasons=errors))
            continue
        receipt = {"method": METHOD, "origin": "agent_plan", "module": mid, "device": alias,
            "supplier_part": part, "fixed_instance": deepcopy(dict(identity)), "field": field, "value": deepcopy(choice["value"]),
            "option_id": oid, "source_option_sha256": digest(option), "source_evidence_sha256": digest(option["evidence"]),
            "knowledge_record_sha256": digest(record), "plan_sha256": digest(plan), "input_sha256": digest(board_input),
            "fixed_inventory_sha256": digest(inventory), "choice_sha256": digest(choice),
            "input_scope": "exact_generation_visible_board_request_requirements",
            "authority": "explicit_input" if input_fact else "source_reviewed_internal_choice",
            "firmware_execution_proven": False}
        receipts.append(receipt)
        category, name = field.split(".", 1)
        conditions.setdefault(alias, {}).setdefault(category, {})[name] = (deepcopy(dict(input_fact)) if input_fact else {
            "value": deepcopy(choice["value"]), "evidence": deepcopy(option["evidence"]),
            "selection_authentication": deepcopy(receipt)})
    # A rejected declaration must not accidentally leave another choice usable
    # for NC/electrical authorization in the same invalid frozen plan.
    return {"schema": SCHEMA, "receipts": sorted(receipts, key=lambda r:(r["device"], r["field"])),
            "conditions_by_alias": {} if diagnostics else conditions, "diagnostics": diagnostics}


def bind_operating_conditions(*, board_input: Mapping, supplier_part: str, source_index: Mapping,
                              operating: Mapping | None = None, planned: Mapping | None = None,
                              fields: list[str] | None = None) -> dict:
    """Combine independently rebuilt instance choices with input-first assertions."""
    operating, planned = _map(operating), _map(planned)
    stripped = deepcopy(dict(planned)); pending, diagnostics = {}, []
    for category in ("configuration", "requirements"):
        for name, fact in _map(operating.get(category)).items():
            if _map(_map(fact).get("selection_authentication")).get("method") != METHOD:
                continue  # Matching explicit input remains owned by the old path.
            field = category + "." + name
            authentication = _map(_map(fact).get("selection_authentication"))
            if (field not in INTERNAL_FIELDS or authentication.get("field") != field or
                    authentication.get("supplier_part") != supplier_part or
                    not _same(authentication.get("value"), _map(fact).get("value")) or
                    field == "configuration.analog_capture_enabled" and _map(fact).get("value") is not True):
                diagnostics.append(_diag("OPERATING_CONDITION_SCOPE", "Rebuilt instance choice cannot grant an input-only or different selector", field=field))
                continue
            asserted = _map(_map(planned.get(category)).get(name))
            wanted = fields is None or field in fields or bool(asserted)
            if not wanted:
                continue
            checked = deepcopy(dict(fact)); auth = fact["selection_authentication"]
            if asserted:
                expected_ref = option_ref(supplier_part, auth["option_id"])
                if not _same(asserted.get("value"), fact.get("value")) or asserted.get("source_refs") != [expected_ref]:
                    diagnostics.append(_diag("OPERATING_PIN_ASSERTION", "Pin assertion differs from the authenticated instance choice or its source option", field=field))
                    checked = {"value": None, "evidence": []}
                stripped.get(category, {}).pop(name, None)
            pending.setdefault(category, {})[name] = checked
    base = bind_frozen_input_conditions(board_input=board_input, supplier_part=supplier_part,
        source_index=source_index, planned=stripped, fields=fields)
    for category, values in pending.items():
        for name, fact in values.items():
            if name not in base["conditions"].get(category, {}):
                base["conditions"].setdefault(category, {})[name] = fact
    base["diagnostics"].extend(diagnostics)
    base["planned_conditions"] = {key: deepcopy(planned[key]) for key in ("configuration", "requirements") if key in planned}
    return base


def make_bound_operating_context(generation: Mapping, knowledge: Mapping, board: Mapping) -> dict:
    """A binder-owned reconstruction context, stored once rather than per pin."""
    inventory = _map(generation.get("fixed_inventory")); plan = _map(generation.get("plan"))
    parts = {_map(_map(inventory.get(c.get("device"))).get("identity")).get("supplier_part")
             for m in plan.get("modules", []) for c in m.get("operating_selections", [])}
    return {"schema": SCHEMA, "plan": deepcopy(dict(plan)), "inventory": deepcopy(dict(inventory)),
            "board_input": deepcopy(dict(board)), "records": {part: deepcopy(knowledge[part]) for part in parts if part in knowledge},
            "source_index": deepcopy(dict(_map(generation.get("source_index")))),
            "resolution": deepcopy(dict(_map(generation.get("operating_selections"))))}


def rebuild_bound_operating_conditions(contracts: Mapping, alias: str, *, component_id: str | None = None) -> dict:
    """Re-authenticate an individual receiver/producer against its own instance."""
    context = _map(contracts.get("operating_selection_context")); records = _map(context.get("records"))
    board = _map(context.get("board_input")); inventory = _map(context.get("inventory"))
    spec = _map(inventory.get(alias)); identity = _map(spec.get("identity")); part = identity.get("supplier_part")
    errors = []
    components = [cc for cc in _map(contracts.get("components")).values() if _map(cc).get("device") == alias]
    if component_id is not None and _map(_map(contracts.get("components")).get(component_id)).get("device") != alias:
        errors.append(_diag("OPERATING_BOUND_RECEIVER", "Receiver cannot inherit another component's operating selection"))
    if (context.get("schema") != SCHEMA or digest(board) != contracts.get("input_sha256") or len(components) != 1 or
            _map(components[0]).get("fixed_identity") != identity or
            _map(components[0]).get("supplier_part") != part):
        errors.append(_diag("OPERATING_BOUND_IDENTITY", "Operating choice context differs from the actual fixed instance or input"))
    for owner, record in records.items():
        frozen = _map(_map(contracts.get("source_bundles")).get(owner))
        if digest(record) != frozen.get("knowledge_record_sha256"):
            errors.append(_diag("OPERATING_BOUND_SOURCE", "Operating option record differs from the independent frozen source bundle"))
    rebuilt = resolve_operating_selections(_map(context.get("plan")), inventory, records,
        board_input=board, source_index=_map(context.get("source_index")))
    if rebuilt != context.get("resolution"):
        errors.append(_diag("OPERATING_BOUND_RECEIPT", "Operating selections changed since final freeze"))
    errors.extend(rebuilt["diagnostics"])
    selected = bind_operating_conditions(board_input=board, supplier_part=part,
        source_index=_input_index(board), operating=rebuilt["conditions_by_alias"].get(alias))
    errors.extend(selected["diagnostics"])
    return {"conditions": {} if errors else selected["conditions"], "diagnostics": errors}


__all__ = ["SCHEMA", "OPTION_SCHEMA", "INTERNAL_FIELDS", "METHOD", "option_ref", "reviewed_operating_options",
           "resolve_operating_selections", "bind_operating_conditions", "make_bound_operating_context",
           "rebuild_bound_operating_conditions", "selection_input"]
