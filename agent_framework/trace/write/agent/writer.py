"""Agent 运行的落库。

比 LLM 侧简单：Agent 的 ``run_id`` 本身就是逻辑标识，不存在"多次物理重试聚合成
一次逻辑调用"的问题，因此不需要独立的 store 层。

两张表分工：

- ``agent_run_info`` 一次运行一行，开始时插入、结束时补齐汇总；
- ``agent_run_records`` 事件流，按 ``seq`` 有序追加。

写入失败不抛给调用方——观测不该成为业务的故障源。失败会计数并保留最后一个异常，
供健康检查读取。
"""

from __future__ import annotations

from concurrent.futures import Future
import json
from typing import Any, Mapping, Sequence

from ..base import BaseTraceWriter
from .contracts import (
    AgentEventType,
    AgentRunSummary,
    AgentTraceContext,
    jsonable,
    utc_now,
)


def _json(value: Any) -> str:
    return json.dumps(jsonable(value), ensure_ascii=False, default=str)


class AgentTraceWriter(BaseTraceWriter):
    """把 Agent 运行写入数据库。

    Args:
        connect_factory: 零参可调用对象，返回一个 DB-API 连接。
        dialect: ``sqlite`` 或 ``mysql``。
        ensure_schema: 构造时建表。
    """

    def _record(
        self,
        run_id: str,
        event_type: AgentEventType,
        *,
        step_index: int | None = None,
        level: str = "INFO",
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[str, Sequence[Any]]:
        seq = self._next_seq(run_id)
        return (
            "INSERT INTO agent_run_records "
            "(event_id, agent_run_id, seq, event_type, step_index, level, "
            "payload_json, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                f"{run_id}:{seq}",
                run_id,
                seq,
                event_type.value,
                step_index,
                level,
                _json(payload or {}),
                self.timestamp(utc_now()),
            ),
        )

    # ------------------------------ 生命周期 ------------------------------ #
    def start_run(
        self,
        run_id: str,
        *,
        agent: str,
        context: AgentTraceContext,
        task_type: str = "",
        turn_no: int = 1,
        session_id: str = "",
        inputs: Mapping[str, Any] | None = None,
    ) -> bool:
        """记录一次运行开始；顺带确保会话行存在。"""

        now = self.timestamp(utc_now())
        statements: list[tuple[str, Sequence[Any]]] = []
        if session_id:
            # 会话行只在首轮插入，后续轮次靠 finish_run 累加，因此用 IGNORE 语义。
            keyword = (
                "INSERT OR IGNORE" if self._db.dialect == "sqlite" else "INSERT IGNORE"
            )
            statements.append(
                (
                    f"{keyword} INTO agent_session_info "
                    "(session_id, trace_id, flow_id, stage_id, agent, task_type, "
                    "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        session_id,
                        context.trace_id,
                        context.flow_id,
                        context.stage_id,
                        agent,
                        task_type or None,
                        now,
                        now,
                    ),
                )
            )
        return self._write(
            statements
            + [
                (
                    "INSERT INTO agent_run_info "
                    "(agent_run_id, trace_id, flow_id, stage_id, session_id, agent, "
                    "task_type, turn_no, inputs_json, repairs_json, output_keys_json, "
                    "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        run_id,
                        context.trace_id,
                        context.flow_id,
                        context.stage_id,
                        session_id or None,
                        agent,
                        task_type or None,
                        turn_no,
                        _json(inputs or {}),
                        _json({}),
                        _json([]),
                        now,
                    ),
                ),
                self._record(
                    run_id,
                    AgentEventType.RUN_STARTED,
                    payload={"agent": agent, "task_type": task_type, "turn": turn_no},
                ),
            ]
        )

    def record_step(
        self,
        run_id: str,
        *,
        kind: str,
        index: int,
        detail: Mapping[str, Any] | None = None,
    ) -> bool:
        """记录一轮推进，保留普通与分层 Agent 的真实语义。

        ``HierarchicalAgent`` 的 plan/subs/merge 发生在 Master 运行结束之后。
        此时内存序号已经释放，因此改为在后台事务中从数据库原子续号；不会重回 1，
        也不会为了支持晚到事件永久保留所有历史 run_id。
        """

        events = {
            "tools": AgentEventType.TOOLS_EXECUTED,
            "tool_started": AgentEventType.TOOL_STARTED,
            "tool_completed": AgentEventType.TOOL_COMPLETED,
            "tool_failed": AgentEventType.TOOL_FAILED,
            "tool_denied": AgentEventType.TOOL_DENIED,
            "tool_timeout": AgentEventType.TOOL_TIMEOUT,
            "tool_cancelled": AgentEventType.TOOL_CANCELLED,
            "correction": AgentEventType.OUTPUT_CORRECTION,
            "validation": AgentEventType.VALIDATION_RETRY,
            "plan": AgentEventType.HIERARCHY_PLAN,
            "subs": AgentEventType.HIERARCHY_SUBTASKS,
            "merge": AgentEventType.HIERARCHY_MERGE,
        }
        event = events.get(kind, AgentEventType.ORCHESTRATION_NOTE)
        outcome = str((detail or {}).get("outcome", "")).lower()
        if outcome in {"failed", "master_failed", "plan_invalid"} or kind in {
            "tool_failed",
            "tool_timeout",
            "tool_cancelled",
        }:
            level = "ERROR"
        elif kind in {"correction", "validation", "tool_denied"}:
            level = "WARNING"
        else:
            level = "INFO"

        if self._sequence_known(run_id):
            return self._write(
                [
                    self._record(
                        run_id,
                        event,
                        step_index=index,
                        level=level,
                        payload=detail,
                    )
                ]
            )
        return self._write_late_record(
            run_id,
            event,
            step_index=index,
            level=level,
            payload=detail,
        )

    def _write_late_record(
        self,
        run_id: str,
        event_type: AgentEventType,
        *,
        step_index: int,
        level: str,
        payload: Mapping[str, Any] | None,
    ) -> bool:
        """在已有事件之后事务内续号，供 Master 结束后的层级记录使用。"""

        frozen_payload = _json(payload or {})
        recorded_at = self.timestamp(utc_now())

        def operation() -> bool:
            def append(cursor: Any) -> bool:
                self._db.execute(
                    cursor,
                    "SELECT COALESCE(MAX(seq), 0) + 1 "
                    "FROM agent_run_records WHERE agent_run_id = %s",
                    (run_id,),
                )
                row = cursor.fetchone()
                raw_seq = (
                    next(iter(row.values()))
                    if isinstance(row, Mapping)
                    else row[0]
                )
                seq = int(raw_seq)
                self._db.execute(
                    cursor,
                    "INSERT INTO agent_run_records "
                    "(event_id, agent_run_id, seq, event_type, step_index, level, "
                    "payload_json, recorded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        f"{run_id}:{seq}",
                        run_id,
                        seq,
                        event_type.value,
                        step_index,
                        level,
                        frozen_payload,
                        recorded_at,
                    ),
                )
                return True

            return self._transaction(append)

        result = self._submit(
            operation,
            suppress_errors=True,
            failure_result=False,
        )
        return True if isinstance(result, Future) else bool(result)

    def finish_run(self, run_id: str, summary: AgentRunSummary) -> bool:
        """补齐汇总并记录终态。"""

        if summary.status == "cancelled":
            event = AgentEventType.RUN_CANCELLED
        elif summary.failed:
            event = AgentEventType.RUN_FAILED
        else:
            event = AgentEventType.RUN_COMPLETED
        statements = [
            (
                "UPDATE agent_run_info SET status = %s, tool_iterations = %s, "
                "output_retries = %s, llm_calls = %s, tool_calls = %s, "
                "tool_failures = %s, total_input_tokens = %s, "
                "total_output_tokens = %s, total_tokens = %s, "
                "cached_tokens = %s, reasoning_tokens = %s, repairs_json = %s, "
                "output_keys_json = %s, duration_ms = %s, error = %s, ended_at = %s "
                "WHERE agent_run_id = %s",
                (
                    summary.status,
                    summary.tool_iterations,
                    summary.output_retries,
                    summary.llm_calls,
                    summary.tool_calls,
                    summary.tool_failures,
                    summary.prompt_tokens or 0,
                    summary.completion_tokens or 0,
                    summary.total_tokens or 0,
                    summary.cached_tokens or 0,
                    summary.reasoning_tokens or 0,
                    _json(summary.repairs),
                    _json(list(summary.output_keys)),
                    summary.duration_ms,
                    summary.error,
                    self.timestamp(utc_now()),
                    run_id,
                ),
            ),
            self._record(
                run_id,
                event,
                level=(
                    "WARNING"
                    if summary.status == "cancelled"
                    else ("ERROR" if summary.failed else "INFO")
                ),
                payload={"status": summary.status, "duration_ms": summary.duration_ms},
            ),
        ]
        if summary.session_id:
            # 用观测事件带来的累计值直接覆盖，而不是在 SQL 里做加法：
            # 累加会在重复写入时把数字放大，覆盖则天然幂等。
            statements.append(
                (
                    "UPDATE agent_session_info SET turns = %s, llm_calls = %s, "
                    "total_tokens = %s, tool_calls = tool_calls + %s, "
                    "tool_failures = tool_failures + %s, "
                    "output_retries = output_retries + %s, "
                    "last_status = %s, updated_at = %s WHERE session_id = %s",
                    (
                        summary.session_turns,
                        summary.session_llm_calls,
                        summary.session_total_tokens or 0,
                        summary.tool_calls,
                        summary.tool_failures,
                        summary.output_retries,
                        summary.status,
                        self.timestamp(utc_now()),
                        summary.session_id,
                    ),
                )
            )
        written = self._write(statements)
        self._forget(run_id)
        return written


__all__ = ["AgentTraceWriter"]
