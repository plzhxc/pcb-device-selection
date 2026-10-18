"""统一的响应、用量与结束状态。

调用方需要回答三个问题，本模块把它们变成与厂商无关的表达：

1. **模型给了什么** —— 正文、思考过程、还是工具调用请求（:class:`LLMResponse`）；
2. **花了多少** —— token 用量（:class:`Usage`）；
3. **为什么结束** —— 正常收尾、长度截断、还是要调工具（:class:`FinishReason`）。

第 3 点尤其需要归一：OpenAI 叫 ``finish_reason``（stop/length/tool_calls），
Anthropic 叫 ``stop_reason``（end_turn/max_tokens/tool_use），取值空间也不一致。
上层若直接判断厂商字符串，换一家就会静默失效——例如"是否被截断"判断不出来，
导致把半截输出当成完整结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence

from ..tool import ToolCall


class FinishReason(str, Enum):
    """统一的结束原因。"""

    #: 模型自然收尾
    STOP = "stop"
    #: 触达 max_tokens 或上下文上限，输出被截断
    LENGTH = "length"
    #: 模型要求调用工具，等待结果后继续
    TOOL_CALLS = "tool_calls"
    #: 被内容审查拦截
    CONTENT_FILTER = "content_filter"
    #: 厂商返回了未知取值；保留原值以便排障
    UNKNOWN = "unknown"

    @property
    def truncated(self) -> bool:
        """输出是否不完整——这是调用方最需要分支的判断。"""

        return self is FinishReason.LENGTH


@dataclass(frozen=True)
class Usage:
    """token 用量。

    各家字段名不同（``prompt_tokens`` / ``input_tokens``），且缓存命中、思考
    token 等扩展项并非都有；缺失时保持 ``None`` 而不是填 0，避免把"没上报"
    误算成"没消耗"。
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    #: 命中提示缓存的 token 数（计费通常更低）
    cached_tokens: int | None = None
    #: 思考过程消耗的 token 数
    reasoning_tokens: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


@dataclass(frozen=True)
class LLMResponse:
    """一次调用的完整结果。

    Attributes:
        content: 正文输出。
        reasoning: 思考过程；模型未返回时为空串。
        tool_calls: 模型请求的工具调用。
        finish_reason: 结束原因。
        usage: token 用量。
        model: 服务端实际使用的模型名，可能与请求的不同（别名、降级）。
        provider: 产出本结果的 provider 名。
    """

    content: str = ""
    reasoning: str = ""
    tool_calls: Sequence[ToolCall] = ()
    finish_reason: FinishReason = FinishReason.UNKNOWN
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    provider: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def truncated(self) -> bool:
        return self.finish_reason.truncated


def accumulate_usage(total: Usage, addition: Usage | None) -> Usage:
    """累加 token 用量；``None`` 项按缺失处理，不当作 0。

    唯一实现。此前 agent/result.py、stage/context.py、workflow/base.py 各写了
    一份完全相同的代码：Usage 加字段时只改一处，另外两层会静默丢掉该字段的累计，
    症状是"stage 汇总的 token 比 agent 之和少"，不报错、极难定位。
    """

    if addition is None:
        return total

    def add(left: int | None, right: int | None) -> int | None:
        if left is None and right is None:
            return None
        return (left or 0) + (right or 0)

    return Usage(
        prompt_tokens=add(total.prompt_tokens, addition.prompt_tokens),
        completion_tokens=add(total.completion_tokens, addition.completion_tokens),
        total_tokens=add(total.total_tokens, addition.total_tokens),
        cached_tokens=add(total.cached_tokens, addition.cached_tokens),
        reasoning_tokens=add(total.reasoning_tokens, addition.reasoning_tokens),
    )


def usage_fields(usage: Usage) -> dict[str, int | None]:
    """把用量摊平成观测事件的关键字参数。

    观测事件靠这一个出口取用量，Usage 加字段时不会再出现"某个事件漏搬"的情况
    ——那种漏法不报错，只是某个指标永远是空的，很久都不会有人发现。
    """

    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": usage.cached_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
    }


__all__ = ["FinishReason", "LLMResponse", "Usage", "accumulate_usage", "usage_fields"]
