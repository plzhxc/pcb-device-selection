"""Stage 的观察者协议。

与 LLM / Agent 两层同构：只定义"发生了什么"，落库交给
``agent_framework.trace.stage``。四层观测由 id 串成一条链::

    workflow_run  →  stage_run  →  agent_run  →  llm_call

本层能回答下层答不了的问题：这个 stage 里 agent 是怎么排布的、哪一步被模板短路
跳过了、纯 Python 的确定性处理吃掉了多少时间、失败发生在哪一步。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

# 归属标识统一在 trace.write.pipeline 里定义（那是叶子模块，依赖方向才不会反）
from ..trace.write.pipeline import _current_stage, current_stage_id


# --------------------------------------------------------------------------- #
# Stage
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StageStarted:
    stage_id: str
    name: str
    workflow_id: str = ""
    index: int = 1
    #: Stage.execute() 入口处的完整输入快照。Workflow 直接参数与已有 Stage 产出
    #: 已合并为一个数据面，不在 Trace 中制造两套“输入”概念。
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepStarted:
    stage_id: str
    step_id: str
    name: str
    index: int
    #: ``agent`` / ``python`` / ``parallel`` —— 纯 Python 步骤同样记录，
    #: 否则确定性处理的耗时会变成时间黑洞
    kind: str = "agent"
    #: Agent 配置快照：类名、工具、提示词指纹
    agent: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepEnded:
    stage_id: str
    step_id: str
    name: str
    index: int
    kind: str = "agent"
    duration_ms: int = 0
    #: 被短路跳过（命中模板、确定性策略）。短路是优化也是缺陷来源，必须留痕
    skipped: bool = False
    skip_reason: str = ""
    output_keys: Sequence[str] = ()
    #: 本步骤触发的 agent 运行 id，用于串到 agent 层观测
    agent_runs: Sequence[str] = ()
    #: 步骤终态：ok / failed / cancelled / skipped。只有 error 字段的话，"agent 跑完了但
    #: 结果是 output_invalid" 这种失败在观测里和成功完全一样——没有异常抛出，
    #: error 就是空的。
    status: str = "ok"
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: 命中提示缓存的 token 数。省钱效果全靠它，只搬三个字段就永远看不到
    cached_tokens: int | None = None
    #: 思考过程消耗的 token 数
    reasoning_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class StageEnded:
    stage_id: str
    name: str
    status: str
    duration_ms: int = 0
    steps: int = 0
    skipped_steps: int = 0
    #: 有多少步骤的 Agent 返回了非成功结果（这类失败不抛异常）
    failed_steps: int = 0
    agent_runs: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: 命中提示缓存的 token 数。省钱效果全靠它，只搬三个字段就永远看不到
    cached_tokens: int | None = None
    #: 思考过程消耗的 token 数
    reasoning_tokens: int | None = None
    #: Stage 退出时通过 ctx.emit() 发布的完整输出快照。
    outputs: dict[str, Any] = field(default_factory=dict)
    #: Stage-produced actual DAG. Trace persists it verbatim and never infers
    #: dependency edges from step sequence or timestamps.
    dag: dict[str, Any] = field(default_factory=dict)
    output_keys: Sequence[str] = ()
    error: str | None = None


@runtime_checkable
class StageObserver(Protocol):
    def on_stage_started(self, event: StageStarted) -> None: ...
    def on_step_started(self, event: StepStarted) -> None: ...
    def on_step_ended(self, event: StepEnded) -> None: ...
    def on_stage_ended(self, event: StageEnded) -> None: ...


class NullStageObserver:
    __slots__ = ()

    def on_stage_started(self, event: StageStarted) -> None:
        return None

    def on_step_started(self, event: StepStarted) -> None:
        return None

    def on_step_ended(self, event: StepEnded) -> None:
        return None

    def on_stage_ended(self, event: StageEnded) -> None:
        return None


NULL_STAGE_OBSERVER = NullStageObserver()


__all__ = [
    "NULL_STAGE_OBSERVER",
    "NullStageObserver",
    "StageEnded",
    "StageObserver",
    "StageStarted",
    "StepEnded",
    "StepStarted",
    "_current_stage",
    "current_stage_id",
]
