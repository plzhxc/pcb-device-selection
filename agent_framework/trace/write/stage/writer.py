"""Stage 运行的落库。

两张表：``stage_run_info``（一个业务环节）、``stage_step_records``（环节内的步骤，
含 Agent 配置画像与短路留痕）。

Workflow 的表在 :mod:`agent_framework.trace.workflow`——与框架的分层一致：
一次业务请求和一个业务环节是两个粒度，混在一个 writer 里查字段要来回翻。

事务、失败计数、序号都在 :class:`~..base.BaseTraceWriter`。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..base import BaseTraceWriter, json_dump as _json


class StageTraceWriter(BaseTraceWriter):
    """把 Stage 与其步骤写入数据库。

    Args:
        connect_factory: 零参可调用对象，返回一个 DB-API 连接。
        dialect: ``sqlite`` 或 ``mysql``。
    """

    # ------------------------------ stage ------------------------------ #
    def start_stage(
        self,
        stage_id: str,
        *,
        trace_id: str,
        name: str,
        workflow_id: str = "",
        seq: int = 1,
        inputs: Mapping[str, Any] | None = None,
    ) -> bool:
        return self._write(
            [
                (
                    "INSERT INTO stage_run_info (stage_run_id, workflow_run_id, trace_id, "
                    "name, seq, inputs_json, upstream_json, outputs_json, dag_json, "
                    "output_keys_json, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        stage_id,
                        workflow_id or None,
                        trace_id,
                        name,
                        seq,
                        _json(dict(inputs or {})),
                        _json({}),
                        _json({}),
                        _json({}),
                        _json([]),
                        self.timestamp(),
                    ),
                )
            ]
        )

    def finish_stage(
        self,
        stage_id: str,
        *,
        status: str,
        duration_ms: int = 0,
        steps: int = 0,
        skipped_steps: int = 0,
        failed_steps: int = 0,
        agent_runs: int = 0,
        output_keys: Sequence[str] = (),
        cached_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        outputs: Mapping[str, Any] | None = None,
        dag: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        written = self._write(
            [
                (
                    "UPDATE stage_run_info SET status=%s, duration_ms=%s, steps=%s, "
                    "skipped_steps=%s, failed_steps=%s, agent_runs=%s, output_keys_json=%s, "
                    "outputs_json=%s, dag_json=%s, "
                    "cached_tokens=%s, reasoning_tokens=%s, "
                    "total_input_tokens=%s, total_output_tokens=%s, total_tokens=%s, "
                    "error=%s, ended_at=%s WHERE stage_run_id=%s",
                    (
                        status,
                        duration_ms,
                        steps,
                        skipped_steps,
                        failed_steps,
                        agent_runs,
                        _json(list(output_keys)),
                        _json(dict(outputs or {})),
                        _json(dict(dag or {})),
                        cached_tokens or 0,
                        reasoning_tokens or 0,
                        prompt_tokens or 0,
                        completion_tokens or 0,
                        total_tokens or 0,
                        error,
                        self.timestamp(),
                        stage_id,
                    ),
                )
            ]
        )
        self._forget(stage_id)
        return written

    # ------------------------------ step ------------------------------ #
    def start_step(
        self,
        step_id: str,
        *,
        stage_id: str,
        name: str,
        kind: str = "agent",
        agent_profile: Mapping[str, Any] | None = None,
    ) -> bool:
        return self._write(
            [
                (
                    "INSERT INTO stage_step_records (step_id, stage_run_id, seq, name, kind, "
                    "agent_profile_json, agent_runs_json, output_keys_json, started_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        step_id,
                        stage_id,
                        self._next_seq(stage_id),
                        name,
                        kind,
                        _json(dict(agent_profile or {})),
                        _json([]),
                        _json([]),
                        self.timestamp(),
                    ),
                )
            ]
        )

    def finish_step(
        self,
        step_id: str,
        *,
        duration_ms: int = 0,
        skipped: bool = False,
        skip_reason: str = "",
        output_keys: Sequence[str] = (),
        agent_runs: Sequence[str] = (),
        total_tokens: int | None = None,
        cached_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        status: str = "",
        error: str | None = None,
    ) -> bool:
        # status 以调用方给的为准：一个 agent 返回 output_invalid 时不抛异常，
        # 只看 error 字段会把这类失败记成 success。
        resolved = status or (
            "error" if error else ("skipped" if skipped else "success")
        )
        return self._write(
            [
                (
                    "UPDATE stage_step_records SET status=%s, duration_ms=%s, skipped=%s, "
                    "skip_reason=%s, output_keys_json=%s, agent_runs_json=%s, "
                    "total_tokens=%s, cached_tokens=%s, reasoning_tokens=%s, "
                    "error=%s, ended_at=%s WHERE step_id=%s",
                    (
                        resolved,
                        duration_ms,
                        1 if skipped else 0,
                        skip_reason or None,
                        _json(list(output_keys)),
                        _json(list(agent_runs)),
                        total_tokens or 0,
                        cached_tokens or 0,
                        reasoning_tokens or 0,
                        error,
                        self.timestamp(),
                        step_id,
                    ),
                )
            ]
        )


__all__ = ["StageTraceWriter"]
