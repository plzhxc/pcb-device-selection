"""统一 Agent/LLM 事件结构，并限制日志载荷大小。

运行时事件可能携带完整 prompt、模型结果甚至内嵌 base64 图片。本模块在写日志前
完成脱敏和截断，同时把 flow → stage → agent 三级标识附加到每条记录。它只构造
并发出事件，不决定业务日志是否开启。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..messages import messages_for_log, value_for_log


@dataclass(frozen=True)
class EventCorrelation:
    """附加到运行时事件的 flow → stage → agent 三级关联标识。"""

    flow_id: Optional[str] = None
    stage_id: Optional[str] = None
    agent_id: Optional[str] = None


def digest_messages(messages: Any, max_chars: int = 12_000) -> Any:
    """返回有界消息摘要，并移除内嵌图片的 base64 字节。"""
    return messages_for_log(messages, max_chars=max_chars)


def digest_result(result: Any, max_chars: int = 30_000) -> Any:
    """把任意结果转换成可序列化、有长度上限的事件值。"""

    result = value_for_log(result, max_chars=max_chars)
    if result is None or isinstance(result, (int, float, bool)):
        return result
    if isinstance(result, str):
        return result if len(result) <= max_chars else result[:max_chars] + f"...({len(result)} chars)"
    if isinstance(result, (dict, list, tuple)):
        try:
            rendered = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            return f"<{type(result).__name__}>"
        if len(rendered) <= max_chars:
            return list(result) if isinstance(result, tuple) else result
        return rendered[:max_chars] + f"...({len(rendered)} chars)"
    return f"<{type(result).__name__}>"


def agent_event_record(
    agent_name: str,
    event: str,
    correlation: EventCorrelation,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造统一的 Agent 生命周期事件。"""
    return {
        "agent": agent_name,
        "event": event,
        "agent_id": correlation.agent_id,
        "stage_id": correlation.stage_id,
        "flow_id": correlation.flow_id,
        "data": data or {},
    }


def llm_event_record(
    event: str,
    llm_id: str,
    correlation: EventCorrelation,
    *,
    model: Optional[str] = None,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """构造统一的单次 LLM 请求事件。"""
    return {
        "event": event,
        "llm_id": llm_id,
        "agent_id": correlation.agent_id,
        "stage_id": correlation.stage_id,
        "flow_id": correlation.flow_id,
        "model": model,
        "data": data or {},
    }


class EventRuntime:
    """通过注入的 logger 发出已关联的标准事件记录。"""

    def __init__(
        self,
        agent_logger: Any,
        llm_logger_factory: Callable[[], Any] | None = None,
    ) -> None:
        """注入 Agent logger，并保留 LLM logger 的延迟工厂。"""
        self._agent_logger = agent_logger
        self._llm_logger_factory = llm_logger_factory or (lambda: agent_logger)

    @staticmethod
    def _emit(logger: Any, level: str, record: dict[str, Any]) -> None:
        """按字符串级别选择 logger 方法，未知级别回退到 info。"""
        method = getattr(logger, level.lower(), logger.info)
        method(record)

    def emit_agent(
        self,
        agent_name: str,
        event: str,
        correlation: EventCorrelation,
        data: Optional[dict[str, Any]] = None,
        *,
        level: str = "INFO",
    ) -> None:
        """发出 Agent 级事件。"""
        self._emit(
            self._agent_logger,
            level,
            agent_event_record(agent_name, event, correlation, data),
        )

    def emit_llm(
        self,
        event: str,
        llm_id: str,
        correlation: EventCorrelation,
        *,
        model: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
        level: str = "INFO",
    ) -> None:
        """发出 LLM 级事件；logger 直到此处才延迟创建。"""
        self._emit(
            self._llm_logger_factory(),
            level,
            llm_event_record(event, llm_id, correlation, model=model, data=data),
        )
