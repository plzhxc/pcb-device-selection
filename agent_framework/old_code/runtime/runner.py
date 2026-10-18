"""一次 BaseAgent 调用的同步/异步生命周期状态机。

主循环固定为：

1. 清理上次运行状态并调用 Agent ``format``；
2. 组装消息并请求 LLM；
3. 若模型请求工具，则执行工具并回到第 2 步；
4. 否则提取/校验结构化输出；失败反馈写入记忆并重试；
5. 通过可选业务校验与后处理后结束。

本模块通过 ``host`` 调用 BaseAgent 的稳定钩子，不导入 BaseAgent 类，从而避免
runtime 与继承层之间形成循环依赖。同步和异步循环刻意保持同一语义。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from ..messages import Message, messages_for_log

from .events import digest_result
from .llm import INHERIT_LLM_TIMEOUT


@dataclass
class RunState:
    """一次 run 内部共享的可变状态。

    ``count`` 统计 LLM/输出重试轮次，``tool_iteration`` 单独限制工具循环，避免
    工具调用消耗普通输出重试的语义。
    """

    request_id: str
    request_info: dict[str, Any]
    on_chunk: Any
    run_llm_timeout: Any = INHERIT_LLM_TIMEOUT
    count: int = 0
    tool_iteration: int = 0
    result: Any = None


class AgentRunRuntime:
    """编排一个 Agent 实例，同时把业务扩展点保留在 host 上。"""

    def __init__(
        self,
        *,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        sync_sleep: Callable[[float], None] = time.sleep,
        async_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        """注入 ID 与等待实现，生产使用默认值，测试可替换为确定性实现。"""
        # 注入 ID、sleep 和 async sleep，使重试测试不依赖真实时间或随机 UUID。
        self._id_factory = id_factory
        self._sync_sleep = sync_sleep
        self._async_sleep = async_sleep

    @staticmethod
    def _tools(host: Any) -> list[Any]:
        """读取本轮 format 已注册的工具对象。"""
        available = host.context_container.available_tools
        return list(available.values()) if available else []

    @staticmethod
    def _business(
        host: Any,
        event: str,
        request_id: str,
        data: dict[str, Any],
        *,
        level: str = "debug",
        force: bool = False,
        correlate: bool = False,
    ) -> None:
        """写可选业务明细；force 用于即使关闭明细也必须记录的失败。"""
        if not force and not host.enable_business_logging:
            return
        record: dict[str, Any] = {
            "agent": host.agent_name,
            "event": event,
            "request_id": request_id,
            "data": data,
        }
        if correlate:
            record.update(
                {
                    "agent_id": request_id,
                    "stage_id": host._current_stage_id,
                    "flow_id": host._current_flow_id,
                }
            )
        getattr(host.business_logger, level)(record)

    def _prepare(self, host: Any, kwargs: dict[str, Any], *, asynchronous: bool) -> RunState:
        """初始化一次运行，并从业务 kwargs 中取出 runtime 专用字段。

        传入的 ``kwargs`` 是 BaseAgent 创建的本地字典，可以安全 pop；剩余字段才
        会交给 ``format`` 和业务钩子。
        """
        # 同一 Agent 实例可以重复运行，因此每次都重建 prompt、记忆、工具和输出声明。
        host.context_container.clear_all()
        on_chunk = kwargs.pop("on_chunk", None)
        run_llm_timeout = kwargs.pop("llm_timeout", INHERIT_LLM_TIMEOUT)
        host._executed_tool_calls = set()

        # 这些字段用于观测关联，不应泄漏到业务 prompt。
        flow_id = kwargs.pop("flow_id", None)
        stage_id = kwargs.pop("stage_id", None)
        agent_id = kwargs.pop("agent_id", None)
        request_id_alias = kwargs.pop("request_id", None)
        request_id = agent_id or request_id_alias or self._id_factory()
        host._current_flow_id = flow_id
        host._current_stage_id = stage_id
        host._current_agent_id = request_id

        if asynchronous:
            request_info = host._creat_request_info(request_id=request_id)
            host._business_start_log(request_id=request_id)
        else:
            request_info = {
                "request_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "request_id": request_id,
                "llm_request_id": [],
            }

        host._emit_agent_event(
            event="agent_run_start",
            data={"kwargs_digest": digest_result(kwargs), "task_type": host.task_type},
        )
        if not asynchronous:
            self._business(
                host,
                "agent_run_start",
                request_id,
                {},
                correlate=True,
            )
        return RunState(
            request_id=request_id,
            request_info=request_info,
            on_chunk=on_chunk,
            run_llm_timeout=run_llm_timeout,
        )

    def _log_call_start(
        self,
        host: Any,
        state: RunState,
        messages: Any,
        tools: list[Any],
        kwargs: dict[str, Any],
        *,
        asynchronous: bool,
    ) -> None:
        """记录发给 LLM 的安全消息摘要、工具声明和业务参数。"""
        self._business(
            host,
            "agent_async_call_llm" if asynchronous else "agent_call_llm",
            state.request_id,
            {
                "messages": messages_for_log(messages),
                "tools": [tool.to_json() for tool in tools],
                "kwargs": kwargs,
            },
        )

    def _log_response(
        self,
        host: Any,
        state: RunState,
        llm_request_id: str,
        reasoning: Any,
        answer: Any,
        tool_calls: Any,
        *,
        asynchronous: bool,
    ) -> None:
        """记录一次模型响应；详细记录受业务日志开关控制。"""
        self._business(
            host,
            "agent_async_llm_response" if asynchronous else "agent_llm_response",
            state.request_id,
            {
                "count": state.count,
                "llm_request_id": llm_request_id,
                "reasoning_content": reasoning,
                "answer_content": answer,
                "tool_calls": tool_calls,
            },
        )

    @staticmethod
    def _remember_assistant(host: Any, reasoning: Any, answer: Any, tool_calls: Any) -> None:
        """把模型回答保存为 assistant turn，供工具循环或纠错重试复用。"""
        host.context_container.short_term_memory.add_turn(
            Message(
                role="assistant",
                content=answer,
                tool_calls=tool_calls,
                reasoning_content=reasoning,
            )
        )

    def _log_tool_start(
        self,
        host: Any,
        state: RunState,
        tool_calls: Any,
        *,
        asynchronous: bool,
    ) -> None:
        """记录即将执行的一组工具调用。"""
        self._business(
            host,
            "tool_async_call_start" if asynchronous else "tool_call_start",
            state.request_id,
            {"count": state.count, "tool_calls": tool_calls},
        )

    @staticmethod
    def _warn_tool_limit(host: Any, *, asynchronous: bool) -> None:
        """达到工具循环上限时发出诊断告警。"""
        if asynchronous:
            event = "tool_warning"
        else:
            event = f"{host.agent_name}_tool_warning"
        host.logger.warning(
            msg={
                "event": event,
                "data": {
                    "content": (
                        "⚠️ 警告: 工具调用次数超过限制 "
                        f"({host.max_tool_iterations})，忽略后续工具调用"
                    )
                },
            }
        )

    def _validate_hook(
        self,
        host: Any,
        state: RunState,
        result: Any,
        kwargs: dict[str, Any],
    ) -> tuple[Any, bool]:
        """执行子类可选的业务级结果校验。

        Schema 校验只判断形状；该钩子可检查分类树、预算约束等业务规则。失败
        信息同样写回短期记忆，让模型在下一轮修正。
        """
        if not hasattr(host, "_validate_structured_output"):
            return result, True
        valid, error_message = host._validate_structured_output(result, **kwargs)
        if valid or not error_message:
            return result, True
        host.context_container.short_term_memory.add_turn(
            Message(
                role="user",
                content=f"输出未通过校验：{error_message}。请按要求重新输出。",
            )
        )
        self._business(
            host,
            "structured_output_validation_fail",
            state.request_id,
            {"error": error_message, "result": result},
        )
        return result, False

    @staticmethod
    def _post_process(host: Any, result: Any, kwargs: dict[str, Any]) -> Any:
        """执行子类可选的确定性结果后处理。"""
        if hasattr(host, "_post_process_result"):
            return host._post_process_result(result, **kwargs)
        return result

    def _finish_success(
        self,
        host: Any,
        state: RunState,
        *,
        result: Any,
        messages: Any,
        reasoning: Any,
        answer: Any,
        tool_calls: Any,
        asynchronous: bool,
    ) -> Any:
        """统一记录成功事件并返回最终结果。"""
        self._business(
            host,
            "agent_async_run_end" if asynchronous else "agent_run_end",
            state.request_id,
            {
                "request_info": state.request_info,
                "result": result,
                "last_prompt": messages_for_log(messages),
                "last_response": answer,
                "last_tool_calls": tool_calls,
                "last_reasoning_content": reasoning,
            },
            correlate=True,
        )
        host._emit_agent_event(
            event="agent_run_end",
            data={"result_digest": digest_result(result), "retries": state.count},
        )
        return result

    def _finish_failure(self, host: Any, state: RunState, *, asynchronous: bool) -> None:
        """重试耗尽后记录最终失败；返回值由公开入口统一设为 None。"""
        if not asynchronous:
            host.logger.warning(
                msg={
                    "event": f"{host.agent_name}_fail",
                    "data": {"result": state.result},
                }
            )
        self._business(
            host,
            "async_agent_run_fail" if asynchronous else "agent_run_fail",
            state.request_id,
            {"count": state.count, "result": state.result},
            correlate=True,
        )
        host._emit_agent_event(
            event="agent_run_error",
            data={
                "reason": "retries_exhausted",
                "retries": state.count,
                "last_result_digest": digest_result(state.result),
            },
            level="ERROR",
        )

    def _log_call_failure(self, host: Any, state: RunState, error: Exception) -> None:
        """记录 provider 调用异常；失败记录不受业务明细开关影响。"""
        self._business(
            host,
            "llm_call_fail",
            state.request_id,
            {"content": str(error), "request_info": state.request_info},
            level="error",
            force=True,
        )

    def run(self, host: Any, kwargs: dict[str, Any]) -> Any:
        """执行同步状态机，成功返回业务结果，重试耗尽返回 ``None``。"""
        state = self._prepare(host, kwargs, asynchronous=False)
        # format 只负责声明上下文；直到进入循环才真正请求模型。
        host.format(**kwargs)

        while state.count < host.retries:
            # 每轮都重新从上下文取消息，因为工具结果/校验反馈可能刚被写入记忆。
            messages = host.context_container.to_message()
            tools = self._tools(host)
            llm_request_id = self._id_factory()
            state.request_info["llm_request_id"].append(llm_request_id)
            self._log_call_start(host, state, messages, tools, kwargs, asynchronous=False)
            call_kwargs = (
                {}
                if state.run_llm_timeout is INHERIT_LLM_TIMEOUT
                else {"timeout": state.run_llm_timeout}
            )
            try:
                # LLMCallRuntime 在 provider 调用两侧统一发出 start/end/error 事件。
                reasoning, answer, tool_calls = host._call_llm(
                    request_id=state.request_id,
                    llm_request_id=llm_request_id,
                    messages=messages,
                    tools=tools,
                    on_chunk=state.on_chunk,
                    retry_count=state.count,
                    **call_kwargs,
                )
                self._log_response(
                    host,
                    state,
                    llm_request_id,
                    reasoning,
                    answer,
                    tool_calls,
                    asynchronous=False,
                )
            except TimeoutError as error:
                host.logger.warning(
                    msg={
                        "agent": host.agent_name,
                        "event": "llm_call_timeout",
                        "request_id": state.request_id,
                        "data": {"content": str(error), "retry": state.count + 1},
                    }
                )
                self._business(
                    host,
                    "llm_call_timeout",
                    state.request_id,
                    {"content": str(error), "request_info": state.request_info},
                    level="error",
                    force=True,
                )
                state.count += 1
                continue
            except Exception as error:
                self._log_call_failure(host, state, error)
                if host._is_rate_limit_error(error):
                    delay = host._rate_limit_backoff_seconds(state.count)
                    host.logger.warning(
                        msg={
                            "agent": host.agent_name,
                            "event": "llm_rate_limit_backoff",
                            "request_id": state.request_id,
                            "data": {
                                "retry": state.count + 1,
                                "sleep_s": round(delay, 2),
                                "error": str(error),
                            },
                        }
                    )
                    self._sync_sleep(delay)
                state.count += 1
                continue

            # 先保存 assistant turn；若执行工具，下一轮会同时看到请求与工具结果。
            self._remember_assistant(host, reasoning, answer, tool_calls)
            if tool_calls is not None:
                self._log_tool_start(host, state, tool_calls, asynchronous=False)
                if state.tool_iteration >= host.max_tool_iterations:
                    self._warn_tool_limit(host, asynchronous=False)
                elif host._process_tool_calls(
                    request_id=state.request_id,
                    tool_calls=tool_calls,
                    **kwargs,
                ):
                    # 至少一个工具成功执行：跳回循环，让模型消费新增的 tool 消息。
                    state.tool_iteration += 1
                    continue
                self._business(
                    host,
                    "structured_output_start",
                    state.request_id,
                    {"count": state.count, "answer_content": answer},
                )

            # 无工具可继续执行时，才把当前 answer 当作最终候选结果解析。
            state.result, success = host._process_structured_outputs(
                request_id=state.request_id,
                answer_content=answer,
                **kwargs,
            )
            if success:
                state.result, accepted = self._validate_hook(
                    host, state, state.result, kwargs
                )
                if not accepted:
                    # 业务校验已写回纠错消息，消耗一次重试后重新询问模型。
                    state.count += 1
                    continue
                host.logger.debug(
                    msg={
                        "event": f"{host.agent_name}_success",
                        "data": {"result": state.result},
                    }
                )
                state.result = self._post_process(host, state.result, kwargs)
                return self._finish_success(
                    host,
                    state,
                    result=state.result,
                    messages=messages,
                    reasoning=reasoning,
                    answer=answer,
                    tool_calls=tool_calls,
                    asynchronous=False,
                )
            self._business(
                host,
                "structured_output_fail",
                state.request_id,
                {
                    "count": state.count,
                    "answer_content": answer,
                    "result": state.result,
                },
            )
            state.count += 1

        self._finish_failure(host, state, asynchronous=False)
        return None

    async def async_run(self, host: Any, kwargs: dict[str, Any]) -> Any:
        """执行与 ``run`` 同语义的异步状态机，并在结束时关闭异步客户端。"""
        state = self._prepare(host, kwargs, asynchronous=True)
        # async_format 当前允许子类使用独立实现；默认仍同步调用 format。
        host.async_format(**kwargs)
        try:
            while state.count < host.retries:
                # 下方阶段与同步版本保持一致：LLM → tools → output → validation。
                messages = host.context_container.to_message()
                tools = self._tools(host)
                llm_request_id = self._id_factory()
                state.request_info["llm_request_id"].append(llm_request_id)
                self._log_call_start(host, state, messages, tools, kwargs, asynchronous=True)
                call_kwargs = (
                    {}
                    if state.run_llm_timeout is INHERIT_LLM_TIMEOUT
                    else {"timeout": state.run_llm_timeout}
                )
                try:
                    reasoning, answer, tool_calls = await host._async_call_llm(
                        request_id=state.request_id,
                        llm_request_id=llm_request_id,
                        messages=messages,
                        tools=tools,
                        on_chunk=state.on_chunk,
                        retry_count=state.count,
                        **call_kwargs,
                    )
                    self._log_response(
                        host,
                        state,
                        llm_request_id,
                        reasoning,
                        answer,
                        tool_calls,
                        asynchronous=True,
                    )
                except Exception as error:
                    self._log_call_failure(host, state, error)
                    if host._is_rate_limit_error(error):
                        delay = host._rate_limit_backoff_seconds(state.count)
                        host.logger.warning(
                            msg={
                                "agent": host.agent_name,
                                "event": "llm_rate_limit_backoff",
                                "request_id": state.request_id,
                                "data": {
                                    "retry": state.count + 1,
                                    "sleep_s": round(delay, 2),
                                    "error": str(error),
                                },
                            }
                        )
                        await self._async_sleep(delay)
                    state.count += 1
                    continue

                self._remember_assistant(host, reasoning, answer, tool_calls)
                if tool_calls is not None:
                    self._log_tool_start(host, state, tool_calls, asynchronous=True)
                    if state.tool_iteration >= host.max_tool_iterations:
                        self._warn_tool_limit(host, asynchronous=True)
                    elif await host._async_process_tool_calls(
                        request_id=state.request_id,
                        tool_calls=tool_calls,
                        **kwargs,
                    ):
                        state.tool_iteration += 1
                        continue

                self._business(
                    host,
                    "async_structured_output_start",
                    state.request_id,
                    {"count": state.count, "answer_content": answer},
                )
                state.result, success = host._process_structured_outputs(
                    request_id=state.request_id,
                    answer_content=answer,
                    **kwargs,
                )
                if success:
                    state.result, accepted = self._validate_hook(
                        host, state, state.result, kwargs
                    )
                    if not accepted:
                        state.count += 1
                        continue
                    state.result = self._post_process(host, state.result, kwargs)
                    return self._finish_success(
                        host,
                        state,
                        result=state.result,
                        messages=messages,
                        reasoning=reasoning,
                        answer=answer,
                        tool_calls=tool_calls,
                        asynchronous=True,
                    )
                self._business(
                    host,
                    "async_structured_output_fail",
                    state.request_id,
                    {
                        "count": state.count,
                        "answer_content": answer,
                        "result": state.result,
                    },
                )
                state.count += 1

            self._finish_failure(host, state, asynchronous=True)
            return None
        finally:
            # 无论成功、耗尽重试还是抛出取消/异常，都尝试释放连接池。
            try:
                await host.async_llm.close()
            except Exception:
                pass
