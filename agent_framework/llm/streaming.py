"""统一的流式增量语义与拼装。

各家的流式格式差异很大：OpenAI 是 ``choices[0].delta`` 的字段增量，Anthropic 是
``content_block_delta`` 等事件类型。provider 负责把一个原始分片翻译成中立的
:class:`StreamDelta`，本模块的拼装器只处理中立形态，因此**只需实现一次**。

工具调用是流式里最麻烦的部分：同一次调用的 JSON 参数会被切成多段陆续到达，
需按 ``index`` 归组后逐段拼接才能还原。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..tool import ToolCall
from .response import FinishReason, LLMResponse, Usage


@dataclass(frozen=True)
class ToolCallDelta:
    """工具调用的一个分片。

    Attributes:
        index: 同一响应内第几个工具调用；乱序到达时靠它归位。
        id: 调用 id，通常只在首个分片出现。
        name: 工具名，通常只在首个分片出现。
        arguments: 参数 JSON 的一个片段，需要按到达顺序拼接。
    """

    index: int = 0
    id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True)
class StreamDelta:
    """一个中立的流式增量事件。

    一个原始分片可能只携带其中一项；provider 无法解析或该分片无实质内容时
    返回 ``None``，拼装器会跳过。
    """

    text: str = ""
    reasoning: str = ""
    #: 兼容字段：等于 ``tool_calls`` 的第一项
    tool_call: ToolCallDelta | None = None
    #: 本分片携带的全部工具调用片段。并行工具调用时一个分片可能带多个，
    #: 只认第一个会静默丢调用。
    tool_calls: tuple[ToolCallDelta, ...] = ()
    finish_reason: FinishReason | None = None
    usage: Usage | None = None
    model: str = ""

    @property
    def has_payload(self) -> bool:
        """是否携带可见内容——用于判定首字延迟。"""

        return bool(self.text or self.reasoning)


class ResponseAccumulator:
    """把中立增量拼装成完整的 :class:`LLMResponse`。"""

    __slots__ = ("_text", "_reasoning", "_calls", "_finish", "_usage", "_model", "_provider")

    def __init__(self, *, provider: str = "", model: str = "") -> None:
        self._text: list[str] = []
        self._reasoning: list[str] = []
        # 以 index 为键，保证乱序到达的分片也能归到正确的调用上。
        self._calls: dict[int, dict[str, Any]] = {}
        self._finish: FinishReason | None = None
        self._usage: Usage | None = None
        self._model = model
        self._provider = provider

    def add(self, delta: StreamDelta | None) -> StreamDelta | None:
        """吸收一个增量；返回它本身，便于调用方转发给流式回调。"""

        if delta is None:
            return None
        if delta.text:
            self._text.append(delta.text)
        if delta.reasoning:
            self._reasoning.append(delta.reasoning)
        fragments = delta.tool_calls or (
            (delta.tool_call,) if delta.tool_call is not None else ()
        )
        for fragment in fragments:
            self._absorb_tool_call(fragment)
        if delta.finish_reason is not None:
            self._finish = delta.finish_reason
        if delta.usage is not None:
            self._usage = delta.usage
        if delta.model:
            self._model = delta.model
        return delta

    def _absorb_tool_call(self, fragment: ToolCallDelta) -> None:
        current = self._calls.setdefault(
            fragment.index, {"id": None, "name": "", "arguments": []}
        )
        # id 与 name 只在首个分片出现，后续为空，不能覆盖已拿到的值。
        current["id"] = fragment.id or current["id"]
        current["name"] = fragment.name or current["name"]
        if fragment.arguments:
            current["arguments"].append(fragment.arguments)

    def result(self) -> LLMResponse:
        """按 index 升序还原工具调用，产出最终结果。"""

        tool_calls = [
            ToolCall(
                id=self._calls[index]["id"],
                name=self._calls[index]["name"],
                arguments="".join(self._calls[index]["arguments"]),
            )
            for index in sorted(self._calls)
        ]
        # 模型请求了工具但未显式给出结束原因时，按语义补齐。
        finish = self._finish
        if finish is None:
            finish = FinishReason.TOOL_CALLS if tool_calls else FinishReason.UNKNOWN
        return LLMResponse(
            content="".join(self._text),
            reasoning="".join(self._reasoning),
            tool_calls=tool_calls,
            finish_reason=finish,
            usage=self._usage or Usage(),
            model=self._model,
            provider=self._provider,
        )


__all__ = ["ResponseAccumulator", "StreamDelta", "ToolCallDelta"]
