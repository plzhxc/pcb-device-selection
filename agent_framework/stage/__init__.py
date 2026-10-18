"""Stage：把一组 Agent 编排成一个能交付契约的业务环节。

**定位**：Stage 是 Agent 的 DAG。业务声明一张图——节点是 Agent 类、Agent 实例或一段
Python，边是顺序或条件——由 :mod:`agent_framework.dag` 的引擎执行：拓扑调度、无依赖
节点自动并行、条件边不触发即显式跳过、节点内有界重试、可选节点失败不拖垮整图。
声明的前驱直接成为步骤的 ``depends_on``，因此 Trace 里的实际 DAG 就是声明的 DAG。

之所以要有这一层，最硬的理由是：一个业务环节里往往有一半时间花在非 LLM 的确定性
处理上。每个节点都是一个步骤埋点，这些时间在观测里才不是黑洞。

串联多个 stage 是上一层的事，见 :mod:`agent_framework.workflow`。
"""

from .base import Stage
from .context import StageContext, StepHandle, agent_profile
from .dag import StageDAG, StageDAGEdge, StageDAGError, StageDAGNode
from .observer import (
    NULL_STAGE_OBSERVER,
    NullStageObserver,
    StageEnded,
    StageObserver,
    StageStarted,
    StepEnded,
    StepStarted,
    current_stage_id,
)
from .result import StageResult, StageStatus

__all__ = [
    "NULL_STAGE_OBSERVER",
    "NullStageObserver",
    "Stage",
    "StageContext",
    "StageDAG",
    "StageDAGEdge",
    "StageDAGError",
    "StageDAGNode",
    "StageEnded",
    "StageObserver",
    "StageResult",
    "StageStarted",
    "StageStatus",
    "StepEnded",
    "StepHandle",
    "StepStarted",
    "agent_profile",
    "current_stage_id",
]
