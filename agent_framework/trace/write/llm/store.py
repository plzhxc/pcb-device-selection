"""LLM 两张明细表的事务内写入规则。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from ...database import TraceDatabase


TERMINAL_EVENT_TYPES = {
    "call_completed",
    "call_failed",
    "call_cancelled",
}


class LLMWriteError(RuntimeError):
    """Base class for stable write-side errors."""


class LLMCallNotFound(LLMWriteError):
    pass


class LLMCallConflict(LLMWriteError):
    pass


class RecordConflict(LLMWriteError):
    pass


class LLMCallTerminal(LLMWriteError):
    pass


@dataclass(frozen=True, slots=True)
class WriteReceipt:
    llm_call_id: str
    event_id: str
    record_id: int
    seq: int
    event_type: str
    created: bool


@dataclass(frozen=True, slots=True)
class RunRecord:
    event_id: str
    event_type: str
    attempt_no: int | None
    level: str
    duration_ms: int | None
    payload: Mapping[str, Any]
    recorded_at: datetime | None


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _stored_json(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            return _json(json.loads(value))
        except (json.JSONDecodeError, TypeError, ValueError):
            return value
    return _json(value)


def _utc_datetime(value: datetime | str | None) -> datetime:
    return TraceDatabase.utc_datetime(value)


def _elapsed_ms(started_at: Any, ended_at: datetime) -> int:
    started = _utc_datetime(started_at)
    return max(0, round((ended_at - started).total_seconds() * 1000))


def _one(
    database: TraceDatabase,
    cursor: Any,
    sql: str,
    parameters: tuple[Any, ...],
) -> dict[str, Any] | None:
    database.execute(cursor, sql, parameters)
    row = cursor.fetchone()
    return database.row_to_dict(cursor, row) if row is not None else None


def _receipt(record: Mapping[str, Any], *, created: bool) -> WriteReceipt:
    return WriteReceipt(
        llm_call_id=str(record["llm_call_id"]),
        event_id=str(record["event_id"]),
        record_id=int(record["id"]),
        seq=int(record["seq"]),
        event_type=str(record["event_type"]),
        created=created,
    )


def _same_optional(left: Any, right: Any) -> bool:
    return (left if left is not None else None) == (
        right if right is not None else None
    )


class LLMWriteStore:
    """LLM 表的事务内操作。

    连接、事务、回滚与后台调度统一由 ``BaseTraceWriter`` 管理；Store 只接收
    已打开的 cursor，专注于 LLM 调用的幂等和聚合规则。
    """

    def __init__(self, database: TraceDatabase) -> None:
        self._database = database

    def _lock_call(
        self,
        cursor: Any,
        llm_call_id: str,
    ) -> dict[str, Any] | None:
        suffix = "" if self._database.dialect == "sqlite" else " FOR UPDATE"
        return _one(
            self._database,
            cursor,
            "SELECT llm_call_id, trace_id, flow_id, stage_id, agent_id, "
            "provider, model, request_json, total_input_tokens, "
            "total_output_tokens, total_tokens, retry_count, duration_ms, "
            "usage_complete, created_at "
            "FROM llm_call_info WHERE llm_call_id=%s" + suffix,
            (llm_call_id,),
        )

    def _record_by_event_id(
        self,
        cursor: Any,
        event_id: str,
    ) -> dict[str, Any] | None:
        return _one(
            self._database,
            cursor,
            "SELECT id, event_id, llm_call_id, seq, event_type, attempt_no, "
            "level, duration_ms, payload_json "
            "FROM llm_run_records WHERE event_id=%s",
            (event_id,),
        )

    def _record_by_seq(
        self,
        cursor: Any,
        llm_call_id: str,
        seq: int,
    ) -> dict[str, Any] | None:
        return _one(
            self._database,
            cursor,
            "SELECT id, event_id, llm_call_id, seq, event_type, attempt_no, "
            "level, duration_ms, payload_json "
            "FROM llm_run_records WHERE llm_call_id=%s AND seq=%s",
            (llm_call_id, seq),
        )

    def _terminal_record(
        self,
        cursor: Any,
        llm_call_id: str,
    ) -> dict[str, Any] | None:
        return _one(
            self._database,
            cursor,
            "SELECT id, event_id, llm_call_id, seq, event_type, attempt_no, "
            "level, duration_ms, payload_json "
            "FROM llm_run_records WHERE llm_call_id=%s "
            "AND event_type IN (%s, %s, %s) ORDER BY seq LIMIT 1",
            (
                llm_call_id,
                "call_completed",
                "call_failed",
                "call_cancelled",
            ),
        )

    @staticmethod
    def _same_record(
        existing: Mapping[str, Any],
        llm_call_id: str,
        record: RunRecord,
    ) -> bool:
        return (
            str(existing["llm_call_id"]) == llm_call_id
            and str(existing["event_type"]) == record.event_type
            and _same_optional(existing.get("attempt_no"), record.attempt_no)
            and str(existing["level"]) == record.level
            and _same_optional(existing.get("duration_ms"), record.duration_ms)
            and _stored_json(existing["payload_json"]) == _json(record.payload)
        )

    @staticmethod
    def _usage_summary(
        record: RunRecord,
    ) -> tuple[int, int, int, int, int, bool]:
        if record.event_type not in {
            "attempt_succeeded",
            "attempt_failed",
            "attempt_cancelled",
        }:
            return 0, 0, 0, 0, 0, True
        usage = record.payload.get("usage")
        if not isinstance(usage, Mapping):
            return 0, 0, 0, 0, 0, False
        input_value = usage.get("input_tokens")
        output_value = usage.get("output_tokens")
        total_value = usage.get("total_tokens")
        cached_value = usage.get("cached_input_tokens")
        reasoning_value = usage.get("reasoning_tokens")
        input_tokens = (
            int(input_value)
            if isinstance(input_value, int)
            and not isinstance(input_value, bool)
            and input_value >= 0
            else 0
        )
        output_tokens = (
            int(output_value)
            if isinstance(output_value, int)
            and not isinstance(output_value, bool)
            and output_value >= 0
            else 0
        )
        total_tokens = (
            int(total_value)
            if isinstance(total_value, int)
            and not isinstance(total_value, bool)
            and total_value >= 0
            else 0
        )
        cached_tokens = (
            int(cached_value)
            if isinstance(cached_value, int)
            and not isinstance(cached_value, bool)
            and cached_value >= 0
            and cached_value <= input_tokens
            else 0
        )
        reasoning_tokens = (
            int(reasoning_value)
            if isinstance(reasoning_value, int)
            and not isinstance(reasoning_value, bool)
            and reasoning_value >= 0
            and reasoning_value <= output_tokens
            else 0
        )
        confirmed = (
            usage.get("source") == "provider"
            and usage.get("complete") is True
            and isinstance(input_value, int)
            and not isinstance(input_value, bool)
            and input_value >= 0
            and isinstance(output_value, int)
            and not isinstance(output_value, bool)
            and output_value >= 0
            and isinstance(total_value, int)
            and not isinstance(total_value, bool)
            and total_value == input_value + output_value
        )
        return (
            input_tokens,
            output_tokens,
            total_tokens,
            cached_tokens,
            reasoning_tokens,
            confirmed,
        )

    def create_call(
        self,
        cursor: Any,
        *,
        llm_call_id: str,
        event_id: str,
        trace_id: str,
        flow_id: str | None,
        stage_id: str | None,
        agent_id: str | None,
        provider: str,
        model: str,
        request: Mapping[str, Any],
        created_at: datetime | None,
    ) -> WriteReceipt:
        """Insert immutable call info and ``call_started`` atomically."""

        request_json = _json(request)
        existing = self._lock_call(cursor, llm_call_id)
        if existing is not None:
            same_call = (
                str(existing["trace_id"]) == trace_id
                and _same_optional(existing.get("flow_id"), flow_id)
                and _same_optional(existing.get("stage_id"), stage_id)
                and _same_optional(existing.get("agent_id"), agent_id)
                and str(existing["provider"]) == provider
                and str(existing["model"]) == model
                and _stored_json(existing["request_json"]) == request_json
            )
            if not same_call:
                raise LLMCallConflict(
                    f"llm_call_id={llm_call_id} already has different info"
                )
            started = self._record_by_seq(cursor, llm_call_id, 1)
            if started is None or started["event_type"] != "call_started":
                raise LLMCallConflict(
                    f"llm_call_id={llm_call_id} has no canonical start record"
                )
            return _receipt(started, created=False)

        reused_event = self._record_by_event_id(cursor, event_id)
        if reused_event is not None:
            raise RecordConflict(
                f"event_id={event_id} already belongs to another record"
            )

        started_at = self._database.timestamp(created_at)
        self._database.execute(
            cursor,
            "INSERT INTO llm_call_info "
            "(llm_call_id, trace_id, flow_id, stage_id, agent_id, "
            "provider, model, request_json, total_input_tokens, "
            "total_output_tokens, total_tokens, retry_count, duration_ms, "
            "usage_complete, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s)",
            (
                llm_call_id,
                trace_id,
                flow_id,
                stage_id,
                agent_id,
                provider,
                model,
                request_json,
                0,
                0,
                0,
                0,
                None,
                True,
                started_at,
            ),
        )
        self._database.execute(
            cursor,
            "INSERT INTO llm_run_records "
            "(event_id, llm_call_id, seq, event_type, attempt_no, level, "
            "duration_ms, payload_json, recorded_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                event_id,
                llm_call_id,
                1,
                "call_started",
                None,
                "INFO",
                None,
                "{}",
                started_at,
            ),
        )
        started = self._record_by_event_id(cursor, event_id)
        assert started is not None
        return _receipt(started, created=True)

    def append_records(
        self,
        cursor: Any,
        llm_call_id: str,
        records: Iterable[RunRecord],
    ) -> list[WriteReceipt]:
        """Append one or more records atomically, assigning per-call sequence."""

        pending = list(records)
        if not pending:
            return []

        call_info = self._lock_call(cursor, llm_call_id)
        if call_info is None:
            raise LLMCallNotFound(f"llm_call_id={llm_call_id} not found")

        terminal = self._terminal_record(cursor, llm_call_id)
        self._database.execute(
            cursor,
            "SELECT COALESCE(MAX(seq), 0) FROM llm_run_records "
            "WHERE llm_call_id=%s",
            (llm_call_id,),
        )
        row = cursor.fetchone()
        next_seq = int(row[0] or 0) + 1
        receipts: list[WriteReceipt] = []

        for record in pending:
            existing = self._record_by_event_id(cursor, record.event_id)
            if existing is not None:
                if not self._same_record(existing, llm_call_id, record):
                    raise RecordConflict(
                        f"event_id={record.event_id} already has "
                        "different content"
                    )
                receipts.append(_receipt(existing, created=False))
                continue

            if terminal is not None:
                raise LLMCallTerminal(
                    f"llm_call_id={llm_call_id} already ended with "
                    f"{terminal['event_type']}"
                )

            recorded_at = _utc_datetime(record.recorded_at)
            self._database.execute(
                cursor,
                "INSERT INTO llm_run_records "
                "(event_id, llm_call_id, seq, event_type, attempt_no, "
                "level, duration_ms, payload_json, recorded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.event_id,
                    llm_call_id,
                    next_seq,
                    record.event_type,
                    record.attempt_no,
                    record.level,
                    record.duration_ms,
                    _json(record.payload),
                    self._database.timestamp(recorded_at),
                ),
            )
            inserted = self._record_by_event_id(cursor, record.event_id)
            assert inserted is not None
            receipts.append(_receipt(inserted, created=True))
            next_seq += 1

            (
                input_token_delta,
                output_token_delta,
                token_delta,
                cached_token_delta,
                reasoning_token_delta,
                usage_confirmed,
            ) = self._usage_summary(record)
            call_duration = (
                _elapsed_ms(
                    call_info["created_at"],
                    recorded_at,
                )
                if record.event_type in TERMINAL_EVENT_TYPES
                else None
            )
            retry_delta = int(record.event_type == "retry_scheduled")
            if (
                input_token_delta
                or output_token_delta
                or token_delta
                or cached_token_delta
                or reasoning_token_delta
                or retry_delta
                or not usage_confirmed
                or call_duration is not None
            ):
                self._database.execute(
                    cursor,
                    "UPDATE llm_call_info SET "
                    "total_input_tokens=total_input_tokens + %s, "
                    "total_output_tokens=total_output_tokens + %s, "
                    "total_tokens=total_tokens + %s, "
                    "cached_tokens=cached_tokens + %s, "
                    "reasoning_tokens=reasoning_tokens + %s, "
                    "retry_count=retry_count + %s, "
                    "usage_complete=CASE "
                    "WHEN usage_complete=%s AND %s=%s THEN %s ELSE %s END, "
                    "duration_ms=COALESCE(%s, duration_ms) "
                    "WHERE llm_call_id=%s",
                    (
                        input_token_delta,
                        output_token_delta,
                        token_delta,
                        cached_token_delta,
                        reasoning_token_delta,
                        retry_delta,
                        True,
                        usage_confirmed,
                        True,
                        True,
                        False,
                        call_duration,
                        llm_call_id,
                    ),
                )
            if record.event_type in TERMINAL_EVENT_TYPES:
                terminal = inserted

        return receipts

    def ping(self, cursor: Any) -> None:
        self._database.execute(cursor, "SELECT 1")
        cursor.fetchone()


__all__ = [
    "LLMCallConflict",
    "LLMCallNotFound",
    "LLMCallTerminal",
    "LLMWriteError",
    "LLMWriteStore",
    "RecordConflict",
    "RunRecord",
    "WriteReceipt",
]
