"""统一的 LLM 异常语义。

各家厂商的失败表达差异很大（HTTP 码、错误码字符串、SDK 专有异常类），调用方
不应该为了判断"是不是限流"而去认某一家的异常类型。provider 负责把厂商异常映射
成本模块的异常树，上层只面对这一套语义。

分类原则：**按调用方的处置方式分**，而不是按厂商的错误码分——
可重试（限流/超时/连接/服务端）与不可重试（认证/请求非法/上下文超限/内容审查）
是两条完全不同的处置路径。
"""

from __future__ import annotations

from typing import Any


class LLMError(Exception):
    """所有 LLM 调用异常的基类。

    Attributes:
        message: 人类可读描述。
        provider: 产生异常的 provider 名。
        model: 本次调用的模型名。
        status_code: HTTP 状态码（若有）。
        raw: 原始异常或响应，便于排障时回溯厂商细节。
    """

    #: 该类异常是否值得重试（限流/瞬时故障为真）
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status_code: int | None = None,
        raw: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.status_code = status_code
        self.raw = raw

    def __str__(self) -> str:
        parts = [self.message]
        if self.provider:
            parts.append(f"provider={self.provider}")
        if self.model:
            parts.append(f"model={self.model}")
        if self.status_code is not None:
            parts.append(f"status={self.status_code}")
        return " | ".join(parts)


# --------------------------------------------------------------------------- #
# 不可重试：调用方必须改配置或改请求
# --------------------------------------------------------------------------- #
class LLMConfigurationError(LLMError):
    """凭据或必要参数缺失，请求根本没发出去。"""


class LLMAuthError(LLMError):
    """凭据无效或无权访问该模型（401 / 403）。"""


class LLMBadRequestError(LLMError):
    """请求本身非法：参数组合错误、模型名不存在等（400 / 404）。"""


class LLMContextLengthError(LLMBadRequestError):
    """输入超出模型上下文窗口。

    单独成类是因为处置方式明确：截断历史或换更大窗口的模型，重试无用。
    """


class LLMContentFilterError(LLMError):
    """请求或响应被内容审查拦截。"""


# --------------------------------------------------------------------------- #
# 可重试：瞬时故障
# --------------------------------------------------------------------------- #
class LLMRateLimitError(LLMError):
    """触发限流（429）。

    Attributes:
        retry_after_s: 厂商建议的等待秒数；未提供时为 ``None``，由调用方退避。
    """

    retryable = True

    def __init__(self, message: str, *, retry_after_s: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after_s = retry_after_s


class LLMTimeoutError(LLMError):
    """请求超时，含流式整体超时。"""

    retryable = True


class LLMConnectionError(LLMError):
    """网络层失败，未能拿到完整响应。"""

    retryable = True


class LLMServerError(LLMError):
    """厂商服务端故障（5xx）。"""

    retryable = True


class LLMResponseError(LLMError):
    """拿到了响应但无法解析成统一语义（协议不符、字段缺失）。"""


__all__ = [
    "LLMAuthError",
    "LLMBadRequestError",
    "LLMConfigurationError",
    "LLMConnectionError",
    "LLMContentFilterError",
    "LLMContextLengthError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMServerError",
    "LLMTimeoutError",
]
