"""Adapter from Agent Framework lifecycle callbacks to Trace records.

This module depends only on the *shape* of the three observer events.  It does
not import Agent Framework at runtime, so :class:`LLMTraceWriter` remains an
independent library.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Sequence
from uuid import UUID, uuid5

from .contracts import (
    ContextSource,
    LLMRunRecord,
    RunEventType,
    TokenUsage,
    TraceContext,
)
from .writer import LLMTraceWriter


if TYPE_CHECKING:
    from agent_framework.llm.observer import (
        CallEnded,
        CallFailed,
        CallCancelled,
        CallStarted,
    )


_EVENT_NAMESPACE = UUID("9ae82442-11f7-4ccf-a94b-962e0c0da6ca")


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [_jsonable(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _jsonable(to_dict())
        except Exception:
            pass
    return str(value)


def _message_snapshot(message: Any) -> dict[str, Any]:
    serialized = _jsonable(message)
    details = serialized if isinstance(serialized, dict) else {
        "value": serialized
    }
    message_type = type(message).__name__
    role = {
        "SystemMessage": "system",
        "UserMessage": "user",
        "AssistantMessage": "assistant",
        "ToolMessage": "tool",
    }.get(message_type)
    return {
        "message_type": message_type,
        **({"role": role} if role else {}),
        **details,
    }


def _tool_snapshot(tool: Any) -> dict[str, Any]:
    serialized = _jsonable(tool)
    details = serialized if isinstance(serialized, dict) else {
        "value": serialized
    }
    return {"tool_type": type(tool).__name__, **details}


def _response_snapshot(response: Any) -> dict[str, Any]:
    return {
        "content": _jsonable(getattr(response, "content", "")),
        "reasoning": _jsonable(getattr(response, "reasoning", "")),
        "tool_calls": _jsonable(getattr(response, "tool_calls", ())),
        "finish_reason": _jsonable(
            getattr(response, "finish_reason", None)
        ),
        "model": _jsonable(getattr(response, "model", "")),
        "provider": _jsonable(getattr(response, "provider", "")),
    }


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _provider_usage(response: Any) -> TokenUsage:
    raw = getattr(response, "usage", None)
    if raw is None:
        return TokenUsage.unavailable()

    input_tokens = _nonnegative_int(
        getattr(raw, "prompt_tokens", None)
    )
    output_tokens = _nonnegative_int(
        getattr(raw, "completion_tokens", None)
    )
    reported_total = _nonnegative_int(
        getattr(raw, "total_tokens", None)
    )
    reported_cached = _nonnegative_int(
        getattr(raw, "cached_tokens", None)
    )
    reported_reasoning = _nonnegative_int(
        getattr(raw, "reasoning_tokens", None)
    )

    if input_tokens is None or output_tokens is None:
        return TokenUsage.unavailable(
            provider_details={"reported_usage": _jsonable(raw)}
        )

    details: dict[str, Any] = {}
    calculated_total = input_tokens + output_tokens
    if reported_total is not None and reported_total != calculated_total:
        details["reported_total_tokens"] = reported_total

    cached_tokens = reported_cached
    if cached_tokens is not None and cached_tokens > input_tokens:
        details["reported_cached_tokens"] = cached_tokens
        cached_tokens = None

    reasoning_tokens = reported_reasoning
    if reasoning_tokens is not None and reasoning_tokens > output_tokens:
        details["reported_reasoning_tokens"] = reasoning_tokens
        reasoning_tokens = None

    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=calculated_total,
        cached_input_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        source="provider",
        complete=True,
        provider_details=details,
    )


def _attempt_no(event: Any) -> int:
    attempt = getattr(event, "attempt", 0)
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ValueError("observer event attempt must be an integer")
    if attempt < 0:
        raise ValueError("observer event attempt cannot be negative")
    return attempt + 1


def _physical_id(event: Any) -> str:
    value = getattr(event, "llm_id", None)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observer event llm_id must be a non-empty string")
    return value.strip()


def _optional_physical_id(event: Any) -> str | None:
    value = getattr(event, "llm_id", None)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observer event llm_id must be a string or empty")
    return value.strip()


def _logical_id(event: Any, physical_id: str | None) -> str:
    value = getattr(event, "logical_call_id", None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if physical_id:
        return physical_id
    raise ValueError(
        "observer event requires logical_call_id when llm_id is empty"
    )


def _event_id(
    logical_id: str,
    physical_id: str,
    event_type: RunEventType,
    attempt_no: int | None,
) -> str:
    identity = (
        f"{logical_id}:{physical_id}:{event_type.value}:"
        f"{attempt_no if attempt_no is not None else '-'}"
    )
    return uuid5(_EVENT_NAMESPACE, identity).hex


class LLMTraceObserver:
    """Persist existing ``LLMObserver`` callbacks without affecting calls.

    Agent Framework creates a new physical ``llm_id`` for each attempt and
    explicitly carries one stable ``logical_call_id`` across retries.  The
    observer never infers retry ownership from ambient context.
    """

    def __init__(
        self,
        writer: LLMTraceWriter,
        *,
        context: ContextSource = None,
        max_attempts: int = 1,
    ) -> None:
        if not isinstance(writer, LLMTraceWriter):
            raise TypeError("writer must be an LLMTraceWriter")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if context is not None and not (
            isinstance(context, TraceContext) or callable(context)
        ):
            raise TypeError(
                "context must be TraceContext, a callable, or None"
            )
        self._writer = writer
        self._context_source = context
        # ``max_attempts`` remains accepted for source compatibility with the
        # old writer.observer(...) factory.  Terminality now comes from the
        # event's ``will_retry`` field; a process-wide guess is incorrect when
        # different Agents use different RetryPolicy values.
        self._failed_writes = 0
        self._last_error: Exception | None = None

    @property
    def failed_writes(self) -> int:
        """Number of callback writes that failed and were swallowed."""

        return self._failed_writes

    @property
    def last_error(self) -> Exception | None:
        """Most recent swallowed write error, useful for health monitoring."""

        return self._last_error

    def _context(self) -> TraceContext:
        source = self._context_source
        result = source() if callable(source) else source
        if result is None:
            return TraceContext()
        if not isinstance(result, TraceContext):
            raise TypeError("context callback must return TraceContext or None")
        return result

    def _capture_failure(self, error: Exception) -> None:
        self._failed_writes += 1
        self._last_error = error

    def on_start(self, event: "CallStarted") -> None:
        try:
            physical_id = _physical_id(event)
            attempt_no = _attempt_no(event)
            logical_id = _logical_id(event, physical_id)
            request = {
                "messages": [
                    _message_snapshot(message)
                    for message in getattr(event, "messages", ())
                ],
                "parameters": _jsonable(getattr(event, "params", {})),
                "tools": [
                    _tool_snapshot(tool)
                    for tool in getattr(event, "tools", ())
                ],
            }
            # 每个 attempt 都重放幂等 start：首次写入若暂时失败，后续 attempt
            # 仍有机会补建逻辑调用；已有行则由 store 校验请求快照完全一致。
            self._writer.start_call(
                llm_call_id=logical_id,
                event_id=_event_id(
                    logical_id,
                    logical_id,
                    RunEventType.CALL_STARTED,
                    None,
                ),
                context=self._context(),
                provider=str(getattr(event, "provider", "")),
                model=str(getattr(event, "model", "")),
                request=request,
            )

            self._writer.start_attempt(
                logical_id,
                attempt_no,
                event_id=_event_id(
                    logical_id,
                    physical_id,
                    RunEventType.ATTEMPT_STARTED,
                    attempt_no,
                ),
                payload={"physical_llm_id": physical_id},
            )
        except Exception as error:
            self._capture_failure(error)

    def on_end(self, event: "CallEnded") -> None:
        try:
            physical_id = _physical_id(event)
            attempt_no = _attempt_no(event)
            logical_id = _logical_id(event, physical_id)
            response = getattr(event, "response", None)
            success_payload = {
                "physical_llm_id": physical_id,
                "response": _response_snapshot(response),
                "usage": _provider_usage(response),
            }
            ttft_ms = _nonnegative_int(getattr(event, "ttft_ms", None))
            chunk_count = _nonnegative_int(
                getattr(event, "chunk_count", None)
            )
            if ttft_ms is not None:
                success_payload["ttft_ms"] = ttft_ms
            if chunk_count is not None:
                success_payload["chunk_count"] = chunk_count

            self._writer.append_records(
                logical_id,
                [
                    LLMRunRecord(
                        event_type=RunEventType.ATTEMPT_SUCCEEDED,
                        event_id=_event_id(
                            logical_id,
                            physical_id,
                            RunEventType.ATTEMPT_SUCCEEDED,
                            attempt_no,
                        ),
                        attempt_no=attempt_no,
                        duration_ms=_nonnegative_int(
                            getattr(event, "duration_ms", None)
                        ),
                        payload=success_payload,
                    ),
                    LLMRunRecord(
                        event_type=RunEventType.CALL_COMPLETED,
                        event_id=_event_id(
                            logical_id,
                            physical_id,
                            RunEventType.CALL_COMPLETED,
                            attempt_no,
                        ),
                        attempt_no=attempt_no,
                        payload={
                            "final_attempt_no": attempt_no,
                            "physical_llm_id": physical_id,
                        },
                    ),
                ],
            )
        except Exception as error:
            self._capture_failure(error)

    def on_error(self, event: "CallFailed") -> None:
        try:
            physical_id = _physical_id(event)
            attempt_no = _attempt_no(event)
            logical_id = _logical_id(event, physical_id)
            retryable = bool(getattr(event, "retryable", False))
            will_retry = bool(getattr(event, "will_retry", False))
            terminal = not will_retry
            error = str(getattr(event, "error", ""))
            error_type = str(
                getattr(event, "error_type", "LLMError")
            )
            failure_payload: dict[str, Any] = {
                "physical_llm_id": physical_id,
                "error": error,
                "error_type": error_type,
                "retryable": retryable,
                "will_retry": will_retry,
                "usage": TokenUsage.unavailable(),
            }
            status_code = getattr(event, "status_code", None)
            if status_code is not None:
                failure_payload["status_code"] = status_code

            records = [
                LLMRunRecord(
                    event_type=RunEventType.ATTEMPT_FAILED,
                    event_id=_event_id(
                        logical_id,
                        physical_id,
                        RunEventType.ATTEMPT_FAILED,
                        attempt_no,
                    ),
                    attempt_no=attempt_no,
                    duration_ms=_nonnegative_int(
                        getattr(event, "duration_ms", None)
                    ),
                    payload=failure_payload,
                )
            ]
            if terminal:
                records.append(
                    LLMRunRecord(
                        event_type=RunEventType.CALL_FAILED,
                        event_id=_event_id(
                            logical_id,
                            physical_id,
                            RunEventType.CALL_FAILED,
                            attempt_no,
                        ),
                        attempt_no=attempt_no,
                        payload={
                            "final_attempt_no": attempt_no,
                            "physical_llm_id": physical_id,
                            "error": error,
                            "error_type": error_type,
                        },
                    )
                )
            else:
                records.append(
                    LLMRunRecord(
                        event_type=RunEventType.RETRY_SCHEDULED,
                        event_id=_event_id(
                            logical_id,
                            physical_id,
                            RunEventType.RETRY_SCHEDULED,
                            attempt_no,
                        ),
                        attempt_no=attempt_no,
                        payload={
                            "next_attempt_no": attempt_no + 1,
                            "reason": error_type,
                        },
                    )
                )
            self._writer.append_records(logical_id, records)
        except Exception as error:
            self._capture_failure(error)

    def on_cancel(self, event: "CallCancelled") -> None:
        """闭合飞行中或重试退避中的逻辑调用。"""

        try:
            physical_id = _optional_physical_id(event)
            logical_id = _logical_id(event, physical_id)
            attempt_no = _attempt_no(event)
            phase = str(getattr(event, "phase", "in_flight") or "in_flight")
            reason = str(getattr(event, "reason", "cancelled") or "cancelled")
            identity_id = physical_id or logical_id
            records: list[LLMRunRecord] = []
            if phase == "in_flight":
                records.append(
                    LLMRunRecord(
                        event_type=RunEventType.ATTEMPT_CANCELLED,
                        event_id=_event_id(
                            logical_id,
                            identity_id,
                            RunEventType.ATTEMPT_CANCELLED,
                            attempt_no,
                        ),
                        attempt_no=attempt_no,
                        duration_ms=_nonnegative_int(
                            getattr(event, "duration_ms", None)
                        ),
                        payload={
                            "physical_llm_id": physical_id,
                            "reason": reason,
                            "phase": phase,
                            "usage": TokenUsage.unavailable(),
                        },
                    )
                )
            records.append(
                LLMRunRecord(
                    event_type=RunEventType.CALL_CANCELLED,
                    event_id=_event_id(
                        logical_id,
                        identity_id,
                        RunEventType.CALL_CANCELLED,
                        attempt_no,
                    ),
                    attempt_no=attempt_no,
                    payload={
                        "final_attempt_no": attempt_no,
                        "physical_llm_id": physical_id,
                        "reason": reason,
                        "phase": phase,
                    },
                )
            )
            self._writer.append_records(logical_id, records)
        except Exception as error:
            self._capture_failure(error)


__all__ = ["LLMTraceObserver"]
