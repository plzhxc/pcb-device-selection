"""运行时：把 DAG 里引用的 Agent **类**变成可运行的实例。

Agent 类是自描述的（提示词、schema、工具策略都在类上），实例化只差共享的
基础设施——LLM client、默认配置、观测器。运行时负责这一步，且**每次运行新建
实例**：BaseAgent 的运行态挂在实例上，跨协程共享会互相污染。

应用层通过三个钩子注入按角色的差异（某类 Agent 用不同配置、挂不同工具、或整体
禁用），框架本身不认识任何业务角色。
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from ..agent.base import BaseAgent
from ..agent.config import AgentConfig
from ..agent.observer import AgentObserver
from ..tool import ToolBox


class NodeRuntime(Protocol):
    """DAG 引擎实例化 Agent 节点时依赖的最小接口。"""

    def agent(self, agent_type: type[BaseAgent]) -> BaseAgent | None:
        """返回一个新实例；返回 ``None`` 表示该类在本运行时被禁用（节点跳过）。"""


class AgentRuntime:
    """默认运行时：共享 client / config / observer，按类现场实例化。

    Args:
        client: 所有 Agent 共用的 LLM 客户端。
        config: 默认 :class:`AgentConfig`。
        observer: Agent 层观测器；``None`` 走进程级默认。
        config_for: ``(agent_type, default_config) -> config``，按类覆盖配置。
        toolbox_for: ``agent_type -> ToolBox | None``；返回 ``None`` 时由
            BaseAgent 自建原生工具箱。
        enabled: ``agent_type -> bool``，为假的类视为禁用。
    """

    def __init__(
        self,
        client: Any,
        config: AgentConfig,
        *,
        observer: AgentObserver | None = None,
        config_for: Callable[[type[BaseAgent], AgentConfig], AgentConfig] | None = None,
        toolbox_for: Callable[[type[BaseAgent]], ToolBox | None] | None = None,
        enabled: Callable[[type[BaseAgent]], bool] | None = None,
    ) -> None:
        self.client = client
        self.config = config
        self.observer = observer
        self._config_for = config_for
        self._toolbox_for = toolbox_for
        self._enabled = enabled

    def agent(self, agent_type: type[BaseAgent]) -> BaseAgent | None:
        if not (isinstance(agent_type, type) and issubclass(agent_type, BaseAgent)):
            raise TypeError(f"{agent_type!r} is not a BaseAgent subclass")
        if self._enabled is not None and not self._enabled(agent_type):
            return None
        config = self._config_for(agent_type, self.config) if self._config_for else self.config
        tools = self._toolbox_for(agent_type) if self._toolbox_for else None
        if tools is not None and not isinstance(tools, ToolBox):
            raise TypeError("toolbox_for must return a ToolBox or None")
        return agent_type(
            self.client,
            config,
            tools=tools,
            observer=self.observer,
            # 给了工具箱就视为已装配完毕，避免 BaseAgent 再建一遍原生工具
            enable_native_tools=tools is None,
        )


__all__ = ["AgentRuntime", "NodeRuntime"]
