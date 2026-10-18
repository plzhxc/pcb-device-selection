"""DAG：一个执行引擎，两层复用。

Workflow 是 Stage 的 DAG，Stage 是 Agent 的 DAG。业务只声明图——节点是能 ``arun``
的东西（Agent 类、实例、一段 Python 或子图），边是顺序或条件——引擎负责拓扑调度、
并行、按集合展开、条件跳过、有界重试、确定性兜底与留痕。

- :mod:`spec`：``DAG`` / ``Node`` / ``Edge``，引用 ``Input`` / ``Upstream`` / ``From`` /
  ``Item`` / ``ItemIndex``，条件谓词 ``succeeded`` / ``failed``；
- :mod:`engine`：``execute_dag`` 与执行器契约 ``NodeExecutor``；
- :mod:`runtime`：把 Agent 类实例化的 ``AgentRuntime``；
- :mod:`stage`：``DAGStage``——用一张图声明的 Stage。
"""

from .engine import (
    NodeAttemptFailed,
    NodeExecutor,
    NodeFailed,
    NodeOutcome,
    NodeUnavailable,
    execute_dag,
    resolve_ref,
)
from .runtime import AgentRuntime, NodeRuntime
from .spec import (
    DAG,
    DAGError,
    Edge,
    From,
    Input,
    Item,
    ItemIndex,
    Node,
    Ref,
    Upstream,
    describe_target,
    dig,
    failed,
    infer_kind,
    succeeded,
)
from .stage import DAGStage, StageNodeExecutor, agent_output, failure_summary

__all__ = [
    "DAG",
    "DAGError",
    "DAGStage",
    "AgentRuntime",
    "Edge",
    "From",
    "Input",
    "Item",
    "ItemIndex",
    "Node",
    "NodeAttemptFailed",
    "NodeExecutor",
    "NodeFailed",
    "NodeOutcome",
    "NodeRuntime",
    "NodeUnavailable",
    "Ref",
    "StageNodeExecutor",
    "Upstream",
    "agent_output",
    "describe_target",
    "dig",
    "execute_dag",
    "failed",
    "failure_summary",
    "infer_kind",
    "resolve_ref",
    "succeeded",
]
