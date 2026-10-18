"""Stage / Workflow 运行观测的写入接口。

补齐四层观测链的上两层::

    workflow_run_info → stage_run_info → agent_run_info → llm_call_info

一条 SQL 就能回答"这次流水线，哪个 stage 最慢、哪一步被模板短路、总共烧了多少"。
"""

from .observer import StageTraceContext, StageTraceObserver
from .writer import StageTraceWriter

__all__ = ["StageTraceContext", "StageTraceObserver", "StageTraceWriter"]
