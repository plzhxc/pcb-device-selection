"""Agent for deterministically aggregating top-port results."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from agent_framework.agent import AgentResult, AgentStatus, BaseAgent, Context

from ...netlist.helpers import as_mapping
from ...roles import AgentRole
from ..helpers import dedupe_dicts, dedupe_by_identity


class TopPortAggregatorAgent(BaseAgent):
    """Aggregate hierarchical or sharded results into one stable contract."""

    role = AgentRole.TOP_PORT_AGGREGATOR
    task_type = "pcb_top_port_deterministic_aggregator"
    purpose = "确定性合并层级或分片端口结果，去重并保留边界分类。"
    step_kind = "python"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        """Local rule: no LLM context is constructed."""

    def run(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                raise TypeError("async execute() requires arun()")
            if not isinstance(values, dict):
                raise TypeError("TopPortAggregatorAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    async def arun(self, **kwargs: Any) -> AgentResult:
        try:
            values = self.execute(**kwargs)
            if inspect.isawaitable(values):
                values = await values
            if not isinstance(values, dict):
                raise TypeError("TopPortAggregatorAgent.execute() must return dict")
        except Exception as error:
            return AgentResult(status=AgentStatus.ERROR, error=error)
        return AgentResult(status=AgentStatus.SUCCESS, values=values)

    @staticmethod
    def execute(**kwargs: Any) -> dict[str, Any]:
        raw_results = kwargs.get("strategy_results") or []
        if not isinstance(raw_results, Sequence) or isinstance(
            raw_results, (str, bytes)
        ):
            raw_results = []
        connections: list[dict[str, Any]] = []
        system_inputs: list[dict[str, Any]] = []
        system_outputs: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []

        for wrapper in raw_results:
            if not isinstance(wrapper, Mapping):
                continue
            module_id = str(wrapper.get("module_id") or "")
            payload = as_mapping(wrapper.get("result"))
            direct = payload.get("internal_connection") or []
            if isinstance(direct, Sequence) and not isinstance(
                direct, (str, bytes)
            ):
                connections.extend(
                    dict(row) for row in direct if isinstance(row, Mapping)
                )

            candidates = payload.get("internal_connection_candidates") or []
            if isinstance(candidates, Sequence) and not isinstance(
                candidates, (str, bytes)
            ):
                for raw in candidates:
                    if not isinstance(raw, Mapping):
                        continue
                    candidate = dict(raw)
                    role = str(candidate.get("role") or "start")
                    peer = str(candidate.get("peer_module") or "")
                    local = str(candidate.get("local") or "")
                    peer_net = str(candidate.get("peer_net") or "")
                    if role == "end":
                        source_module, source_net = peer, peer_net
                        target_module, target_net = module_id, local
                    else:
                        source_module, source_net = module_id, local
                        target_module, target_net = peer, peer_net
                    connections.append(
                        {
                            "source_module": source_module,
                            "source_net": source_net,
                            "target_module": target_module,
                            "target_net": target_net,
                            "type": candidate.get("type") or "other",
                            "reason": candidate.get("reason") or "",
                        }
                    )

            for key, target in (
                ("system_input", system_inputs),
                ("system_inputs", system_inputs),
                ("system_output", system_outputs),
                ("system_outputs", system_outputs),
                ("unmatched", unmatched),
            ):
                rows = payload.get(key) or []
                if isinstance(rows, Sequence) and not isinstance(
                    rows, (str, bytes)
                ):
                    target.extend(
                        dict(row) if isinstance(row, Mapping) else {"net": row}
                        for row in rows
                    )

            exposed = payload.get("exposed_ports") or []
            if isinstance(exposed, Sequence) and not isinstance(
                exposed, (str, bytes)
            ):
                for raw in exposed:
                    if not isinstance(raw, Mapping):
                        continue
                    port = {
                        "module_id": module_id,
                        "net": raw.get("local"),
                        "direction": raw.get("direction"),
                        "reason": raw.get("reason") or "",
                    }
                    guess = raw.get("guess")
                    if guess == "system_input":
                        system_inputs.append(port)
                    elif guess == "system_output":
                        system_outputs.append(port)
                    else:
                        unmatched.append(port)

        boundary = as_mapping(kwargs.get("boundary"))
        for key, target in (
            ("system_inputs", system_inputs),
            ("system_outputs", system_outputs),
            ("unmatched", unmatched),
        ):
            rows = boundary.get(key) or []
            if isinstance(rows, Sequence) and not isinstance(
                rows, (str, bytes)
            ):
                target.extend(
                    dict(row) for row in rows if isinstance(row, Mapping)
                )

        return {
            "top_port_result": {
                "internal_connection": dedupe_by_identity(connections, ("source_module", "source_net", "target_module", "target_net", "type")),
                "system_input": dedupe_by_identity(system_inputs, ("module_id", "net", "direction")),
                "system_output": dedupe_by_identity(system_outputs, ("module_id", "net", "direction")),
                "unmatched": dedupe_dicts(unmatched),
            }
        }


__all__ = ["TopPortAggregatorAgent"]
