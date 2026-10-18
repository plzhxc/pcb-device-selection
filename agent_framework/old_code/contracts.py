"""Stable dependency contracts for the shared Agent framework.

The framework owns orchestration, context, retry, tools and structured output.
Applications own provider credentials, configuration policy and logging sinks and
pass those dependencies through :class:`AgentServices`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentSettings:
    """Immutable settings consumed by ``BaseAgent`` and provider adapters."""

    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    default_model: str = "qwen-plus"
    default_reasoning: bool = False
    llm_call_params: Mapping[str, Any] = field(default_factory=dict)
    task_model_mapping: Mapping[str, str] = field(default_factory=dict)
    task_llm_params: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    reasoning_mode_config: Mapping[str, bool] = field(default_factory=dict)
    llm_request_timeout_s: float | None = 180.0
    task_timeout_mapping: Mapping[str, float] = field(default_factory=dict)
    rate_limit_backoff_base_s: float = 8.0
    rate_limit_backoff_max_s: float = 90.0
    rate_limit_backoff_jitter_s: float = 2.0
    business_logging_enabled: bool = True
    max_parallel_sub_agents: int = 8
    max_concurrent_requests: int = 12
    min_request_interval_s: float = 0.2

    @classmethod
    def from_env(cls, **overrides: Any) -> "AgentSettings":
        """Build standalone settings without importing an application package."""

        values: dict[str, Any] = {
            "api_key": (
                os.environ.get("QWEN_API_KEY")
                or os.environ.get("DASHSCOPE_API_KEY")
                or ""
            ).strip(),
            "base_url": os.environ.get(
                "QWEN_BASE_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ).strip(),
            "default_model": os.environ.get("AGENT_DEFAULT_MODEL", "qwen-plus").strip(),
        }
        values.update(overrides)
        return cls(**values)


@runtime_checkable
class LoggerProtocol(Protocol):
    """Small structured-logging surface required by the runtime."""

    def debug(self, msg: Any, *, immediate: bool = True) -> None: ...
    def info(self, msg: Any, *, immediate: bool = True) -> None: ...
    def warning(self, msg: Any, *, immediate: bool = True) -> None: ...
    def error(self, msg: Any, *, immediate: bool = True) -> None: ...


@runtime_checkable
class SyncLLMClientProtocol(Protocol):
    """Provider adapter used by synchronous Agent runs."""

    def call_llm(self, **kwargs: Any) -> tuple[Any, Any, Any]: ...


@runtime_checkable
class AsyncLLMClientProtocol(Protocol):
    """Provider adapter used by asynchronous Agent runs."""

    async def call_async_llm(self, **kwargs: Any) -> tuple[Any, Any, Any]: ...
    async def close(self) -> None: ...


@dataclass(frozen=True)
class AgentServices:
    """All application-owned dependencies needed to construct one Agent."""

    settings: AgentSettings
    sync_llm_factory: Callable[[], SyncLLMClientProtocol]
    async_llm_factory: Callable[[], AsyncLLMClientProtocol]
    logger: LoggerProtocol
    business_logger: LoggerProtocol
    llm_logger: LoggerProtocol


AgentServicesFactory = Callable[[], AgentServices]


__all__ = [
    "AgentServices",
    "AgentServicesFactory",
    "AgentSettings",
    "AsyncLLMClientProtocol",
    "LoggerProtocol",
    "SyncLLMClientProtocol",
]
