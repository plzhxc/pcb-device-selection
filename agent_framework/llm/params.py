"""统一的调用参数。

这里只放**影响单次生成结果**的参数。另外两类刻意不放进来，因为生命周期不同：

- 连接信息（凭据、端点）属于 provider，构造时给定，不随调用变化；
- 限流与重试属于 client 的编排职责，与"生成什么"无关。

各家对同一语义的字段名与取值范围不同（如 ``max_tokens`` 在 Anthropic 是必填），
归一由 provider 完成，本模块只表达意图。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class CallParams:
    """一次生成的参数。

    Attributes:
        model: 模型名。
        temperature: 采样温度；``None`` 表示用服务端默认。
        top_p: 核采样阈值。
        max_tokens: 最大输出 token 数。
        seed: 随机种子；用于可复现实验。
        stop: 停止序列。
        timeout_s: 单次请求超时（含流式整体耗时）。
        reasoning: 是否开启思考模式。
        provider_extra: 厂商专有参数，原样透传。
            使用它即意味着这段调用绑定了特定厂商，应尽量避免；仅用于新特性尚未
            抽象成中立字段的过渡期。
    """

    model: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: tuple[str, ...] = ()
    timeout_s: float | None = None
    reasoning: bool = False
    provider_extra: dict[str, Any] = field(default_factory=dict)

    def merged(self, **overrides: Any) -> "CallParams":
        """返回覆盖部分字段后的新参数；``None`` 视为"不覆盖"。

        便于按 task 覆盖模型或超时，而不必重建整份参数。
        """

        effective = {key: value for key, value in overrides.items() if value is not None}
        unknown = set(effective) - {f.name for f in self.__dataclass_fields__.values()}
        if unknown:
            raise TypeError(f"未知的调用参数：{', '.join(sorted(unknown))}")
        return replace(self, **effective) if effective else self

    def as_overrides(self) -> dict[str, Any]:
        """转成可交给 :meth:`merged` 的覆盖项，只含被显式设定的字段。

        空的 ``stop`` 与 ``provider_extra`` 不参与覆盖：它们的"未设定"与"设为空"
        在语义上无法区分，若一并下传会把上游已配好的值清掉。
        """

        overrides: dict[str, Any] = {"model": self.model}
        for name in ("temperature", "top_p", "max_tokens", "seed", "timeout_s"):
            value = getattr(self, name)
            if value is not None:
                overrides[name] = value
        if self.reasoning:
            overrides["reasoning"] = True
        if self.stop:
            overrides["stop"] = self.stop
        if self.provider_extra:
            overrides["provider_extra"] = dict(self.provider_extra)
        return overrides

    def digest(self) -> dict[str, Any]:
        """本次生效参数的快照，用于观测与排障核对。"""

        return {
            "model": self.model,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "seed": self.seed,
            "stop": list(self.stop),
            "timeout_s": self.timeout_s,
            "reasoning": self.reasoning,
            "provider_extra": dict(self.provider_extra),
        }


__all__ = ["CallParams"]
