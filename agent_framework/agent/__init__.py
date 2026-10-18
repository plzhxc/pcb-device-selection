"""Agent 层：编排 LLM、工具与结构化输出。

    from agent_framework.agent import BaseAgent, AgentConfig, Context

    class Writer(BaseAgent):
        outputs = []                      # 固定的声明在类上

        def prepare(self, ctx: Context, *, topic: str) -> None:
            ctx.system("你是专家。")       # 这次运行才准备的
            ctx.user(f"写一段关于{topic}的说明")

    result = Writer(client, AgentConfig(params)).run(topic="电源设计")
    result.ok, result.text, result.values, result.usage

配置在这一层落地：LLM 层只接受已确定的 ``CallParams``，把 YAML 翻译成
:class:`AgentConfig` 是应用的职责，框架不依赖任何配置文件格式。
"""

from .base import BaseAgent
from .config import AgentConfig, RetryPolicy
from .context import Context, Knowledge
from .observer import (
    NULL_AGENT_OBSERVER,
    AgentObserver,
    NullAgentObserver,
    RunEnded,
    RunStarted,
    StepRecorded,
    current_run_id,
)
from .hierarchical import HierarchicalAgent, HierarchicalResult, SubRun
from .session import Session
from .result import AgentResult, AgentStatus, accumulate_usage
from .validation import (
    ValidationContext,
    ValidationIssue,
    ValidationReport,
    Validator,
)

__all__ = [
    "NULL_AGENT_OBSERVER",
    "AgentConfig",
    "AgentObserver",
    "AgentResult",
    "AgentStatus",
    "BaseAgent",
    "Context",
    "Knowledge",
    "HierarchicalAgent",
    "HierarchicalResult",
    "RetryPolicy",
    "NullAgentObserver",
    "RunEnded",
    "RunStarted",
    "Session",
    "StepRecorded",
    "SubRun",
    "ValidationContext",
    "ValidationIssue",
    "ValidationReport",
    "Validator",
    "accumulate_usage",
    "current_run_id",
]
