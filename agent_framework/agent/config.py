"""Agent 运行配置。

配置在这一层落地：LLM 层只接受已经确定的 :class:`CallParams`，把 YAML、环境变量
等外部来源翻译成本模块的结构，是应用（而非框架）的职责。框架因此不依赖任何
具体的配置文件格式。

三类上限刻意分开计数，因为它们表达完全不同的失败模式：

- **工具迭代**：模型反复要求调工具，可能陷入循环；
- **输出纠错**：模型输出不符合 Schema，需要带着错误信息重试；
- **规则重试**：格式对但业务规则不满足，需要带着规则问题重试；
- **传输重试**：限流或瞬时故障，需要退避后重发。

三者混在一个计数器里，会让"模型不听话"和"网络抖动"变得无法区分。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..llm.params import CallParams


@dataclass(frozen=True)
class RetryPolicy:
    """可重试异常的退避策略。

    仅作用于 :attr:`~agent_framework.llm.errors.LLMError.retryable` 为真的失败；
    认证错误、参数非法等重试无用的失败会立即抛出。
    """

    max_attempts: int = 3
    base_s: float = 1.0
    max_s: float = 60.0
    #: 抖动上限，避免并发请求在同一时刻集体重试
    jitter_s: float = 0.5

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的等待秒数（指数退避，不含抖动）。"""

        return min(self.base_s * (2 ** max(0, attempt)), self.max_s)


@dataclass(frozen=True)
class AgentConfig:
    """一个 Agent 的运行参数。

    Attributes:
        params: 传给 LLM 的生成参数。
        max_tool_iterations: 工具循环的最大轮数；``None`` 表示该 Agent
            明确不设轮数上限。
        max_tool_calls_per_iteration: 单次模型响应最多允许多少个工具调用；
            ``None`` 表示该 Agent 明确不设单轮上限。默认值用于防止坏响应
            一次创建过多线程或网络请求。
        max_output_retries: 结构化输出校验失败后的最大纠错次数。
        max_validation_retries: 业务规则校验失败后的最大重试次数。与格式纠错
            分开：格式反复错指向提示词，规则反复不满足指向上游数据或规则本身。
        retry: 传输层可重试失败的退避策略。
        stream: 是否以流式方式调用。
        model_tiers: 模型强度档位到具体模型名的映射（如
            ``{"strong": "qwen3.7-max", "standard": "qwen3.7-plus"}``）。Agent 用
            ``ctx.model("strong")`` 或类属性 ``model_tier`` 表达强度，调用时经此表
            解析；表里没有的档位直接报错，不会静默退回默认模型。
        tier: 这个 Agent 配置自身指定的档位（通常来自配置文件的角色一节）；
            ``None`` 表示用 ``params.model``。
        strict_validation: 校验器**自身抛异常**时是否终止本次运行。默认终止。

            崩掉的规则没有做出判断，把它当成通过，等于业务校验被悄悄绕过——
            输出照常返回 ``SUCCESS``，谁也不会去查为什么那条规则没拦住。把它当
            成不通过也不对：发回给模型让它改，改的是我们代码里的 bug。所以默认
            当场失败。确认某条规则可有可无时再关掉。
    """

    params: CallParams
    max_tool_iterations: int | None = 10
    max_tool_calls_per_iteration: int | None = 16
    max_output_retries: int = 3
    max_validation_retries: int = 2
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    stream: bool = False
    strict_validation: bool = True
    model_tiers: Mapping[str, str] = field(default_factory=dict)
    tier: str | None = None

    def resolve_tier(self, tier: str) -> str:
        """档位 → 模型名；未配置的档位是配置错误，当场报出。"""

        try:
            return self.model_tiers[tier]
        except KeyError as exc:
            known = ", ".join(sorted(self.model_tiers)) or "<none>"
            raise ValueError(f"unknown model tier {tier!r}; configured tiers: {known}") from exc


__all__ = ["AgentConfig", "RetryPolicy"]
