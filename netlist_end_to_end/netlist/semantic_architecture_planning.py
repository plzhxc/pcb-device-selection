"""Coordinate bounded architecture and per-module peripheral planning."""
from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from agent_framework.agent import BaseAgent
from validation_platform.planning_obligations import CANDIDATE_MODE, inventory_from_pool, validate_obligations

from .prompts import load_prompt
from .planner_projection import boundary as project_boundary, peripheral as project_peripheral
from .semantic_selection import fixed_device_part
from .semantic_architecture_agents import (SemanticArchitectureAgent,
    SemanticArchitectureBoundaryAgent, SemanticArchitectureInventoryRepairAgent)
from .semantic_architecture_context import _knowledge_summary, _source_subset
from .semantic_architecture_validation import (_error, _boundary_errors, validate_architecture,
    architecture_inventory_repair_targets, merge_architecture_inventory_repair)
from .semantic_architecture_reconciliation import (_RECONCILIATION_CONSTRAINTS,
    _PROJECTED_RECONCILIATION_CONSTRAINTS, _boundary_reconciliation, _reconciliation_invariants)
from .semantic_presentation import architecture_configuration_choices, with_referenced_source_context


async def plan_with_architecture(*, board: Mapping, request: str, requirements: Any,
    pool: Mapping, knowledge: Mapping, source_index: Mapping, templates: list,
    call: Any, peripheral_agent: type[BaseAgent], freeze: Any,
    concurrency: int, max_module_core_pins: int, max_repairs: int,
    obligation_knowledge: Mapping | None = None,
    planning_validation_mode: str = "strict",
    architecture_context_profile: str = "legacy_full_contracts",
    frozen_knowledge_sha256: str | None = None,
    reconciliation: Mapping | None = None) -> dict:
    """Allow one source-checked architecture reconsideration after boundary failure."""
    arguments = dict(board=board, request=request, requirements=requirements, pool=pool,
        knowledge=knowledge, source_index=source_index, templates=templates, call=call,
        peripheral_agent=peripheral_agent, freeze=freeze, concurrency=concurrency,
        max_module_core_pins=max_module_core_pins, max_repairs=max_repairs,
        obligation_knowledge=obligation_knowledge, planning_validation_mode=planning_validation_mode,
        architecture_context_profile=architecture_context_profile,
        frozen_knowledge_sha256=frozen_knowledge_sha256)
    if reconciliation is not None:
        # The caller has consumed the one shared owner reconsideration budget.
        return await _plan_architecture_attempt(**arguments, reconciliation=reconciliation)
    first = await _plan_architecture_attempt(**arguments)
    feedback = _boundary_reconciliation(first)
    if feedback is None:
        return first
    second = await _plan_architecture_attempt(**arguments, reconciliation=feedback)
    second["repair_history"] = [*first["repair_history"], {
        "stage": "netlist_architecture_reconciliation", "round": 1,
        "maximum_reconciliations": 1, "diagnostics": deepcopy(first["diagnostics"]),
        "previous_architecture": deepcopy(first["architecture"]),
        "discarded_peripheral_plans": deepcopy(first.get("peripheral_plans", [])),
        "observations": feedback["observations"], "peripheral_unresolved": feedback["peripheral_unresolved"],
        "constraints": _RECONCILIATION_CONSTRAINTS,
        "regeneration_policy": "Regenerate every module against the newly validated architecture; never relabel old pin or component plans."
    }, *second["repair_history"]]
    return second


async def _plan_architecture_attempt(*, board: Mapping, request: str, requirements: Any,
    pool: Mapping, knowledge: Mapping, source_index: Mapping, templates: list,
    call: Any, peripheral_agent: type[BaseAgent], freeze: Any,
    concurrency: int, max_module_core_pins: int, max_repairs: int,
    obligation_knowledge: Mapping | None = None,
    planning_validation_mode: str = "strict",
    architecture_context_profile: str = "legacy_full_contracts",
    frozen_knowledge_sha256: str | None = None,
    reconciliation: Mapping | None = None) -> dict:
    """Plan bounded modules independently, then verify their combined inventory."""
    result = {"architecture": None, "plan": None, "context": None, "diagnostics": [],
              "repair_history": [], "input_derived_fields": [], "normalizations": [], "planning_status": "failed"}
    summary = _knowledge_summary(knowledge, profile=architecture_context_profile,
        frozen_knowledge_sha256=frozen_knowledge_sha256)
    stage_context = ({"architecture_context_profile": architecture_context_profile,
        "configuration_choices": architecture_configuration_choices(requirements, knowledge)}
        if architecture_context_profile == "stage_boundary_v1" else {})
    previous = deepcopy(reconciliation["architecture"]) if reconciliation is not None else None
    issues = ([_error("ARCHITECTURE_DOWNSTREAM_BOUNDARY", "Review the source-backed architecture boundary after repeated peripheral conflicts",
                      observations=deepcopy(reconciliation["observations"]))] if reconciliation is not None else [])
    inventory_repair_used = reconciliation is not None
    projected = getattr(peripheral_agent, "planner_projection_kind", None) == "pcbagent.peripheral_choices.v1"
    if reconciliation is not None and projected:
        stage_context.update(planner_projection="pcbagent.architecture_boundary_patch.v1",
            projection_baseline=deepcopy(reconciliation["architecture"]))
    if reconciliation is not None:
        stage_context["architecture_reconciliation"] = {"round": 1, "maximum_reconciliations": 1,
            "constraints": (_PROJECTED_RECONCILIATION_CONSTRAINTS if projected else _RECONCILIATION_CONSTRAINTS), "observations": deepcopy(reconciliation["observations"]),
            "peripheral_unresolved": deepcopy(reconciliation["peripheral_unresolved"])}
        if reconciliation.get("origin") == "final_internal_port":
            stage_context["architecture_reconciliation"].update(
                origin="final_internal_port", history_sha256=reconciliation["history_sha256"])
            stage_context["architecture_reconciliation"]["constraints"] += load_prompt('architecture_final_reconciliation.md')
            stage_context["architecture_reconciliation"].update(
                discarded_peripheral_plans=deepcopy(reconciliation.get("discarded_peripheral_plans", [])),
                construction_diagnostics=deepcopy(reconciliation.get("construction_diagnostics", [])))
    for attempt in range(max_repairs + 1):
        core_inventory = {alias: {"device_id": fixed_device_part(spec), "model": spec["device"]["name"],
                 "component_type": spec["device"]["type"], "identity": deepcopy(spec.get("identity")),
                                         "pin_count": len(spec["device"]["pins"])} for alias,spec in pool.items()}
        missing = (architecture_inventory_repair_targets(previous, pool, source_index, issues,
            max_module_core_pins=max_module_core_pins, static_environment=board.get("static_environment")) if attempt and not inventory_repair_used else [])
        stage = "netlist_architecture_inventory_repair" if missing else "netlist_architecture"
        if missing:
            inventory_repair_used = True
            parts = {core_inventory[alias]["device_id"] for alias in missing}
            response = await call(SemanticArchitectureInventoryRepairAgent, stage, None, attempt,
                {"board": board, "request": request, "requirements": requirements,
                 "missing_core_inventory": {alias: core_inventory[alias] for alias in missing},
                 "available_modules": [{"id": row["id"], "label": row["label"], "intent": row["intent"],
                     "core_instances": list(row["core_instances"]),
                     "core_pin_count": sum(core_inventory[alias]["pin_count"] for alias in row["core_instances"])}
                     for row in previous["modules"]],
                 "device_boundary_facts": {part: summary[part] for part in parts if part in summary},
                 "frozen_unresolved": deepcopy(previous["unresolved"]),
                 "max_module_core_pins": max_module_core_pins, "diagnostics": issues})
        else:
            owner_agent = SemanticArchitectureBoundaryAgent if reconciliation is not None and projected else SemanticArchitectureAgent
            response = await call(owner_agent, stage, None, attempt,
            {"board": board, "request": request, "requirements": requirements, "core_inventory": core_inventory,
             "knowledge_summary": summary, "source_index": source_index, **stage_context,
             "max_module_core_pins": max_module_core_pins, "previous_architecture": previous, "diagnostics": issues,
             **({"planning_validation_mode": planning_validation_mode} if planning_validation_mode == CANDIDATE_MODE else {})})
        if response.ok:
            patch_errors = []
            if missing:
                previous, patch_errors = merge_architecture_inventory_repair(previous, response.values.get("result"),
                    pool, source_index, max_module_core_pins=max_module_core_pins, static_environment=board.get("static_environment"))
            else:
                try:
                    previous = (project_boundary(reconciliation["architecture"], reconciliation["peripheral_unresolved"], response.values.get("result"))
                        if reconciliation is not None and projected else deepcopy(response.values.get("result")))
                except (TypeError, ValueError) as exc:
                    patch_errors = [_error("ARCHITECTURE_SCHEMA", str(exc))]
            issues = [*validate_architecture(previous, pool, source_index, max_module_core_pins=max_module_core_pins, static_environment=board.get("static_environment")),
                      *patch_errors]
            if not any(issue["code"] == "ARCHITECTURE_SCHEMA" for issue in issues):
                if reconciliation is not None:
                    issues.extend(_reconciliation_invariants(previous, reconciliation["architecture"],
                        peripheral_unresolved=reconciliation["peripheral_unresolved"]))
                state = validate_obligations(previous, inventory_from_pool(pool), source_index,
                    obligation_knowledge if obligation_knowledge is not None else knowledge)
                result["architecture_obligation_validation"] = state
                # Independent source-consumer errors must be visible in the same
                # repair round as boundary errors; duplicated scope errors add no information.
                issues.extend(issue for issue in state["diagnostics"] if issue not in issues)
            else:
                result.pop("architecture_obligation_validation", None)
        else:
            issues = [_error("ARCHITECTURE_AGENT_FAILED", response.failure_summary() or response.status.value)]
        if not issues:
            break
        result["repair_history"].append({"round": attempt, "stage": stage, "diagnostics": issues})
        if response.status.value in {"error", "cancelled", "content_filtered"}:
            break
    result.update(architecture=previous, diagnostics=issues)
    if issues:
        if isinstance(previous, Mapping) and "architecture_obligation_validation" in result:
            result["diagnostics"] = [*issues,
                *[_error("ARCHITECTURE_NOT_READY", message) for message in previous["unresolved"]],
                *[_error("ARCHITECTURE_OBLIGATION_BLOCKED", row["description"],
                         obligation=row["id"], kind=row["kind"])
                  for row in result.get("architecture_obligation_validation", {}).get("blocking", [])]]
        return result
    architecture = previous
    if architecture["unresolved"] and planning_validation_mode != CANDIDATE_MODE:
        result["diagnostics"] = [_error("ARCHITECTURE_NOT_READY", message) for message in architecture["unresolved"]]
        result["planning_status"] = "not_ready"
        return result
    obligation_state = validate_obligations(architecture, inventory_from_pool(pool), source_index,
        obligation_knowledge if obligation_knowledge is not None else knowledge)
    result["architecture_obligation_validation"] = obligation_state
    blocking = [row for row in obligation_state["blocking"]
                if planning_validation_mode != CANDIDATE_MODE]
    if blocking:
        result["diagnostics"] = [_error("ARCHITECTURE_OBLIGATION_BLOCKED", row["description"],
            obligation=row["id"], kind=row["kind"]) for row in blocking]
        result["planning_status"] = "not_ready"
        return result
    semaphore = asyncio.Semaphore(concurrency)

    async def peripheral(module: Mapping) -> dict:
        mid = module["id"]
        own_pool = {alias: pool[alias] for alias in module["core_instances"]}
        parts = {fixed_device_part(row) for row in own_pool.values()}
        quantities = Counter((fixed_device_part(row), row["device"]["type"]) for row in own_pool.values())
        own_board = {**board, "core_devices": [
            {"device_id": part, "model": knowledge[part]["device"]["device_name"], "quantity": quantity}
            for (part, kind), quantity in sorted(quantities.items()) if kind == "ic"]}
        if "functional_devices" in board or any(kind == "module" for _, kind in quantities):
            own_board["functional_devices"] = [
                {"device_id": part, "model": knowledge[part]["device"]["device_name"],
                 "quantity": quantity, "component_type": kind}
                for (part, kind), quantity in sorted(quantities.items()) if kind == "module"]
        if "physical_devices" in board or any(kind not in {"ic", "module"} for _, kind in quantities):
            own_board["physical_devices"] = [
                {"device_id": part, "model": knowledge[part]["device"]["device_name"],
                 "quantity": quantity, "component_type": kind}
                for (part, kind), quantity in sorted(quantities.items()) if kind not in {"ic", "module"}]
        own_sources = _source_subset(source_index, parts)
        obligations = [deepcopy(row) for row in obligation_state["deferred"] if row["module"] == mid]
        if planning_validation_mode == CANDIDATE_MODE:
            obligations.extend(deepcopy(row) for row in obligation_state["blocking"] if row["module"] == mid)
        # Architecture boundary provenance is an approved input to this stage,
        # even when an interface also cites the device on its other side.
        for record in [*module["ports"].values(), *architecture["voltage_domains"], *obligations]:
            for ref in record["source_refs"]:
                own_sources[ref] = deepcopy(source_index[ref])
        previous, errors, history, derived, normalizations = None, [], [], [], []
        for attempt in range(max_repairs + 1):
            async with semaphore:
                stage_inputs = with_referenced_source_context(
                    {"board": own_board, "request": request, "requirements": requirements,
                     "planning_constraints": load_prompt('architecture_peripheral_choices.md' if projected else 'architecture_peripheral.md'),
                     **({"planner_projection": "pcbagent.peripheral_choices.v1"} if projected else {}),
                     "reserved_fixed_aliases": sorted(pool),
                     "core_pool": own_pool, "knowledge": {part: knowledge[part] for part in sorted(parts)},
                     "source_index": own_sources, "common_templates": templates,
                     "planning_obligations": obligations,
                     "frozen_architecture": {"module": module, "voltage_domains": architecture["voltage_domains"],
                                             "obligations": obligations},
                     "previous_plan": previous, "diagnostics": errors,
                     **({"planning_validation_mode": planning_validation_mode,
                         "retained_architecture_unresolved": deepcopy(architecture["unresolved"])}
                        if planning_validation_mode == CANDIDATE_MODE else {})},
                    knowledge=knowledge, source_index=source_index,
                    source_bundles=obligation_knowledge if obligation_knowledge is not None else knowledge)
                own_sources = stage_inputs["source_index"]
                response = await call(peripheral_agent, "netlist_peripheral", mid, attempt, stage_inputs)
            if response.ok:
                boundary_request = None
                try:
                    if projected:
                        previous, boundary_request = project_peripheral(stage_inputs["frozen_architecture"], response.values.get("result"))
                    else:
                        previous = deepcopy(response.values.get("result"))
                    errors = _boundary_errors(previous, architecture, module, set(pool))
                    if boundary_request is not None:
                        requested_refs = [*boundary_request["source_refs"],
                            *(ref for port in boundary_request.get("ports", {}).values() for ref in port["source_refs"]),
                            *(ref for domain in boundary_request.get("voltage_domains", []) for ref in domain["source_refs"])]
                        if any(ref not in own_sources for ref in requested_refs):
                            errors.append(_error("PERIPHERAL_BOUNDARY_SOURCE", "Boundary request needs known source references", module=mid))
                        elif not any(boundary_request[field] != (module["ports"] if field == "ports" else architecture[field])
                                     for field in ("ports", "voltage_domains") if field in boundary_request):
                            errors.append(_error("PERIPHERAL_BOUNDARY_REQUEST", "Boundary request proposes no actual change", module=mid))
                        else:
                            errors.append(_error("PERIPHERAL_BOUNDARY_REQUEST", "Only the architecture owner may accept this explicit proposal", module=mid, request=boundary_request))
                except (TypeError, ValueError) as exc:
                    errors = [_error("PERIPHERAL_PROJECTION", str(exc), module=mid)]
                if not errors:
                    derived = []
                    normalizations = []
                    _, errors = freeze(previous, own_pool, own_sources, board["board_id"], input_derived_fields=derived,
                                       normalizations=normalizations)
            else:
                errors = [_error("PERIPHERAL_AGENT_FAILED", response.failure_summary() or response.status.value, module=mid)]
            if not errors:
                return {"plan": previous, "diagnostics": [], "repair_history": history, "input_derived_fields": derived,
                        "normalizations": normalizations}
            history.append({"round": attempt, "stage": "netlist_peripheral", "module": mid,
                            "diagnostics": errors, "normalizations": deepcopy(normalizations)})
            if any(row.get("code") == "PERIPHERAL_BOUNDARY_REQUEST" and isinstance(row.get("request"), Mapping) for row in errors):
                break
            if response.status.value in {"error", "cancelled", "content_filtered"}:
                break
        return {"plan": previous, "diagnostics": errors, "repair_history": history, "input_derived_fields": [], "normalizations": normalizations}

    rows = await asyncio.gather(*(peripheral(module) for module in sorted(architecture["modules"], key=lambda row: row["id"])))
    result["peripheral_plans"] = [row["plan"] for row in rows]
    for row in rows:
        result["repair_history"].extend(row["repair_history"])
        result["diagnostics"].extend(row["diagnostics"])
    result["normalizations"] = [record for row in rows for record in row["normalizations"]]
    if result["diagnostics"]:
        return result
    plan = {"modules": [row["plan"]["modules"][0] for row in rows],
            "voltage_domains": deepcopy(architecture["voltage_domains"]),
            "assumptions": sorted(set(architecture["assumptions"]).union(*(set(row["plan"]["assumptions"]) for row in rows))),
            "unresolved": sorted(set().union(*(set(row["plan"]["unresolved"]) for row in rows)))}
    derived = []
    context, errors = freeze(plan, pool, source_index, board["board_id"], input_derived_fields=derived)
    result.update(plan=plan, context=context, diagnostics=errors, input_derived_fields=derived,
                  planning_status="planned" if context is not None else "failed")
    return result

