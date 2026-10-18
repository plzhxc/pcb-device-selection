"""Agents and bounded repair presentation for architecture planning."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from agent_framework.agent import BaseAgent, Context
from agent_framework.message import UserMessage
from validation_platform.planning_obligations import CANDIDATE_MODE

from ..roles import AgentRole
from ..schemas import result_output
from .prompts import load_prompt, render_prompt
from .planner_projection import INVENTORY_REPAIR_SCHEMA, boundary_schema
from .semantic_architecture_validation import ARCHITECTURE_SCHEMA
from .semantic_presentation import (source_index_prompt_view, SOURCE_POINTER_CONSTRAINT,
    prompt_json_text, STAGE_BOUNDARY_CONSTRAINT)

OBLIGATION_PROMPT = load_prompt('planning_obligations.md')
CANDIDATE_PROMPT = load_prompt('planning_candidate.md')


class SemanticArchitectureInventoryRepairAgent(BaseAgent):
    role = AgentRole.NETLIST_PLANNER
    task_type = "pcb_semantic_architecture_missing_inventory_repair"
    outputs = (result_output(INVENTORY_REPAIR_SCHEMA),)

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(load_prompt('architecture_inventory_repair.md'))
        ctx.user(prompt_json_text(kwargs))


class SemanticArchitectureAgent(BaseAgent):
    role = AgentRole.NETLIST_PLANNER
    task_type = "pcb_semantic_board_architecture"
    plan_schema = ARCHITECTURE_SCHEMA
    outputs = (result_output(ARCHITECTURE_SCHEMA),)

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(load_prompt('architecture_system.md'))
        ctx.constraint(OBLIGATION_PROMPT)
        if kwargs.get("architecture_context_profile") == "stage_boundary_v1":
            ctx.constraint(STAGE_BOUNDARY_CONSTRAINT)
        if kwargs.get("planning_validation_mode") == CANDIDATE_MODE:
            ctx.constraint(CANDIDATE_PROMPT)
        if kwargs.get("planner_projection") == "pcbagent.architecture_boundary_patch.v1":
            ctx.constraint(load_prompt('architecture_boundary_patch.md'))
            ctx.constraint(load_prompt('architecture_boundary_replacement.md'))
        presentation, pointers = source_index_prompt_view({**kwargs, "output_schema": self.plan_schema})
        if pointers:
            ctx.constraint(SOURCE_POINTER_CONSTRAINT)
        focus = _architecture_repair_focus(kwargs)
        if focus is None:
            ctx.user(prompt_json_text(presentation))
        else:
            # Context.user replaces its value; raw messages precede the final user turn.
            ctx.message(UserMessage(prompt_json_text(presentation)))
            ctx.user(render_prompt('architecture_boundary_repair.md' if kwargs.get('planner_projection') else 'architecture_repair.md', focus=prompt_json_text(focus)))


class SemanticArchitectureBoundaryAgent(SemanticArchitectureAgent):
    task_type = "pcb_semantic_architecture_boundary_patch"
    plan_schema = boundary_schema()
    outputs = (result_output(plan_schema),)


def _architecture_repair_focus(inputs: Mapping) -> dict | None:
    """Repeat existing repair targets last; no selection, validation or fact inference."""
    diagnostics = inputs.get("diagnostics")
    if not isinstance(diagnostics, list) or not diagnostics:
        return None
    shown = diagnostics[:16]
    targets = {row.get("obligation") for row in shown
               if isinstance(row, Mapping) and isinstance(row.get("obligation"), str)}
    previous = inputs.get("previous_architecture")
    obligations = previous.get("obligations", []) if isinstance(previous, Mapping) else []
    choices = inputs.get("configuration_choices")
    focus = {"diagnostics": deepcopy(shown), "diagnostic_count": len(diagnostics),
            "diagnostics_truncated": len(diagnostics) > len(shown),
            "previous_obligations": [deepcopy(row) for row in obligations
                if isinstance(row, Mapping) and row.get("id") in targets],
            "configuration_choices": {key: deepcopy(choices[key]) for key in
                ("already_selected_input", "source_internal_options")
                if isinstance(choices, Mapping) and key in choices}}
    domain_targets = {row.get("domain") for row in shown
                      if isinstance(row, Mapping) and isinstance(row.get("domain"), str)}
    if domain_targets and isinstance(previous, Mapping):
        focus["previous_voltage_domains"] = [deepcopy(row) for row in previous.get("voltage_domains", [])
            if isinstance(row, Mapping) and row.get("id") in domain_targets][:16]
    if any(isinstance(row, Mapping) and row.get("code") == "ARCHITECTURE_DOMAIN_PROFILE" for row in shown):
        focus["profile_domain_constraint"] = load_prompt('architecture_profile_domain.md')
    return focus

