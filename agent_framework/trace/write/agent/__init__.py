"""Agent 运行观测的写入接口。

与 ``llm_write`` 同构：LLM 侧记录单次模型请求，本包记录一次任务的全过程
（几轮工具、纠错几次、什么终态、累计用量）。

两侧靠 Agent 的 ``run_id`` 关联——它会作为 LLM 侧的 ``agent_id`` 下传，因此
"这次 Agent 运行下挂了哪几次模型调用"可以直接 join 查出来::

    SELECT a.agent, a.status, COUNT(l.llm_call_id), SUM(l.total_tokens)
    FROM agent_run_info a LEFT JOIN llm_call_info l ON l.agent_id = a.agent_run_id
    GROUP BY a.agent_run_id
"""

from .contracts import (
    AgentContextSource,
    AgentEventType,
    AgentRunSummary,
    AgentTraceContext,
)
from .observer import AgentTraceObserver
from .writer import AgentTraceWriter

__all__ = [
    "AgentContextSource",
    "AgentEventType",
    "AgentRunSummary",
    "AgentTraceContext",
    "AgentTraceObserver",
    "AgentTraceWriter",
]
