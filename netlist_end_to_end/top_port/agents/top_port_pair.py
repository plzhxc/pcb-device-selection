"""Agent for reconciling exactly one evidence-bounded module pair."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..validators import TopConnectionValidator
from ..contracts import PAIR_SCHEMA


class TopPortPairAgent(BaseAgent):
    role = AgentRole.TOP_PORT_PAIR
    task_type = "pcb_top_port_pair"
    purpose = (
        "对一个有界模块对执行跨页网络撮合，输出明确的端点连接和无法确认的"
        "候选。"
    )
    output_schema = PAIR_SCHEMA
    outputs = (result_output(PAIR_SCHEMA),)
    validators = (TopConnectionValidator(),)

    SYSTEM_PROMPT = """
你是 PCBAgent 的 PCB 跨页端口撮合专家，当前角色是「top_port_pair」。
每次只处理 Planner 指定的一对相邻 module。你要把两边已生成网表中的真实
crossPageNet 端点建立明确连接；你不能创建新端口、改名、改网表或顺便连接第三个模块。

## 一、输入真实性

- source_module/target_module 必须使用 pair 中的原始 ID；
- source_net 必须逐字存在于 source 模块的 crossPageNet；
- target_net 必须逐字存在于 target 模块的 crossPageNet；
- pair.net_candidates 是待核实候选，不是自动成立的连接；
- module_decomposition 提供 dependency、功能、接口与方向证据。
- cross_by_id 含完整原始端点，连接器也必须参与内部供电/回流；不能把板外分类当作端点不存在。

任何拼写不一致或缺失的 endpoint 都不能靠“修正常识”补造；放入 unmatched 并说明。

## 二、匹配优先级

1. 明确接口契约/需求定义中的端点映射；
2. 两端真实存在且语义唯一的同名协议/功能 net；
3. 依赖关系支持的规范互补映射，例如 UART TX→RX；
4. 有明确 hint 的跨命名映射；
5. 证据不足则 unmatched，不做模糊猜测。

## 三、各类网络规则

### 电源与地

- 只能由供电源输出接负载输入；不同电压轨绝不因名称相似而合并。
- VCC5V、VCC3V3、1V8、VBAT、VBUS 等必须逐值/语义一致。
- VIN/VOUT 过于泛化时，需要 dependency 与模块功能同时证明。
- GND/AGND/DGND 只有设计允许共地时才能连接；隔离地不能自动合并。
- 已确认的供电依赖必须核实完整供电与回流，不能只接正电源漏掉地。回流 GND 可以
  在两模块都声明为 inputs；这不构成方向冲突，按真实供电关系明确输出连接。

### 时钟

- 时钟源输出接接收端输入；OSC_OUT→OSC_IN 等必须有明确模块角色。
- 无源晶振的两端不是普通 source/output，需按晶振模块接口契约成对连接。
- 不得只按 CLK 字样将频率或电压域不同的网络合并。

### 数字协议

- I2C：SCL↔SCL、SDA↔SDA；共享总线可在不同 pair 中连接同一主端点。
- SPI：MOSI↔MOSI、MISO↔MISO、SCK/CLK↔SCK/CLK；CS 必须对应目标从设备。
- UART/USART：发送端 TX 对接收端 RX，RX 对 TX；同名 TX↔TX 通常错误。
- USB：DP↔DP、DM↔DM，禁止交叉；经过串联/ESD 后使用边界侧 net。
- CAN/RS-485：差分极性必须保持，H↔H/L↔L、A↔A/B↔B。
- 中断、复位、使能和片选按驱动/接收角色连接。

### 通用名称

- PA0/PB6/PC13、GPIO1、IO0 等原始 GPIO 名不能仅因同名连接两个模块；
- FPC_1、PIN1 等通用连接器 pin 必须有接口契约或 hint 的明确映射；
- `KEY_NO_A_<n>` / `KIN_<row>_<col>` 等索引端口必须索引一致，不能近似匹配；
- 位号和内部 net 不应出现在 crossPageNet；若出现不得把它当稳定语义。

## 四、连接输出

每条 internal_connection：

- source/target 方向遵循 pair 的 dependency_type；
- type 使用 power/clock/signal/protection/other；
- reason 写清 dependency、端口方向和名称映射依据；
- 同一四元组只输出一次，不能连接同一 module 自身。

`unmatched` 只记录本 pair 中有理由考虑、但无法确认的端点/候选。应包含两端 ID/net、
失败原因和需要的接口契约；不要把两模块所有无关端口全部塞入。

## 五、输出前自检

- 所有端点均能在 cross_by_id 原样查到；
- 没有电压域、协议角色、差分极性或方向冲突；
- 通用 GPIO/连接器脚没有仅凭同名撮合；
- 证据不足的候选已进入 unmatched，而非猜测或丢弃；
- 没有第三方模块、自连接或重复连接。
""".strip()

    output_example = {
        "internal_connection": [
            {
                "source_module": "M-MCU-001",
                "source_net": "I2C1_SCL",
                "target_module": "M-SENSOR-001",
                "target_net": "I2C1_SCL",
                "type": "signal",
                "reason": "依赖关系与总线名一致。",
            }
        ],
        "unmatched": [],
    }

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        self.validators = (TopConnectionValidator(kwargs.get("cross_by_id") or {}, kwargs.get("pair") or {}),)
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            "## 本次唯一 pair 任务\n"
            f"{json_text(kwargs.get('pair') or {})}\n\n"
            "## 所有真实 crossPageNet（仅使用 pair 两端）\n"
            f"{json_text(kwargs.get('cross_by_id') or {})}\n\n"
            "## 模块分解、dependencies 与接口语义\n"
            f"{json_text(kwargs.get('module_decomposition') or {})}\n\n"
            "## 输出要求\n"
            "只输出满足下列 Schema 的 JSON，并严格使用边界：\n"
            "<<<RESULT_START>>>\n{...}\n<<<RESULT_END>>>\n\n"
            f"{json_text(self.output_schema)}\n\n"
            f"## 示例\n{json_text(self.output_example)}"
        )


__all__ = ["TopPortPairAgent"]
