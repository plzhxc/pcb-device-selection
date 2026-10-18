"""Stage 的运行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from ..llm.response import Usage
from .context import StepHandle
from .dag import StageDAG


class StageStatus(str, Enum):
    SUCCESS = "success"
    #: 编排过程中抛出未捕获异常
    ERROR = "error"
    #: 编排本身跑完了，但里面有步骤失败（Agent 返回了非成功结果）。
    #:
    #: 这类失败不抛异常——``step.record(await agent.arun(...))`` 里 agent 返回
    #: ``output_invalid`` 时一切照常继续。不单独区分的话，一条明明有环节失败的
    #: 流水线在观测里和全绿的完全一样。是否继续往下跑由编排代码自己决定
    #: （检查 ``result.ok``），这里只负责如实上报。
    PARTIAL = "partial"
    #: 上游未满足前置条件，本 stage 未执行
    SKIPPED = "skipped"
    #: 被取消（上层超时、Ctrl+C、兄弟任务失败触发的 gather 取消）
    CANCELLED = "cancelled"

    @property
    def ok(self) -> bool:
        return self is StageStatus.SUCCESS


@dataclass(frozen=True)
class StageResult:
    """一个 stage 的运行结果。"""

    stage_id: str
    name: str
    status: StageStatus
    outputs: dict[str, Any] = field(default_factory=dict)
    steps: Sequence[StepHandle] = ()
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    error: BaseException | None = None
    #: Stage 内通过 ctx.input()/ctx.upstream() 实际读取的数据，不是 Workflow
    #: 根参数的机械复制。与 outputs 一起构成可检查的真实数据交接。
    inputs: dict[str, Any] = field(default_factory=dict)
    upstream: dict[str, Any] = field(default_factory=dict)
    #: The exact graph returned by this Stage for this run.
    dag: StageDAG | None = None

    @property
    def ok(self) -> bool:
        """编排是否跑通。有步骤失败（PARTIAL）仍算跑通——产出可用，下游照常。"""

        return self.status in (StageStatus.SUCCESS, StageStatus.PARTIAL)

    def __bool__(self) -> bool:
        return self.ok

    @property
    def skipped_steps(self) -> list[StepHandle]:
        return [s for s in self.steps if s.skipped]

    @property
    def failed_steps(self) -> list[StepHandle]:
        return [s for s in self.steps if s.status() == "failed"]


__all__ = ["StageResult", "StageStatus"]
