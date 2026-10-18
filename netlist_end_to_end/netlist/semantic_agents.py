"""Model-facing semantic planning and local connection agents."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from agent_framework.agent import BaseAgent, Context
from agent_framework.message import UserMessage

from ..roles import AgentRole
from ..schemas import json_text, result_output
from .planner_projection import PERIPHERAL
from .prompts import load_prompt
from .semantic_compilation import _declared_rail_requirements
from .semantic_contracts import PLAN_SCHEMA, SCL_LANGUAGE
from .semantic_presentation import (
    CATALOG_ANNOTATION_CONSTRAINT,
    SOURCE_POINTER_CONSTRAINT,
    present_scl_repair,
    prompt_json_text,
    source_index_prompt_view,
)


class SemanticPlannerAgent(BaseAgent):
    role = AgentRole.NETLIST_PLANNER
    task_type = "pcb_semantic_netlist_plan"
    plan_schema = PLAN_SCHEMA
    outputs = (result_output(PLAN_SCHEMA),)

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(load_prompt('semantic_planner_system.md'))
        if kwargs.get("planning_validation_mode") == "retain_unresolved_candidate":
            ctx.constraint(load_prompt("planning_candidate.md"))
        ctx.constraint(CATALOG_ANNOTATION_CONSTRAINT)
        if "frozen_architecture" in kwargs:
            if kwargs.get("planner_projection") == "pcbagent.peripheral_choices.v1":
                ctx.constraint(load_prompt('semantic_planner_projection.md'))
            else:
                ctx.constraint(load_prompt('semantic_planner_frozen_module.md'))

            ctx.constraint(load_prompt('semantic_planner_obligations.md'))
        expected = {alias: [pin["id"] for pin in spec["device"]["pins"]]
                    for alias, spec in kwargs.get("core_pool", {}).items()}
        previous = kwargs.get("previous_plan")
        if isinstance(previous, Mapping) and isinstance(previous.get("modules"), list):
            for module in previous["modules"]:
                if not isinstance(module, Mapping) or not isinstance(module.get("common_devices"), Mapping):
                    continue
                for alias, spec in module["common_devices"].items():
                    device = spec.get("device") if isinstance(spec, Mapping) else None
                    if isinstance(device, Mapping) and isinstance(device.get("pins"), list):
                        expected[f"{module.get('id', '?')}.{alias}"] = [pin["id"] for pin in device["pins"] if isinstance(pin, Mapping) and "id" in pin]
        presentation, pointers = source_index_prompt_view({**kwargs, "expected_pin_usage": expected, "output_schema": self.plan_schema})
        if pointers:
            ctx.constraint(SOURCE_POINTER_CONSTRAINT)
        focus = _peripheral_repair_focus(kwargs)
        if focus is None:
            ctx.user(prompt_json_text(presentation))
        else:
            # Context.user is a setter; preserve the full user input before the final focus.
            ctx.message(UserMessage(prompt_json_text(presentation)))
            ctx.user((load_prompt('semantic_peripheral_focus_intro.md') if kwargs.get("planner_projection") else load_prompt('semantic_planner_focus_intro.md')) +
                load_prompt('semantic_repair_focus_rules.md')
                + prompt_json_text(focus))


class SemanticPeripheralAgent(SemanticPlannerAgent):
    task_type = "pcb_semantic_peripheral_choices"
    planner_projection_kind = "pcbagent.peripheral_choices.v1"
    plan_schema = PERIPHERAL
    outputs = (result_output(plan_schema),)


def _peripheral_repair_focus(inputs: Mapping) -> dict | None:
    """Repeat actual peripheral repair facts last; never change the plan or source keys."""
    diagnostics = inputs.get("diagnostics")
    if (not isinstance(inputs.get("frozen_architecture"), Mapping)
            or not isinstance(diagnostics, list) or not diagnostics):
        return None
    shown = diagnostics[:16]
    previous = inputs.get("previous_plan")
    modules = previous.get("modules", []) if isinstance(previous, Mapping) else []
    index = inputs.get("source_index")
    keys = list(index) if isinstance(index, Mapping) else []
    targets, invalid = [], []
    def refs(value, path):
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "source_refs" and isinstance(child, list):
                    bad = [ref for ref in child if isinstance(ref, str) and ref not in keys]
                    if bad:
                        invalid.append({"path": path + [key], "invalid_refs": deepcopy(bad)})
                else:
                    refs(child, path + [key])
        elif isinstance(value, list):
            for i, child in enumerate(value):
                refs(child, path + [i])
    refs(previous, ["previous_plan"])
    for module in modules if isinstance(modules, list) else []:
        if not isinstance(module, Mapping):
            continue
        mid = module.get("id")
        for usage in module.get("pin_usage", []) if isinstance(module.get("pin_usage"), list) else []:
            if isinstance(usage, Mapping) and any(isinstance(d, Mapping) and
                    (d.get("module"), d.get("device"), d.get("pin")) == (mid, usage.get("device"), usage.get("pin")) for d in shown):
                targets.append({"module": mid, "pin_usage": deepcopy(usage)})
        common = module.get("common_devices", {})
        for alias, spec in common.items() if isinstance(common, Mapping) else []:
            name = f"{mid}.{alias}"
            if isinstance(spec, Mapping) and any(isinstance(d, Mapping) and
                    (d.get("module") == mid and d.get("device") == alias or
                     isinstance(d.get("message"), str) and d["message"].startswith((name + " ", "common " + name + " ")))
                    for d in shown):
                device = spec.get("device", {})
                targets.append({"module": mid, "common_device": alias,
                    "device": {key: deepcopy(device[key]) for key in ("type", "value", "pins")
                               if isinstance(device, Mapping) and key in device},
                    "parameters": deepcopy(spec.get("parameters")), "source_refs": deepcopy(spec.get("source_refs"))})
    return {"diagnostics": deepcopy(shown), "diagnostic_count": len(diagnostics),
            "diagnostics_truncated": len(shown) < len(diagnostics),
            "previous_targets": targets[:16], "target_count": len(targets), "targets_truncated": len(targets) > 16,
            "invalid_source_ref_locations": invalid[:24], "invalid_location_count": len(invalid),
            "invalid_locations_truncated": len(invalid) > 24,
            "available_source_keys": keys[:96], "source_key_count": len(keys), "source_keys_truncated": len(keys) > 96}


class SemanticModuleAgent(BaseAgent):
    role = AgentRole.NETLIST_WORKER
    task_type = "pcb_semantic_module_connections"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(load_prompt('semantic_module_system.md') + SCL_LANGUAGE)
        ctx.constraint(CATALOG_ANNOTATION_CONSTRAINT)
        ctx.constraint(load_prompt('semantic_module_physical_connections.md'))
        ctx.constraint(load_prompt('semantic_module_required_pin_guard.md'))
        module_id = kwargs.get("module_id")
        local = kwargs.get("context", {}).get("modules", {}).get(module_id, {})
        required_pins = [{"device": alias, "pin": pin["id"], "name": pin.get("name")}
                         for alias, device in sorted(local.get("devices", {}).items())
                         for pin in device.get("pins", []) if pin.get("unconnected") is not True]
        missing_pins = [row for row in kwargs.get("diagnostics", [])
                        if row.get("code") == "UNCONNECTED_REQUIRED_PIN"
                        and row.get("endpoint", {}).get("module") == module_id]
        if missing_pins:
            ctx.constraint(load_prompt('semantic_module_missing_pins.md') + json_text(missing_pins))
        ctx.constraint(load_prompt('semantic_module_rail_domains.md'))
        ctx.constraint(load_prompt('semantic_module_frozen_domains.md'))
        rail_requirements = _declared_rail_requirements(kwargs.get("module_plan", {}))
        if rail_requirements:
            ctx.constraint(load_prompt('semantic_module_rail_requirements.md') + json_text(rail_requirements))
        rail_failures = [row for row in kwargs.get("diagnostics", [])
                         if row.get("code") == "PORT_RAIL_DOMAIN_NET" and row.get("module") == kwargs.get("module_id")]
        if rail_failures:
            ctx.constraint(load_prompt('semantic_module_rail_repair.md') + json_text(rail_failures))
        present_scl_repair(ctx, {**kwargs, "required_rail_bindings": rail_requirements,
                                "required_physical_endpoints": required_pins}, module=module_id)

