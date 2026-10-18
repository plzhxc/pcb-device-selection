"""Frozen architecture obligations consumed by existing static checks.

This ledger grants no electrical fact, input guarantee, NC permission or mode.
Model prose is a task description, never a proof of completion. Unknown
consumers remain unresolved; legacy untyped unresolved strings still block.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any

from jsonschema import Draft202012Validator

from .certificate_facts import digest, certificate_source_errors, frozen_source_bundle
from .conditional_pin_rules import CONDITIONAL_RULE_SCHEMA, _review_errors
from .operating_selections import (INTERNAL_FIELDS, reviewed_operating_options,
                                   resolve_operating_selections, selection_input)

SCHEMA = "pcbagent.architecture_obligations.v1"
_TEXT = {"type": "string", "minLength": 1}
_TEXTS = {"type": "array", "items": _TEXT, "uniqueItems": True}
_BASE = {"id": _TEXT, "module": _TEXT, "devices": _TEXTS,
         "source_refs": {**_TEXTS, "minItems": 1}, "description": _TEXT}
_DEFERRED = {"module_realization", "internal_operating_selection", "existing_source_rule"}
_HARD = {"external_boundary", "fixed_identity", "missing_source"}
PLANNING_VALIDATION_MODES = ("strict", "retain_unresolved_candidate")
CANDIDATE_MODE = "retain_unresolved_candidate"
CANDIDATE_PROMPT = """本轮显式生成待独立校验的诊断候选。仍须满足全部固定实例、真实脚、端子、来源、配置权限和电压域结构约束。
尚未证明的必要电气事实或设计任务如实保留unresolved/obligations；它们将冻结并阻止最终合格，不能删掉或改成已完成。
只要已有真实端子和合法定义足以构造，继续完成本阶段的具体设计。不能从未决问题推导新事实、默认电压、悬空许可或编造缺失器件。
pin_usage.unconnected=true仅记录诊断候选中的实际不接线选择，不是allow_nc或no_connect角色证据；不得通过改写角色或来源来授权悬空。真实必接脚开路仍由最终检查判fail，缺少适用许可仍判unresolved；不得为减少待验证项关闭输入要求的功能。
真实声明不接线的脚可以保留capability/selected_mode=null作为未选定信息；不得填入来源不支持的非空token，也不得省略已连接脚的必要选择。最终检查继续保留适用的模式/能力未决，不自动配置GPIO。
本模式不授权最终电路正确；后续仍执行原独立检查和完整比对。"""

def pending_planning_issues(architecture: Mapping, plan: Mapping,
                            peripheral_plans: list | None = None) -> list[dict]:
    """Freeze unanswered statements with their actual proposal origin, never closure."""
    rows = []
    def add(origin: str, proposal: Mapping) -> None:
        for index, text in enumerate(_rows(proposal.get("unresolved"))):
            rows.append({"origin": origin, "path": f"unresolved/{index}",
                         "proposal_sha256": digest(proposal), "description": deepcopy(text)})
    add("architecture", architecture)
    for index, proposal in enumerate(peripheral_plans or []):
        if isinstance(proposal, Mapping):
            add(f"peripheral_plans/{index}", proposal)
    add("plan", plan)
    for index, obligation in enumerate(_rows(architecture.get("obligations"))):
        if _map(obligation).get("kind") in _HARD:
            rows.append({"origin": "architecture", "path": f"obligations/{index}",
                         "proposal_sha256": digest(architecture),
                         "description": _map(obligation).get("description"),
                         "obligation": deepcopy(obligation)})
    return rows

def _last_planner_records(agent_runs: list | None) -> dict:
    selected = {}
    for record in agent_runs or []:
        if not isinstance(record, Mapping) or record.get("status") != "success":
            continue
        stage, module = record.get("stage"), record.get("module")
        if stage not in {"netlist_architecture", "netlist_peripheral"} and not (stage == "netlist" and module is None):
            continue
        selected[(stage, module)] = record
    return selected

def _planner_pending_provenance(architecture: Mapping, plan: Mapping, peripheral_plans: list | None,
                                agent_runs: list | None) -> tuple[list[dict], list[dict]]:
    """Recheck retained statements against the actual last planner answers."""
    from agent_framework.structured_output import extract_tolerant, loads_tolerant
    from netlist_end_to_end.schemas import RESULT_PATTERN
    import hashlib
    from netlist_end_to_end.netlist.planner_projection import replay, owned_fields_match
    projected, _, projection_issues = replay(agent_runs)
    selected, issues = _last_planner_records(agent_runs), list(projection_issues)
    expected = ({("netlist_architecture", None): architecture,
        **{("netlist_peripheral", _map(_rows(p.get("modules"))[0]).get("id")): p
           for p in peripheral_plans or [] if isinstance(p, Mapping) and _rows(p.get("modules"))}}
        if architecture else {("netlist", None): plan})
    summaries = []
    if set(selected) != set(expected):
        issues.append(_issue("PLANNING_PENDING_PROVENANCE", "Actual planner records must cover the frozen architecture and every peripheral proposal"))
    for key, proposal in expected.items():
        record = selected.get(key, {})
        text = record.get("text")
        raw, _ = extract_tolerant(RESULT_PATTERN, text if isinstance(text, str) else None, expect_json=True)
        parsed, _ = loads_tolerant(raw or "")
        raw_value = _map(_map(record.get("values")).get("result"))
        value = projected.get(key, raw_value)
        if key in projected:
            # replay already checked exact raw text/values and its prior owner.
            parsed = value
            if not owned_fields_match(proposal, value, key[0]):
                issues.append(_issue("PLANNING_PENDING_PROVENANCE", "Host-owned fields differ from independently replayed actual planner projection", stage=key[0], module=key[1]))
        unresolved = value.get("unresolved")
        if (not isinstance(parsed, Mapping) or parsed.get("unresolved") != unresolved
                or not isinstance(unresolved, list) or any(not isinstance(x, str) for x in unresolved)
                or proposal.get("unresolved") != unresolved):
            issues.append(_issue("PLANNING_PENDING_PROVENANCE", "Frozen unresolved statements must match parsed actual planner text and values", stage=key[0], module=key[1]))
        if key[0] == "netlist_architecture" and (proposal.get("obligations", []) != value.get("obligations", [])
                or _map(parsed).get("obligations", []) != value.get("obligations", [])):
            issues.append(_issue("PLANNING_PENDING_PROVENANCE", "Frozen obligations must match the actual architecture answer; changing kind is not closure"))
        summaries.append({"stage": key[0], "module": key[1], "round": record.get("round"),
            "input_sha256": record.get("input_sha256"), "proposal_sha256": digest(value),
            "answer_sha256": hashlib.sha256((text if isinstance(text, str) else "").encode()).hexdigest(),
            "unresolved": deepcopy(unresolved)})
    if architecture and projected:
        if not owned_fields_match(plan, {"voltage_domains": architecture.get("voltage_domains"),
                "modules": [module for proposal in peripheral_plans or []
                    for module in proposal.get("modules", [])]}, "netlist"):
            issues.append(_issue("PLANNING_PENDING_PROVENANCE", "Merged plan changed host-owned architecture/peripheral fields"))
    if architecture:
        merged = sorted(set().union(*(set(_rows(p.get("unresolved"))) for p in peripheral_plans or [] if isinstance(p, Mapping))))
        if plan.get("unresolved") != merged:
            issues.append(_issue("PLANNING_PENDING_PROVENANCE", "Combined plan must retain the union of all actual peripheral unresolved statements"))
    return summaries, issues

def _variant(kind: str, extra: dict | None = None) -> dict:
    properties = {**_BASE, "kind": {"const": kind}, **(extra or {})}
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _scl_architecture_reentries(agent_runs: list | None) -> list[int]:
    """Detect an owner phase after physical construction from actual call order.

    Consecutive architecture retries form one phase. Ordinary monolithic plan
    calls have module=None and are not local SCL construction.
    """
    construction_started = False
    entries = []
    for index, record in enumerate(agent_runs or []):
        if not isinstance(record, Mapping):
            continue
        stage, module = record.get("stage"), record.get("module")
        if stage == "top_port" or stage == "netlist" and isinstance(module, str) and module:
            construction_started = True
        elif stage == "netlist_architecture" and construction_started:
            entries.append(index)
            construction_started = False
    return entries


def _reconciliation_provenance(architecture: Mapping, agent_runs: list | None,
                               repair_history: list | None, frozen_state: Mapping) -> tuple[list, list]:
    """Recheck historical pending work against actual answers, not a new closure claim."""
    histories = [r for r in repair_history or [] if isinstance(r, Mapping)
                 and r.get("origin") == "final_internal_port"]
    requests = []
    for index, record in enumerate(agent_runs or []):
        if not isinstance(record, Mapping) or record.get("stage") != "netlist_architecture":
            continue
        for message in _rows(record.get("messages")):
            if _map(message).get("type") != "UserMessage":
                continue
            try:
                packet = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                continue
            review = _map(_map(packet).get("architecture_reconciliation"))
            if review.get("origin") == "final_internal_port":
                requests.append((index, review))
    reentries = _scl_architecture_reentries(agent_runs)
    if not histories and not requests and not reentries:
        return [], []
    def failed(message: str) -> tuple[list, list]:
        return [], [_issue("PLANNING_PENDING_PROVENANCE", message)]
    owner_histories = [r for r in repair_history or [] if isinstance(r, Mapping)
                       and r.get("stage") == "netlist_architecture_reconciliation"]
    if len(reentries) != 1 or len(histories) != 1 or len(owner_histories) != 1 or not requests:
        return failed("Final boundary reconsideration needs its unique retained history and actual owner request")
    history = histories[0]
    boundary = history.get("previous_agent_runs_count")
    if (history.get("stage") != "netlist_architecture_reconciliation"
            or type(boundary) is not int or not 0 < boundary == reentries[0] == requests[0][0]
            or history.get("frozen_state") != frozen_state
            or any(review.get("history_sha256") != digest(history) for _, review in requests)):
        return failed("Boundary history, actual request, original input, knowledge and inventory must remain bound")
    old = _map(history.get("previous_architecture"))
    peripherals = history.get("discarded_peripheral_plans")
    if not isinstance(peripherals, list):
        return failed("Discarded peripheral proposals must remain available for independent pending checks")
    for proposal in [old, architecture, *peripherals, history.get("discarded_plan")]:
        if (not isinstance(proposal, Mapping) or not isinstance(proposal.get("unresolved"), list)
                or any(not isinstance(text, str) for text in proposal["unresolved"])
                or not isinstance(proposal.get("modules"), list)
                or any(not isinstance(row, Mapping) or not isinstance(row.get("id"), str)
                       for row in proposal["modules"])
                or any(not isinstance(row, Mapping) or not isinstance(row.get("id"), str)
                       for row in _rows(proposal.get("obligations")))):
            return failed("Retained boundary proposals must preserve their valid pending and module shape")
    _, issues = _planner_pending_provenance(old, _map(history.get("discarded_plan")),
                                           peripherals, (agent_runs or [])[:boundary])
    old_pending = set(_rows(old.get("unresolved")))
    pending_by_module = {}
    for plan in peripherals:
        rows = _rows(_map(plan).get("modules"))
        if len(rows) != 1:
            return failed("Discarded module ownership is incomplete")
        pending_by_module[_map(rows[0]).get("id")] = _rows(_map(plan).get("unresolved"))
        old_pending.update(pending_by_module[_map(rows[0]).get("id")])
    if (history.get("peripheral_unresolved") != pending_by_module
            or any(review.get("peripheral_unresolved") != pending_by_module
                   or review.get("observations") != history.get("observations") for _, review in requests)
            or not old_pending <= set(_rows(architecture.get("unresolved")))):
        return failed("Boundary reconsideration cannot silently drop old unanswered statements")
    obligations = {row.get("id"): row for row in _rows(architecture.get("obligations"))}
    if any(obligations.get(row.get("id")) != row for row in _rows(old.get("obligations"))):
        return failed("Historical obligations must retain their existing objective discharge consumers")
    before = {row["id"]: row for row in _rows(old.get("modules"))}
    after = {row["id"]: row for row in _rows(architecture.get("modules"))}
    if (set(before) != set(after) or any(before[mid].get(field) != after[mid].get(field)
            for mid in before for field in ("label", "intent"))
            or any(sorted(before[mid].get("core_instances", [])) != sorted(after[mid].get("core_instances", []))
                   for mid in before)):
        return failed("Boundary reconsideration cannot change fixed module ownership or functional intent")
    from netlist_end_to_end.netlist.scl import SCLSource, compile_scl
    try:
        checked = compile_scl([SCLSource(**row) for row in history["discarded_compiled_sources"]],
                              history["discarded_context"])
    except (KeyError, TypeError, ValueError):
        return failed("The discarded physical construction cannot reproduce the boundary observation")
    observations = _rows(history.get("observations"))
    if any(not isinstance(row, Mapping) for row in observations):
        return failed("Boundary observations must remain structured original evidence")
    from netlist_end_to_end.netlist.planner_projection import frozen_boundary_observations
    from .contract_binding import check_top_port_bindings
    diagnostics = [*checked.diagnostics, *(check_top_port_bindings(old, checked.manifest) if checked.ok else [])]
    if any(row.get("code") == "PORT_BINDING_KIND_CONFLICT" for row in diagnostics):
        from netlist_end_to_end.netlist.planner_projection import owned_fields_match
        if (history.get("diagnostics") != diagnostics
                or not owned_fields_match(history["discarded_plan"], old, "netlist")):
            return failed("Port binding history must preserve the frozen plan and independently reproduced construction diagnostics")
    expected = frozen_boundary_observations(old, history["discarded_context"], checked.manifest, diagnostics)
    versioned = any("observation_schema" in row for row in observations)
    if not versioned:
        # Historical consumer recorded only orphan ports, even when the same
        # compilation contained other structural failures. Do not relabel it v2.
        expected = [{key: value for key, value in row.items() if key != "observation_schema"}
                    for row in expected or []
                    if _map(row.get("diagnostic")).get("code") == "UNCONNECTED_INTERNAL_PORT"]
    if not observations or observations != expected:
        return failed("Boundary observations must reproduce exact frozen boundaries and the discarded compiler result")
    # Observation-version compatibility never changes provenance strength.
    # An older record without successful original SCL remains unverifiable.
    from dataclasses import asdict
    from netlist_end_to_end.netlist.semantic_domains import materialize_voltage_domains
    last = {}
    board_ids = set()
    for record in (agent_runs or [])[:boundary]:
        stage, mid = record.get("stage"), record.get("module")
        if stage == "top_port" or stage == "netlist" and isinstance(mid, str) and mid:
            if record.get("status") != "success":
                return failed("Boundary history cannot substitute an unsuccessful SCL request for an actual answer")
            last[mid if stage == "netlist" else None] = record.get("text")
            board_ids.add(record.get("board_id"))
    if set(last) != {*before, None} or len(board_ids) != 1 or not all(isinstance(v, str) for v in last.values()):
        return failed("Boundary history requires complete original local and top SCL answers")
    board_id = next(iter(board_ids))
    try:
        domain_source, _ = materialize_voltage_domains(history["discarded_plan"], board_id)
    except (KeyError, TypeError, ValueError):
        return failed("Boundary history frozen domain declarations cannot be reconstructed")
    actual_sources = [asdict(SCLSource(f"{board_id}:{mid}:local" if mid else f"{board_id}:top", mid, text))
                      for mid, text in last.items()]
    if domain_source is not None:
        actual_sources.append(asdict(domain_source))
    if sorted(actual_sources, key=lambda row: row["name"]) != history["discarded_compiled_sources"]:
        return failed("Boundary history construction must match the last actual ordered SCL answers and frozen declarations")
    # New observations carry actual discarded plans/errors as planning context.
    if any((versioned or "discarded_peripheral_plans" in review or "construction_diagnostics" in review)
            and (review.get("discarded_peripheral_plans") != peripherals
                 or review.get("construction_diagnostics") != diagnostics) for _, review in requests):
        return failed("Owner requests must preserve the observed physical plans and all final construction errors")
    return [{"history_sha256": digest(history), "previous_agent_runs_count": boundary,
             "owner_request_indices": [index for index, _ in requests]}], issues

OBLIGATION_SCHEMA = {"oneOf": [
    _variant("module_realization"),
    _variant("internal_operating_selection", {"field": {"enum": sorted(INTERNAL_FIELDS)}}),
    _variant("existing_source_rule", {"pin": _TEXT, "rule_id": _TEXT}),
    *[_variant(kind) for kind in sorted(_HARD)],
]}
OBLIGATIONS_SCHEMA = {"type": "array", "maxItems": 256, "items": OBLIGATION_SCHEMA}

OBLIGATION_PROMPT = """可选obligations把本阶段尚未完成的具体任务交给后续验证，不得用文字自称完成。
每项必须有唯一id、module、该模块固定devices aliases、source_refs、description及闭集kind。
module_realization的devices是该任务关注的真实固定实例子集；有固定实例的模块至少列一个，纯common模块可空。代码另行冻结整个模块全部fixed与最终common为required_coverage，最终所有实际元件仍执行原静态检查；模型不得提供或缩小required_coverage。
internal_operating_selection另给field，仅限已审核source selection_options支持的内部选择；不授权宿主、环境、电压或关闭必需接口。
existing_source_rule另给真实physical pin和已有rule_id，最终必须由现有来源规则检查器评估。
external_boundary/fixed_identity/missing_source保持架构阻塞，不能改个kind把缺失事实变成许可。
不允许模型提供status、discharged、证明或检查器结果。source_refs仅指已有来源键，不因作为任务引用就成为新事实。
旧unresolved字符串仍全部阻塞。已完成范围以外的RF法规、波形、布局等记assumptions并明确unassessed，
这不会关闭任何所需静态角色、供电、绝缘或必接检查；必要外部范围/负载/身份和来源缺项仍如实阻塞。"""

def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}

def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []

def _issue(code: str, message: str, **details: Any) -> dict:
    return {"severity": "error", "code": code, "message": message, **details}

def inventory_from_pool(pool: Mapping) -> dict:
    return {alias: {"identity": deepcopy(spec.get("identity", {})),
                    "pins": deepcopy(_map(spec.get("device")).get("pins", []))}
            for alias, spec in pool.items()}

def validate_obligations(architecture: Mapping, inventory: Mapping,
                         source_index: Mapping, knowledge: Mapping | None = None) -> dict:
    """Validate task scope; this is not a source fact or electrical approval."""
    raw = architecture.get("obligations", [])
    issues = [_issue("ARCHITECTURE_OBLIGATION_SCHEMA", err.message,
                     path=".".join(map(str, err.absolute_path)))
              for err in Draft202012Validator(OBLIGATIONS_SCHEMA).iter_errors(raw)]
    if issues:
        return {"deferred": [], "blocking": [], "diagnostics": issues}
    modules = {_map(m).get("id"): _map(m) for m in _rows(architecture.get("modules"))}
    identifiers, deferred, blocking = set(), [], []
    for item in raw:
        oid, mid, kind = item["id"], item["module"], item["kind"]
        module = modules.get(mid)
        if oid in identifiers:
            issues.append(_issue("ARCHITECTURE_OBLIGATION_ID", "Obligation IDs must be unique", obligation=oid))
        identifiers.add(oid)
        aliases = item["devices"]
        if module is None or any(alias not in inventory or alias not in module.get("core_instances", []) for alias in aliases):
            issues.append(_issue("ARCHITECTURE_OBLIGATION_SCOPE", "Target aliases must belong to this exact module", obligation=oid, module=mid, actual_aliases=aliases, expected_aliases=sorted(module.get("core_instances", [])) if module else []))
        if any(ref not in source_index for ref in item["source_refs"]):
            issues.append(_issue("ARCHITECTURE_OBLIGATION_SOURCE", "Task references must be existing source-index keys", obligation=oid))
        if kind in {"internal_operating_selection", "existing_source_rule"} and len(aliases) != 1:
            issues.append(_issue("ARCHITECTURE_OBLIGATION_SCOPE", "This consumer requires one exact fixed instance", obligation=oid, actual_aliases=aliases, expected_count=1))
        if kind == "module_realization" and module and module.get("core_instances") and not aliases:
            issues.append(_issue("ARCHITECTURE_OBLIGATION_SCOPE", "A module with fixed instances requires a nonempty task focus; full-module coverage is derived separately", obligation=oid,
                                 actual_aliases=aliases, expected_aliases=sorted(module["core_instances"])))
        alias = aliases[0] if len(aliases) == 1 else None
        part = _map(_map(inventory.get(alias)).get("identity")).get("supplier_part")
        record = _map(_map(knowledge).get(part))
        if kind == "internal_operating_selection" and knowledge is not None:
            options = reviewed_operating_options(record, part)["options"]
            if not any(option["field"] == item["field"] for option in options.values()):
                available = [{"option_id": identifier, "field": option["field"]}
                             for identifier, option in sorted(options.items())]
                issues.append(_issue("ARCHITECTURE_OBLIGATION_CONSUMER", "No independently reviewed internal source option exists for this selector", obligation=oid,
                    supplier_part=part, selected_field=item["field"], available_options=available[:32],
                    available_option_count=len(available), available_options_truncated=len(available) > 32,
                    required_action="The schema field enum is not selection authority. Use internal_operating_selection only for a listed independently reviewed source option and its exact field. If the input already fixes this selector, carry realization and verification through module_realization while retaining that exact input condition. A genuine missing source must remain missing_source. Do not invent options, silently delete pending obligations, or treat a task description as electrical proof."))
        if kind == "existing_source_rule":
            numbers = {_map(pin).get("id") for pin in _rows(_map(inventory.get(alias)).get("pins"))}
            if item["pin"] not in numbers:
                issues.append(_issue("ARCHITECTURE_OBLIGATION_PIN", "Rule target must be an actual frozen physical pin", obligation=oid))
            if knowledge is not None:
                fact = _map(_map(_map(record.get("contracts")).get("pins")).get(item["pin"]))
                rules = [r for r in _rows(fact.get("conditional_rules")) if _map(r).get("id") == item["rule_id"]]
                if len(rules) != 1:
                    available = sorted({_map(rule).get("id") for rule in _rows(fact.get("conditional_rules"))
                                        if isinstance(_map(rule).get("id"), str)})
                    issues.append(_issue("ARCHITECTURE_OBLIGATION_CONSUMER", "No unique existing source rule with this exact part/pin/rule ID", obligation=oid,
                        supplier_part=part, pin=item["pin"], rule_id=item["rule_id"],
                        available_rule_ids=available[:32], available_rule_count=len(available),
                        required_action="Use only a unique already reviewed rule for this exact pin. Do not invent rule IDs. Detailed circuit realization can use module_realization; an actual missing source must remain missing_source. Neither task kind grants electrical evidence."))
                else:
                    source_view = {"source_bundles": {part: frozen_source_bundle(record, part)},
                        "components": {"target": {"supplier_part": part,
                            "source_registry": _map(record.get("contracts")).get("source_registry")}}}
                    errors = certificate_source_errors(source_view, part, _map(rules[0]).get("evidence"), [])
                    errors.extend(err.message for err in Draft202012Validator(CONDITIONAL_RULE_SCHEMA).iter_errors(rules[0]))
                    errors.extend(_review_errors(rules[0]))
                    if _map(rules[0]).get("scope") != {"supplier_part": part, "pin_number": item["pin"]}:
                        errors.append("source rule must match the exact instance supplier and physical pin")
                    if errors:
                        issues.append(_issue("ARCHITECTURE_OBLIGATION_CONSUMER", "Source rule must already have authentic source scope and independent review", obligation=oid, reasons=errors))
        entry = {**deepcopy(item), "owner": "external_preparation" if kind in _HARD else "netlist_peripheral",
                 "blocking_stage": "architecture" if kind in _HARD else "final_static_validation",
                 "status": "blocked" if kind in _HARD else "pending"}
        if kind == "module_realization":
            entry["required_coverage"] = {"scope": "entire_module",
                "fixed_devices": sorted(module.get("core_instances", [])) if module else [],
                "common_devices": None}  # Final peripheral inventory is not known at architecture time.
        (blocking if kind in _HARD else deferred).append(entry)
    return {"deferred": sorted(deferred, key=lambda row: row["id"]),
            "blocking": sorted(blocking, key=lambda row: row["id"]), "diagnostics": issues}

def freeze_obligations(architecture: Mapping, plan: Mapping, context: Mapping,
                       inventory: Mapping, knowledge: Mapping, *,
                       board_input: Mapping, source_index: Mapping,
                       planning_validation_mode: str = "strict",
                       peripheral_plans: list | None = None,
                       agent_runs: list | None = None,
                       repair_history: list | None = None) -> dict:
    validated = validate_obligations(architecture, inventory, source_index, knowledge)
    issues = list(validated["diagnostics"])
    if planning_validation_mode not in PLANNING_VALIDATION_MODES:
        issues.append(_issue("PLANNING_VALIDATION_MODE", "Unknown planning validation mode"))
    retain = planning_validation_mode == CANDIDATE_MODE
    issues.extend(_issue("ARCHITECTURE_OBLIGATION_BLOCKED", row["description"], obligation=row["id"])
                  for row in validated["blocking"] if not retain)
    if architecture.get("unresolved") and not retain:
        issues.append(_issue("ARCHITECTURE_LEGACY_UNRESOLVED", "Legacy string unresolved items cannot be downgraded into deferred tasks"))
    final_modules = {_map(m).get("id"): _map(m) for m in _rows(plan.get("modules"))}
    for item in validated["deferred"]:
        if item["kind"] != "module_realization":
            continue
        final = final_modules.get(item["module"], {})
        coverage = item["required_coverage"]
        actual_fixed = final.get("core_instances", [])
        common = final.get("common_devices")
        if (not isinstance(actual_fixed, list) or set(actual_fixed) != set(coverage["fixed_devices"])
                or not isinstance(common, Mapping)):
            issues.append(_issue("ARCHITECTURE_OBLIGATION_COVERAGE", "Final module must preserve its complete fixed inventory and declare final common instances", obligation=item["id"],
                                 actual_aliases=actual_fixed, expected_aliases=coverage["fixed_devices"]))
        else:
            coverage["common_devices"] = sorted(common)
    receipt = {"schema": SCHEMA, "input_projection": selection_input(board_input),
               "architecture_sha256": digest(architecture), "plan_sha256": digest(plan),
               "context_sha256": digest(context), "fixed_inventory_sha256": digest(inventory),
               "knowledge_sha256": digest(knowledge), "source_index_sha256": digest(source_index),
               "obligations": validated["deferred"],
               "scope": "Existing static consumers only; no new source, input, NC, operating or equivalence authority"}
    reconciliations, history_issues = _reconciliation_provenance(architecture, agent_runs, repair_history,
        {"input": digest(board_input), "knowledge": digest(knowledge),
         "inventory": digest(inventory), "source_index": digest(source_index)})
    issues.extend(history_issues)
    if reconciliations:
        receipt["boundary_reconciliations"] = reconciliations
    from netlist_end_to_end.netlist.planner_projection import uses_projection
    projected_protocol = uses_projection(agent_runs)
    if projected_protocol and not retain:
        projections, projection_issues = _planner_pending_provenance(architecture, plan, peripheral_plans, agent_runs)
        issues.extend(projection_issues)
        receipt["planner_projections_sha256"] = digest(projections)
    if retain:
        proposals, source_issues = _planner_pending_provenance(architecture, plan, peripheral_plans, agent_runs)
        issues.extend(source_issues)
        receipt.update(planning_validation_mode=planning_validation_mode,
            peripheral_plans_sha256=digest(peripheral_plans or []),
            planning_proposals_sha256=digest(proposals),
            pending_planning_issues=pending_planning_issues(architecture, plan, peripheral_plans),
            pending_resolution_policy="No automatic closure; retained issues remain independently unresolved for this frozen candidate.")
    return {"receipt": receipt, "diagnostics": issues}

def bind_obligations(generation: Mapping, knowledge: Mapping, board_input: Mapping,
                     bound: Mapping) -> dict:
    """Rebuild final ledger and instance mapping; do not trust model receipts."""
    architecture = _map(generation.get("architecture"))
    supplied = generation.get("architecture_obligations")
    manifest = _map(_map(generation.get("compilation")).get("manifest"))
    mode = generation.get("planning_validation_mode", "strict")
    pending = pending_planning_issues(architecture, _map(generation.get("plan")), generation.get("peripheral_plans"))
    from netlist_end_to_end.netlist.planner_projection import uses_projection
    projected_protocol = uses_projection(generation.get("agent_runs"))
    recorded_pending = any(_map(_map(record.get("values")).get("result")).get("unresolved")
        or any(_map(item).get("kind") in _HARD for item in _rows(_map(_map(record.get("values")).get("result")).get("obligations")))
        for record in _last_planner_records(generation.get("agent_runs")).values())
    if (not architecture.get("obligations") and not pending and supplied is None
            and "architecture_obligations_sha256" not in manifest and mode == "strict"
            and "planning_validation_mode" not in manifest and not recorded_pending and not projected_protocol
            and not any(_map(row).get("origin") == "final_internal_port"
                        for row in _rows(generation.get("repair_history")))
            and not _scl_architecture_reentries(generation.get("agent_runs"))):
        return {"schema": SCHEMA, "status": "absent", "entries": [], "diagnostics": []}
    rebuilt = freeze_obligations(architecture, _map(generation.get("plan")), _map(generation.get("context")),
        _map(generation.get("fixed_inventory")), knowledge, board_input=board_input,
        source_index=_map(generation.get("source_index")), planning_validation_mode=mode,
        peripheral_plans=generation.get("peripheral_plans"), agent_runs=generation.get("agent_runs"),
        repair_history=generation.get("repair_history"))
    issues = [{"rule": row["code"], "status": "fail", "message": row["message"]} for row in rebuilt["diagnostics"]]
    if mode != CANDIDATE_MODE and recorded_pending:
        issues.append({"rule": "PLANNING_PENDING_PROVENANCE", "status": "fail",
                       "message": "Actual planner answers retain unresolved issues; deleting diagnostic metadata does not close them"})
    expected = rebuilt["receipt"]
    if mode == CANDIDATE_MODE or "planning_validation_mode" in manifest:
        if manifest.get("planning_validation_mode") != mode:
            issues.append({"rule": "PLANNING_VALIDATION_BINDING", "status": "fail",
                           "message": "The explicit diagnostic mode must match the frozen compilation manifest"})
    if supplied != expected or manifest.get("architecture_obligations_sha256") != digest(expected):
        issues.append({"rule": "ARCHITECTURE_OBLIGATION_BINDING", "status": "fail",
                       "message": "Final architecture, plan, context, input, source, inventory and ledger must be frozen together"})
    if _map(bound.get("fixed_inventory_authentication")).get("status") != "authenticated":
        issues.append({"rule": "ARCHITECTURE_OBLIGATION_INVENTORY", "status": "fail",
                       "message": "Deferred tasks require the existing independent fixed-inventory authentication"})
    components = _map(bound.get("components"))
    entries = []
    operating = None
    for row in expected["obligations"]:
        module_components = {cid: spec for cid, spec in components.items() if _map(spec).get("module") == row["module"]}
        targets = {cid: spec for cid, spec in module_components.items()
                   if row["kind"] == "module_realization" or _map(spec).get("device") in row["devices"]}
        entry = {**deepcopy(row), "components": sorted(targets), "consumer_ready": bool(targets)}
        if row["kind"] == "module_realization":
            coverage = row["required_coverage"]
            expected_aliases = coverage["fixed_devices"] + _rows(coverage["common_devices"])
            actual_aliases = [_map(spec).get("device") for spec in targets.values()]
            if (coverage["common_devices"] is None or set(actual_aliases) != set(expected_aliases)
                    or len(actual_aliases) != len(expected_aliases)):
                entry["consumer_ready"] = False
        elif not targets or any(not any(_map(spec).get("device") == alias for spec in targets.values()) for alias in row["devices"]):
            entry["consumer_ready"] = False
        if row["kind"] == "internal_operating_selection":
            if operating is None:
                operating = resolve_operating_selections(_map(generation.get("plan")), _map(generation.get("fixed_inventory")),
                    knowledge, board_input=board_input, source_index=_map(generation.get("source_index")))
            found = [r for r in operating["receipts"] if r.get("device") == row["devices"][0] and r.get("field") == row["field"]]
            entry["consumer_ready"] = entry["consumer_ready"] and not operating["diagnostics"] and len(found) == 1
            entry["operating_receipt_sha256"] = digest(found[0]) if len(found) == 1 else None
        if row["kind"] == "existing_source_rule":
            found = [_map(spec).get("pins", {}).get(row["pin"], {}) for spec in targets.values()]
            entry["consumer_ready"] = entry["consumer_ready"] and len(found) == 1 and sum(
                _map(rule).get("id") == row["rule_id"] for fact in found for rule in _rows(fact.get("conditional_rules"))) == 1
        entries.append(entry)
    return {"schema": SCHEMA, "status": "invalid" if issues else "bound",
            "receipt_sha256": digest(expected), "entries": entries, "diagnostics": issues,
            **({"pending_planning_issues": deepcopy(expected["pending_planning_issues"])}
               if mode == CANDIDATE_MODE else {})}

def consume_obligations(graph: Any, contracts: Mapping, static_diagnostics: list[dict]) -> list[dict]:
    """Consume positive evidence emitted by the existing independent checker.

    Called after all normal checks. It does not rerun or relax those checks and
    never accepts a caller-supplied status as a source fact.
    """
    ledger = _map(contracts.get("architecture_obligations"))
    if not ledger or ledger.get("status") == "absent":
        return []
    output = list(_rows(ledger.get("diagnostics")))
    if ledger.get("status") != "bound":
        return output or [{"rule": "ARCHITECTURE_OBLIGATION_BINDING", "status": "fail", "message": "Obligation ledger is not independently bound"}]
    for item in _rows(ledger.get("pending_planning_issues")):
        output.append({"rule": "PLANNING_PENDING_FACT", "status": "unresolved",
            "message": "This frozen candidate retains an unanswered planning issue; compilation grants no closure",
            "issue": deepcopy(item), "receipt_sha256": ledger.get("receipt_sha256")})
    actual_components = {key[1]: component for key, component in graph.components.items()}
    for entry in _rows(ledger.get("entries")):
        targets = set(entry["components"])
        relevant = [d for d in static_diagnostics if d.get("component") in targets or d.get("component") is None]
        blockers = [d for d in relevant if d.get("status") in {"fail", "unresolved"}]
        covered, missing = [], []
        for cid in sorted(targets):
            component = actual_components.get(cid)
            if component is None:
                missing.append({"component": cid, "reason": "actual component missing"}); continue
            pin_ids = set(graph.pin_map(component.key))
            if not any(d.get("component") == cid and d.get("rule") in {"PHYSICAL_PIN_SET", "LOGICAL_TERMINAL_SET"} and d.get("status") == "pass" for d in relevant):
                missing.append({"component": cid, "reason": "complete terminal-set check missing"})
            for pin in sorted(pin_ids):
                rows = [d for d in relevant if d.get("component") == cid and d.get("pin") == pin]
                required = {"PIN_ROLE", "PIN_USAGE", "PIN_MODE"}
                seen = {d.get("rule") for d in rows if d.get("status") in {"pass", "not_applicable"}}
                if not required <= seen or not ({"PIN_REQUIRED", "PIN_NC"} & {d.get("rule") for d in rows if d.get("status") == "pass"}):
                    missing.append({"component": cid, "pin": pin, "reason": "positive role/use/mode/actual connection or NC-policy coverage missing"})
                else:
                    covered.append({"component": cid, "pin": pin})
        rule_rows = []
        if entry["kind"] == "existing_source_rule":
            rule_rows = [d for d in relevant if d.get("component") in targets and d.get("pin") == entry["pin"] and
                         d.get("rule") == "CONDITIONAL_PIN_RULE" and d.get("rule_id") == entry["rule_id"]]
            if len(rule_rows) != 1 or rule_rows[0].get("status") not in {"pass", "not_applicable"}:
                missing.append({"reason": "exact existing source-rule evaluation missing"})
        status = ("fail" if any(d.get("status") == "fail" for d in blockers) else
                  "unresolved" if blockers or missing or not entry["consumer_ready"] or not covered else "pass")
        output.append({"rule": "ARCHITECTURE_OBLIGATION", "status": status,
            "message": "Existing static consumers evaluated this frozen planning obligation; no source or input authority added",
            "obligation_id": entry["id"], "kind": entry["kind"], "module": entry["module"],
            "components": sorted(targets), "task_focus": deepcopy(entry["devices"]),
            "required_coverage": deepcopy(entry.get("required_coverage")),
            "covered_pins": covered, "missing_coverage": missing,
            "blocking_checks": [{key: d.get(key) for key in ("rule", "status", "component", "pin")} for d in blockers],
            "consumed_rule_results": deepcopy(rule_rows), "receipt_sha256": ledger.get("receipt_sha256")})
    return output

__all__ = ["SCHEMA", "OBLIGATIONS_SCHEMA", "OBLIGATION_PROMPT", "inventory_from_pool",
           "validate_obligations", "freeze_obligations", "bind_obligations", "consume_obligations"]
