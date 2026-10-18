"""Default standalone services and the application service-factory hook."""

from __future__ import annotations

from threading import RLock
from .contracts import AgentServices, AgentServicesFactory, AgentSettings, LoggerProtocol
from .logging import StructuredLogger
from .providers import AsyncOpenAICompatibleClient, OpenAICompatibleClient


def build_default_agent_services(
    settings: AgentSettings | None = None,
    *,
    logger: LoggerProtocol | None = None,
    business_logger: LoggerProtocol | None = None,
    llm_logger: LoggerProtocol | None = None,
) -> AgentServices:
    """Create services suitable for a standalone OpenAI-compatible application."""

    resolved_settings = settings or AgentSettings.from_env()
    resolved_logger = logger or StructuredLogger("agent_framework.agent")
    resolved_business = business_logger or StructuredLogger("agent_framework.business")
    resolved_llm = llm_logger or StructuredLogger("agent_framework.llm")
    return AgentServices(
        settings=resolved_settings,
        sync_llm_factory=lambda: OpenAICompatibleClient(
            resolved_settings, resolved_llm
        ),
        async_llm_factory=lambda: AsyncOpenAICompatibleClient(
            resolved_settings, resolved_llm
        ),
        logger=resolved_logger,
        business_logger=resolved_business,
        llm_logger=resolved_llm,
    )


_factory_lock = RLock()
_default_factory: AgentServicesFactory = build_default_agent_services


def configure_default_agent_services(factory: AgentServicesFactory) -> None:
    """Install an application adapter without making the framework import it."""

    if not callable(factory):
        raise TypeError("factory must be callable")
    global _default_factory
    with _factory_lock:
        _default_factory = factory


def get_default_agent_services() -> AgentServices:
    """Resolve fresh Agent services from the currently installed factory."""

    with _factory_lock:
        factory = _default_factory
    return factory()


__all__ = [
    "build_default_agent_services",
    "configure_default_agent_services",
    "get_default_agent_services",
]
