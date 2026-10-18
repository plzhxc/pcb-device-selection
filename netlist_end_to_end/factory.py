"""Agent role table and the single runtime that instantiates PCBAgent agents."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from agent_framework.agent import AgentConfig, AgentObserver, BaseAgent
from agent_framework.dag import AgentRuntime
from agent_framework.llm import AsyncLLMClient
from agent_framework.native_tools import create_native_toolbox
from agent_framework.tool import ToolBox

from .netlist import NETLIST_AGENT_TYPES
from .roles import AgentRole
from .top_port import TOP_PORT_AGENT_TYPES


DEFAULT_AGENT_TYPES: dict[AgentRole, type[BaseAgent]] = {
    **NETLIST_AGENT_TYPES,
    **TOP_PORT_AGENT_TYPES,
}

if set(DEFAULT_AGENT_TYPES) != set(AgentRole):
    missing = sorted(role.value for role in set(AgentRole) - set(DEFAULT_AGENT_TYPES))
    extra = sorted(role.value for role in set(DEFAULT_AGENT_TYPES) - set(AgentRole))
    raise RuntimeError(
        f"default Agent mapping is incomplete; missing={missing}, extra={extra}"
    )

for _role, _agent_type in DEFAULT_AGENT_TYPES.items():
    if not issubclass(_agent_type, BaseAgent):
        raise RuntimeError(f"{_role.value} must be implemented by a BaseAgent subclass")


@dataclass(frozen=True)
class AgentDescriptor:
    role: str
    class_name: str
    execution: str
    purpose: str

    def as_dict(self) -> dict[str, str]:
        return {
            "role": self.role,
            "class_name": self.class_name,
            "execution": self.execution,
            "purpose": self.purpose,
        }


def agent_manifest() -> tuple[AgentDescriptor, ...]:
    """Describe the complete default role wiring without constructing clients."""

    rows: list[AgentDescriptor] = []
    for role in AgentRole:
        agent_type = DEFAULT_AGENT_TYPES[role]
        local = getattr(agent_type, "step_kind", "agent") == "python"
        rows.append(
            AgentDescriptor(
                role=role.value,
                class_name=agent_type.__name__,
                execution="local" if local else "llm",
                purpose=str(getattr(agent_type, "purpose", "") or ""),
            )
        )
    return tuple(rows)


def _normalize_mapping(
    values: Mapping[Any, Any] | None,
) -> dict[AgentRole, Any]:
    return {AgentRole.parse(role): value for role, value in (values or {}).items()}


def _without_tool(tools: ToolBox, name: str) -> ToolBox:
    """Return an equivalent toolbox without one forbidden capability."""

    if name not in tools.names:
        return tools
    remaining = [
        tool
        for registered_name in tools.names
        if registered_name != name
        if (tool := tools.get(registered_name)) is not None
    ]
    # ToolBox intentionally has no public remove API.  Preserve whether the
    # source policy was explicit so a filtered box keeps the same future merge
    # semantics as native, knowledge, and role-specific custom boxes.
    explicit_policy = bool(getattr(tools, "_policy_explicit", False))
    return ToolBox(
        remaining,
        policy=tools.policy if explicit_policy else None,
    )


def build_agent_runtime(
    client: AsyncLLMClient,
    config: AgentConfig,
    *,
    configs: Mapping[Any, AgentConfig] | None = None,
    toolboxes: Mapping[Any, ToolBox] | None = None,
    native_toolbox_factory: Callable[[], ToolBox] = create_native_toolbox,
    observer: AgentObserver | None = None,
) -> AgentRuntime:
    """The one place that knows how a PCBAgent role is instantiated.

    ``native_toolbox_factory`` is called once per instantiation, so Agent boxes
    never share mutable registry state.  A role-specific box is merged last and
    may explicitly replace a same-named tool.  SQL/Milvus netlist evidence is a
    Stage dependency, not an Agent toolbox grant.
    """

    role_configs = _normalize_mapping(configs)
    role_tools = _normalize_mapping(toolboxes)
    role_of = {agent_type: role for role, agent_type in DEFAULT_AGENT_TYPES.items()}

    def config_for(agent_type: type[BaseAgent], default: AgentConfig) -> AgentConfig:
        role = role_of.get(agent_type) or getattr(agent_type, "role", None)
        agent_config = role_configs.get(role, default) if role is not None else default
        return agent_config

    def toolbox_for(agent_type: type[BaseAgent]) -> ToolBox:
        role = role_of.get(agent_type) or getattr(agent_type, "role", None)
        tools = native_toolbox_factory()
        if not isinstance(tools, ToolBox):
            raise TypeError("native_toolbox_factory must return ToolBox")
        custom = role_tools.get(role) if role is not None else None
        if custom is not None:
            tools.extend(custom, replace=True)
        # Public web search is neither deterministic nor required by any flow
        # contract, and failed searches can consume an entire tool loop.
        return _without_tool(tools, "web_search")

    return AgentRuntime(
        client,
        config,
        observer=observer,
        config_for=config_for,
        toolbox_for=toolbox_for,
    )


__all__ = [
    "DEFAULT_AGENT_TYPES",
    "AgentDescriptor",
    "agent_manifest",
    "build_agent_runtime",
]
