"""Agent for deriving evidence-bounded netlist topology guidance."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..prompts import PromptJSON, PromptText, render_prompt
from ..contracts import GUIDANCE_SCHEMA


class ReferenceGuidanceAgent(BaseAgent):
    role = AgentRole.REFERENCE_GUIDANCE
    task_type = "pcb_netlist_reference_guidance"
    purpose = PromptText("legacy_reference_guidance_purpose.md")
    output_schema = GUIDANCE_SCHEMA
    outputs = (result_output(GUIDANCE_SCHEMA),)

    SYSTEM_PROMPT = PromptText("legacy_reference_guidance_system.md")

    output_example = PromptJSON("legacy_reference_guidance_example.md")

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            render_prompt(
                "legacy_reference_guidance_user.md",
                module=json_text(kwargs.get("module") or {}),
                selection=json_text(kwargs.get("selection") or {}),
                knowledge_evidence=json_text(kwargs.get("knowledge_evidence") or {}),
                requirement_docs=kwargs.get("requirement_docs") or "",
                schema=json_text(self.output_schema),
                example=json_text(self.output_example),
            )
        )


__all__ = ["ReferenceGuidanceAgent"]
