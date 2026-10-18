"""Agent for implementing one bounded and electrically closed sub-circuit."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..prompts import PromptJSON, PromptText, render_prompt
from ..validators import NetlistFragmentValidator
from ..contracts import WORKER_SCHEMA


class NetlistWorkerAgent(BaseAgent):
    role = AgentRole.NETLIST_WORKER
    task_type = "pcb_netlist_worker"
    purpose = PromptText("legacy_worker_purpose.md")
    output_schema = WORKER_SCHEMA
    outputs = (result_output(WORKER_SCHEMA),)
    validators = (NetlistFragmentValidator(),)

    SYSTEM_PROMPT = PromptText("legacy_worker_system.md")

    output_example = PromptJSON("legacy_worker_example.md")

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            render_prompt(
                "legacy_worker_user.md",
                module=json_text(kwargs.get("module") or {}),
                selection=json_text(kwargs.get("selection") or {}),
                knowledge_evidence=json_text(kwargs.get("knowledge_evidence") or {}),
                plan=json_text(kwargs.get("plan") or {}),
                sub_task=json_text(kwargs.get("sub_task") or {}),
                reference_guidance=json_text(kwargs.get("reference_guidance") or {}),
                schema=json_text(self.output_schema),
                example=json_text(self.output_example),
            )
        )


__all__ = ["NetlistWorkerAgent"]
