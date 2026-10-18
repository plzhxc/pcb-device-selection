"""厂商适配器。

每个模块适配一家厂商，实现 :class:`~agent_framework.llm.providers.base.Provider`
协议。厂商专有名词只应出现在本目录内。
"""

from .base import Provider
from .openai import OpenAICompatibleProvider

__all__ = ["OpenAICompatibleProvider", "Provider"]
