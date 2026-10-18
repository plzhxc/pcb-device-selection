"""Agent for deterministically linting reconciled top ports."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import AgentResult, AgentStatus, BaseAgent, Context

from ...netlist.helpers import as_mapping, cross_page, netlist_info
from ...roles import AgentRole


class TopPortLinterAgent(BaseAgent):
    """Validate that reconciled endpoints exist and are not contradictory."""

    role = AgentRole.TOP_PORT_LINTER
    task_type = "pcb_top_port_basic_linter"
    purpose = "确定性验证顶层连接端点存在、方向分类不冲突且未被重复消费。"
    step_kind = "python"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        """Local rule: no LLM context is constructed."""

    def run(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                raise TypeError("async execute() requires arun()")
            if not isinstance(values, dict):
                raise TypeError("TopPortLinterAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    async def arun(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                values = await values
            if not isinstance(values, dict):
                raise TypeError("TopPortLinterAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        result = as_mapping(kwargs.get("top_port_result"))
        entries = self._entries(kwargs.get("netlist_output"))
        issues: list[dict[str, str]] = []
        covered: set[tuple[str, str]] = set()
        boundary: dict[tuple[str, str], tuple[str, str]] = {}
        seen_edges: set[tuple] = set()

        def issue(code, path, message):
            self._issue(issues, code, "error", path, message)

        def endpoint(module, net, path):
            if not isinstance(module, str) or not isinstance(net, str) or not module or not net:
                issue("invalid_endpoint", path, "端点必须提供非空 module ID 和 net。")
                return None
            key = (module, net)
            if module not in entries or net not in entries[module]:
                issue("unknown_endpoint", path, f"端点 {module}:{net} 不在真实跨页端口中。")
                return None
            return key

        connections = result.get("internal_connection", [])
        if not isinstance(connections, list):
            issue("invalid_connections", "internal_connection", "连接必须是数组。")
            connections = []
        for index, row in enumerate(connections):
            path = f"internal_connection.{index}"
            if not isinstance(row, Mapping):
                issue("invalid_connection", path, "连接项必须是对象。")
                continue
            source = endpoint(row.get("source_module"), row.get("source_net"), path + ".source")
            target = endpoint(row.get("target_module"), row.get("target_net"), path + ".target")
            if source is None or target is None:
                continue
            if source[0] == target[0]:
                issue("same_module_connection", path, "顶层只能连接不同模块。")
            identity = tuple(sorted((source, target)))
            if identity in seen_edges:
                issue("duplicate_connection", path, "同一无向端点连接被重复声明。")
            seen_edges.add(identity)
            covered.update((source, target))

        for key in ("system_input", "system_output"):
            rows = result.get(key, [])
            if not isinstance(rows, list):
                issue("invalid_system_ports", key, "系统边界端口必须是数组。")
                continue
            expected = "input" if key == "system_input" else "output"
            for index, row in enumerate(rows):
                path = f"{key}.{index}"
                if not isinstance(row, Mapping):
                    issue("invalid_system_port", path, "系统边界端口必须是对象。")
                    continue
                port = endpoint(row.get("module_id"), row.get("net"), path)
                direction = row.get("direction")
                if direction not in (expected, "inout"):
                    issue("boundary_direction_conflict", path, f"{key} 中 direction 必须为 {expected} 或 inout。")
                if port is None:
                    continue
                classification = (key, str(direction))
                if port in boundary and boundary[port] != classification:
                    issue("conflicting_boundary_classification", path, f"边界端点 {port[0]}:{port[1]} 同时具有矛盾的输入/输出分类。")
                boundary[port] = classification
                # A connector supply may be both an internal net and external
                # reference; sharing an endpoint is not itself a contradiction.
                covered.add(port)

        unmatched = result.get("unmatched", [])
        if not isinstance(unmatched, list):
            issue("invalid_unmatched", "unmatched", "unmatched 必须是数组。")
        else:
            for index, row in enumerate(unmatched):
                reason = str(row.get("reason") or row) if isinstance(row, Mapping) else str(row)
                issue("unresolved_endpoint", f"unmatched.{index}", "端口撮合尚未解决：" + reason)
        required = {(module, net) for module, nets in entries.items() for net in nets}
        for module, net in sorted(required - covered):
            issue("unclassified_endpoint", "top_port_result", f"跨页端点 {module}:{net} 没有显式连接或系统边界分类。")

        errors = sum(item["severity"] == "error" for item in issues)
        return {"ok": errors == 0, "issues": issues,
                "summary": {"modules": len(entries), "connections": len(connections),
                            "declared_endpoints": len(required), "covered_endpoints": len(required & covered),
                            "errors": errors}}

    @staticmethod
    def _entries(value: Any) -> dict[str, set[str]]:
        rows = value or []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            return {}
        entries: dict[str, set[str]] = {}
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            module = as_mapping(row.get("module_info") or row.get("module"))
            module_id = str(
                module.get("id")
                or module.get("name")
                or f"module_{index + 1}"
            )
            info = netlist_info(row.get("netlist_info"))
            cross = cross_page(info.get("crossPageNet"))
            entries[module_id] = set(cross["inputs"]) | set(cross["outputs"])
        return entries

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


__all__ = ["TopPortLinterAgent"]
