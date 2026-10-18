"""LLM 调用的观察者协议。

本模块只定义"发生了什么"，不定义"记到哪里"：LLM 层保持零外部依赖，落库、打点、
埋 trace 由外部实现者接管（见 ``agent_framework.trace.llm``）。

未注入观察者时使用 :class:`NullObserver`，开销为零。

失败事件不再自行猜测错误类别——异常类型本身就是分类结果
（``LLMRateLimitError`` / ``LLMTimeoutError`` …），由 provider 在归一时确定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence, runtime_checkable

from ..message import Message
from ..tool import ToolSpec
from .response import LLMResponse


@dataclass(frozen=True)
class CallStarted:
    """一次调用发出前的快照。"""

    llm_id: str
    provider: str
    model: str
    messages: Sequence[Message] = ()
    tools: Sequence[ToolSpec] = ()
    #: 本次实际生效的参数
    params: dict[str, Any] = field(default_factory=dict)
    #: 第几次尝试，0 表示首次
    attempt: int = 0
    #: 一次业务调用跨物理重试保持不变；为空时退回 ``llm_id``，兼容直接调用 Client。
    logical_call_id: str = ""


@dataclass(frozen=True)
class CallEnded:
    """一次调用成功返回后的结果与耗时。"""

    llm_id: str
    provider: str
    model: str
    response: LLMResponse
    duration_ms: int = 0
    #: 首字延迟；仅流式有意义，非流式为 ``None``
    ttft_ms: int | None = None
    #: 流式分片数；非流式为 ``None``
    chunk_count: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    logical_call_id: str = ""


@dataclass(frozen=True)
class CallFailed:
    """一次调用失败。"""

    llm_id: str
    provider: str
    model: str
    error: str
    #: 统一异常类名，即错误分类
    error_type: str = "LLMError"
    #: 该类失败是否值得重试
    retryable: bool = False
    status_code: int | None = None
    duration_ms: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    #: 由真正持有 RetryPolicy 的调用方给出，Trace 不再猜测本次失败是否为终态。
    will_retry: bool = False
    logical_call_id: str = ""


@dataclass(frozen=True)
class CallCancelled:
    """一次逻辑调用被调用方取消。

    ``phase`` 区分正在等待供应商返回与物理重试之间的退避等待。后者没有活跃的
    ``llm_id``，但仍必须把已经写入 ``retry_scheduled`` 的逻辑调用闭合。
    """

    llm_id: str
    provider: str
    model: str
    reason: str = "cancelled"
    duration_ms: int = 0
    params: dict[str, Any] = field(default_factory=dict)
    attempt: int = 0
    logical_call_id: str = ""
    phase: str = "in_flight"


@runtime_checkable
class LLMObserver(Protocol):
    """LLM 调用生命周期观察者。

    实现者应自行吞掉异常；客户端侧也会再兜一层，观察失败绝不影响主流程。
    """

    def on_start(self, event: CallStarted) -> None: ...

    def on_end(self, event: CallEnded) -> None: ...

    def on_error(self, event: CallFailed) -> None: ...

    def on_cancel(self, event: CallCancelled) -> None: ...


class NullObserver:
    """默认观察者：什么都不做。"""

    __slots__ = ()

    def on_start(self, event: CallStarted) -> None:
        return None

    def on_end(self, event: CallEnded) -> None:
        return None

    def on_error(self, event: CallFailed) -> None:
        return None

    def on_cancel(self, event: CallCancelled) -> None:
        return None


NULL_OBSERVER = NullObserver()


__all__ = [
    "NULL_OBSERVER",
    "CallEnded",
    "CallFailed",
    "CallCancelled",
    "CallStarted",
    "LLMObserver",
    "NullObserver",
]
