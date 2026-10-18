"""Public contracts for the framework-native PCBAgent application.

This package intentionally owns only workflow-shaped data.  Circuit-domain
models, persistence records, and validation reports will be introduced behind
explicit ports later; importing the legacy application to obtain those types
would couple the new flow to the implementation it is meant to replace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from agent_framework.workflow import WorkflowResult


class StageName(str, Enum):
    """The two public PCBAgent stages in execution order."""

    NETLIST = "netlist"
    TOP_PORT = "top_port"

    @classmethod
    def parse(
        cls,
        value: "StageName | str",
    ) -> "StageName":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError as exc:
            choices = ", ".join(item.value for item in cls)
            raise ValueError(
                f"unknown workflow stage {value!r}; choose from: {choices}"
            ) from exc


STAGE_ORDER: tuple[StageName, ...] = tuple(StageName)


class FlowContractError(ValueError):
    """A stage input or output does not satisfy the public hand-off contract."""


def _positive(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _non_negative(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class FlowOptions:
    """Control-flow knobs independent from concrete Agent implementations."""

    netlist_concurrency: int = 8
    netlist_repair_rounds: int = 1
    top_port_concurrency: int = 8
    top_port_strategy: str = "hierarchical"
    fail_on_lint: bool = False

    def __post_init__(self) -> None:
        _positive("netlist_concurrency", self.netlist_concurrency)
        _non_negative("netlist_repair_rounds", self.netlist_repair_rounds)
        _positive("top_port_concurrency", self.top_port_concurrency)

        strategy = str(self.top_port_strategy).strip().lower()
        if strategy not in {"hierarchical", "sharded"}:
            raise ValueError(
                "top_port_strategy must be 'hierarchical' or 'sharded'"
            )
        object.__setattr__(self, "top_port_strategy", strategy)


def unwrap_stage_output(value: Any) -> Any:
    """Unwrap the uniform ``{"result": ...}`` stage boundary."""

    if isinstance(value, Mapping) and set(value) == {"result"}:
        return value["result"]
    return value


def wrap_stage_output(value: Any) -> dict[str, Any]:
    """Normalize a caller-supplied resume value for ``Workflow.upstream``."""

    if isinstance(value, Mapping) and set(value) == {"result"}:
        return dict(value)
    return {"result": value}


@dataclass(frozen=True)
class PCBFlowResult:
    """Application view over :class:`agent_framework.workflow.WorkflowResult`."""

    framework: WorkflowResult
    netlist_output: tuple[dict[str, Any], ...] = ()
    top_port_result: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_framework(cls, result: WorkflowResult) -> "PCBFlowResult":
        def output(name: StageName, default: Any) -> Any:
            raw = result.outputs.get(name.value, default)
            return unwrap_stage_output(raw)

        netlist = output(StageName.NETLIST, ())
        top_port = output(StageName.TOP_PORT, {})
        return cls(
            framework=result,
            netlist_output=tuple(
                dict(item)
                for item in (netlist if isinstance(netlist, Sequence) else ())
                if isinstance(item, Mapping)
            ),
            top_port_result=(
                dict(top_port) if isinstance(top_port, Mapping) else {}
            ),
        )

    @property
    def ok(self) -> bool:
        return self.framework.ok

    def as_dict(self) -> dict[str, Any]:
        executed = [stage.name for stage in self.framework.stages]
        data: dict[str, Any] = {
            "flow_request_id": self.framework.workflow_id,
            "status": self.framework.status.value,
            "netlist_output": [dict(item) for item in self.netlist_output],
            "top_port_result": dict(self.top_port_result),
            "executed_stages": executed,
            "failed_stage": self.framework.failed_stage,
        }
        if self.framework.error is not None:
            data["error"] = (
                f"{type(self.framework.error).__name__}: "
                f"{self.framework.error}"
            )
        return data


__all__ = [
    "FlowContractError",
    "FlowOptions",
    "StageName",
    "PCBFlowResult",
    "STAGE_ORDER",
    "StageName",
    "unwrap_stage_output",
    "wrap_stage_output",
]
