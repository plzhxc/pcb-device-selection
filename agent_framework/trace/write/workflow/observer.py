"""把 Workflow 的观察者回调落库。

鸭子类型读取事件（``getattr``），与 agent_framework 的四层核心无运行时依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class WorkflowTraceContext:
    """一次业务请求的归属。``trace_id`` 由应用给定，贯穿四层。"""

    trace_id: str = ""


ContextSource = WorkflowTraceContext | Callable[[], WorkflowTraceContext] | None


class WorkflowTraceObserver:
    """实现 WorkflowObserver 的两个回调，写 workflow_run_info。"""

    def __init__(self, writer: Any, *, context: ContextSource = None) -> None:
        self._writer = writer
        self._context_source = context

    @property
    def failed_writes(self) -> int:
        return self._writer.failed_writes

    def _trace_id(self) -> str:
        source = self._context_source
        if source is None:
            return ""
        try:
            ctx = source() if callable(source) else source
            return getattr(ctx, "trace_id", "") or ""
        except Exception:
            return ""

    def on_workflow_started(self, event: Any) -> None:
        self._writer.start_workflow(
            getattr(event, "workflow_id", ""),
            trace_id=self._trace_id(),
            name=getattr(event, "name", ""),
            stage_names=tuple(getattr(event, "stages", ()) or ()),
            inputs=dict(getattr(event, "inputs", {}) or {}),
        )

    def on_workflow_ended(self, event: Any) -> None:
        self._writer.finish_workflow(
            getattr(event, "workflow_id", ""),
            status=str(getattr(event, "status", "")),
            duration_ms=int(getattr(event, "duration_ms", 0) or 0),
            stages_total=int(getattr(event, "stages_total", 0) or 0),
            stages_done=int(getattr(event, "stages_done", 0) or 0),
            failed_stage=getattr(event, "failed_stage", None),
            failed_stages=tuple(getattr(event, "failed_stages", ()) or ()),
            prompt_tokens=getattr(event, "prompt_tokens", None),
            completion_tokens=getattr(event, "completion_tokens", None),
            total_tokens=getattr(event, "total_tokens", None),
            cached_tokens=getattr(event, "cached_tokens", None),
            reasoning_tokens=getattr(event, "reasoning_tokens", None),
            error=getattr(event, "error", None),
        )


__all__ = ["WorkflowTraceContext", "WorkflowTraceObserver"]
