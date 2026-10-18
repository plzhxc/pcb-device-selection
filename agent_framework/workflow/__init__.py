"""Workflow：把业务环节串成一次完整的业务请求。

**定位**：Workflow 是 Stage 的 DAG，与 Stage 内的 Agent 图用**同一个执行引擎**
（:mod:`agent_framework.dag.engine`）。不给边时按顺序串成线性链——这是当前唯一的
用法；需要并行或条件 stage 时只加边，不改引擎。它只碰 :meth:`Stage.execute` 和
``stage.name`` 两个公开接口，完全不关心环节内部怎么跑。

独立成包是因为它与 Stage 是**使用者关系而非同类关系**。它独有的能力是**断点续跑**
——可重入的边界只在 stage 之间成立::

    workflow = Workflow("pcb", [需求意图(), 器件选型(), 网表(), 顶层端口()],
                        runtime=AgentRuntime(client, config))

    result = await workflow.arun(user_input="...")           # 全跑
    result = await workflow.arun(                            # 从第二步续跑
        start_from="器件选型",
        upstream={"需求意图": 已有产出},
        user_input="...",
    )
"""

from .base import Workflow
from .observer import (
    NULL_WORKFLOW_OBSERVER,
    NullWorkflowObserver,
    WorkflowEnded,
    WorkflowObserver,
    WorkflowStarted,
    current_workflow_id,
)
from .result import WorkflowResult, WorkflowStatus

__all__ = [
    "NULL_WORKFLOW_OBSERVER",
    "NullWorkflowObserver",
    "Workflow",
    "WorkflowEnded",
    "WorkflowObserver",
    "WorkflowResult",
    "WorkflowStarted",
    "WorkflowStatus",
    "current_workflow_id",
]
