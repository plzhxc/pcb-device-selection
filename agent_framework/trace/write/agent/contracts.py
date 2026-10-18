"""Agent 观测的值对象。

只用标准库，与 Agent Framework 之间没有运行时依赖：观察者按结构约定读取事件，
因此本平台可以独立部署，也能采集任何产出同形状事件的调用方。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Sequence


class AgentEventType(str, Enum):
    """写入 ``agent_run_records`` 的生命周期事件。"""

    RUN_STARTED = "run_started"
    #: 模型请求了工具，本轮执行了一批
    TOOLS_EXECUTED = "tools_executed"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    TOOL_DENIED = "tool_denied"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_CANCELLED = "tool_cancelled"
    #: 输出不合格式，把错误发回让模型自我纠正
    OUTPUT_CORRECTION = "output_correction"
    #: 格式正确但业务规则未通过，反馈给模型重试
    VALIDATION_RETRY = "validation_retry"
    #: HierarchicalAgent 的计划、子任务汇总与确定性合并
    HIERARCHY_PLAN = "hierarchy_plan"
    HIERARCHY_SUBTASKS = "hierarchy_subtasks"
    HIERARCHY_MERGE = "hierarchy_merge"
    #: 扩展 Agent 的通用编排记录；未知 kind 不能冒充输出纠错
    ORCHESTRATION_NOTE = "orchestration_note"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"


#: 非成功终态；用于决定记 ``RUN_FAILED`` 而非 ``RUN_COMPLETED``
FAILED_STATUSES = frozenset(
    {
        "tool_limit",
        "output_invalid",
        "validation_failed",
        "truncated",
        "error",
        "cancelled",
        "content_filtered",
        "plan_invalid",
        "merge_failed",
    }
)


@dataclass(frozen=True, slots=True)
class AgentTraceContext:
    """一次 Agent 运行的归属。

    ``trace_id`` 是贯穿一次业务请求的主线；``flow_id`` / ``stage_id`` 对应上层
    编排的流程与阶段。这三者由应用提供——框架不知道业务怎么划分阶段。
    """

    trace_id: str = ""
    flow_id: str | None = None
    stage_id: str | None = None


#: 上下文来源：定值，或每次现取的可调用对象（同一 Agent 跨阶段复用时需要后者）
AgentContextSource = AgentTraceContext | Callable[[], AgentTraceContext] | None


@dataclass(frozen=True, slots=True)
class AgentRunSummary:
    """一次运行结束时的汇总。

    这些数字是排查的入口：``tool_iterations`` 高说明任务拆解或工具描述有问题，
    ``output_retries`` 高说明提示词与 Schema 没对齐，``repairs`` 反复出现说明
    容错在长期替提示词兜底。
    """

    status: str
    duration_ms: int = 0
    tool_iterations: int = 0
    output_retries: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: 命中提示缓存 / 思考过程各花了多少。省钱效果与推理成本全看这两项，
    #: 只落 in/out/total 三个数的话，它们在库里永远不存在。
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    repairs: Mapping[str, Sequence[str]] = field(default_factory=dict)
    output_keys: Sequence[str] = ()
    error: str | None = None
    #: 所属会话及其到此刻的累计。会话行每轮结束就刷新，因此不需要"会话结束"
    #: 这个显式信号——多轮追问本来就没有明确终点。
    session_id: str = ""
    session_turns: int = 0
    session_llm_calls: int = 0
    session_total_tokens: int | None = None

    @property
    def failed(self) -> bool:
        return self.status in FAILED_STATUSES


def utc_now() -> datetime:
    """当前 UTC 时间；库内统一用它，避免跨时区比较出错。"""

    return datetime.now(timezone.utc)


def jsonable(value: Any) -> Any:
    """转成可序列化结构；无法处理的降级为字符串。"""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    return str(value)


__all__ = [
    "FAILED_STATUSES",
    "AgentContextSource",
    "AgentEventType",
    "AgentRunSummary",
    "AgentTraceContext",
    "jsonable",
    "utc_now",
]
