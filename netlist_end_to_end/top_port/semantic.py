"""An explicit top-port SCL agent; it has no authority to create devices."""

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ..roles import AgentRole
from ..netlist.semantic_presentation import present_scl_repair

TOP_SCL_LANGUAGE = """只输出顶层SCL源码，不输出JSON、Markdown或RESULT标签。
顶层没有任何器件句柄：U1/R1/C1/J1等都未绑定，不能使用pin、connect_device_pin或voltage_domain。
唯一需要的连线写法是连接两个不同module的已声明端口，例如：
connect(module_port("module_a","bus",bit=0),module_port("module_b","bus",bit=0))
例子仅演示语法，使用本次module_interfaces中的实际module/port/bit；不创造或复制端口。
可给module_port端点定义本地句柄再connect，但不能引用任何模块内部器件或脚号。
每次connect的两个端点必须属于不同module；不能在顶层补模块内部连线。
多位接口要逐bit匹配宽度与purpose。external=false端口需明确跨模块对端，external=true是板外边界。
禁止import、循环、条件、属性调用、任意函数、设备创建、取值修改、引脚操作和域声明。
如果仅一个模块且所有端口均external=true，顶层没有跨模块连线，只输出：
# No inter-module connections required
绝不重复模块源码。模块内部器件连线和共享电压域已由独立模块阶段处理。"""


class SemanticTopPortAgent(BaseAgent):
    role = AgentRole.TOP_PORT_PLANNER
    task_type = "pcb_semantic_top_port"

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system("你负责同一块板内已生成模块之间的显式端口连接。只根据需求与module_interfaces的端口语义连接模块。"
                   "不能改变模块内部电路、器件、取值、电压域、端口或脚用途，不能读取参考网表，不能猜跨板线束。"
                   "所有非external端口必须有明确对端；外部端口按需求保留。\n" + TOP_SCL_LANGUAGE)
        present_scl_repair(ctx, kwargs, module=None)


__all__ = ["SemanticTopPortAgent", "TOP_SCL_LANGUAGE"]
