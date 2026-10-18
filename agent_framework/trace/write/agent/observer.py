"""把 Agent Framework 的观察者回调落库。

与 Agent Framework 之间只有结构约定，没有运行时导入——鸭子类型读取事件字段，
因此本平台可独立部署，也能采集任何产出同形状事件的调用方。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .contracts import AgentContextSource, AgentRunSummary, AgentTraceContext
from .writer import AgentTraceWriter

if TYPE_CHECKING:  # 仅供类型检查
    from agent_framework.agent.observer import RunEnded, RunStarted, StepRecorded


class AgentTraceObserver:
    """实现 Agent Framework 的三个回调并写入数据库。

    上下文（trace/flow/stage）不在框架视野内，由应用注入。允许传可调用对象：
    同一个 Agent 在不同阶段间复用时，每次现取才能记到正确的归属。
    """

    def __init__(
        self,
        writer: AgentTraceWriter,
        *,
        context: AgentContextSource = None,
    ) -> None:
        if not isinstance(writer, AgentTraceWriter):
            raise TypeError("writer must be an AgentTraceWriter")
        if context is not None and not (
            isinstance(context, AgentTraceContext) or callable(context)
        ):
            raise TypeError("context must be AgentTraceContext, a callable, or None")
        self._writer = writer
        self._context_source = context

    @property
    def failed_writes(self) -> int:
        """写库失败次数；健康检查读它，而不是靠异常发现问题。"""

        return self._writer.failed_writes

    def _context(self) -> AgentTraceContext:
        source = self._context_source
        if source is None:
            return AgentTraceContext()
        if callable(source):
            try:
                return source() or AgentTraceContext()
            except Exception:
                return AgentTraceContext()
        return source

    # ----------------------- 协议实现 ----------------------- #
    def on_run_started(self, event: "RunStarted") -> None:
        self._writer.start_run(
            getattr(event, "run_id", ""),
            agent=getattr(event, "agent", ""),
            context=self._context(),
            task_type=getattr(event, "task_type", "") or "",
            turn_no=int(getattr(event, "turn", 1) or 1),
            session_id=getattr(event, "session_id", "") or "",
            inputs=dict(getattr(event, "inputs", {}) or {}),
        )

    def on_step(self, event: "StepRecorded") -> None:
        self._writer.record_step(
            getattr(event, "run_id", ""),
            kind=str(getattr(event, "kind", "")),
            index=int(getattr(event, "index", 1) or 1),
            detail=dict(getattr(event, "detail", {}) or {}),
        )

    def on_run_ended(self, event: "RunEnded") -> None:
        self._writer.finish_run(
            getattr(event, "run_id", ""),
            AgentRunSummary(
                status=str(getattr(event, "status", "")),
                duration_ms=int(getattr(event, "duration_ms", 0) or 0),
                tool_iterations=int(getattr(event, "iterations", 0) or 0),
                output_retries=int(getattr(event, "output_retries", 0) or 0),
                llm_calls=int(getattr(event, "llm_calls", 0) or 0),
                tool_calls=int(getattr(event, "tool_calls", 0) or 0),
                tool_failures=int(getattr(event, "tool_failures", 0) or 0),
                prompt_tokens=getattr(event, "prompt_tokens", None),
                completion_tokens=getattr(event, "completion_tokens", None),
                total_tokens=getattr(event, "total_tokens", None),
                cached_tokens=getattr(event, "cached_tokens", None),
                reasoning_tokens=getattr(event, "reasoning_tokens", None),
                repairs=dict(getattr(event, "repairs", {}) or {}),
                output_keys=tuple(getattr(event, "output_keys", ()) or ()),
                error=getattr(event, "error", None),
                session_id=getattr(event, "session_id", "") or "",
                session_turns=int(getattr(event, "session_turns", 0) or 0),
                session_llm_calls=int(getattr(event, "session_llm_calls", 0) or 0),
                session_total_tokens=getattr(event, "session_total_tokens", None),
            ),
        )


__all__ = ["AgentTraceObserver"]
