"""Workflow 的观察者协议。

一次业务请求一行（``workflow_run_info``）。字段回答的是"这次请求在哪儿断的、
还能不能续"——环节内部的细节属于 stage 层，不在这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

# 归属标识统一在 trace.write.pipeline 里定义（那是叶子模块，依赖方向才不会反）
from ..trace.write.pipeline import _current_workflow, current_workflow_id





@dataclass(frozen=True)
class WorkflowStarted:
    workflow_id: str
    name: str
    stages: Sequence[str] = ()
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowEnded:
    workflow_id: str
    name: str
    status: str
    duration_ms: int = 0
    stages_total: int = 0
    stages_done: int = 0
    #: **第一个**失败的 stage —— 五阶段流水线里这是排查的第一个问题；
    #: 后续失败往往只是它的连锁反应
    failed_stage: str | None = None
    #: 全部失败的 stage（``stop_on_error=False`` 时可能有多个）
    failed_stages: Sequence[str] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: 命中提示缓存的 token 数。省钱效果全靠它，只搬三个字段就永远看不到
    cached_tokens: int | None = None
    #: 思考过程消耗的 token 数
    reasoning_tokens: int | None = None
    error: str | None = None


@runtime_checkable
class WorkflowObserver(Protocol):
    def on_workflow_started(self, event: WorkflowStarted) -> None: ...
    def on_workflow_ended(self, event: WorkflowEnded) -> None: ...


class NullWorkflowObserver:
    __slots__ = ()

    def on_workflow_started(self, event: WorkflowStarted) -> None: return None
    def on_workflow_ended(self, event: WorkflowEnded) -> None: return None


NULL_WORKFLOW_OBSERVER = NullWorkflowObserver()


__all__ = [
    "NULL_WORKFLOW_OBSERVER",
    "NullWorkflowObserver",
    "WorkflowEnded",
    "WorkflowObserver",
    "WorkflowStarted",
    "_current_workflow",
    "current_workflow_id",
]
