"""Agent 运行的观察者协议。

与 LLM 层的观察者同构：本模块只定义"发生了什么"，不定义"记到哪里"。落库由外部
实现者接管（见 ``agent_framework.trace.agent``），框架本身零依赖。

**它和 LLM 层观测的关系**：LLM 层记录的是单次请求（模型、token、耗时、重试），
Agent 层记录的是一次任务的全过程（几轮对话、跑了哪些工具、纠错几次、最终什么
终态）。两者靠 :attr:`RunStarted.run_id` 关联——该 id 会作为 LLM 侧的 ``agent_id``
下传，因此"这次 Agent 运行下挂了哪几次模型调用"是可查的。

只有 Agent 层能回答的问题，正是最难排查的那些：为什么跑了 8 轮工具还没收敛、
纠错重试到底在纠什么、结构化输出是靠容错兜回来的还是模型本来就对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence, runtime_checkable

# 归属标识统一定义在 trace.write.pipeline（叶子模块），各层只取用——
# 反过来放在各层里会让 pipeline 依赖它们，产生循环导入。
from ..trace.write.pipeline import _current_run, current_run_id


#: 一轮之内的推进类型
StepKind = Literal[
    "tools",
    "tool_started",
    "tool_completed",
    "tool_failed",
    "tool_denied",
    "tool_timeout",
    "tool_cancelled",
    "correction",
    "validation",
    "plan",
    "subs",
    "merge",
]



@dataclass(frozen=True)
class RunStarted:
    """一轮运行开始。

    Attributes:
        run_id: 本次运行的标识；同时作为 LLM 侧的 ``agent_id`` 下传。
        agent: Agent 类名。
        task_type: 任务类型标识。
        turn: 会话内第几轮，从 1 开始。
        session_id: 所属会话；多轮追问时同一会话的多次运行共享它。
        inputs: ``prepare`` 收到的入参摘要。
    """

    run_id: str
    agent: str
    task_type: str = ""
    turn: int = 1
    session_id: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepRecorded:
    """一轮之内的一次推进。

    工具执行与输出纠错各自计数，因为它们指向完全不同的问题：前者多说明任务拆得
    不好或工具描述不清，后者说明提示词与 Schema 没对齐。
    """

    run_id: str
    kind: StepKind
    #: 该类推进的序号，从 1 开始
    index: int
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunEnded:
    """一轮运行结束。"""

    run_id: str
    status: str
    duration_ms: int = 0
    #: 工具循环轮数
    iterations: int = 0
    #: 输出纠错次数
    output_retries: int = 0
    #: 业务规则重试次数
    validation_retries: int = 0
    #: 校验发现的问题码（含 warning），空表示全通过
    validation_codes: Sequence[str] = ()
    #: 实际发起的 LLM 调用次数
    llm_calls: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    #: 执行过的工具调用数与其中失败数
    tool_calls: int = 0
    tool_failures: int = 0
    #: 为拿到结构化输出而施加的修复；持续出现说明提示词该改了
    repairs: dict[str, list[str]] = field(default_factory=dict)
    #: 结构化输出的键名（不含内容，避免观测数据膨胀）
    output_keys: Sequence[str] = ()
    error: str | None = None
    #: 所属会话，以及该会话到此刻的累计值。多轮追问时用它做会话级汇总，
    #: 不必等会话显式结束——每轮结束都会把最新累计带出来。
    session_id: str = ""
    session_turns: int = 0
    session_llm_calls: int = 0
    session_total_tokens: int | None = None


@runtime_checkable
class AgentObserver(Protocol):
    """Agent 运行生命周期观察者。

    实现者应自行吞掉异常；框架侧也会再兜一层，观测失败绝不影响主流程。
    """

    def on_run_started(self, event: RunStarted) -> None: ...

    def on_step(self, event: StepRecorded) -> None: ...

    def on_run_ended(self, event: RunEnded) -> None: ...


class NullAgentObserver:
    """默认观察者：什么都不做。"""

    __slots__ = ()

    def on_run_started(self, event: RunStarted) -> None:
        return None

    def on_step(self, event: StepRecorded) -> None:
        return None

    def on_run_ended(self, event: RunEnded) -> None:
        return None


NULL_AGENT_OBSERVER = NullAgentObserver()


__all__ = [
    "NULL_AGENT_OBSERVER",
    "_current_run",
    "current_run_id",
    "AgentObserver",
    "NullAgentObserver",
    "RunEnded",
    "RunStarted",
    "StepKind",
    "StepRecorded",
]
