"""Reusable Agent framework independent from every business application.

Applications inherit :class:`BaseAgent`, implement ``format`` and inject their
own provider/configuration/logging adapter through :class:`AgentServices`.
"""

from .base_agent import BaseAgent
from .contracts import (
    AgentServices,
    AgentServicesFactory,
    AgentSettings,
    AsyncLLMClientProtocol,
    LoggerProtocol,
    SyncLLMClientProtocol,
)
from .hierarchical_agent import HierarchicalAgent
from .messages import ContentPart, Message, MessageContent
from .runtime.context import (
    ContextDataContainer,
    ProblemContext,
    SourceType,
    StructuredOutput,
    SystemPrompt,
    UserPrompt,
)
from .services import (
    build_default_agent_services,
    configure_default_agent_services,
    get_default_agent_services,
)
from .tools import BaseTool, ToolParameter

__all__ = [
    "AgentServices",
    "AgentServicesFactory",
    "AgentSettings",
    "AsyncLLMClientProtocol",
    "BaseAgent",
    "BaseTool",
    "ContentPart",
    "ContextDataContainer",
    "HierarchicalAgent",
    "LoggerProtocol",
    "Message",
    "MessageContent",
    "ProblemContext",
    "SourceType",
    "StructuredOutput",
    "SyncLLMClientProtocol",
    "SystemPrompt",
    "ToolParameter",
    "UserPrompt",
    "build_default_agent_services",
    "configure_default_agent_services",
    "get_default_agent_services",
]
