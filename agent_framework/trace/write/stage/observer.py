"""把 Stage 的观察者回调落库。

鸭子类型读取事件（``getattr``），与 agent_framework 的四层核心无运行时依赖——
事件多一个字段不会让这里报错，少一个也不会。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class StageTraceContext:
    """一个业务环节的归属。``trace_id`` 是贯穿一次业务请求的主线。"""

    trace_id: str = ""


ContextSource = StageTraceContext | Callable[[], StageTraceContext] | None


class StageTraceObserver:
    """实现 StageObserver 的四个回调，写 stage_run_info 与 stage_step_records。"""

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

    # ----------------------- stage ----------------------- #
    def on_stage_started(self, event: Any) -> None:
        self._writer.start_stage(
            getattr(event, "stage_id", ""),
            trace_id=self._trace_id(),
            name=getattr(event, "name", ""),
            workflow_id=getattr(event, "workflow_id", "") or "",
            seq=int(getattr(event, "index", 1) or 1),
            inputs=dict(getattr(event, "inputs", {}) or {}),
        )

    def on_stage_ended(self, event: Any) -> None:
        self._writer.finish_stage(
            getattr(event, "stage_id", ""),
            status=str(getattr(event, "status", "")),
            duration_ms=int(getattr(event, "duration_ms", 0) or 0),
            steps=int(getattr(event, "steps", 0) or 0),
            skipped_steps=int(getattr(event, "skipped_steps", 0) or 0),
            failed_steps=int(getattr(event, "failed_steps", 0) or 0),
            agent_runs=int(getattr(event, "agent_runs", 0) or 0),
            output_keys=tuple(getattr(event, "output_keys", ()) or ()),
            prompt_tokens=getattr(event, "prompt_tokens", None),
            completion_tokens=getattr(event, "completion_tokens", None),
            total_tokens=getattr(event, "total_tokens", None),
            cached_tokens=getattr(event, "cached_tokens", None),
            reasoning_tokens=getattr(event, "reasoning_tokens", None),
            outputs=dict(getattr(event, "outputs", {}) or {}),
            dag=dict(getattr(event, "dag", {}) or {}),
            error=getattr(event, "error", None),
        )

    # ----------------------- step ----------------------- #
    def on_step_started(self, event: Any) -> None:
        self._writer.start_step(
            getattr(event, "step_id", ""),
            stage_id=getattr(event, "stage_id", ""),
            name=getattr(event, "name", ""),
            kind=str(getattr(event, "kind", "agent")),
            agent_profile=dict(getattr(event, "agent", {}) or {}),
        )

    def on_step_ended(self, event: Any) -> None:
        self._writer.finish_step(
            getattr(event, "step_id", ""),
            duration_ms=int(getattr(event, "duration_ms", 0) or 0),
            skipped=bool(getattr(event, "skipped", False)),
            skip_reason=getattr(event, "skip_reason", "") or "",
            output_keys=tuple(getattr(event, "output_keys", ()) or ()),
            agent_runs=tuple(getattr(event, "agent_runs", ()) or ()),
            total_tokens=getattr(event, "total_tokens", None),
            cached_tokens=getattr(event, "cached_tokens", None),
            reasoning_tokens=getattr(event, "reasoning_tokens", None),
            status=getattr(event, "status", "") or "",
            error=getattr(event, "error", None),
        )


__all__ = ["StageTraceContext", "StageTraceObserver"]
