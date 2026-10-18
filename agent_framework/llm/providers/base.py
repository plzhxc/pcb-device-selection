"""Provider 协议：厂商适配的唯一收敛点。

一家厂商 = 一个实现本协议的类。除本目录外，全仓不应出现任何厂商专有名词
（``extra_body``、``choices``、``stop_reason``、``content_block`` 等）；一旦出现，
说明抽象漏了，换厂商时会在意想不到的地方失效。

协议分三组职责：

出（中立 → 厂商）
    :meth:`Provider.build_request` —— Message / Tool / CallParams 三个维度的翻译

传输
    :meth:`Provider.complete` / :meth:`Provider.stream` 及其异步版本。
    传输之所以也归 provider，是因为各家 SDK 不同（openai、anthropic…），
    client 不应知道用哪个 SDK 发请求。

入（厂商 → 中立）
    :meth:`Provider.parse_response` —— 响应与 Record（用量、结束原因）
    :meth:`Provider.parse_delta` —— 流式增量
    :meth:`Provider.map_error` —— 异常与状态
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator, Protocol, Sequence, runtime_checkable

from ...message import Message
from ...tool import ToolChoice, ToolSpec
from ..errors import LLMError
from ..params import CallParams
from ..response import LLMResponse
from ..streaming import StreamDelta


@runtime_checkable
class Provider(Protocol):
    """一家厂商的双向适配器。"""

    #: provider 名，用于观测与排障标注
    name: str

    # ----------------------------- 出 ----------------------------- #
    def build_request(
        self,
        *,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        params: CallParams,
        stream: bool = False,
        tool_choice: ToolChoice | None = None,
    ) -> dict[str, Any]:
        """把中立模型翻译成厂商请求负载。"""
        ...

    # --------------------------- 传输 ----------------------------- #
    def complete(self, request: dict[str, Any]) -> Any:
        """发送非流式请求，返回厂商原始响应。"""
        ...

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        """发送流式请求，逐个产出厂商原始分片。"""
        ...

    async def acomplete(self, request: dict[str, Any]) -> Any:
        """:meth:`complete` 的异步版本。"""
        ...

    async def astream(self, request: dict[str, Any]) -> AsyncIterator[Any]:
        """:meth:`stream` 的异步版本；await 后得到异步迭代器。"""
        ...

    async def aclose(self) -> None:
        """释放底层连接；重复调用应安全。"""
        ...

    # ----------------------------- 入 ----------------------------- #
    def parse_response(self, raw: Any) -> LLMResponse:
        """把厂商原始响应翻译成中立结果。"""
        ...

    def parse_delta(self, chunk: Any) -> StreamDelta | None:
        """把一个厂商原始分片翻译成中立增量；无实质内容时返回 ``None``。"""
        ...

    def map_error(self, error: BaseException, *, model: str = "") -> LLMError:
        """把厂商异常翻译成统一异常语义；``model`` 由调用方补充上下文。"""
        ...


__all__ = ["Provider"]
