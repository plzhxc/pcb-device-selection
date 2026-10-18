"""Agent for independently classifying one module's cross-page endpoints."""

from __future__ import annotations

from typing import Any

from agent_framework.agent import BaseAgent, Context

from ...schemas import json_text, result_output
from ...roles import AgentRole
from ..contracts import SHARD_SCHEMA


class TopPortShardAgent(BaseAgent):
    role = AgentRole.TOP_PORT_SHARD
    task_type = "pcb_top_port_shard"
    purpose = (
        "在层级配对不可用时独立分析一个模块的每条跨页网络，形成邻居连接候选"
        "或系统暴露端口候选。"
    )
    output_schema = SHARD_SCHEMA
    outputs = (result_output(SHARD_SCHEMA),)

    SYSTEM_PROMPT = """
你是 PCBAgent 的 PCB 跨页端口分片分析专家，当前角色是「top_port_shard」。
这是层级 pair 流程不可用时的独立 fallback：你一次只分析一个 module 的全部
crossPageNet，并为每个本地端口给出“邻居连接候选”或“暴露端口分类”。
你只能提出候选，后续聚合器会去重；不得修改任何输入网表。

## 一、输入边界

- `module_id` 与 `entry` 是当前唯一主模块；
- `entry.crossPageNet.inputs/outputs` 是唯一合法 local 集合；
- `cross_by_id` 提供合法 peer_module 与 peer_net；
- `module_decomposition` 提供 dependencies、物理功能和接口方向。

每个 local 必须逐字来自当前 entry；每个 peer_module/peer_net 必须逐字存在于
cross_by_id。不得规范化后输出新名字。

## 二、寻找邻居

优先顺序：

1. decomposition 中当前模块显式依赖的模块；
2. 反向依赖当前模块的下游模块；
3. 接口说明与两端真实 net 能共同证明的模块；
4. 无证据则不建立候选。

不能因为所有模块都有 GND/VCC 就把当前模块连向每个模块；不能遍历生成笛卡尔积。

## 三、匹配规则

- 完全同名且语义明确的协议/功能 net 优先；
- power：电源模块 output→负载 input，电压值/域必须一致；
- 已确认供电关系同时核实回流；共地输入可以 inputs→inputs，隔离地不可自动合并；
- clock：时钟源→接收端，频率/电压域不能冲突；
- I2C：SCL↔SCL、SDA↔SDA；
- SPI：MOSI/MISO/SCK/CS 分别保持角色，CS 必须属于对应从设备；
- UART：TX↔RX、RX↔TX；
- USB/CAN/RS-485/差分信号保持信号名和极性；
- 跨命名匹配必须有 dependency、接口契约或明确自然语言证据；
- PA0/PB6、GPIO1、FPC_1、PIN1 等通用名不能只凭同名匹配。

一个 local 可以在多点共享总线中指向多个真实 dependency peer，但每个候选都要说明
共享总线依据。普通点对点端口不能无理由多配。

## 四、role 与 type

- `role="start"`：当前 module 是驱动/供电/时钟源；
- `role="end"`：当前 module 是接收/负载端；
- type 使用 power/clock/signal/protection/other。

role 必须与模块功能和 dependency 方向一致；公共回流的 inputs→inputs 不视为冲突。
若 crossPageNet 方向本身疑似错误，不得反向硬配；放 exposed_ports.unmatched 并说明。

## 五、exposed_ports

未能可靠匹配邻居的每个 local 都应进入 exposed_ports：

- `system_input`：有明确板外电源、连接器、按键、电极、测试点等物理输入证据；
- `system_output`：有明确驱动板外负载/连接器端点证据；
- `unmatched`：可能缺邻居端口、命名不一致、dependency 漏配或证据不足。

direction 从整个 PCB 视角判断；system_input 用 input/inout，system_output 用 output/inout。
普通未匹配 GPIO、内部协议、VCC/GND 不是自动系统 IO，默认 unmatched。

## 六、覆盖与去重

当前 module 的每个 crossPageNet local 必须至少被归类一次：

- 已形成可靠 neighbor 候选 → internal_connection_candidates；
- 否则 → exposed_ports。

明确多点总线允许扇出。连接器的电源参考/回流可以同时内部连接并对外暴露；
同一端点不能同时作为 system_input 和 system_output，双向用 inout 并只归类一次。

## 七、输出前自检

- local/peer ID/net 全部逐字存在；
- 每个当前端口均有归宿，且无无理由重复；
- power/clock/协议方向和电压域合理；
- 通用 GPIO/连接器脚没有仅凭同名撮合；
- 无明确物理边界的端口均保守标为 unmatched。
""".strip()

    output_example = {
        "internal_connection_candidates": [
            {
                "local": "I2C1_SCL",
                "peer_module": "M-SENSOR-001",
                "peer_net": "I2C1_SCL",
                "type": "signal",
                "role": "start",
                "reason": "同名 I2C 时钟且模块存在依赖。",
            }
        ],
        "exposed_ports": [],
    }

    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        ctx.system(self.SYSTEM_PROMPT)
        ctx.user(
            "## 当前 module ID\n"
            f"{kwargs.get('module_id') or ''}\n\n"
            "## 当前 module 与 crossPageNet\n"
            f"{json_text(kwargs.get('entry') or {})}\n\n"
            "## 全系统可选 peer 与真实 crossPageNet\n"
            f"{json_text(kwargs.get('cross_by_id') or {})}\n\n"
            "## 模块分解与 dependencies\n"
            f"{json_text(kwargs.get('module_decomposition') or {})}\n\n"
            "## 输出要求\n"
            "只输出满足下列 Schema 的 JSON，并严格使用边界：\n"
            "<<<RESULT_START>>>\n{...}\n<<<RESULT_END>>>\n\n"
            f"{json_text(self.output_schema)}\n\n"
            f"## 示例\n{json_text(self.output_example)}"
        )


__all__ = ["TopPortShardAgent"]
