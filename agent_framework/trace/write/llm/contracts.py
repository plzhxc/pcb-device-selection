"""Public value objects for writing LLM traces.

The write side intentionally uses only the Python standard library.  Agent
Framework events are adapted in :mod:`agent_framework.trace.llm.observer`; users
that instrument another runtime can write these objects directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Literal, Mapping
from uuid import uuid4


TokenSource = Literal["provider", "estimated", "unavailable"]
RecordLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class RunEventType(str, Enum):
    """Lifecycle events persisted in ``llm_run_records``."""

    CALL_STARTED = "call_started"
    ATTEMPT_STARTED = "attempt_started"
    ATTEMPT_SUCCEEDED = "attempt_succeeded"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPT_CANCELLED = "attempt_cancelled"
    RETRY_SCHEDULED = "retry_scheduled"
    CALL_COMPLETED = "call_completed"
    CALL_FAILED = "call_failed"
    CALL_CANCELLED = "call_cancelled"


ATTEMPT_EVENT_TYPES = frozenset(
    {
        RunEventType.ATTEMPT_STARTED,
        RunEventType.ATTEMPT_SUCCEEDED,
        RunEventType.ATTEMPT_FAILED,
        RunEventType.ATTEMPT_CANCELLED,
        RunEventType.RETRY_SCHEDULED,
    }
)
USAGE_EVENT_TYPES = frozenset(
    {
        RunEventType.ATTEMPT_SUCCEEDED,
        RunEventType.ATTEMPT_FAILED,
        RunEventType.ATTEMPT_CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class TraceContext:
    """Business context attached to one logical LLM call.

    ``trace_id`` may be omitted for standalone use; the writer then uses the
    generated ``llm_call_id`` as the trace id.  Applications should normally
    provide it so calls from the same workflow can be queried together.
    """

    trace_id: str | None = None
    flow_id: str | None = None
    stage_id: str | None = None
    agent_id: str | None = None


ContextSource = TraceContext | Callable[[], TraceContext | None] | None


def _token_count(name: str, value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(f"{name} must be a non-negative integer or None")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token usage for one physical attempt.

    Unknown usage is represented by :meth:`unavailable`, never by zero.
    ``cached_input_tokens`` is a subset of input tokens and
    ``reasoning_tokens`` is a subset of output tokens.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    source: TokenSource = "unavailable"
    complete: bool = False
    tokenizer: str | None = None
    provider_details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
        ):
            _token_count(name, getattr(self, name))

        if self.source not in {"provider", "estimated", "unavailable"}:
            raise ValueError(
                "source must be 'provider', 'estimated', or 'unavailable'"
            )
        if self.source == "estimated" and not self.tokenizer:
            raise ValueError("estimated usage must name its tokenizer")

        counts = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cached_input_tokens,
            self.reasoning_tokens,
        )
        if self.source == "unavailable":
            if any(value is not None for value in counts):
                raise ValueError(
                    "unavailable usage cannot contain token counts"
                )
            if self.complete:
                raise ValueError("unavailable usage must set complete=False")
            return

        if self.complete and (
            self.input_tokens is None or self.output_tokens is None
        ):
            raise ValueError(
                "complete usage requires input_tokens and output_tokens"
            )
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError(
                "input_tokens and output_tokens must be reported together"
            )

        if self.input_tokens is not None and self.output_tokens is not None:
            calculated = self.input_tokens + self.output_tokens
            if self.total_tokens is None:
                object.__setattr__(self, "total_tokens", calculated)
            elif self.total_tokens != calculated:
                raise ValueError(
                    "total_tokens must equal input_tokens + output_tokens"
                )

        if self.cached_input_tokens is not None:
            if self.input_tokens is None:
                raise ValueError(
                    "cached_input_tokens requires input_tokens"
                )
            if self.cached_input_tokens > self.input_tokens:
                raise ValueError(
                    "cached_input_tokens cannot exceed input_tokens"
                )
        if self.reasoning_tokens is not None:
            if self.output_tokens is None:
                raise ValueError("reasoning_tokens requires output_tokens")
            if self.reasoning_tokens > self.output_tokens:
                raise ValueError(
                    "reasoning_tokens cannot exceed output_tokens"
                )

    @classmethod
    def unavailable(
        cls,
        *,
        provider_details: Mapping[str, Any] | None = None,
    ) -> "TokenUsage":
        return cls(
            source="unavailable",
            complete=False,
            provider_details=provider_details or {},
        )

    def to_payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source": self.source,
            "complete": self.complete,
        }
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "tokenizer",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.provider_details:
            result["provider_details"] = dict(self.provider_details)
        return result


def _default_level(event_type: RunEventType) -> RecordLevel:
    if event_type in {
        RunEventType.ATTEMPT_FAILED,
        RunEventType.CALL_FAILED,
    }:
        return "ERROR"
    if event_type in {
        RunEventType.ATTEMPT_CANCELLED,
        RunEventType.CALL_CANCELLED,
    }:
        return "WARNING"
    return "INFO"


@dataclass(frozen=True, slots=True)
class LLMRunRecord:
    """One append-only lifecycle record."""

    event_type: RunEventType
    event_id: str = field(default_factory=lambda: uuid4().hex)
    attempt_no: int | None = None
    level: RecordLevel | None = None
    duration_ms: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    recorded_at: datetime | None = None

    def __post_init__(self) -> None:
        event_type = self.event_type
        if not isinstance(event_type, RunEventType):
            event_type = RunEventType(event_type)
            object.__setattr__(self, "event_type", event_type)

        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")
        if len(self.event_id.strip()) > 64:
            raise ValueError("event_id must be at most 64 characters")
        object.__setattr__(self, "event_id", self.event_id.strip())

        if event_type in ATTEMPT_EVENT_TYPES and self.attempt_no is None:
            raise ValueError(f"{event_type.value} requires attempt_no")
        if self.attempt_no is not None and (
            isinstance(self.attempt_no, bool)
            or not isinstance(self.attempt_no, int)
            or self.attempt_no < 1
        ):
            raise ValueError("attempt_no must be a positive integer or None")
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ValueError(
                "duration_ms must be a non-negative integer or None"
            )

        level = self.level or _default_level(event_type)
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError(
                "level must be DEBUG, INFO, WARNING, or ERROR"
            )
        object.__setattr__(self, "level", level)

        payload = dict(self.payload)
        usage = payload.get("usage")
        if usage is not None:
            if event_type not in USAGE_EVENT_TYPES:
                raise ValueError(
                    "payload usage is only valid on an attempt end event"
                )
            if isinstance(usage, TokenUsage):
                normalized_usage = usage
            elif isinstance(usage, Mapping):
                normalized_usage = TokenUsage(**dict(usage))
            else:
                raise ValueError(
                    "payload usage must be TokenUsage or a mapping"
                )
            payload["usage"] = normalized_usage.to_payload()
        object.__setattr__(self, "payload", payload)


__all__ = [
    "ATTEMPT_EVENT_TYPES",
    "ContextSource",
    "LLMRunRecord",
    "RecordLevel",
    "RunEventType",
    "TokenSource",
    "TokenUsage",
    "TraceContext",
    "USAGE_EVENT_TYPES",
]
