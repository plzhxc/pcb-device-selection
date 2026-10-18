"""Public Python interface for the LLM trace write side."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Iterable, Mapping
from uuid import uuid4

from ...database import ConnectionFactory, Dialect
from ..base import (
    BaseTraceWriter,
    TraceWriteDispatcher,
    TraceWriteFuture,
)
from .contracts import (
    ContextSource,
    LLMRunRecord,
    RunEventType,
    TokenUsage,
    TraceContext,
)
from .store import LLMWriteStore, RunRecord, WriteReceipt


def _identifier(
    name: str,
    value: str | None,
    *,
    required: bool = False,
    max_length: int = 64,
) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        if required:
            raise ValueError(f"{name} cannot be empty")
        return None
    if len(normalized) > max_length:
        raise ValueError(
            f"{name} must be at most {max_length} characters"
        )
    return normalized


def _payload_with_usage(
    payload: Mapping[str, Any] | None,
    usage: TokenUsage | None,
) -> dict[str, Any]:
    result = dict(payload or {})
    result["usage"] = (usage or TokenUsage.unavailable()).to_payload()
    return result


class LLMTraceWriter(BaseTraceWriter):
    """Write logical LLM calls through an application-owned connection.

    Args:
        connect_factory: A zero-argument callable returning a DB-API
            connection.  Passing ``pool.get_connection`` is supported.
        dialect: SQL dialect emitted by Trace, currently ``sqlite`` or
            ``mysql``.
        ensure_schema: Create the two Trace tables during construction.
    """

    def __init__(
        self,
        connect_factory: ConnectionFactory,
        *,
        dialect: Dialect,
        ensure_schema: bool = True,
        dispatcher: TraceWriteDispatcher | None = None,
        background: bool = False,
    ) -> None:
        super().__init__(
            connect_factory,
            dialect=dialect,
            ensure_schema=ensure_schema,
            dispatcher=dispatcher,
            background=background,
        )
        self._store = LLMWriteStore(self._db)

    def start_call(
        self,
        *,
        provider: str,
        model: str,
        request: Mapping[str, Any],
        context: TraceContext | None = None,
        llm_call_id: str | None = None,
        event_id: str | None = None,
        created_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        """Start one logical call and return its idempotent write receipt."""

        logical_id = _identifier(
            "llm_call_id",
            llm_call_id or uuid4().hex,
            required=True,
        )
        start_event_id = _identifier(
            "event_id",
            event_id or uuid4().hex,
            required=True,
        )
        normalized_provider = _identifier(
            "provider",
            provider,
            required=True,
        )
        normalized_model = _identifier(
            "model",
            model,
            required=True,
            max_length=191,
        )
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")

        call_context = context or TraceContext()
        trace_id = _identifier(
            "trace_id",
            call_context.trace_id or logical_id,
            required=True,
        )
        flow_id = _identifier("flow_id", call_context.flow_id)
        stage_id = _identifier("stage_id", call_context.stage_id)
        agent_id = _identifier("agent_id", call_context.agent_id)
        assert logical_id is not None
        assert start_event_id is not None
        assert trace_id is not None
        assert normalized_provider is not None
        assert normalized_model is not None

        request_snapshot = deepcopy(dict(request))
        return self._submit(
            lambda: self._transaction(
                lambda cursor: self._store.create_call(
                    cursor,
                    llm_call_id=logical_id,
                    event_id=start_event_id,
                    trace_id=trace_id,
                    flow_id=flow_id,
                    stage_id=stage_id,
                    agent_id=agent_id,
                    provider=normalized_provider,
                    model=normalized_model,
                    request=request_snapshot,
                    created_at=created_at,
                )
            )
        )

    def append_record(
        self,
        llm_call_id: str,
        record: LLMRunRecord,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        """Append one record to an existing logical call."""

        logical_id, pending = self._prepare_records(llm_call_id, [record])
        return self._submit(
            lambda: self._transaction(
                lambda cursor: self._store.append_records(
                    cursor,
                    logical_id,
                    pending,
                )[0]
            )
        )

    def append_records(
        self,
        llm_call_id: str,
        records: Iterable[LLMRunRecord],
    ) -> list[WriteReceipt] | TraceWriteFuture[list[WriteReceipt]]:
        """Atomically append records and assign their per-call sequence."""

        logical_id, pending = self._prepare_records(llm_call_id, records)
        if not pending:
            return []
        return self._submit(
            lambda: self._transaction(
                lambda cursor: self._store.append_records(
                    cursor,
                    logical_id,
                    pending,
                )
            )
        )

    @staticmethod
    def _prepare_records(
        llm_call_id: str,
        records: Iterable[LLMRunRecord],
    ) -> tuple[str, tuple[RunRecord, ...]]:
        logical_id = _identifier(
            "llm_call_id",
            llm_call_id,
            required=True,
        )
        pending = list(records)
        for record in pending:
            if not isinstance(record, LLMRunRecord):
                raise TypeError("records must contain LLMRunRecord objects")
            if record.event_type is RunEventType.CALL_STARTED:
                raise ValueError(
                    "call_started is created by start_call and cannot "
                    "be appended"
                )
        assert logical_id is not None
        return (
            logical_id,
            tuple(
                RunRecord(
                    event_id=record.event_id,
                    event_type=record.event_type.value,
                    attempt_no=record.attempt_no,
                    level=record.level or "INFO",
                    duration_ms=record.duration_ms,
                    payload=deepcopy(dict(record.payload)),
                    recorded_at=record.recorded_at,
                )
                for record in pending
            ),
        )

    def start_attempt(
        self,
        llm_call_id: str,
        attempt_no: int,
        *,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        return self.append_record(
            llm_call_id,
            LLMRunRecord(
                event_type=RunEventType.ATTEMPT_STARTED,
                event_id=event_id or uuid4().hex,
                attempt_no=attempt_no,
                payload=payload or {},
                recorded_at=recorded_at,
            ),
        )

    def complete_attempt(
        self,
        llm_call_id: str,
        attempt_no: int,
        *,
        usage: TokenUsage | None = None,
        duration_ms: int | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        return self.append_record(
            llm_call_id,
            LLMRunRecord(
                event_type=RunEventType.ATTEMPT_SUCCEEDED,
                event_id=event_id or uuid4().hex,
                attempt_no=attempt_no,
                duration_ms=duration_ms,
                payload=_payload_with_usage(payload, usage),
                recorded_at=recorded_at,
            ),
        )

    def fail_attempt(
        self,
        llm_call_id: str,
        attempt_no: int,
        *,
        error: str,
        error_type: str,
        retryable: bool,
        status_code: int | None = None,
        usage: TokenUsage | None = None,
        duration_ms: int | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        details = dict(payload or {})
        details.update(
            {
                "error": error,
                "error_type": error_type,
                "retryable": retryable,
            }
        )
        if status_code is not None:
            details["status_code"] = status_code
        return self.append_record(
            llm_call_id,
            LLMRunRecord(
                event_type=RunEventType.ATTEMPT_FAILED,
                event_id=event_id or uuid4().hex,
                attempt_no=attempt_no,
                duration_ms=duration_ms,
                payload=_payload_with_usage(details, usage),
                recorded_at=recorded_at,
            ),
        )

    def schedule_retry(
        self,
        llm_call_id: str,
        attempt_no: int,
        *,
        delay_ms: int | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        if delay_ms is not None and (
            isinstance(delay_ms, bool)
            or not isinstance(delay_ms, int)
            or delay_ms < 0
        ):
            raise ValueError(
                "delay_ms must be a non-negative integer or None"
            )
        details = dict(payload or {})
        if delay_ms is not None:
            details["delay_ms"] = delay_ms
        return self.append_record(
            llm_call_id,
            LLMRunRecord(
                event_type=RunEventType.RETRY_SCHEDULED,
                event_id=event_id or uuid4().hex,
                attempt_no=attempt_no,
                payload=details,
                recorded_at=recorded_at,
            ),
        )

    def complete_call(
        self,
        llm_call_id: str,
        *,
        attempt_no: int | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        return self.append_record(
            llm_call_id,
            LLMRunRecord(
                event_type=RunEventType.CALL_COMPLETED,
                event_id=event_id or uuid4().hex,
                attempt_no=attempt_no,
                payload=payload or {},
                recorded_at=recorded_at,
            ),
        )

    def fail_call(
        self,
        llm_call_id: str,
        *,
        error: str,
        error_type: str,
        attempt_no: int | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        details = dict(payload or {})
        details.update({"error": error, "error_type": error_type})
        return self.append_record(
            llm_call_id,
            LLMRunRecord(
                event_type=RunEventType.CALL_FAILED,
                event_id=event_id or uuid4().hex,
                attempt_no=attempt_no,
                payload=details,
                recorded_at=recorded_at,
            ),
        )

    def cancel_call(
        self,
        llm_call_id: str,
        *,
        reason: str | None = None,
        attempt_no: int | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> WriteReceipt | TraceWriteFuture[WriteReceipt]:
        details = dict(payload or {})
        if reason is not None:
            details["reason"] = reason
        return self.append_record(
            llm_call_id,
            LLMRunRecord(
                event_type=RunEventType.CALL_CANCELLED,
                event_id=event_id or uuid4().hex,
                attempt_no=attempt_no,
                payload=details,
                recorded_at=recorded_at,
            ),
        )

    def ping(self) -> None:
        """Raise when the injected database is unavailable."""

        self.flush()
        self._transaction(self._store.ping)

    def observer(
        self,
        *,
        context: ContextSource = None,
        max_attempts: int = 1,
    ) -> "LLMTraceObserver":
        """Build an Agent Framework-compatible observer.

        The import is intentionally local so the direct writer never requires
        Agent Framework to be installed.
        """

        from .observer import LLMTraceObserver

        return LLMTraceObserver(
            self,
            context=context,
            max_attempts=max_attempts,
        )


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .observer import LLMTraceObserver


__all__ = ["LLMTraceWriter"]
