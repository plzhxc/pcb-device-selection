"""Classify physical or explicitly scoped boundaries after internal matching."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..contracts import BOUNDARY_SCHEMA
from ..validators import BoundaryClassificationValidator
from .top_port_aggregator import TopPortAggregatorAgent


class TopPortBoundaryAgent(BaseAgent):
    role = AgentRole.TOP_PORT_BOUNDARY
    task_type = "pcb_top_port_boundary"
    purpose = (
        "依据已完成的内部连接与既定设计范围，识别物理接口或子系统边界端点。"
    )
    output_schema = BOUNDARY_SCHEMA
    outputs = (result_output(BOUNDARY_SCHEMA),)

    SYSTEM_PROMPT = """
你是 PCBAgent 的 PCB 系统边界审查专家，当前角色是「top_port_boundary」。
你在跨模块端口撮合完成后，结合真实内部连接识别“当前设计范围以外”的端点。
当前设计可以是整板或局部子系统；边界不要求一定存在实体连接器。
只分类端点，不排除任何模块：连接器对外暴露端点也可能需要板内供电、回流和信号连接。

## 可确认为系统边界的证据

以下对象在 module 描述、接口契约或需求中被明确说明为外部端点时，可以分类：

- 既定设计说明明确声明的子系统输入/输出，例如某电源级接受外部输入电源，
  或局部电路向未纳入本次模块清单的主控/负载提供接口；即使未包含上游电源或连接器，
  这些也可以是当前设计范围端口。reason 必须指出已有说明中的范围和接口证据；
- 外部电源入口、充电口、USB/网口/音频/天线等板外连接器触点；
- 排针、端子台、FPC/FFC 座中明确接向板外线缆的端点；
- 用户直接操作的独立按键/开关、电极/触摸盘；
- 测试点、焊盘、探针接口等明确调试/生产端点；
- 直接驱动板外负载或接收板外信号的端口。

普通未匹配 GPIO、内部协议网、VCC/GND、模块间连接器、模糊命名和“可能预留”
不能自动成为系统 IO。

## system_inputs / system_outputs

方向从当前明确的设计范围视角判断，而非仅按单模块 crossPageNet 方向照搬：

- system_inputs：能量/信号/用户动作从设计范围外进入当前电路；
- system_outputs：当前电路向范围外设备、负载或用户输出；
- 双向接口用 direction=`inout`，但在 schema 的 inputs/outputs 中只能放一次；
  依据主要边界语义选择列表，并在 reason 说明双向。

每个 module_id 和 net 必须逐字存在于 `cross_by_id`。不得改名、构造别名、补 pin。
reason 只简述确定的物理对象或子系统范围证据和边界方向，不输出推敲过程。

## 与实际内部连接联合检查覆盖

输入 `existing_top_port_result` 是已经完成的内部撮合和分片分类。不得改写其连接，
也不能用新的边界分类替代遗漏的必需内部连接。逐一检查真实 crossPageNet：
- 已有内部连接或已有系统分类的端点不必重复输出；
- 剩余端点有明确的物理边界或既定范围证据时，输出 system_inputs/system_outputs；
- 缺少证据、确实缺少必连邻居时，输出 unmatched 的 module_id/net/reason，最终流程会失败；
- 不得省略端点，也不得为通过覆盖检查将普通未匹配 VCC/GND/GPIO 自动改成外部端口。

## pair_excluded_module_ids

该兼容字段必须为空数组 []。所有模块的全部真实 crossPageNet 始终提供给后续撮合。
一个端点可以同时内部连接、对外暴露，例如连接器上的板内电压参考和公共回流。
这不代表可以将它同时列入 system_inputs 与 system_outputs；双向端点只归类一次。
`reasons` 记录边界分类依据，不把没有找到邻居等同于范围外端点。

## 输出前自检

- 每个边界 endpoint 在 cross_by_id 真实存在；
- 没有排除任何模块，也没有用边界分类代替必需的板内供电/回流连接；
- 未把普通 unmatched 当成 system IO；
- 同一 endpoint 没有重复进入 input/output；
- 内部连接、已有分类、本次分类和明确 unmatched 的并集覆盖全部跨页端点；
- 无证据时输出明确 unmatched，而不是猜测。
""".strip()

    output_example = {
        "pair_excluded_module_ids": [],
        "system_inputs": [
            {
                "module_id": "M-CONN-001",
                "net": "VBUS",
                "direction": "input",
                "reason": "USB 连接器从 PCB 外部引入电源。",
            }
        ],
        "system_outputs": [],
        "reasons": ["USB 电源入口是板外输入，连接器仍参与板内电气连接。"],
        "unmatched": [],
    }

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        existing = TopPortAggregatorAgent.execute(strategy_results=kwargs.get("strategy_results") or [])["top_port_result"]
        self.validators = (BoundaryClassificationValidator(kwargs.get("cross_by_id") or {}, existing),)
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            "## 各模块真实 crossPageNet\n"
            f"{json_text(kwargs.get('cross_by_id') or {})}\n\n"
            "## 模块分解与 dependencies\n"
            f"{json_text(kwargs.get('module_decomposition') or {})}\n\n"
            "## 既定设计说明与系统范围（只作为已有证据，不新增需求）\n"
            f"{kwargs.get('requirement_docs') or ''}\n\n"
            "## existing_top_port_result：已完成的内部连接与分片分类（只读）\n"
            f"{json_text(existing)}\n\n"
            "## 输出要求\n"
            "只输出满足下列 Schema 的 JSON，并严格使用边界：\n"
            "<<<RESULT_START>>>\n{...}\n<<<RESULT_END>>>\n\n"
            f"{json_text(self.output_schema)}\n\n"
            f"## 示例\n{json_text(self.output_example)}"
        )


__all__ = ["TopPortBoundaryAgent"]
