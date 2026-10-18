"""Workflow 的运行结果。

一次业务请求的终态。粒度刻意粗：``stages`` 里挂着每个环节的
:class:`~..stage.result.StageResult`，要细节往里钻，不在这层平铺。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from ..llm.response import Usage
from ..stage.result import StageResult


class WorkflowStatus(str, Enum):
    SUCCESS = "success"
    #: 某个 stage 失败且被判定为必须中断
    FAILED = "failed"
    #: 按 start_from / stop_after 只跑了一段
    PARTIAL = "partial"

    @property
    def ok(self) -> bool:
        return self is WorkflowStatus.SUCCESS


@dataclass(frozen=True)
class WorkflowResult:
    """一次完整流水线运行的结果。"""

    workflow_id: str
    name: str
    status: WorkflowStatus
    stages: Sequence[StageResult] = ()
    outputs: dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    #: 失败发生在哪个 stage —— 五阶段流水线排查的第一个问题
    failed_stage: str | None = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.status.ok

    def __bool__(self) -> bool:
        return self.ok

    def stage(self, name: str) -> StageResult | None:
        for result in self.stages:
            if result.name == name:
                return result
        return None


__all__ = ["WorkflowResult", "WorkflowStatus"]
