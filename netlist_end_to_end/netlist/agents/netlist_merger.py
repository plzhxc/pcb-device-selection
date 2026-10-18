"""Agent for deterministically merging netlist worker fragments."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import AgentResult, AgentStatus, BaseAgent, Context

from ...roles import AgentRole
from ..prompts import PromptText
from ..helpers import as_mapping, cross_page, netlist_info


class NetlistMergerAgent(BaseAgent):
    """Merge worker fragments and materialize the planned main chip."""

    role = AgentRole.NETLIST_MERGER
    task_type = "pcb_netlist_deterministic_merger"
    purpose = PromptText("legacy_merger_purpose.md")
    step_kind = "python"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        """Local rule: no LLM context is constructed."""

    def run(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                raise TypeError("async execute() requires arun()")
            if not isinstance(values, dict):
                raise TypeError("NetlistMergerAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    async def arun(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                values = await values
            if not isinstance(values, dict):
                raise TypeError("NetlistMergerAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        plan = as_mapping(kwargs.get("plan"))
        sub_results = kwargs.get("sub_results") or []
        if not isinstance(sub_results, Sequence) or isinstance(
            sub_results, (str, bytes)
        ):
            sub_results = []

        merged: dict[str, Any] = {}
        used_designators: set[str] = set()

        main = as_mapping(plan.get("main_chip"))
        if main:
            device = self._main_device(main)
            designator = str(device["props"]["designator"])
            used_designators.add(designator)
            merged["gge1"] = device

        next_key = len(merged) + 1
        notes: list[str] = []
        crosses = [cross_page(plan.get("crossPageNet"))]
        for raw_result in sub_results:
            info = netlist_info(raw_result)
            notes.append(str(info.get("notes") or ""))
            crosses.append(cross_page(info.get("crossPageNet")))
            for raw_device in info["netlist"].values():
                if not isinstance(raw_device, Mapping):
                    continue
                device = {
                    **dict(raw_device),
                    "props": as_mapping(raw_device.get("props")),
                    "pins": as_mapping(raw_device.get("pins")),
                }
                raw_designator = str(
                    device["props"].get("designator") or "X1"
                )
                designator = self._unique_designator(
                    raw_designator,
                    used_designators,
                )
                device["props"]["designator"] = designator
                used_designators.add(designator)
                while f"gge{next_key}" in merged:
                    next_key += 1
                merged[f"gge{next_key}"] = device
                next_key += 1

        inputs = list(
            dict.fromkeys(
                name for cross in crosses for name in cross["inputs"]
            )
        )
        outputs = list(
            dict.fromkeys(
                name for cross in crosses for name in cross["outputs"]
            )
        )
        return {
            "netlist_info": {
                "netlist": merged,
                "crossPageNet": {"inputs": inputs, "outputs": outputs},
                "notes": "\n".join(note for note in notes if note).strip(),
            }
        }

    @staticmethod
    def _main_device(main: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "props": {
                "designator": str(main.get("designator") or "U1"),
                "deviceName": str(
                    main.get("device_name") or main.get("deviceName") or "IC"
                ),
                "supplierPart": str(
                    main.get("supplier_part")
                    or main.get("supplierPart")
                    or ""
                ),
                "value": str(main.get("value") or ""),
                "deviceType": str(main.get("device_type") or "chip"),
                "deviceNotes": str(main.get("note") or ""),
            },
            "pins": {
                str(pin): str(net)
                for pin, net in as_mapping(main.get("pins")).items()
            },
        }

    @staticmethod
    def _unique_designator(raw: str, used: set[str]) -> str:
        candidate = raw.strip() or "X1"
        if candidate not in used:
            return candidate
        match = re.fullmatch(r"([A-Za-z]+)(\d+)", candidate)
        prefix = match.group(1) if match else re.sub(r"\d+$", "", candidate)
        prefix = prefix or "X"
        number = int(match.group(2)) + 1 if match else 2
        while f"{prefix}{number}" in used:
            number += 1
        return f"{prefix}{number}"


__all__ = ["NetlistMergerAgent"]
