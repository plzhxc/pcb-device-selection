"""对话消息的中立语义。

四类消息覆盖人与模型交流的全部角色：

- :class:`SystemMessage` —— 设定模型的身份与规则；
- :class:`UserMessage` —— 人类输入，可含多模态内容；
- :class:`AssistantMessage` —— 模型输出，可含思考过程与工具调用请求；
- :class:`ToolMessage` —— 工具执行结果的回填。

本模块**不含厂商知识**：不产出 ``role``/``tool_calls`` 之类的线格式，翻译由
``llm/providers/`` 负责。各家协议的差异因此不会渗进这里，例如 Anthropic 把
system 放在顶层参数、把工具结果折进 user 消息——那些都是 provider 的投影方式，
中立模型只表达"这是一条系统消息""这是一条工具结果"。

关于 :class:`AssistantMessage` 的工具调用：以独立字段而非内容块表达。两种形态
都能投影到各家协议（Anthropic 的 ``tool_use`` 块由该字段生成），而字段形态在
业务代码里更直观。仅当需要保留"文本—工具—文本"的交错顺序时块结构才有优势，
这一需求在实际编排中很少出现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence, TypeAlias, Union

from .tool import ToolCall


# --------------------------------------------------------------------------- #
# 内容分段（多模态）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TextPart:
    """一段纯文本。"""

    text: str


@dataclass(frozen=True)
class ImagePart:
    """一张图片。

    Attributes:
        source: 图片 URL，或 ``data:`` 内联数据。
        media_type: 媒体类型；内联数据未标注时由 provider 推断。
    """

    source: str
    media_type: str | None = None


ContentPart: TypeAlias = Union[TextPart, ImagePart]
#: 消息内容：纯文本，或多模态分段序列
Content: TypeAlias = Union[str, Sequence[ContentPart]]


def content_to_text(content: Content | None) -> str:
    """提取内容中的文本部分，图片等非文本分段被忽略。"""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(part.text for part in content if isinstance(part, TextPart))


# --------------------------------------------------------------------------- #
# 四类消息
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SystemMessage:
    """系统指令。"""

    content: str

    @property
    def text(self) -> str:
        return self.content


@dataclass(frozen=True)
class UserMessage:
    """人类输入；``content`` 可为纯文本或多模态分段。"""

    content: Content

    @property
    def text(self) -> str:
        return content_to_text(self.content)


@dataclass(frozen=True)
class AssistantMessage:
    """模型输出。

    Attributes:
        content: 正文，可为空（模型只请求工具时）。
        tool_calls: 模型请求的工具调用。
        reasoning: 思考过程；仅部分模型返回。
    """

    content: Content = ""
    tool_calls: Sequence[ToolCall] = ()
    reasoning: str = ""

    @property
    def text(self) -> str:
        return content_to_text(self.content)

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class ToolMessage:
    """工具执行结果的回填。

    ``tool_call_id`` 必须与模型请求的 :class:`~agent_framework.tool.ToolCall`
    一一对应，否则模型无法把结果与请求关联。
    """

    tool_call_id: str | None
    content: str
    name: str | None = None
    is_error: bool = False

    @property
    def text(self) -> str:
        return self.content


#: 任意一条消息
Message: TypeAlias = Union[SystemMessage, UserMessage, AssistantMessage, ToolMessage]


# --------------------------------------------------------------------------- #
# 对话
# --------------------------------------------------------------------------- #
@dataclass
class Conversation:
    """有序消息序列。"""

    messages: list[Message] = field(default_factory=list)

    def add(self, message: Message) -> "Conversation":
        self.messages.append(message)
        return self

    def extend(self, messages: Iterable[Message]) -> "Conversation":
        self.messages.extend(messages)
        return self

    def add_tool_results(self, results: Iterable[Any]) -> "Conversation":
        """把 :class:`~agent_framework.tool.ToolResult` 批量回填为工具消息。"""

        for result in results:
            self.messages.append(
                ToolMessage(
                    tool_call_id=result.tool_call_id,
                    content=result.as_text(),
                    name=result.name,
                    is_error=not result.ok,
                )
            )
        return self

    def __len__(self) -> int:
        return len(self.messages)

    def __iter__(self):
        return iter(self.messages)


__all__ = [
    "AssistantMessage",
    "Content",
    "ContentPart",
    "Conversation",
    "ImagePart",
    "Message",
    "SystemMessage",
    "TextPart",
    "ToolMessage",
    "UserMessage",
    "content_to_text",
]
