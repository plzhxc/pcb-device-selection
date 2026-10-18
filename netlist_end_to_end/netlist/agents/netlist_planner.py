"""Agent for planning one module's complete, bounded netlist work."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..prompts import PromptJSON, PromptText, render_prompt
from ..contracts import PLANNER_SCHEMA


class NetlistPlannerAgent(BaseAgent):
    role = AgentRole.NETLIST_PLANNER
    task_type = "pcb_netlist_master_planner"
    purpose = PromptText("legacy_planner_purpose.md")
    output_schema = PLANNER_SCHEMA
    outputs = (result_output(PLANNER_SCHEMA),)

    SYSTEM_PROMPT = PromptText("legacy_planner_system.md")

    output_example = PromptJSON("legacy_planner_example.md")

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            render_prompt(
                "legacy_planner_user.md",
                module=json_text(kwargs.get("module") or {}),
                selection=json_text(kwargs.get("selection") or {}),
                knowledge_evidence=json_text(kwargs.get("knowledge_evidence") or {}),
                module_decomposition=json_text(kwargs.get("module_decomposition") or {}),
                reference_guidance=json_text(kwargs.get("reference_guidance") or {}),
                module_index=kwargs.get("module_index") or 0,
                module_total=kwargs.get("module_total") or 0,
                requirement_docs=kwargs.get("requirement_docs") or "",
                schema=json_text(self.output_schema),
                example=json_text(self.output_example),
            )
        )


__all__ = ["NetlistPlannerAgent"]
