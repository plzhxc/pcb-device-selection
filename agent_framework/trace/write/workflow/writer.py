"""Workflow 运行的落库。

一张表 ``workflow_run_info``，一次业务请求一行。粒度刻意粗：环节内部的细节在
``stage_run_info`` / ``stage_step_records``，靠 ``workflow_run_id`` 往下钻。

事务、失败计数都在 :class:`~..base.BaseTraceWriter`。
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..base import BaseTraceWriter, json_dump as _json


class WorkflowTraceWriter(BaseTraceWriter):
    """把一次流水线运行写入数据库。

    Args:
        connect_factory: 零参可调用对象，返回一个 DB-API 连接。
        dialect: ``sqlite`` 或 ``mysql``。
    """

    def start_workflow(
        self, workflow_id: str, *, trace_id: str, name: str,
        stage_names: Sequence[str] = (), inputs: Mapping[str, Any] | None = None,
    ) -> bool:
        return self._write([(
            "INSERT INTO workflow_run_info (workflow_run_id, trace_id, name, "
            "inputs_json, stage_names_json, failed_stages_json, stages_total, "
            "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (workflow_id, trace_id, name, _json(dict(inputs or {})),
             _json(list(stage_names)), _json([]), len(stage_names),
             self.timestamp()),
        )])

    def finish_workflow(
        self, workflow_id: str, *, status: str, duration_ms: int = 0,
        stages_total: int = 0, stages_done: int = 0,
        failed_stage: str | None = None, failed_stages: Sequence[str] = (),
        prompt_tokens: int | None = None, completion_tokens: int | None = None,
        cached_tokens: int | None = None, reasoning_tokens: int | None = None,
        total_tokens: int | None = None, error: str | None = None,
    ) -> bool:
        """收尾。

        ``failed_stage`` 记的是**第一个**失败的环节——后续失败多半是它的连锁反应，
        记最后一个会把排查引到最末端那个"受害者"身上。全部失败的环节在
        ``failed_stages_json`` 里，供 ``stop_on_error=False`` 的场景查看。
        """

        return self._write([(
            "UPDATE workflow_run_info SET status=%s, duration_ms=%s, stages_total=%s, "
            "stages_done=%s, failed_stage=%s, failed_stages_json=%s, "
            "cached_tokens=%s, reasoning_tokens=%s, total_input_tokens=%s, "
            "total_output_tokens=%s, total_tokens=%s, error=%s, ended_at=%s "
            "WHERE workflow_run_id=%s",
            (status, duration_ms, stages_total, stages_done, failed_stage,
             _json(list(failed_stages)),
             cached_tokens or 0, reasoning_tokens or 0,
             prompt_tokens or 0, completion_tokens or 0, total_tokens or 0,
             error, self.timestamp(), workflow_id),
        )])


__all__ = ["WorkflowTraceWriter"]
