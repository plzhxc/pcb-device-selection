"""只读查询：可视化的唯一数据来源。

**只读是硬约束**。本包不含任何 INSERT / UPDATE / DELETE，也不 import 任何 writer。
可视化连的可以是只读副本，也可以是生产库——无论哪种，它都不可能改坏数据。

查询按"钻取路径"组织，与四层一一对应::

    列表：workflows()  →  完整树：workflow_tree(id)
                                  └─ stages(workflow_id)
                                        └─ steps(stage_id)
                                              └─ agent_runs
                                                    └─ llm_calls(agent_run_id)
"""

from .reader import TraceReader

__all__ = ["TraceReader"]
