"""Orchestrate bounded semantic planning, SCL generation and repair attempts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from typing import Any, Callable, Mapping
import hashlib
import inspect
import json

from agent_framework.agent import AgentConfig, AgentResult, AgentStatus, BaseAgent
from agent_framework.llm import AsyncLLMClient
from validation_platform.certificate_facts import digest as certificate_digest
from validation_platform.planning_obligations import freeze_obligations

from ..roles import AgentRole
from ..top_port.semantic import SemanticTopPortAgent
from .prompts import load_prompt
from .scl import SCLSource
from .semantic_agents import SemanticModuleAgent, SemanticPeripheralAgent, SemanticPlannerAgent
from .semantic_architecture import plan_with_architecture
from .semantic_compilation import _compile_iteration, _top_port_diagnostics
from .semantic_domains import materialize_voltage_domains
from .semantic_inventory import _prepare, _template_facts
from .semantic_plan import _freeze
from .semantic_presentation import with_referenced_source_context
from .semantic_selection import fixed_device_part, resolve_plan_operating_selections
from .semantic_shared import MAX_REPAIRS, _diagnostic, _digest


def _config(configs: Mapping | AgentConfig, role: AgentRole) -> AgentConfig:
    if isinstance(configs, AgentConfig):
        config = configs
    else:
        choices = {getattr(key, "value", key): value for key, value in configs.items()}
        config = choices.get(role.value, choices.get("default"))
    if not isinstance(config, AgentConfig):
        raise ValueError(f"missing AgentConfig for {role.value}")
    return replace(config, max_output_retries=0, max_validation_retries=0,
                   max_tool_iterations=0, max_tool_calls_per_iteration=0)


async def _emit(callback: Callable | None, event: dict) -> None:
    if callback is not None:
        result = callback(event)
        if inspect.isawaitable(result):
            await result


async def generate_semantic_board(board_input: Mapping[str, Any], *, request: str, requirements: Any,
                                  knowledge: Mapping[str, Any], client: AsyncLLMClient,
                                  configs: Mapping | AgentConfig, on_event: Callable | None = None,
                                  planning_strategy: str = "monolithic", planner_concurrency: int = 3,
                                  max_module_core_pins: int = 128,
                                  planning_validation_mode: str = "strict",
                                  architecture_context_profile: str = "legacy_full_contracts") -> dict[str, Any]:
    """Generate only the netlist and top_port stages, with at most two repairs.

    Monolithic planning shares two repairs with SCL compilation. Architecture
    planning permits two repairs per bounded planner, then two SCL repairs;
    this explicit strategy is recorded for controlled comparisons. Structural
    compilation never claims electrical correctness.
    Model calls receive no native tools and never fetch reference/case data.
    """
    board_id = str(board_input.get("board_id") or "") if isinstance(board_input, Mapping) else ""
    result: dict[str, Any] = {"schema_version": "pcbagent.semantic_board.v1", "board_id": board_id,
        "status": "not_ready", "stages": {"netlist": "not_ready", "top_port": "not_started"},
        "plan": None, "context": None, "sources": [], "compiled_sources": [],
        "domain_materialization": None, "compilation": None,
        "diagnostics": [], "agent_runs": [], "repair_history": [], "source_index": {},
        "planning_strategy": planning_strategy}
    from validation_platform.planning_obligations import PLANNING_VALIDATION_MODES, CANDIDATE_MODE
    if planning_validation_mode not in PLANNING_VALIDATION_MODES:
        result["diagnostics"] = [_diagnostic("INVALID_PLANNING_CONFIGURATION", "Unknown planning_validation_mode")]
        return result
    from .semantic_presentation import ARCHITECTURE_CONTEXT_PROFILES
    from validation_platform.certificate_facts import digest as frozen_knowledge_digest
    if (architecture_context_profile not in ARCHITECTURE_CONTEXT_PROFILES
            or (architecture_context_profile != "legacy_full_contracts" and planning_strategy != "architecture")):
        result["diagnostics"] = [_diagnostic("INVALID_PLANNING_CONFIGURATION", "Unknown or inapplicable architecture_context_profile")]
        return result
    result["architecture_context_profile"] = architecture_context_profile
    retain = planning_validation_mode == CANDIDATE_MODE
    if retain:
        result["planning_validation_mode"] = planning_validation_mode
    if not board_id or not isinstance(request, str) or not request.strip() or not isinstance(knowledge, Mapping):
        result["diagnostics"] = [_diagnostic("INVALID_INPUT", "board_id, nonempty request and knowledge object are required")]
        return result
    if (planning_strategy not in {"monolithic", "architecture"} or type(planner_concurrency) is not int
            or not 1 <= planner_concurrency <= 16 or type(max_module_core_pins) is not int
            or not 1 <= max_module_core_pins <= 2000):
        result["diagnostics"] = [_diagnostic("INVALID_PLANNING_CONFIGURATION", "planning_strategy must be monolithic/architecture, planner_concurrency 1..16 and max_module_core_pins 1..2000")]
        return result
    result["planning_limits"] = {"planner_concurrency": planner_concurrency, "max_module_core_pins": max_module_core_pins,
        "max_repairs_per_planner": MAX_REPAIRS, "compiler_max_repairs": MAX_REPAIRS,
        "max_architecture_reconciliations": 1 if planning_strategy == "architecture" else 0,
        "repair_budget_scope": "per_bounded_planner_then_compiler" if planning_strategy == "architecture" else "shared_planner_and_compiler"}
    pool, selected, source_index, errors = _prepare(board_input, knowledge, requirements)
    result["fixed_inventory"] = {alias: {"identity": deepcopy(specification["identity"]),
        "pins": deepcopy(specification["device"]["pins"])} for alias, specification in pool.items()}
    result["fixed_inventory_sha256"] = hashlib.sha256(json.dumps(result["fixed_inventory"],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    result["source_index"] = source_index
    source_index["request"]["value"] = request
    operating_authority = {"inventory": result["fixed_inventory"], "knowledge": knowledge,
        "board_input": {**deepcopy(dict(board_input)), "request": request, "requirements": deepcopy(requirements)},
        "source_index": source_index}

    def freeze_plan(candidate: Any, local_pool: dict, local_sources: dict, bid: str, **kwargs: Any):
        return _freeze(candidate, local_pool, local_sources, bid, operating_authority=operating_authority,
                       static_environment=board_input.get("static_environment"),
                       planning_validation_mode=planning_validation_mode, **kwargs)
    if errors:
        result["diagnostics"] = errors
        await _emit(on_event, {"event": "preparation_not_ready", "board_id": board_id, "diagnostics": errors})
        return result
    safe_board = {"board_id": board_id, "label": board_input.get("label"),
                  "core_devices": [{k: row.get(k) for k in ("device_id", "model", "quantity")} for row in board_input["core_devices"]]}
    for collection in ("functional_devices", "physical_devices"):
        if collection in board_input:
            safe_board[collection] = [{k: row.get(k) for k in ("device_id", "model", "quantity", "component_type")}
                                      for row in board_input[collection]]
    if "static_environment" in board_input:
        safe_board["static_environment"] = deepcopy(board_input["static_environment"])
    templates = _template_facts()
    for template in templates:
        key = f"template:{template['template_id']}@{template['version']}"
        source_index[key] = {"kind": "template", "template_id": template["template_id"],
                             "version": template["version"], "sha256": template["definition_sha256"]}
    try:
        role_configs = {role: _config(configs, role) for role in (AgentRole.NETLIST_PLANNER, AgentRole.NETLIST_WORKER, AgentRole.TOP_PORT_PLANNER)}
    except (TypeError, AttributeError, ValueError) as exc:
        result["diagnostics"] = [_diagnostic("INVALID_CONFIGURATION", str(exc))]
        return result

    async def call(agent_type: type[BaseAgent], stage: str, module: str | None, round_index: int, payload: dict):
        await _emit(on_event, {"event": "agent_started", "board_id": board_id, "stage": stage,
                              "role": agent_type.role.value, "module": module, "round": round_index,
                              "inputs": deepcopy(payload), "params": role_configs[agent_type.role].params.digest()})
        agent = agent_type(client, role_configs[agent_type.role], enable_native_tools=False)
        try:
            response = await agent.arun(**payload)
        except Exception as exc:
            response = AgentResult(status=AgentStatus.ERROR, error=exc)
        record = {"event": "agent_completed", "board_id": board_id, "stage": stage,
                  "role": agent_type.role.value, "module": module, "round": round_index,
                  "run_id": response.run_id, "status": response.status.value,
                  "text": response.text, "values": response.values, "usage": response.usage.to_dict(),
                  "llm_calls": response.llm_calls, "input_sha256": _digest(payload),
                  "error": response.failure_summary(),
                  "messages": [{"type": type(m).__name__, "content": getattr(m, "text", str(m))} for m in response.messages]}
        result["agent_runs"].append(record)
        await _emit(on_event, deepcopy(record))
        return response

    reconciliation = None
    # One owner reconsideration is shared with the existing peripheral-boundary path.
    for construction_attempt in range(2):
        repairs = 0
        plan, context, plan_errors, previous = None, None, [], None
        if planning_strategy == "architecture":
            planned = await plan_with_architecture(board=safe_board, request=request, requirements=requirements,
                pool=pool, knowledge=selected, source_index=source_index, templates=templates, call=call,
                peripheral_agent=SemanticPeripheralAgent, freeze=freeze_plan, concurrency=planner_concurrency,
                max_module_core_pins=max_module_core_pins, max_repairs=MAX_REPAIRS, obligation_knowledge=knowledge,
                planning_validation_mode=planning_validation_mode,
                architecture_context_profile=architecture_context_profile, frozen_knowledge_sha256=frozen_knowledge_digest(knowledge), reconciliation=reconciliation)
            result.update({key: planned[key] for key in ("architecture", "plan", "context", "diagnostics", "input_derived_fields", "normalizations")})
            result["peripheral_plans"] = planned.get("peripheral_plans", [])
            if "architecture_obligation_validation" in planned:
                result["architecture_obligation_validation"] = deepcopy(planned["architecture_obligation_validation"])
            result["repair_history"].extend(planned["repair_history"])
            plan, context = planned["plan"], planned["context"]
            if context is None:
                result["status"] = planned["planning_status"]
                result["stages"]["netlist"] = planned["planning_status"]
                return result
        while context is None:
            response = await call(SemanticPlannerAgent, "netlist", None, repairs,
                                  {"board": safe_board, "request": request, "requirements": requirements,
                                   "core_pool": pool, "knowledge": selected, "source_index": source_index,
                                   "common_templates": templates,
                                   "previous_plan": previous, "diagnostics": plan_errors,
                                   **({"planning_validation_mode": planning_validation_mode} if retain else {})})
            if response.ok:
                plan = deepcopy(response.values.get("result"))
                input_derived_fields = []
                normalizations = []
                context, plan_errors = freeze_plan(plan, pool, source_index, board_id, input_derived_fields=input_derived_fields,
                                               normalizations=normalizations)
                result["input_derived_fields"] = input_derived_fields
                result["normalizations"] = normalizations
            else:
                plan_errors = [_diagnostic("PLANNER_OUTPUT_FAILED", response.failure_summary() or response.status.value)]
            if context is not None:
                break
            if repairs >= MAX_REPAIRS or response.status.value in {"error", "cancelled", "content_filtered"}:
                result.update(status="failed", plan=plan, diagnostics=plan_errors)
                result["stages"]["netlist"] = "failed"
                return result
            repairs += 1
            previous = plan if plan is not None else response.text
            result["repair_history"].append({"round": repairs, "stage": "netlist_plan", "diagnostics": plan_errors,
                                              "normalizations": deepcopy(result.get("normalizations", []))})
        result.update(plan=plan, context=context)
        if result.get("architecture", {}).get("obligations") or retain or reconciliation is not None:
            obligation_result = freeze_obligations(result.get("architecture") or {}, plan, context, result["fixed_inventory"],
                knowledge, board_input=operating_authority["board_input"], source_index=source_index,
                planning_validation_mode=planning_validation_mode, peripheral_plans=result.get("peripheral_plans"),
                agent_runs=result["agent_runs"], repair_history=result["repair_history"])
            if obligation_result["diagnostics"]:
                result.update(status="not_ready", diagnostics=obligation_result["diagnostics"])
                result["stages"]["netlist"] = "not_ready"
                return result
            result["architecture_obligations"] = obligation_result["receipt"]
        # Export only the final normalized, merged plan's independently rebuilt
        # receipt. Provisional per-module results and raw model output are distinct.
        final_operating = resolve_plan_operating_selections(plan, pool, operating_authority)
        if final_operating["receipts"] or final_operating["diagnostics"]:
            result["operating_selections"] = final_operating
            if final_operating["diagnostics"]:
                result.update(status="failed", diagnostics=final_operating["diagnostics"])
                result["stages"]["netlist"] = "failed"
                return result
        if plan["unresolved"] and not retain:
            result["diagnostics"] = [_diagnostic("PLAN_NOT_READY", message) for message in plan["unresolved"]]
            return result
        domain_source, domain_record = materialize_voltage_domains(plan, board_id)
        result["domain_materialization"] = deepcopy(domain_record)
        await _emit(on_event, {"event": "voltage_domains_materialized", "board_id": board_id,
                              "record": deepcopy(domain_record), "source": asdict(domain_source) if domain_source else None})
        module_plans = {module["id"]: module for module in plan["modules"]}
        sources: dict[str, SCLSource] = {}
        affected = set(module_plans)
        repair_top = True
        compile_errors: list[dict] = []
        while True:
            for module in sorted(affected):
                local_context = {**context, "modules": {module: context["modules"][module]}}
                part_ids = {fixed_device_part(pool[alias]) for alias in module_plans[module]["core_instances"]}
                response = await call(SemanticModuleAgent, "netlist", module, repairs,
                                      with_referenced_source_context({"request": request, "requirements": requirements, "module_id": module,
                                       "context": local_context, "module_plan": module_plans[module],
                                       "voltage_domains": plan["voltage_domains"],
                                       "knowledge": {part: selected[part] for part in sorted(part_ids)},
                                       "previous_scl": sources[module].text if module in sources else None,
                                       "previous_scl_source": sources[module].name if module in sources else None,
                                       "diagnostics": compile_errors,
                                       **({"planning_validation_mode": planning_validation_mode,
                                           "retained_planning_issues": deepcopy(result["architecture_obligations"]["pending_planning_issues"])}
                                          if retain else {})}, knowledge=selected, source_index=source_index,
                                          source_bundles=knowledge))
                if not response.ok:
                    result.update(status="failed", diagnostics=[_diagnostic("MODULE_AGENT_FAILED", response.failure_summary() or response.status.value, module=module)])
                    result["stages"]["netlist"] = "failed"
                    return result
                sources[module] = SCLSource(f"{board_id}:{module}:local", module, response.text)
                result["sources"] = [asdict(s) for s in sorted(sources.values(), key=lambda s: s.name)]
            if repair_top:
                response = await call(SemanticTopPortAgent, "top_port", None, repairs,
                                      {"request": request, "requirements": requirements,
                                       "module_interfaces": [{key: deepcopy(module_plans[module][key]) for key in ("id", "label", "intent", "ports")}
                                                             for module in sorted(module_plans)],
                                       "available_endpoints": [{"module": module, "port": pid, "bit": bit, "external": port["external"]}
                                                               for module in sorted(module_plans)
                                                               for pid,port in sorted(module_plans[module]["ports"].items()) for bit in range(port["width"])],
                                       "scope": load_prompt('semantic_top_scope.md'),
                                       "previous_scl": sources["__top__"].text if "__top__" in sources else None,
                                       "previous_scl_source": sources["__top__"].name if "__top__" in sources else None,
                                       "diagnostics": _top_port_diagnostics(compile_errors, board_id)})
                if not response.ok:
                    result.update(status="failed", diagnostics=[_diagnostic("TOP_PORT_AGENT_FAILED", response.failure_summary() or response.status.value)])
                    result["stages"].update(netlist="generated", top_port="failed")
                    return result
                sources["__top__"] = SCLSource(f"{board_id}:top", None, response.text)
            compiled, compilation, compile_errors = _compile_iteration(
                result, sources, domain_source=domain_source, pool=pool, knowledge=knowledge,
                operating_authority=operating_authority, templates=templates, repairs=repairs,
                retain=retain, planning_validation_mode=planning_validation_mode)
            await _emit(on_event, {"event": "compilation", "board_id": board_id, "round": repairs,
                                  "ok": not compile_errors, "diagnostics": compile_errors})
            if result.get("pin_contract_precheck", {}).get("unassigned_failures"):
                result["status"] = "failed"
                result["stages"].update(netlist="failed", top_port="failed")
                return result
            if not compile_errors:
                result["status"] = "compiled"
                result["stages"].update(netlist="compiled", top_port="compiled")
                return result
            if repairs >= MAX_REPAIRS:
                from .semantic_architecture import final_port_reconciliation
                reconciliation = final_port_reconciliation(result) if construction_attempt == 0 else None
                if reconciliation is not None:
                    history = {"stage": "netlist_architecture_reconciliation", "origin": "final_internal_port",
                        "round": 1, "maximum_reconciliations": 1,
                        "previous_agent_runs_count": len(result["agent_runs"]),
                        "previous_architecture": deepcopy(result["architecture"]),
                        "discarded_peripheral_plans": deepcopy(result["peripheral_plans"]),
                        "discarded_plan": deepcopy(plan), "discarded_context": deepcopy(context),
                        "discarded_compiled_sources": deepcopy(result["compiled_sources"]),
                        "discarded_compilation": deepcopy(compilation),
                        "discarded_architecture_obligations": deepcopy(result.get("architecture_obligations")),
                        "discarded_operating_selections": deepcopy(result.get("operating_selections")),
                        "discarded_domain_materialization": deepcopy(result.get("domain_materialization")),
                        "discarded_electrical_precheck": deepcopy(result.get("electrical_precheck")),
                        "diagnostics": deepcopy(compile_errors),
                        "observations": deepcopy(reconciliation["observations"]),
                        "peripheral_unresolved": deepcopy(reconciliation["peripheral_unresolved"]),
                        "frozen_state": {"input": certificate_digest(operating_authority["board_input"]),
                            "knowledge": certificate_digest(knowledge),
                            "inventory": certificate_digest(result["fixed_inventory"]),
                            "source_index": certificate_digest(source_index)},
                        "regeneration_policy": "Regenerate all peripheral plans and local/top SCL; prior outputs are history, not source authority."}
                    reconciliation["history_sha256"] = certificate_digest(history)
                    result["repair_history"].append(history)
                    await _emit(on_event, {"event": "architecture_reconciliation_requested", "board_id": board_id,
                        "origin": "final_internal_port", "history_sha256": reconciliation["history_sha256"],
                        "diagnostics": deepcopy(compile_errors)})
                    for key in ("architecture_obligations", "operating_selections", "electrical_precheck",
                                "architecture_obligation_validation", "architecture", "peripheral_plans"):
                        result.pop(key, None)
                    result.update(plan=None, context=None, sources=[], compiled_sources=[], compilation=None,
                                  domain_materialization=None, diagnostics=[], status="not_ready")
                    result["stages"].update(netlist="not_ready", top_port="not_started")
                    break
                result["status"] = "failed"
                result["stages"].update(netlist="failed", top_port="failed")
                return result
            repairs += 1
            result["repair_history"].append({"round": repairs, "stage": "scl_compile", "diagnostics": compile_errors})
            affected, repair_top = set(), False
            missing_top_ports = {(d["endpoint"]["module"], d["endpoint"]["port"], d["endpoint"]["bit"])
                                 for d in compile_errors if d["code"] == "UNCONNECTED_INTERNAL_PORT"}
            top_blocked_pins = set()
            for network in compiled.manifest["networks"]:
                if any((member["module"], member["port"], member["bit"]) in missing_top_ports
                       for member in network["members"] if member["kind"] == "port"):
                    top_blocked_pins.update((member["module"], member["device"], member["pin"])
                                           for member in network["members"] if member["kind"] == "pin")
            for diagnostic in compile_errors:
                if diagnostic["code"] == "UNCONNECTED_INTERNAL_PORT" or diagnostic.get("source") == f"{board_id}:top":
                    repair_top = True
                    continue
                endpoint = diagnostic.get("endpoint", {})
                if (diagnostic["code"] == "UNCONNECTED_REQUIRED_PIN"
                        and (endpoint.get("module"), endpoint.get("device"), endpoint.get("pin")) in top_blocked_pins):
                    repair_top = True
                    continue
                module = diagnostic.get("module") or diagnostic.get("endpoint", {}).get("module")
                if module in module_plans:
                    affected.add(module)
                else:
                    affected.update(module_plans)
                    repair_top = True

    return result

