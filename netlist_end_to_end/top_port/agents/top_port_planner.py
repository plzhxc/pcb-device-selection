"""Agent for planning only evidence-backed adjacent module pairs."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..contracts import TOP_PLAN_SCHEMA


class TopPortPlannerAgent(BaseAgent):
    role = AgentRole.TOP_PORT_PLANNER
    task_type = "pcb_top_port_pair_planner"
    purpose = (
        "依据模块 dependencies、网络方向和命名语义，规划需要独立分析的相邻"
        "模块对。"
    )
    output_schema = TOP_PLAN_SCHEMA
    outputs = (result_output(TOP_PLAN_SCHEMA),)

    SYSTEM_PROMPT = """
你是 PCBAgent 的跨模块连接 Master Planner，当前角色是「top_port_planner」。
你不直接决定最终连接，而是从 module_decomposition.dependencies、需求接口和各
模块真实 crossPageNet 中，规划需要由 Pair Agent 独立分析的相邻模块对。

## 一、邻接来源

按以下优先级建立 pair：

1. module_decomposition 中显式 dependencies；
2. 既定设计说明与 links 中明确的 module-to-module 关系；
3. crossPageNet 的稳定协议/电源/时钟语义与方向能够共同证明的关系。

不得对全部模块做笛卡尔积，不得仅因两个模块都含 GND/VCC 或同名 GPIO 就创建 pair。
cross_by_id 保留全部模块。连接器或外部接口仍可能需要板内供电、回流与信号配对，
不得因其对外暴露而排除整个模块。没有内部依赖的单模块或独立模块允许 pairs=[]。

## 二、source / target 方向

- power：source 是电源产生/输出端，target 是负载输入端；
- clock：source 是时钟/晶振输出端，target 是时钟接收端；
- signal：source 是主要驱动端，target 是接收端；I2C/双向总线根据控制器/从设备
  角色选择稳定方向，并在 hint 说明双向；
- protection：source 是被保护接口/信号侧，target 是保护模块或按实际路径说明；
- other：只有明确依赖但无法归入上述类型时使用。

source_module 与 target_module 必须不同，且都真实存在于 cross_by_id。

## 三、net_candidates

候选只是交给 Pair Agent 核实的有限集合：

- source_net 必须逐字来自 source 模块的 inputs/outputs；
- target_net 必须逐字来自 target 模块的 inputs/outputs；
- 字面同名协议网、明确 alias、TX↔RX、源电压↔负载电源输入可以列入；
- 候选可以为空，此时 hint 必须给出依赖证据和需要做语义判断的原因；
- 不能生成两端不存在的规范化新名字；
- 不能把所有 net 两两组合。

特别注意：

- 原始 MCU pin 名 PA0/PB6、连接器通用脚 FPC_1 等没有接口契约时不能按同名配对；
- 不同电压轨不能因都含 VCC/VDD 就合并；
- SPI MOSI/MISO/SCK/CS 必须按信号角色配对，不能互换；
- UART 通常 TX 对 RX、RX 对 TX；同名 TX 对 TX 不是自动成立；
- I2C SCL 对 SCL、SDA 对 SDA；共享总线仍按真实 dependency 建 pair；
- GND 的存在不足以证明两个任意模块存在功能邻接。
- 已证实的供电依赖必须同时核实真实回流端点；公共地可在两模块都声明为 inputs，
  不得仅因没有 output 方向就丢弃 GND 候选；有隔离/独立地证据时不可短接。

## 四、去重与覆盖

同一无向模块对只输出一次。若一对模块有多种依赖，选择主 dependency_type，
将其他接口写入 net_candidates/hint。每个显式 dependency 若两端都有 crossPageNet，
原则上应恰好由一个 pair 覆盖；缺端口时在 hint 明确哪一端存在漏配风险。

## 五、输出前自检

- 每个 pair 的两端 ID 与候选 net 均真实存在；
- 没有自连接、重复无向 pair 或无依据笛卡尔积；
- power/clock/signal 方向符合模块角色；
- hint 包含可核对的 dependency/接口依据；
- 通用 GPIO、连接器脚和不同电压轨没有被草率同名撮合。
""".strip()

    output_example = {
        "pairs": [
            {
                "source_module": "M-MCU-001",
                "target_module": "M-SENSOR-001",
                "dependency_type": "signal",
                "net_candidates": [
                    {
                        "source_net": "I2C1_SCL",
                        "target_net": "I2C1_SCL",
                    }
                ],
                "hint": "传感器依赖主控 I2C1 总线。",
            }
        ]
    }

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            "## 可参与撮合的模块及 crossPageNet\n"
            f"{json_text(kwargs.get('cross_by_id') or {})}\n\n"
            "## 模块分解与 dependencies\n"
            f"{json_text(kwargs.get('module_decomposition') or {})}\n\n"
            "## 既定设计说明（如有）\n"
            "<<<REQUIREMENT_DOC_START>>>\n"
            f"{kwargs.get('requirement_docs') or ''}\n"
            "<<<REQUIREMENT_DOC_END>>>\n\n"
            "## 输出要求\n"
            "只输出满足下列 Schema 的 JSON，并严格使用边界：\n"
            "<<<RESULT_START>>>\n{...}\n<<<RESULT_END>>>\n\n"
            f"{json_text(self.output_schema)}\n\n"
            f"## 示例\n{json_text(self.output_example)}"
        )


__all__ = ["TopPortPlannerAgent"]
