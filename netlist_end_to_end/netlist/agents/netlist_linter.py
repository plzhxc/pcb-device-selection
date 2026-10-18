"""Agent for deterministic structural and topology linting."""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Mapping
from typing import Any

from agent_framework.agent import AgentResult, AgentStatus, BaseAgent, Context

from ...roles import AgentRole
from ...tools.netlist_evidence.common import core_device_type
from ..prompts import PromptText
from ..helpers import as_mapping, cross_page, netlist_info, nonphysical_connection_device


class NetlistLinterAgent(BaseAgent):
    """Small independent structural/topological gate."""

    role = AgentRole.NETLIST_LINTER
    task_type = "pcb_netlist_basic_linter"
    purpose = PromptText("legacy_linter_purpose.md")
    step_kind = "python"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        """Local rule: no LLM context is constructed."""

    def run(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                raise TypeError("async execute() requires arun()")
            if not isinstance(values, dict):
                raise TypeError("NetlistLinterAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    async def arun(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                values = await values
            if not isinstance(values, dict):
                raise TypeError("NetlistLinterAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    @classmethod
    def execute(cls, **kwargs: Any) -> dict[str, Any]:
        info = netlist_info(kwargs.get("netlist_info"))
        netlist = info["netlist"]
        issues: list[dict[str, str]] = []
        designators: list[str] = []
        net_counts: Counter[str] = Counter()

        if not netlist:
            cls._issue(
                issues,
                "empty_netlist",
                "error",
                "netlist",
                "网表不能为空。",
            )

        for key, raw_device in netlist.items():
            if not isinstance(raw_device, Mapping):
                cls._issue(
                    issues,
                    "invalid_device",
                    "error",
                    f"netlist.{key}",
                    "器件必须是对象。",
                )
                continue
            props = as_mapping(raw_device.get("props"))
            pins = as_mapping(raw_device.get("pins"))
            designator = str(props.get("designator") or "").strip()
            device_type = str(props.get("deviceType") or "").strip()
            if nonphysical_connection_device(props):
                cls._issue(issues, "nonphysical_connection_device", "error", f"netlist.{key}",
                           "虚构的连线占位不是器件；直连应使用同一个网络，不能把不同网络交给 other 占位物连通。")
            if not designator:
                cls._issue(
                    issues,
                    "missing_designator",
                    "error",
                    f"netlist.{key}.props.designator",
                    "器件缺少位号。",
                )
            else:
                designators.append(designator)
            if not props.get("deviceName"):
                cls._issue(
                    issues,
                    "missing_device_name",
                    "error",
                    f"netlist.{key}.props.deviceName",
                    "器件缺少名称。",
                )
            if not pins:
                cls._issue(
                    issues,
                    "empty_pins",
                    "error",
                    f"netlist.{key}.pins",
                    "器件至少需要一个引脚连接。",
                )
            for pin, raw_net in pins.items():
                net = str(raw_net or "").strip()
                if not str(pin).strip() or not net:
                    cls._issue(
                        issues,
                        "empty_pin_or_net",
                        "error",
                        f"netlist.{key}.pins",
                        "引脚标识和网络名不能为空。",
                    )
                    continue
                if net.upper() == "NC" and not core_device_type(device_type):
                    cls._issue(
                        issues,
                        "non_ic_nc",
                        "error",
                        f"netlist.{key}.pins.{pin}",
                        "只有 IC 未用引脚可以连接 NC。",
                    )
                if net.upper() != "NC":
                    net_counts[net] += 1

        for designator, count in Counter(designators).items():
            if count > 1:
                cls._issue(
                    issues,
                    "duplicate_designator",
                    "error",
                    "netlist",
                    f"位号 {designator} 重复 {count} 次。",
                )

        cross = cross_page(info.get("crossPageNet"))
        boundary = set(cross["inputs"]) | set(cross["outputs"])
        for net, count in sorted(net_counts.items()):
            if count == 1 and net not in boundary:
                cls._issue(issues, "unconnected_internal_net", "error", "netlist",
                           f"内部网络 {net!r} 只有一个器件引脚；应连接真实端点、把直连脚放在同一网络，或将未用 IC 脚明确设为 NC。")
        overlap = set(cross["inputs"]) & set(cross["outputs"])
        for net in sorted(overlap):
            cls._issue(
                issues,
                "cross_page_direction_conflict",
                "warning",
                "crossPageNet",
                f"网络 {net} 同时声明为输入和输出。",
            )
        for direction in ("inputs", "outputs"):
            for net in cross[direction]:
                if net not in net_counts:
                    cls._issue(
                        issues,
                        "unused_cross_page_net",
                        "error",
                        f"crossPageNet.{direction}",
                        f"跨页网络 {net} 没有出现在任何器件引脚。",
                    )

        errors = sum(issue["severity"] == "error" for issue in issues)
        warnings = sum(issue["severity"] == "warning" for issue in issues)
        return {
            "ok": errors == 0,
            "issues": issues,
            "summary": {
                "devices": len(netlist),
                "nets": len(net_counts),
                "errors": errors,
                "warnings": warnings,
            },
        }

    @staticmethod
    def _issue(
        issues: list[dict[str, str]],
        code: str,
        severity: str,
        path: str,
        message: str,
    ) -> None:
        issues.append(
            {
                "code": code,
                "severity": severity,
                "path": path,
                "message": message,
            }
        )


__all__ = ["NetlistLinterAgent"]
