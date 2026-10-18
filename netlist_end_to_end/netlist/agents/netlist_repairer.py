"""Agent for minimally repairing a netlist from structured lint findings."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..prompts import PromptJSON, PromptText, render_prompt
from ..contracts import REPAIR_SCHEMA


class NetlistRepairerAgent(BaseAgent):
    role = AgentRole.NETLIST_REPAIRER
    task_type = "pcb_netlist_repairer"
    purpose = PromptText("legacy_repairer_purpose.md")
    output_schema = REPAIR_SCHEMA
    outputs = (result_output(REPAIR_SCHEMA),)

    SYSTEM_PROMPT = PromptText("legacy_repairer_system.md")

    output_example = PromptJSON("legacy_repairer_example.md")

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            render_prompt(
                "legacy_repairer_user.md",
                module=json_text(kwargs.get("module") or {}),
                plan=json_text(kwargs.get("plan") or {}),
                knowledge_evidence=json_text(kwargs.get("knowledge_evidence") or {}),
                netlist_info=json_text(kwargs.get("netlist_info") or {}),
                lint=json_text(kwargs.get("lint") or {}),
                reference_guidance=json_text(kwargs.get("reference_guidance") or {}),
                schema=json_text(self.output_schema),
                example=json_text(self.output_example),
            )
        )


__all__ = ["NetlistRepairerAgent"]
