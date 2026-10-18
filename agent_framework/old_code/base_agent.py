"""所有应用共享的唯一 Agent 继承边界。

``BaseAgent`` 保存 Agent 的公共状态并暴露少量扩展钩子；真正的执行机制通过组合
委托给 ``agent_framework.runtime``。这里不再定义第二套 VLM Agent 或包装基类：
文本和多模态输入都由同一个消息契约处理。

一次调用的主链路是：``format`` 组装上下文 → LLM → 可选工具调用 → 结构化输出
提取/校验 → 可选业务校验与后处理。业务代码统一从
``agent_framework.BaseAgent`` 导入本类。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .runtime import (
    AgentRunRuntime,
    EventCorrelation,
    EventRuntime,
    INHERIT_LLM_TIMEOUT,
    LLMCallRuntime,
    RetryRuntime,
    StructuredOutputRuntime,
    ToolCallSchema,
    ToolRuntime,
    prepare_call_parameters,
)
from .runtime.context import ContextDataContainer
from .contracts import AgentServices, LoggerProtocol
from .services import get_default_agent_services
from .tools import BaseTool


class BaseAgent(ABC):
    """所有应用智能体共享的唯一基类。

    子类只负责实现 :meth:`format`，并按需覆写工具、业务校验和结果后处理钩子。
    模型选择、上下文、LLM/工具执行、重试和结构化输出由本类及其 runtime 服务
    统一管理。需要流式展示时，在 ``run``/``async_run`` 调用处显式传入
    ``on_chunk``，不使用构造期的显示开关。

    Args:
        logger: 可选的实例级诊断 logger 覆盖。
        enable_business_logging: 是否记录详细业务日志；``None`` 继承中心配置。
        task_type: 用于查找模型和调用参数默认值的任务配置键。
        llm_timeout: 单次 LLM 请求超时；哨兵值表示继承中心配置。
        model: 当前 Agent 的显式模型覆盖。
        reasoning: 当前 Agent 的显式 reasoning 模式覆盖。
    """

    def __init__(
        self,
        logger: LoggerProtocol | None = None,
        enable_business_logging: bool | None = None,
        task_type: Optional[str] = None,
        llm_timeout: Optional[float] | object = INHERIT_LLM_TIMEOUT,
        model: Optional[str] = None,
        reasoning: Optional[bool] = None,
        services: AgentServices | None = None,
    ):
        """初始化实例级客户端、上下文、日志设置和组合式 runtime 服务。"""
        # services 是应用与框架之间唯一的基础设施边界。框架不会反向导入应用。
        self.services = services or get_default_agent_services()
        self.settings = self.services.settings

        # 每个 Agent 实例独占客户端包装器与可变上下文，避免并发调用串状态。
        self.retries = 7
        self.max_tool_iterations = 10
        self.agent_name = self.__class__.__name__
        self.async_llm = self.services.async_llm_factory()
        self.llm = self.services.sync_llm_factory()
        self.logger = logger or self.services.logger
        self.business_logger = self.services.business_logger
        self.llm_logger = self.services.llm_logger
        self.context_container = ContextDataContainer(
            agent_name=self.agent_name,
            logger=self.business_logger,
        )
        self.task_type = task_type
        self.model = model
        self.reasoning = reasoning
        self.llm_timeout = (
            self._resolve_inherited_timeout()
            if llm_timeout is INHERIT_LLM_TIMEOUT
            else llm_timeout
        )
        self._executed_tool_calls: set[str] = set()

        if enable_business_logging is None:
            enable_business_logging = self.settings.business_logging_enabled
        self.enable_business_logging = enable_business_logging

        # 每次 run 开始时由 AgentRunRuntime 写入，用于串联 flow → stage → agent 日志。
        self._current_flow_id: Optional[str] = None
        self._current_stage_id: Optional[str] = None
        self._current_agent_id: Optional[str] = None

        # 以下对象都是组合服务，不进入继承树；BaseAgent 是唯一业务继承边界。
        self._event_runtime = EventRuntime(
            self.business_logger,
            lambda: self.llm_logger,
        )
        self._retry_runtime = RetryRuntime()
        self._llm_runtime = LLMCallRuntime(self.business_logger)
        self._tool_runtime = ToolRuntime(
            lambda: self.context_container,
            lambda: self.logger,
            lambda: self.business_logger,
        )
        self._structured_output_runtime = StructuredOutputRuntime(
            lambda: self.context_container,
            lambda: self.logger,
            lambda: self.business_logger,
        )
        self._run_runtime = AgentRunRuntime()

    # ------------------------------------------------------------------
    # 事件关联：所有 Agent/LLM 事件都复用当前运行的三级关联标识。
    # ------------------------------------------------------------------
    def _correlation(self) -> EventCorrelation:
        """返回当前运行对应的 flow、stage 和 agent 标识。"""
        return EventCorrelation(
            flow_id=self._current_flow_id,
            stage_id=self._current_stage_id,
            agent_id=self._current_agent_id,
        )

    def _emit_agent_event(
        self,
        event: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        level: str = "INFO",
    ) -> None:
        """发出 Agent 级事件，并自动附加当前关联标识。"""
        self._event_runtime.emit_agent(
            self.agent_name,
            event,
            self._correlation(),
            data,
            level=level,
        )

    def _emit_llm_event(
        self,
        event: str,
        llm_id: str,
        *,
        model: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
        level: str = "INFO",
    ) -> None:
        """发出一次 LLM 请求事件，并关联到当前 Agent。"""
        self._event_runtime.emit_llm(
            event,
            llm_id,
            self._correlation(),
            model=model,
            data=data,
            level=level,
        )

    # ------------------------------------------------------------------
    # 配置解析：实例显式覆盖优先，其次 task 配置，最后使用全局默认值。
    # ------------------------------------------------------------------
    def _resolve_inherited_timeout(self) -> Optional[float]:
        """按 task_type 解析继承超时，未配置则回退全局默认。"""
        if self.task_type:
            task_timeout = self.settings.task_timeout_mapping.get(self.task_type)
            if task_timeout is not None:
                return task_timeout
        return self.settings.llm_request_timeout_s

    def _get_model_for_task(self) -> str:
        """解析本次调用使用的模型名称。"""
        if self.model:
            return self.model
        configured_model = (
            self.settings.task_model_mapping.get(self.task_type)
            if self.task_type
            else None
        )
        if configured_model is None:
            if self.task_type:
                self.logger.warning(
                    msg={
                        "event": "task_model_not_found",
                        "agent": self.agent_name,
                        "data": {"task_type": self.task_type},
                    }
                )
            return self.settings.default_model
        return configured_model

    def _get_thinking_enabled(self) -> bool:
        """解析 task 默认 reasoning，并应用实例级覆盖。"""
        thinking = self.settings.reasoning_mode_config.get(self.task_type, None)
        if thinking is None:
            self.logger.warning(
                msg={
                    "event": "thinking_params_not_found",
                    "agent": self.agent_name,
                    "data": {"task_type": self.task_type},
                }
            )
            thinking = False
        if self.reasoning is not None:
            thinking = self.reasoning
        return bool(thinking)

    def _get_call_params(self) -> Dict[str, Any]:
        """合并全局与 task 调用参数，仅保留实际配置的字段。"""

        params = dict(self.settings.llm_call_params)
        params.update(self.settings.task_llm_params.get(self.task_type, {}))
        return params

    def _is_rate_limit_error(self, error: Exception) -> bool:
        """把限流错误识别委托给无状态重试策略。"""
        return self._retry_runtime.is_rate_limit_error(error)

    def _rate_limit_backoff_seconds(self, retry_count: int) -> float:
        """按配置计算指数退避时间。"""
        return self._retry_runtime.backoff_seconds(
            retry_count,
            base=float(self.settings.rate_limit_backoff_base_s),
            cap=float(self.settings.rate_limit_backoff_max_s),
            jitter=float(self.settings.rate_limit_backoff_jitter_s),
        )

    def _prepare_llm_call(
        self,
        supplied: dict[str, Any],
        *,
        on_chunk: Optional[Callable[[str, Optional[str]], None]],
    ):
        """补齐模型、reasoning 和超时参数。"""
        return prepare_call_parameters(
            supplied,
            call_defaults=self._get_call_params(),
            enable_thinking=self._get_thinking_enabled(),
            default_model=self._get_model_for_task,
            on_chunk=on_chunk,
            default_timeout=self.llm_timeout,
        )

    # ------------------------------------------------------------------
    # LLM 调用边界：负责准备参数；日志、计时和异常事件由 LLMCallRuntime 处理。
    # ------------------------------------------------------------------
    def _call_llm(
        self,
        messages,
        tools: List[BaseTool],
        request_id: str,
        llm_request_id: str,
        on_chunk: Optional[Callable[[str, Optional[str]], None]] = None,
        retry_count: int = 0,
        **kwargs,
    ):
        """执行同步 LLM 请求。"""
        prepared = self._prepare_llm_call(
            kwargs,
            on_chunk=on_chunk,
        )
        return self._llm_runtime.call_sync(
            self.llm,
            agent_name=self.agent_name,
            task_type=self.task_type,
            business_logging=self.enable_business_logging,
            request_id=request_id,
            llm_request_id=llm_request_id,
            messages=messages,
            tools=tools or [],
            retry_count=retry_count,
            prepared=prepared,
            event_emitter=self._emit_llm_event,
        )

    async def _async_call_llm(
        self,
        messages,
        tools: List[BaseTool],
        request_id: str,
        llm_request_id: str,
        on_chunk: Optional[Callable[[str, Optional[str]], None]] = None,
        retry_count: int = 0,
        **kwargs,
    ):
        """执行异步 LLM 请求。"""
        prepared = self._prepare_llm_call(
            kwargs,
            on_chunk=on_chunk,
        )
        return await self._llm_runtime.call_async(
            self.async_llm,
            agent_name=self.agent_name,
            task_type=self.task_type,
            business_logging=self.enable_business_logging,
            request_id=request_id,
            llm_request_id=llm_request_id,
            messages=messages,
            tools=tools or [],
            retry_count=retry_count,
            prepared=prepared,
            event_emitter=self._emit_llm_event,
        )

    # ------------------------------------------------------------------
    # 工具扩展点：子类可在解析前注入参数，或在执行后同步自己的业务状态。
    # ------------------------------------------------------------------
    def _tool_calls_injection(self, tool_call_name: str, parameters: dict, **kwargs) -> bool:
        """工具解析前钩子；默认不修改参数，返回值当前不参与控制流。"""
        pass

    def _on_tool_call_executed(
        self,
        request_id: str,
        tool_call_name: str,
        parameters: dict,
        result: Any,
        **kwargs,
    ) -> None:
        """工具执行后钩子；默认无动作，子类可覆写。"""
        pass

    def _tool_avaliable(self) -> bool:
        """检查当前上下文是否注册了工具。

        方法名中的 ``avaliable`` 是既有受保护接口拼写，暂时保留给已有子类。
        """
        return self._tool_runtime.available()

    def _tool_parse(
        self,
        request_id: str,
        tool_calls: List,
        **kwargs,
    ) -> List[ToolCallSchema]:
        """解析并过滤模型返回的工具调用。"""
        return self._tool_runtime.parse(
            request_id,
            tool_calls,
            executed_fingerprints=self._executed_tool_calls,
            injection=self._tool_calls_injection,
            hook_kwargs=kwargs,
        )

    def _log_tool_calls_end(
        self,
        request_id: str,
        tool_calls_schema: List[ToolCallSchema],
        **kwargs,
    ):
        """记录一轮工具调用的最终状态。"""
        self._tool_runtime.log_end(
            request_id,
            tool_calls_schema,
            enabled=self.enable_business_logging,
        )

    def _process_tool_calls(self, request_id: str, tool_calls: List[Dict], **kwargs) -> bool:
        """同步执行合法工具调用；返回是否至少执行了一个工具。"""
        if not self._tool_avaliable():
            return False
        return self._tool_runtime.process_sync(
            request_id,
            tool_calls,
            parser=self._tool_parse,
            executed_fingerprints=self._executed_tool_calls,
            on_executed=self._on_tool_call_executed,
            log_end=self._log_tool_calls_end,
            hook_kwargs=kwargs,
        )

    async def _async_process_tool_calls(
        self,
        request_id: str,
        tool_calls: List,
        **kwargs,
    ) -> bool:
        """异步执行合法工具调用；返回是否至少执行了一个工具。"""
        if not self._tool_avaliable():
            return False
        return await self._tool_runtime.process_async(
            request_id,
            tool_calls,
            parser=self._tool_parse,
            on_executed=self._on_tool_call_executed,
            log_end=self._log_tool_calls_end,
            hook_kwargs=kwargs,
        )

    # ------------------------------------------------------------------
    # 结构化输出：校验失败信息会写回短期记忆，让下一轮 LLM 自我修正。
    # ------------------------------------------------------------------
    def _add_validation_error(self, error_msg: str):
        """向对话追加 JSON Schema 校验反馈。"""
        self._structured_output_runtime.add_validation_error(error_msg)

    def _add_extraction_error(self, extract_pattern: str):
        """向对话追加输出区块提取失败反馈。"""
        self._structured_output_runtime.add_extraction_error(extract_pattern)

    def _schema_validate_outputs(self, answer_content: str, **kwargs):
        """执行默认的区块提取与 JSON Schema 校验。"""
        return self._structured_output_runtime.validate_outputs(
            answer_content,
            add_validation_error=self._add_validation_error,
            add_extraction_error=self._add_extraction_error,
        )

    def _process_structured_outputs(
        self,
        request_id: str,
        answer_content: str,
        **kwargs,
    ) -> Tuple[Optional[Dict], bool]:
        """把校验结果转换成 runner 使用的 ``(result, success)`` 契约。"""
        return self._structured_output_runtime.process(
            request_id,
            answer_content,
            validator=self._schema_validate_outputs,
            business_logging=self.enable_business_logging,
            validator_kwargs=kwargs,
        )

    # ------------------------------------------------------------------
    # 公共执行入口：同步/异步循环都集中在 AgentRunRuntime，避免子类复制状态机。
    # ------------------------------------------------------------------
    def run(self, **kwargs) -> Any:
        """同步执行一次 Agent。"""
        return self._run_runtime.run(self, kwargs)

    def _creat_request_info(self, request_id: str) -> Dict:
        """创建兼容现有业务日志格式的请求元数据。

        ``creat`` 是历史受保护接口名，runner 和已有覆写仍依赖该拼写。
        """
        return {
            "request_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "request_id": request_id,
            "llm_request_id": [],
            "tool_calls_ids": {},
        }

    def _business_start_log(self, request_id: str):
        """按配置记录异步 Agent 启动明细。"""
        if self.enable_business_logging:
            self.business_logger.debug(
                {
                    "agent": self.agent_name,
                    "event": "agent_async_run_start",
                    "request_id": request_id,
                    "data": {},
                }
            )

    async def async_run(self, **kwargs) -> Any:
        """异步执行一次 Agent。"""
        return await self._run_runtime.async_run(self, kwargs)

    @abstractmethod
    def format(self, **kwargs) -> List[Dict]:
        """把业务输入写入 ``context_container``；子类必须实现。"""
        pass

    def async_format(self, **kwargs):
        """异步格式化扩展点；默认复用同步 ``format``。"""
        return self.format(**kwargs)
