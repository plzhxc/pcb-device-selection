"""LLM 请求参数合成与单次调用生命周期封装。

``BaseAgent`` 决定 task/model/reasoning，``runner`` 决定何时重试；本模块位于两者
之间，负责把参数合成为 ``PreparedCall``，调用同步或异步 provider 客户端，并
保证 start/end/error 三类事件字段一致。这里不解析业务输出，也不执行工具。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .events import digest_messages, digest_result


class _InheritLLMTimeout:
    """区分“继承默认超时”和调用者显式传入 ``None`` 的哨兵类型。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "INHERIT_LLM_TIMEOUT"


INHERIT_LLM_TIMEOUT = _InheritLLMTimeout()


@dataclass(frozen=True)
class PreparedCall:
    """一次 provider 调用最终使用的参数及生效超时。"""

    parameters: dict[str, Any]
    effective_timeout: Optional[float]


def prepare_call_parameters(
    supplied: Mapping[str, Any],
    *,
    call_defaults: Mapping[str, Any],
    enable_thinking: bool,
    default_model: str | Callable[[], str],
    on_chunk: Optional[Callable[[str, Optional[str]], None]] = None,
    default_timeout: Optional[float] = None,
) -> PreparedCall:
    """补齐一次 LLM 调用所需的公共参数。

    先载入配置默认值，再由调用者显式参数覆盖，最后补充 reasoning、model 和
    timeout。返回独立字典，不修改输入 mapping。
    """

    parameters = dict(call_defaults)
    parameters.update(supplied)
    parameters.setdefault("stream", True)
    if on_chunk is not None:
        parameters["on_chunk"] = on_chunk

    parameters["enable_thinking"] = bool(enable_thinking)
    if "model" not in parameters:
        parameters["model"] = default_model() if callable(default_model) else default_model

    if "timeout" in parameters:
        # 显式 None 表示关闭配置的单请求超时；未提供才继承 default_timeout。
        effective_timeout = parameters.pop("timeout")
    else:
        effective_timeout = default_timeout
    if effective_timeout is not None:
        parameters["timeout"] = effective_timeout
    return PreparedCall(parameters=parameters, effective_timeout=effective_timeout)


class LLMCallRuntime:
    """调用注入客户端，并统一记录参数、耗时和关联事件。"""

    def __init__(self, business_logger: Any) -> None:
        """保存用于可选调用参数明细的业务 logger。"""
        self._business_logger = business_logger

    @staticmethod
    def _tools_digest(tools: Sequence[Any]) -> list[Any]:
        """把工具压缩为可记录的 JSON 描述。"""
        return [tool.to_json() if hasattr(tool, "to_json") else str(tool) for tool in tools]

    def _log_parameters(
        self,
        *,
        enabled: bool,
        agent_name: str,
        task_type: Optional[str],
        request_id: str,
        llm_request_id: str,
        prepared: PreparedCall,
        asynchronous: bool,
    ) -> None:
        """按业务日志开关记录最终生效的调用参数。"""
        if not enabled:
            return
        data = {
            "task_type": task_type,
            "kwargs": prepared.parameters,
            "timeout": prepared.effective_timeout,
        }
        self._business_logger.debug(
            {
                "agent": agent_name,
                "event": "llm_call_parameters",
                "request_id": request_id,
                "llm_request_id": llm_request_id,
                "data": data,
            }
        )

    def _emit_start(
        self,
        *,
        task_type: Optional[str],
        llm_request_id: str,
        messages: Any,
        tools: Sequence[Any],
        retry_count: int,
        prepared: PreparedCall,
        event_emitter: Callable[..., None],
    ) -> None:
        """在访问 provider 前发出 LLM start 事件。"""
        data = {
            "messages_digest": digest_messages(messages),
            "tools": self._tools_digest(tools),
            "attempt": retry_count,
            "task_type": task_type,
        }
        event_emitter(
            event="llm_call_start",
            llm_id=llm_request_id,
            model=prepared.parameters.get("model"),
            data=data,
        )

    def _emit_end(
        self,
        *,
        llm_request_id: str,
        prepared: PreparedCall,
        retry_count: int,
        started_at: float,
        reasoning_content: Any,
        answer_content: Any,
        tool_calls: Any,
        event_emitter: Callable[..., None],
    ) -> None:
        """成功后记录摘要、工具调用与墙钟耗时。"""
        data = {
            "answer_digest": digest_result(answer_content),
            "reasoning_digest": digest_result(reasoning_content) if reasoning_content else None,
            "tool_calls": tool_calls,
            "duration_ms": int((time.perf_counter() - started_at) * 1000.0),
            "attempt": retry_count,
        }
        event_emitter(
            event="llm_call_end",
            llm_id=llm_request_id,
            model=prepared.parameters.get("model"),
            data=data,
        )

    def _emit_error(
        self,
        *,
        llm_request_id: str,
        prepared: PreparedCall,
        retry_count: int,
        started_at: float,
        error: Exception,
        event_emitter: Callable[..., None],
    ) -> None:
        """失败后记录异常类型、重试序号与耗时，再由调用方重新抛出。"""
        data = {
            "error": f"{type(error).__name__}: {error}",
            "attempt": retry_count,
            "duration_ms": int((time.perf_counter() - started_at) * 1000.0),
        }
        event_emitter(
            event="llm_call_error",
            llm_id=llm_request_id,
            model=prepared.parameters.get("model"),
            data=data,
            level="ERROR",
        )

    def call_sync(
        self,
        client: Any,
        *,
        agent_name: str,
        task_type: Optional[str],
        business_logging: bool,
        request_id: str,
        llm_request_id: str,
        messages: Any,
        tools: Sequence[Any],
        retry_count: int,
        prepared: PreparedCall,
        event_emitter: Callable[..., None],
    ) -> Any:
        """执行一次同步 provider 调用。

        Provider 客户端统一返回 ``(reasoning, answer, tool_calls)``；本方法只包装
        观测事件，不吞掉异常。
        """
        self._log_parameters(
            enabled=business_logging,
            agent_name=agent_name,
            task_type=task_type,
            request_id=request_id,
            llm_request_id=llm_request_id,
            prepared=prepared,
            asynchronous=False,
        )
        started_at = time.perf_counter()
        self._emit_start(
            task_type=task_type,
            llm_request_id=llm_request_id,
            messages=messages,
            tools=tools,
            retry_count=retry_count,
            prepared=prepared,
            event_emitter=event_emitter,
        )
        try:
            # 只有这一行进入具体 provider；其余逻辑均与 provider 无关。
            result = client.call_llm(
                request_id=llm_request_id,
                messages=messages,
                tools=tools,
                **prepared.parameters,
            )
            reasoning, answer, tool_calls = result
            self._emit_end(
                llm_request_id=llm_request_id,
                prepared=prepared,
                retry_count=retry_count,
                started_at=started_at,
                reasoning_content=reasoning,
                answer_content=answer,
                tool_calls=tool_calls,
                event_emitter=event_emitter,
            )
            return reasoning, answer, tool_calls
        except Exception as error:
            self._emit_error(
                llm_request_id=llm_request_id,
                prepared=prepared,
                retry_count=retry_count,
                started_at=started_at,
                error=error,
                event_emitter=event_emitter,
            )
            raise

    async def call_async(
        self,
        client: Any,
        *,
        agent_name: str,
        task_type: Optional[str],
        business_logging: bool,
        request_id: str,
        llm_request_id: str,
        messages: Any,
        tools: Sequence[Any],
        retry_count: int,
        prepared: PreparedCall,
        event_emitter: Callable[..., None],
    ) -> Any:
        """执行一次异步 provider 调用，返回契约与同步版本一致。"""
        self._log_parameters(
            enabled=business_logging,
            agent_name=agent_name,
            task_type=task_type,
            request_id=request_id,
            llm_request_id=llm_request_id,
            prepared=prepared,
            asynchronous=True,
        )
        started_at = time.perf_counter()
        self._emit_start(
            task_type=task_type,
            llm_request_id=llm_request_id,
            messages=messages,
            tools=tools,
            retry_count=retry_count,
            prepared=prepared,
            event_emitter=event_emitter,
        )
        try:
            # 异步路径与同步路径使用同一个 PreparedCall 和事件模型。
            result = await client.call_async_llm(
                request_id=llm_request_id,
                messages=messages,
                tools=tools,
                **prepared.parameters,
            )
            reasoning, answer, tool_calls = result
            self._emit_end(
                llm_request_id=llm_request_id,
                prepared=prepared,
                retry_count=retry_count,
                started_at=started_at,
                reasoning_content=reasoning,
                answer_content=answer,
                tool_calls=tool_calls,
                event_emitter=event_emitter,
            )
            return reasoning, answer, tool_calls
        except Exception as error:
            self._emit_error(
                llm_request_id=llm_request_id,
                prepared=prepared,
                retry_count=retry_count,
                started_at=started_at,
                error=error,
                event_emitter=event_emitter,
            )
            raise
