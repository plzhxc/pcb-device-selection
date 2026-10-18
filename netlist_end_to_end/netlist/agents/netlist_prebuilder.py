"""Agent for the explicit-template netlist short-circuit."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import AgentResult, AgentStatus, BaseAgent, Context

from ...roles import AgentRole
from ..prompts import PromptText
from ..helpers import as_mapping, netlist_info


class NetlistPrebuilderAgent(BaseAgent):
    """Use an explicitly supplied template payload, otherwise decline."""

    role = AgentRole.NETLIST_PREBUILDER
    task_type = "pcb_netlist_explicit_prebuilder"
    purpose = PromptText("legacy_prebuilder_purpose.md")
    step_kind = "python"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        """Local rule: no LLM context is constructed."""

    def run(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                raise TypeError("async execute() requires arun()")
            if not isinstance(values, dict):
                raise TypeError("NetlistPrebuilderAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    async def arun(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                values = await values
            if not isinstance(values, dict):
                raise TypeError("NetlistPrebuilderAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        module = as_mapping(kwargs.get("module"))
        selection = as_mapping(kwargs.get("selection"))
        candidates = (
            selection.get("prebuilt_netlist"),
            selection.get("template_payload"),
            module.get("prebuilt_netlist"),
            module.get("template_payload"),
        )
        payload = next(
            (item for item in candidates if isinstance(item, Mapping)),
            None,
        )
        if payload is None:
            template = str(module.get("circuit_template") or "")
            reason = (
                f"template {template!r} has no supplied template_payload"
                if template
                else "module has no explicit prebuilt payload"
            )
            return {"matched": False, "reason": reason}
        info = netlist_info(payload)
        issues = self._template_issues(info)
        if issues:
            return {
                "matched": False,
                "reason": "explicit template payload failed basic checks",
                "template_issues": issues,
            }
        return {
            "matched": True,
            "netlist_info": info,
            "cite_info": {
                "source": "explicit_template_payload",
                "template": module.get("circuit_template"),
            },
            "lint": {
                "ok": True,
                "status": "prebuilt_checked",
                "issues": [],
            },
        }

    @staticmethod
    def _template_issues(info: Mapping[str, Any]) -> list[str]:
        netlist = as_mapping(info.get("netlist"))
        if not netlist:
            return ["netlist is empty"]
        used_nets: set[str] = set()
        issues: list[str] = []
        for key, raw_device in netlist.items():
            device = as_mapping(raw_device)
            props = as_mapping(device.get("props"))
            pins = as_mapping(device.get("pins"))
            if not props.get("designator") or not props.get("deviceName"):
                issues.append(f"{key}: missing designator or deviceName")
            if not pins:
                issues.append(f"{key}: pins are empty")
            used_nets.update(
                str(net) for net in pins.values() if str(net).upper() != "NC"
            )
        cross = as_mapping(info.get("crossPageNet"))
        for direction in ("inputs", "outputs"):
            rows = cross.get(direction) or []
            if not isinstance(rows, Sequence) or isinstance(
                rows, (str, bytes)
            ):
                issues.append(f"crossPageNet.{direction} must be a list")
                continue
            for net in rows:
                if str(net) not in used_nets:
                    issues.append(
                        f"crossPageNet.{direction}: unused net {net!r}"
                    )
        return issues


__all__ = ["NetlistPrebuilderAgent"]
