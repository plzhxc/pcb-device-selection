"""由 :class:`agent_framework.BaseAgent` 组合使用的内部执行服务。

本包没有任何业务 Agent 子类，也不在导入时执行 I/O。各服务只处理一个职责：
``runner`` 编排生命周期，``context`` 组织消息，``llm`` 包装模型调用，``tools``
执行工具，``structured_output`` 校验输出，``retry`` 计算重试策略，``events``
统一事件载荷，``response_parsing`` 解析 LLM 文本和 JSON。它们通过显式参数或
provider 获取状态，``BaseAgent`` 始终是唯一继承边界。
"""

from .events import EventCorrelation, EventRuntime, digest_messages, digest_result
from .llm import (
    INHERIT_LLM_TIMEOUT,
    LLMCallRuntime,
    PreparedCall,
    prepare_call_parameters,
)
from .retry import RetryRuntime, is_rate_limit_error
from .response_parsing import extract_text_block, safe_json_loads
from .runner import AgentRunRuntime
from .structured_output import StructuredOutputRuntime
from .tools import ToolCallSchema, ToolRuntime

__all__ = [
    "AgentRunRuntime",
    "EventCorrelation",
    "EventRuntime",
    "INHERIT_LLM_TIMEOUT",
    "LLMCallRuntime",
    "PreparedCall",
    "RetryRuntime",
    "StructuredOutputRuntime",
    "ToolCallSchema",
    "ToolRuntime",
    "digest_messages",
    "digest_result",
    "extract_text_block",
    "is_rate_limit_error",
    "prepare_call_parameters",
    "safe_json_loads",
]
