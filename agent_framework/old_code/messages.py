"""Provider-neutral chat message contracts used by every Agent application."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, Optional, TypeAlias


RoleType = Literal["system", "user", "assistant", "tool"]
ContentPart: TypeAlias = dict[str, Any]
MessageContent: TypeAlias = str | list[ContentPart]


def _bounded_text(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + f"...({len(value)} chars)"


def _safe_binary_reference(value: Any) -> Any:
    """Redact inline image bytes while retaining their media type and size."""
    if not isinstance(value, str):
        return value
    if value.startswith("data:") and ";base64," in value[:160]:
        header, payload = value.split(",", 1)
        return f"{header},<redacted {len(payload)} base64 chars>"
    return value


def value_for_log(value: Any, *, max_chars: int = 12_000) -> Any:
    """Recursively bound text and redact inline binary data without mutation."""
    if isinstance(value, str):
        safe = _safe_binary_reference(value)
        return _bounded_text(safe, max_chars) if isinstance(safe, str) else safe
    if isinstance(value, dict):
        return {
            key: value_for_log(item, max_chars=max_chars)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [value_for_log(item, max_chars=max_chars) for item in value]
    if isinstance(value, tuple):
        return tuple(value_for_log(item, max_chars=max_chars) for item in value)
    return value


def content_for_log(content: Any, *, max_chars: int = 12_000) -> Any:
    """Return bounded message content without inline image bytes."""
    return value_for_log(content, max_chars=max_chars)


def messages_for_log(messages: Any, *, max_chars: int = 12_000) -> Any:
    """Normalize messages for observability without mutating provider input."""
    if not messages:
        return []
    output: list[dict[str, Any]] = []
    try:
        for message in messages:
            if hasattr(message, "to_dict"):
                item = dict(message.to_dict())
            elif isinstance(message, dict):
                item = dict(message)
            else:
                item = {"repr": str(message)}
            if "content" in item:
                item["content"] = content_for_log(item["content"], max_chars=max_chars)
            output.append(item)
        return output
    except Exception:
        size = len(messages) if hasattr(messages, "__len__") else "?"
        return f"<{size} messages>"


@dataclass
class Message:
    role: RoleType
    content: Optional[MessageContent] = None
    tool_calls: Optional[list[Any]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """Preserve text or multimodal content in OpenAI-compatible form."""
        content: Any = self.content if self.content is not None else ""
        # OpenAI-compatible chat APIs reserve list content for typed
        # multimodal parts. Tool executors commonly return lists/dicts, but
        # sending those objects directly makes the API interpret each result
        # item as a multimodal part and reject it for missing ``type``.
        if self.role == "tool" and not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False, default=str)
        payload: dict[str, Any] = {
            "role": self.role,
            "content": content,
        }
        if self.tool_calls is not None:
            payload["tool_calls"] = []
            for tool_call in self.tool_calls:
                if hasattr(tool_call, "to_dict"):
                    payload["tool_calls"].append(tool_call.to_dict())
                else:
                    payload["tool_calls"].append(
                        {
                            "id": tool_call["id"],
                            "type": "function",
                            "function": {
                                "name": tool_call["function"]["name"],
                                "arguments": tool_call["function"]["arguments"],
                            },
                        }
                    )
        if self.tool_call_id is not None:
            payload["tool_call_id"] = self.tool_call_id
        if self.reasoning_content is not None:
            payload["reasoning_content"] = self.reasoning_content
        return payload


__all__ = [
    "ContentPart",
    "Message",
    "MessageContent",
    "RoleType",
    "content_for_log",
    "messages_for_log",
    "value_for_log",
]
