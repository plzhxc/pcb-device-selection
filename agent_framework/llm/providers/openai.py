"""OpenAI 协议兼容 provider。

覆盖 OpenAI 本家以及所有声称"OpenAI 兼容"的端点：DashScope（通义千问）、
DeepSeek、vLLM、Ollama 的兼容接口、各类中转网关等。

"OpenAI 兼容"实际是一个方言家族而非单一协议——同一语义在各家的表达可能不同，
最典型的是思考模式：DashScope 用 ``extra_body.enable_thinking``，别家未必。
这类差异通过构造参数（如 ``reasoning_style``）在本文件内消化，不外泄。

本文件是厂商知识的唯一容身处：``choices``、``delta``、``extra_body``、
``finish_reason`` 等名词只应出现在这里。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator, Literal, Sequence

from ...message import (
    AssistantMessage,
    ImagePart,
    Message,
    SystemMessage,
    TextPart,
    ToolMessage,
    UserMessage,
)
from ...tool import ToolCall, ToolChoice, ToolSpec
from ..errors import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigurationError,
    LLMConnectionError,
    LLMContentFilterError,
    LLMContextLengthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseError,
    LLMServerError,
    LLMTimeoutError,
)
from ..params import CallParams
from ..response import FinishReason, LLMResponse, Usage
from ..streaming import StreamDelta, ToolCallDelta


ReasoningStyle = Literal["dashscope", "none"]


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    """同时兼容 dict 与 SDK 模型对象的取值。"""

    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# --------------------------------------------------------------------------- #
# ② Message：中立 → OpenAI
# --------------------------------------------------------------------------- #
def _encode_content(content: Any) -> Any:
    """把中立内容翻译成 OpenAI 的 content 字段。"""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            parts.append({"type": "image_url", "image_url": {"url": part.source}})
    return parts


def _encode_tool_call(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": call.arguments},
    }


def _encode_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """四类中立消息 → OpenAI messages 数组。"""

    encoded: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            encoded.append({"role": "system", "content": message.content})
        elif isinstance(message, UserMessage):
            encoded.append({"role": "user", "content": _encode_content(message.content)})
        elif isinstance(message, AssistantMessage):
            item: dict[str, Any] = {
                "role": "assistant",
                "content": _encode_content(message.content),
            }
            if message.tool_calls:
                item["tool_calls"] = [_encode_tool_call(c) for c in message.tool_calls]
            encoded.append(item)
        elif isinstance(message, ToolMessage):
            encoded.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
        else:
            raise LLMConfigurationError(f"无法编码的消息类型：{type(message).__name__}")
    return encoded


# --------------------------------------------------------------------------- #
# ③ Tool：中立 → OpenAI
# --------------------------------------------------------------------------- #
def _encode_tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }
        for tool in tools
    ]


def _encode_tool_choice(choice: ToolChoice) -> Any:
    """中立策略 → OpenAI 的 tool_choice 取值。"""

    if choice.mode == "specific" and choice.tool_name:
        return {"type": "function", "function": {"name": choice.tool_name}}
    if choice.mode in ("none", "required"):
        return choice.mode
    return "auto"


# --------------------------------------------------------------------------- #
# ④ Call Parameter：中立 → OpenAI
# --------------------------------------------------------------------------- #
def _encode_params(
    params: CallParams,
    *,
    stream: bool,
    reasoning_style: ReasoningStyle,
) -> dict[str, Any]:
    body: dict[str, Any] = {"model": params.model, "stream": stream}
    if params.temperature is not None:
        body["temperature"] = params.temperature
    if params.top_p is not None:
        body["top_p"] = params.top_p
    if params.max_tokens is not None:
        body["max_tokens"] = params.max_tokens
    if params.seed is not None:
        body["seed"] = params.seed
    if params.stop:
        body["stop"] = list(params.stop)
    if params.timeout_s is not None:
        body["timeout"] = params.timeout_s
    if stream:
        # 不显式索取，流式响应不会带 token 用量。
        body["stream_options"] = {"include_usage": True}

    if reasoning_style == "dashscope":
        extra = dict(body.get("extra_body") or {})
        extra["enable_thinking"] = params.reasoning
        body["extra_body"] = extra

    if params.provider_extra:
        body.update(params.provider_extra)
    return body


# --------------------------------------------------------------------------- #
# ⑤⑥ Record 与状态：OpenAI → 中立
# --------------------------------------------------------------------------- #
_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "max_tokens": FinishReason.LENGTH,
    "tool_calls": FinishReason.TOOL_CALLS,
    "function_call": FinishReason.TOOL_CALLS,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def _decode_finish_reason(value: Any) -> FinishReason | None:
    if value is None:
        return None
    return _FINISH_REASONS.get(str(value), FinishReason.UNKNOWN)


def _decode_usage(raw: Any) -> Usage | None:
    if raw is None:
        return None
    prompt_details = _attr(raw, "prompt_tokens_details")
    completion_details = _attr(raw, "completion_tokens_details")
    return Usage(
        prompt_tokens=_attr(raw, "prompt_tokens"),
        completion_tokens=_attr(raw, "completion_tokens"),
        total_tokens=_attr(raw, "total_tokens"),
        cached_tokens=_attr(prompt_details, "cached_tokens"),
        reasoning_tokens=_attr(completion_details, "reasoning_tokens"),
    )


def _decode_tool_calls(raw_calls: Any) -> list[ToolCall]:
    calls = []
    for item in raw_calls or ():
        function = _attr(item, "function")
        calls.append(
            ToolCall(
                id=_attr(item, "id"),
                name=_attr(function, "name") or "",
                arguments=_attr(function, "arguments") or "",
            )
        )
    return calls


def _decode_response(raw: Any, *, provider: str) -> LLMResponse:
    choices = _attr(raw, "choices")
    if not choices:
        raise LLMResponseError("响应不含 choices", provider=provider, raw=raw)
    choice = choices[0]
    message = _attr(choice, "message")
    tool_calls = _decode_tool_calls(_attr(message, "tool_calls"))
    finish = _decode_finish_reason(_attr(choice, "finish_reason"))
    if finish is None:
        finish = FinishReason.TOOL_CALLS if tool_calls else FinishReason.UNKNOWN
    return LLMResponse(
        content=_attr(message, "content") or "",
        reasoning=_attr(message, "reasoning_content") or "",
        tool_calls=tool_calls,
        finish_reason=finish,
        usage=_decode_usage(_attr(raw, "usage")) or Usage(),
        model=_attr(raw, "model") or "",
        provider=provider,
    )


# --------------------------------------------------------------------------- #
# ⑦ 流式：OpenAI → 中立
# --------------------------------------------------------------------------- #
def _decode_delta(chunk: Any) -> StreamDelta | None:
    usage = _decode_usage(_attr(chunk, "usage"))
    choices = _attr(chunk, "choices")
    if not choices:
        # 末尾的纯用量分片没有 choices，但用量必须留下。
        return StreamDelta(usage=usage) if usage else None

    choice = choices[0]
    delta = _attr(choice, "delta")
    # 并行工具调用时，DashScope / vLLM / 各类网关常把整个 tool_calls 数组放在
    # 同一个分片里下发。只取第一个会静默丢掉其余调用，模型请求了三个工具却只
    # 执行一个，且没有任何报错。
    fragments = tuple(
        ToolCallDelta(
            index=int(_attr(item, "index", 0) or 0),
            id=_attr(item, "id"),
            name=_attr(_attr(item, "function"), "name"),
            arguments=_attr(_attr(item, "function"), "arguments") or "",
        )
        for item in _attr(delta, "tool_calls") or ()
    )

    return StreamDelta(
        text=_attr(delta, "content") or "",
        reasoning=_attr(delta, "reasoning_content") or "",
        tool_call=fragments[0] if fragments else None,
        tool_calls=fragments,
        finish_reason=_decode_finish_reason(_attr(choice, "finish_reason")),
        usage=usage,
        model=_attr(chunk, "model") or "",
    )


# --------------------------------------------------------------------------- #
# ⑥ 异常：OpenAI SDK → 中立
# --------------------------------------------------------------------------- #
_CONTEXT_LENGTH_HINTS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "reduce the length",
)


def _decode_retry_after(error: BaseException) -> float | None:
    """从 429 响应里取厂商建议的等待秒数。

    不取的话 :class:`LLMRateLimitError` 的 ``retry_after_s`` 永远是 ``None``，
    限流器只能凭经验猜退避时长——而厂商明明已经告诉我们该等多久。
    ``Retry-After`` 允许是秒数或 HTTP 日期，这里只认秒数（各家实际都发秒数），
    日期形式当作没给，退回自适应降速。
    """

    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    # httpx 的 Headers 是大小写不敏感的，但网关和测试桩常常就是个普通 dict，
    # 那里 "Retry-After" 与 "retry-after" 是两个键。统一按小写比对。
    try:
        lowered = {str(k).lower(): v for k, v in dict(headers).items()}
    except Exception:
        return None
    for key in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = lowered.get(key)
        if not raw:
            continue
        text = str(raw).strip().rstrip("s")  # 有的网关发 "20s"
        try:
            seconds = float(text)
        except ValueError:
            continue
        if seconds > 0:
            return seconds
    return None


def _decode_error(error: BaseException, *, provider: str, model: str) -> LLMError:
    """把 SDK 异常翻译成统一语义。

    按 SDK 异常类型判定，取不到类型时退回文本特征——中转网关常把上游错误
    重新包装，类型信息可能丢失。
    """

    context = {"provider": provider, "model": model, "raw": error}
    text = str(error).lower()
    status = getattr(error, "status_code", None)

    try:
        import openai
    except ImportError:  # pragma: no cover - 仅在未装 SDK 时
        openai = None  # type: ignore[assignment]

    if openai is not None:
        if isinstance(error, openai.RateLimitError):
            return LLMRateLimitError(
                str(error),
                status_code=status,
                retry_after_s=_decode_retry_after(error),
                **context,
            )
        if isinstance(error, openai.APITimeoutError):
            return LLMTimeoutError(str(error), status_code=status, **context)
        if isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return LLMAuthError(str(error), status_code=status, **context)
        if isinstance(error, openai.BadRequestError):
            if any(hint in text for hint in _CONTEXT_LENGTH_HINTS):
                return LLMContextLengthError(str(error), status_code=status, **context)
            if "content" in text and "filter" in text:
                return LLMContentFilterError(str(error), status_code=status, **context)
            return LLMBadRequestError(str(error), status_code=status, **context)
        if isinstance(error, openai.APIConnectionError):
            return LLMConnectionError(str(error), status_code=status, **context)
        if isinstance(error, openai.InternalServerError):
            return LLMServerError(str(error), status_code=status, **context)

    if isinstance(error, TimeoutError) or "timeout" in text or "timed out" in text:
        return LLMTimeoutError(str(error), status_code=status, **context)
    # 流式中途断开与协议层截断都是瞬时故障，重发通常就好。判成不可重试会让
    # 一次网络抖动直接毁掉整轮运行。
    if any(
        hint in text
        for hint in (
            "during streaming",
            "incomplete",
            "peer closed",
            "connection reset",
            "connection aborted",
            "server disconnected",
            "remoteprotocol",
        )
    ):
        return LLMConnectionError(str(error), status_code=status, **context)
    if "429" in text or "rate limit" in text:
        return LLMRateLimitError(
            str(error),
            status_code=status,
            retry_after_s=_decode_retry_after(error),
            **context,
        )
    if any(hint in text for hint in _CONTEXT_LENGTH_HINTS):
        return LLMContextLengthError(str(error), status_code=status, **context)
    if isinstance(status, int) and status >= 500:
        return LLMServerError(str(error), status_code=status, **context)
    return LLMError(str(error), status_code=status, **context)


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #
class OpenAICompatibleProvider:
    """OpenAI 协议兼容端点的适配器。

    只做组装与传输，翻译逻辑在上面六个函数里，便于与其他厂商实现逐条比对。

    ``reasoning_style`` 默认 ``"none"``：本 provider 覆盖 OpenAI 本家、DeepSeek、
    vLLM、各类网关，给它们注入 DashScope 专有的 ``extra_body.enable_thinking``
    会被拒绝或静默忽略。接 DashScope 时显式传 ``reasoning_style="dashscope"``，
    ``CallParams.reasoning`` 才会生效。

    ``max_retries`` 默认 ``0``：重试归我们自己管（:class:`~..agent.config.RetryPolicy`）。
    留着 SDK 那层会变成两层相乘——配 3 次实际最多发 9 次，而且 SDK 内部的重发
    既不过限流账本也不产生观测事件，账对不上、429 时越撞越凶。
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        name: str = "openai",
        max_retries: int = 0,
        reasoning_style: ReasoningStyle = "none",
        sync_client: Any = None,
        async_client: Any = None,
    ) -> None:
        self.name = name
        self._api_key = api_key
        self._base_url = base_url
        self._max_retries = max_retries
        self._reasoning_style = reasoning_style
        self._sync = sync_client
        self._async = async_client

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
        request = _encode_params(
            params, stream=stream, reasoning_style=self._reasoning_style
        )
        request["messages"] = _encode_messages(messages)
        encoded_tools = _encode_tools(tools)
        if encoded_tools:
            request["tools"] = encoded_tools
            request["tool_choice"] = _encode_tool_choice(tool_choice or ToolChoice())
        return request

    # --------------------------- 传输 ----------------------------- #
    def _require_key(self) -> str:
        key = str(self._api_key or "").strip()
        if not key:
            raise LLMConfigurationError(
                "LLM 凭据未配置：请在构造 provider 时提供 api_key。",
                provider=self.name,
            )
        return key

    def _sync_client(self) -> Any:
        if self._sync is None:
            from openai import OpenAI

            self._sync = OpenAI(
                api_key=self._require_key(),
                base_url=self._base_url,
                max_retries=self._max_retries,
            )
        return self._sync

    def _async_client(self) -> Any:
        if self._async is None:
            from openai import AsyncOpenAI

            self._async = AsyncOpenAI(
                api_key=self._require_key(),
                base_url=self._base_url,
                max_retries=self._max_retries,
            )
        return self._async

    def complete(self, request: dict[str, Any]) -> Any:
        return self._sync_client().chat.completions.create(**request)

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        return self._sync_client().chat.completions.create(**request)

    async def acomplete(self, request: dict[str, Any]) -> Any:
        return await self._async_client().chat.completions.create(**request)

    async def astream(self, request: dict[str, Any]) -> AsyncIterator[Any]:
        # SDK 的 create 返回可等待对象，await 之后才是异步迭代器。
        return await self._async_client().chat.completions.create(**request)

    async def aclose(self) -> None:
        client = self._async
        self._async = None
        if client is None:
            return
        close = getattr(client, "close", None)
        if close is None:
            return
        outcome = close()
        if hasattr(outcome, "__await__"):
            await outcome

    # ----------------------------- 入 ----------------------------- #
    def parse_response(self, raw: Any) -> LLMResponse:
        return _decode_response(raw, provider=self.name)

    def parse_delta(self, chunk: Any) -> StreamDelta | None:
        return _decode_delta(chunk)

    def map_error(self, error: BaseException, *, model: str = "") -> LLMError:
        return _decode_error(error, provider=self.name, model=model)


__all__ = ["OpenAICompatibleProvider", "ReasoningStyle"]
