"""LLM 调用层：多厂商适配与统一入口。

上层只面对本包的中立语义，永远不接触厂商协议：

    from agent_framework.llm import AsyncLLMClient, CallParams
    from agent_framework.llm.providers import OpenAICompatibleProvider
    from agent_framework.message import UserMessage

    provider = OpenAICompatibleProvider(api_key=..., base_url=...)
    client = AsyncLLMClient(provider, CallParams(model="qwen3.6-flash"))
    response = await client.call([UserMessage("你好")])

    response.content        # 正文
    response.tool_calls     # 工具调用请求
    response.finish_reason  # 统一的结束状态
    response.usage          # 统一的 token 用量

换厂商只需换 provider，其余代码不动。消息与工具的中立定义在
:mod:`agent_framework.message` 与 :mod:`agent_framework.tool`。
"""

from .client import AsyncLLMClient, LLMClient, OnDelta
from .errors import (
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
from .observer import (
    NULL_OBSERVER,
    CallEnded,
    CallFailed,
    CallStarted,
    LLMObserver,
    NullObserver,
)
from .params import CallParams
from .providers import OpenAICompatibleProvider, Provider
from .rate_limit import (
    Lease,
    RateLimiter,
    RateLimits,
    configure_default_limiter,
    get_default_limiter,
)
from .response import FinishReason, LLMResponse, Usage
from .streaming import ResponseAccumulator, StreamDelta, ToolCallDelta

__all__ = [
    "NULL_OBSERVER",
    "AsyncLLMClient",
    "CallEnded",
    "CallFailed",
    "CallParams",
    "CallStarted",
    "FinishReason",
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMClient",
    "LLMConfigurationError",
    "LLMConnectionError",
    "LLMContentFilterError",
    "LLMContextLengthError",
    "LLMError",
    "LLMObserver",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMResponseError",
    "LLMServerError",
    "LLMTimeoutError",
    "Lease",
    "NullObserver",
    "OnDelta",
    "OpenAICompatibleProvider",
    "Provider",
    "RateLimiter",
    "RateLimits",
    "Usage",
    "configure_default_limiter",
    "get_default_limiter",
    "ResponseAccumulator",
    "StreamDelta",
    "ToolCallDelta",
]
