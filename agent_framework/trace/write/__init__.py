"""Trace 写入侧。

框架事件在这里被转换为数据库记录，并通过共享 FIFO 后台队列写入数据库。
本包只依赖 :mod:`agent_framework.trace.database`，不依赖读取或可视化代码。
"""

from .agent import (
    AgentContextSource,
    AgentEventType,
    AgentRunSummary,
    AgentTraceContext,
    AgentTraceObserver,
    AgentTraceWriter,
)
from .base import (
    BaseTraceWriter,
    TraceWriteDispatcher,
    TraceWriteFuture,
    TraceWriterClosed,
)
from .llm import (
    ContextSource,
    LLMCallConflict,
    LLMCallNotFound,
    LLMCallTerminal,
    LLMRunRecord,
    LLMTraceObserver,
    LLMTraceWriter,
    LLMWriteError,
    RecordConflict,
    RecordLevel,
    RunEventType,
    TokenSource,
    TokenUsage,
    TraceContext,
    WriteReceipt,
)
from .pipeline import (
    current_run_id,
    current_stage_id,
    current_workflow_id,
    digest,
    emit,
    observer_failures,
    reset_observer_failures,
)
from .runtime import (
    DISABLED,
    TraceRuntime,
    current,
    enabled,
    flush,
    init,
    shutdown,
)
from .stage import StageTraceContext, StageTraceObserver, StageTraceWriter
from .workflow import (
    WorkflowTraceContext,
    WorkflowTraceObserver,
    WorkflowTraceWriter,
)

__all__ = [
    "DISABLED",
    "AgentContextSource",
    "AgentEventType",
    "AgentRunSummary",
    "AgentTraceContext",
    "AgentTraceObserver",
    "AgentTraceWriter",
    "BaseTraceWriter",
    "ContextSource",
    "LLMCallConflict",
    "LLMCallNotFound",
    "LLMCallTerminal",
    "LLMRunRecord",
    "LLMTraceObserver",
    "LLMTraceWriter",
    "LLMWriteError",
    "RecordConflict",
    "RecordLevel",
    "RunEventType",
    "StageTraceContext",
    "StageTraceObserver",
    "StageTraceWriter",
    "TokenSource",
    "TokenUsage",
    "TraceContext",
    "TraceRuntime",
    "TraceWriteDispatcher",
    "TraceWriteFuture",
    "TraceWriterClosed",
    "WorkflowTraceContext",
    "WorkflowTraceObserver",
    "WorkflowTraceWriter",
    "WriteReceipt",
    "current",
    "current_run_id",
    "current_stage_id",
    "current_workflow_id",
    "digest",
    "emit",
    "enabled",
    "flush",
    "init",
    "observer_failures",
    "reset_observer_failures",
    "shutdown",
]
