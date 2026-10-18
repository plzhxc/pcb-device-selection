"""模型工具调用的解析、过滤、执行与结果回写。

处理链为：provider tool_calls → 参数解析 → 重复/未知工具过滤 → 调用已注册工具 →
把 tool 角色消息写回短期记忆 → 通知 Agent 钩子。runner 看到至少一个工具成功
执行后会立即开始下一轮 LLM，使模型能够基于工具结果继续推理。
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable, Optional, TypedDict

from ..messages import Message
from .response_parsing import safe_json_loads


class ToolCallSchema(TypedDict):
    """runtime 内部统一的单条工具调用状态。"""

    tool_call_name: str
    parameters: dict[str, Any]
    tool_call_id: str
    call_fingerprint: str
    has_executed: bool
    result: Optional[Any]


class ToolRuntime:
    """封装工具机制，但不成为新的 Agent 继承边界。

    provider 函数用于在每次访问时取得 BaseAgent 当前对象，服务本身不复制工具
    注册表或记忆状态。
    """

    def __init__(
        self,
        context: Callable[[], Any],
        logger: Callable[[], Any],
        business_logger: Callable[[], Any],
    ) -> None:
        """保存上下文和 logger 的延迟 provider。"""
        self._context_provider = context
        self._logger_provider = logger
        self._business_logger_provider = business_logger

    @property
    def _context(self) -> Any:
        """取得当前上下文容器。"""
        return self._context_provider()

    @property
    def _logger(self) -> Any:
        """取得当前诊断 logger。"""
        return self._logger_provider()

    @property
    def _business_logger(self) -> Any:
        """取得结构化业务 logger。"""
        return self._business_logger_provider()

    def available(self) -> bool:
        """判断上下文是否注册了至少一个工具。"""
        if self._context.available_tools is None:
            self._logger.warning(
                msg={
                    "event": "no_available_tools",
                    "data": {"available_tools": self._context.available_tools},
                }
            )
            return False
        return True

    def parse(
        self,
        request_id: str,
        tool_calls: list[Any],
        *,
        executed_fingerprints: set[str],
        injection: Callable[..., Any],
        hook_kwargs: dict[str, Any],
    ) -> list[ToolCallSchema]:
        """把 provider tool_calls 转成可安全执行的内部结构。

        非字典参数会降级为空字典；重复调用会收到一条 tool 角色纠错消息；模型
        请求未注册工具时只记录告警，不进入执行队列。
        """
        parsed_calls: list[ToolCallSchema] = []
        available_tools = self._context.available_tools
        for tool_call in tool_calls:
            function = tool_call["function"]
            tool_call_name = function["name"]
            parameters = safe_json_loads(function["arguments"])
            if not isinstance(parameters, dict):
                self._logger.warning(
                    msg={
                        "event": "invalid_tool_arguments",
                        "request_id": request_id,
                        "data": {
                            "tool_call_name": tool_call_name,
                            "tool_call_id": tool_call.get("id"),
                            "raw_arguments": function.get("arguments"),
                            "parsed_type": type(parameters).__name__,
                        },
                    }
                )
                parameters = {}

            # 指纹只由工具名和规范化参数构成，用于阻止同步循环重复执行同一动作。
            fingerprint = f"{tool_call_name}:{json.dumps(parameters, sort_keys=True)}"
            tool_call_id = tool_call["id"]
            # 兼容扩展钩子可原地补充参数；返回值不参与 runtime 控制流。
            injection(tool_call_name=tool_call_name, parameters=parameters, **hook_kwargs)

            if fingerprint in executed_fingerprints:
                self._logger.warning(
                    msg={
                        "event": "detected_duplicate_tool_call",
                        "data": {
                            "tool_call_name": tool_call_name,
                            "tool_call_id": tool_call_id,
                        },
                    }
                )
                self._context.short_term_memory.add_turn(
                    Message(
                        role="tool",
                        content=(
                            "SYSTEM ERROR: You have ALREADY executed "
                            f"{tool_call_name} with these EXACT parameters. Repeating "
                            "the same action will NOT yield different results. Please "
                            "proceed to generate the final output JSON based on the "
                            "information you already have."
                        ),
                        tool_call_id=tool_call_id,
                    )
                )
                continue

            if tool_call_name not in available_tools:
                self._logger.warning(
                    msg={
                        "event": "tool_not_in_available_tools",
                        "request_id": request_id,
                        "data": {
                            "tool_call_name": tool_call_name,
                            "tool_call_id": tool_call_id,
                            "available_tools": available_tools,
                        },
                    }
                )
                continue

            parsed_calls.append(
                ToolCallSchema(
                    tool_call_name=tool_call_name,
                    parameters=parameters,
                    tool_call_id=tool_call_id,
                    call_fingerprint=fingerprint,
                    has_executed=False,
                    result=None,
                )
            )
        return parsed_calls

    def log_end(
        self,
        request_id: str,
        calls: list[ToolCallSchema],
        *,
        enabled: bool,
    ) -> None:
        """按开关记录整轮工具调用的最终状态。"""
        if enabled:
            self._business_logger.debug(
                {
                    "agent": self._context.agent_name,
                    "event": "tool_calls_end",
                    "request_id": request_id,
                    "data": {"tool_calls_schema": calls},
                }
            )

    def _notify_hook(
        self,
        *,
        request_id: str,
        call: ToolCallSchema,
        result: Any,
        on_executed: Callable[..., Any],
        hook_kwargs: dict[str, Any],
    ) -> None:
        """隔离执行后钩子异常，避免观测/同步逻辑推翻工具主结果。"""
        try:
            on_executed(
                request_id=request_id,
                tool_call_name=call["tool_call_name"],
                parameters=call["parameters"],
                result=result,
                **hook_kwargs,
            )
        except Exception as error:
            self._logger.error(
                {
                    "event": "tool_call_hook_error",
                    "agent": self._context.agent_name,
                    "request_id": request_id,
                    "data": {
                        "tool_call_name": call["tool_call_name"],
                        "tool_call_id": call["tool_call_id"],
                        "error": str(error),
                    },
                }
            )

    def process_sync(
        self,
        request_id: str,
        tool_calls: list[dict[str, Any]],
        *,
        parser: Callable[..., list[ToolCallSchema]],
        executed_fingerprints: set[str],
        on_executed: Callable[..., Any],
        log_end: Callable[..., Any],
        hook_kwargs: dict[str, Any],
    ) -> bool:
        """顺序执行同步工具，并返回是否至少写回一个有效结果。

        返回 ``True`` 时 runner 会携带新增 tool 消息进入下一轮 LLM；返回
        ``False`` 时继续尝试解析当前回答的结构化输出。
        """
        calls = parser(request_id=request_id, tool_calls=tool_calls, **hook_kwargs)
        if not calls:
            return False

        executed_any = False
        for call in calls:
            # 同步工具异常沿用既有行为向上抛出；None 则转成模型可见的纠错消息。
            result = self._context.available_tools[call["tool_call_name"]].call(
                **call["parameters"]
            )
            if result is None:
                self._context.short_term_memory.add_turn(
                    Message(
                        role="tool",
                        content=(
                            f"SYSTEM ERROR: {call['tool_call_name']} returned None. "
                            "Please check the parameters and try again."
                        ),
                    )
                )
                continue
            executed_fingerprints.add(call["call_fingerprint"])
            self._context.short_term_memory.add_turn(
                Message(role="tool", content=result, tool_call_id=call["tool_call_id"])
            )
            call["has_executed"] = True
            call["result"] = result
            self._notify_hook(
                request_id=request_id,
                call=call,
                result=result,
                on_executed=on_executed,
                hook_kwargs=hook_kwargs,
            )
            executed_any = True

        log_end(request_id=request_id, tool_calls_schema=calls)
        return executed_any

    async def process_async(
        self,
        request_id: str,
        tool_calls: list[Any],
        *,
        parser: Callable[..., list[ToolCallSchema]],
        on_executed: Callable[..., Any],
        log_end: Callable[..., Any],
        hook_kwargs: dict[str, Any],
    ) -> bool:
        """顺序执行异步工具，并把执行异常转换为 tool 消息。

        当前异步契约只接受 coroutine ``call``；同步工具不会在线程池中隐式运行。
        """
        calls = parser(request_id=request_id, tool_calls=tool_calls, **hook_kwargs)
        if not calls:
            return False

        executed_any = False
        for call in calls:
            tool = self._context.available_tools[call["tool_call_name"]]
            if hasattr(tool, "call") and inspect.iscoroutinefunction(tool.call):
                try:
                    result = await tool.call(**call["parameters"])
                except Exception as error:
                    result = (
                        f"Tool Error: {call['tool_call_name']} 执行失败: "
                        f"{type(error).__name__}: {error}"
                    )
                    self._logger.error(
                        {
                            "event": "tool_async_execution_error",
                            "agent": self._context.agent_name,
                            "request_id": request_id,
                            "data": {
                                "tool_call_name": call["tool_call_name"],
                                "error": str(error),
                            },
                        }
                    )
                self._context.short_term_memory.add_turn(
                    Message(role="tool", content=result, tool_call_id=call["tool_call_id"])
                )
            else:
                result = None
                self._logger.error(
                    {
                        "event": "tool_call_not_coroutine",
                        "agent": self._context.agent_name,
                        "request_id": request_id,
                        "data": {
                            "tool_call_name": call["tool_call_name"],
                            "tool_call_id": call["tool_call_id"],
                        },
                    }
                )
                self._context.short_term_memory.add_turn(
                    Message(
                        role="tool",
                        content="Tool Error: 工具执行失败,没有获得对应的工具结果",
                        tool_call_id=call["tool_call_id"],
                    )
                )

            call["has_executed"] = True
            call["result"] = result
            self._notify_hook(
                request_id=request_id,
                call=call,
                result=result,
                on_executed=on_executed,
                hook_kwargs=hook_kwargs,
            )
            executed_any = True

        log_end(request_id=request_id, tool_calls_schema=calls)
        return executed_any
