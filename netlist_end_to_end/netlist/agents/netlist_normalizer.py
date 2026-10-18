"""Agent for normalizing netlist aliases and ordering."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import AgentResult, AgentStatus, BaseAgent, Context

from ...roles import AgentRole
from ..prompts import PromptText
from ..helpers import as_mapping, netlist_info


class NetlistNormalizerAgent(BaseAgent):
    """Normalize aliases and ordering without changing electrical meaning."""

    role = AgentRole.NETLIST_NORMALIZER
    task_type = "pcb_netlist_normalizer"
    purpose = PromptText("legacy_normalizer_purpose.md")
    step_kind = "python"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        """Local rule: no LLM context is constructed."""

    def run(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                raise TypeError("async execute() requires arun()")
            if not isinstance(values, dict):
                raise TypeError("NetlistNormalizerAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    async def arun(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                values = await values
            if not isinstance(values, dict):
                raise TypeError("NetlistNormalizerAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    _TYPE_ALIASES = {
        "resistor": "resistance",
        "capacitor": "capacitance",
        "inductor": "inductance",
        "ic": "chip",
        "integrated_circuit": "chip",
        "connector": "connector",
    }

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        rows = kwargs.get("module_results") or []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            rows = []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            result = dict(row)
            info = netlist_info(row.get("netlist_info"))
            rebuilt: dict[str, Any] = {}
            for index, raw_device in enumerate(
                info["netlist"].values(),
                start=1,
            ):
                if not isinstance(raw_device, Mapping):
                    continue
                device = dict(raw_device)
                props = as_mapping(raw_device.get("props"))
                raw_type = str(props.get("deviceType") or "other").lower()
                props["deviceType"] = self._TYPE_ALIASES.get(
                    raw_type,
                    raw_type,
                )
                device["props"] = props
                device["pins"] = {
                    str(pin): str(net)
                    for pin, net in as_mapping(
                        raw_device.get("pins")
                    ).items()
                }
                rebuilt[f"gge{index}"] = device
            info["netlist"] = rebuilt
            result["netlist_info"] = info
            normalized.append(result)
        return {"module_results": normalized}


__all__ = ["NetlistNormalizerAgent"]
