"""BaseAgent：编排 LLM、工具与结构化输出的一次完整运行。

职责边界——本层**只做编排**：

- 决定何时调模型、何时执行工具、何时让模型自我纠错；
- 施加三类上限（工具轮数 / 纠错次数 / 传输重试）并区分终态；
- 累计用量与对话轨迹。

它不翻译厂商协议（LLM 层负责），不解析结构化输出（``structured_output`` 负责），
也不执行工具的业务逻辑（工具自身负责）。

子类只需实现 :meth:`BaseAgent.prepare`，往传入的 :class:`~.context.Context` 里
逐条累积本次要问的东西。Agent 实例本身不持有运行期状态（状态都在
:class:`~.session.Session` 里），因此可以被多个协程并发复用。

同步与异步两条路径共用同一套决策函数 :func:`_classify`，避免两边逻辑漂移——
这类"看起来一样、实际微妙不同"的重复是并发 bug 的常见来源。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Mapping, Sequence

from ..llm.client import AsyncLLMClient, LLMClient
from ..llm.errors import LLMError
from ..llm.response import FinishReason, LLMResponse, Usage, usage_fields
from ..message import AssistantMessage, ToolMessage, UserMessage
from ..native_tools import create_native_toolbox
from ..trace.write.pipeline import emit as emit_event
from ..trace.write.runtime import agent_observer as _default_agent_observer
from ..structured_output import (
    OutputError,
    ParseResult,
    StructuredOutput,
    parse_structured_outputs,
)
from ..tool import (
    ToolBox,
    ToolCall,
    ToolChoice,
    ToolExecutionContext,
    ToolResult,
    ToolStatus,
)
from .config import AgentConfig, RetryPolicy
from .context import Context
from .observer import (
    NULL_AGENT_OBSERVER,
    _current_run,
    AgentObserver,
    RunEnded,
    RunStarted,
    StepKind,
    StepRecorded,
)
from .session import Session
from .result import AgentResult, AgentStatus, accumulate_usage
from .validation import (
    ValidationContext,
    ValidationReport,
    Validator,
    arun_validators,
    run_validators,
)


class _Step(str, Enum):
    """对一次模型响应的处置决定。

    五种取值覆盖了一次响应的全部可能去向：继续跑工具、让模型自我纠错、
    正常结束，以及两类"重试也没用"的终止（被截断、被内容审查拦下）。
    """

    RUN_TOOLS = "run_tools"
    CORRECT_OUTPUT = "correct_output"
    #: 输出声明写错了——错在我们，重试无意义
    SCHEMA_BROKEN = "schema_broken"
    DONE = "done"
    TRUNCATED = "truncated"
    FILTERED = "filtered"


def _classify(
    response: LLMResponse,
    outputs: Sequence[StructuredOutput],
) -> tuple[_Step, ParseResult | None]:
    """判断拿到响应后该做什么。

    判断顺序是有讲究的：

    1. **先看内容审查**。被拦截不是格式问题，发纠错提示只会再次被拦，白白消耗
       三轮重试，还会以 ``OUTPUT_INVALID`` 收场、误导排查方向。
    2. **再看截断**。被截断的输出可能恰好也没有工具调用、正文也能解析，不先
       拦下就会把半截结果当成功返回；截断的工具调用参数还是残缺 JSON，执行它
       更危险。
    3. **然后看工具调用**，模型要工具就先给工具。
    4. **空回答单独判**，且不论是否声明了结构化输出——空响应多为服务瞬时故障，
       重试有效；若按"无声明即成功"处理，调用方会拿到 ``ok`` 但内容为空的结果。
    """

    if response.finish_reason is FinishReason.CONTENT_FILTER:
        return _Step.FILTERED, None
    if response.truncated:
        return _Step.TRUNCATED, None
    if response.has_tool_calls:
        return _Step.RUN_TOOLS, None
    if not (response.content or "").strip():
        return _Step.CORRECT_OUTPUT, ParseResult(
            errors=[
                OutputError(
                    kind="empty_answer",
                    message="模型返回空响应（可能为服务瞬时故障或提示过长）",
                )
            ]
        )
    parsed = parse_structured_outputs(response.content, outputs)
    if any(error.kind == "schema_broken" for error in parsed.errors):
        # Schema 定义本身非法：让模型重来只会把纠错额度烧光，最后还报成
        # "模型输出格式错误"，把排查方向整个带偏。
        return _Step.SCHEMA_BROKEN, parsed
    return (_Step.DONE if parsed.ok else _Step.CORRECT_OUTPUT), parsed


def _backoff_delay(policy: RetryPolicy, attempt: int) -> float:
    """指数退避 + 抖动。抖动用于打散并发请求的集体重试。"""

    return policy.delay_for(attempt) + random.uniform(0, policy.jitter_s)


def _reject_non_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON constant: {value}")


def _history_safe_tool_call(call: ToolCall) -> ToolCall:
    """Normalize malformed model arguments before replaying assistant history.

    Tool execution and Trace deliberately consume the original ``ToolCall`` so
    malformed JSON remains an observable ``tool_failed`` result.  OpenAI-style
    providers, including DashScope, reject the *next* request if an earlier
    assistant tool call contains malformed ``function.arguments``.  Store a
    valid, deterministic error object in conversation history instead, giving
    the model both the original text and the paired tool error it needs to
    correct the call.
    """

    raw = call.arguments or ""
    if not raw.strip():
        normalized = "{}"
    else:
        try:
            parsed = json.loads(
                raw,
                parse_constant=_reject_non_json_constant,
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = None
        normalized = json.dumps(
            parsed
            if isinstance(parsed, dict)
            else {
                "_agent_framework_error": "invalid_tool_arguments_json",
                "raw_arguments": raw,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return ToolCall(id=call.id, name=call.name, arguments=normalized)


def _tool_call_signature(call: ToolCall) -> tuple[str, str]:
    safe = _history_safe_tool_call(call)
    return safe.name, safe.arguments


class _Turn:
    """单轮的计数与产出。

    三类上限分开计数：工具轮数、纠错次数、传输重试。混在一个计数器里，会让
    "模型不听话"和"网络抖动"变得无法区分。
    """

    __slots__ = (
        "iterations",
        "output_retries",
        "llm_calls",
        "response",
        "seen_tool_calls",
        "duplicate_tool_correction_sent",
        "tools_disabled",
        "usage",
        "tool_results",
        "run_id",
        "started",
        "validation_retries",
        "validation",
    )

    def __init__(self) -> None:
        self.run_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.iterations = 0
        self.output_retries = 0
        self.validation_retries = 0
        self.seen_tool_calls: set[tuple[str, str]] = set()
        self.duplicate_tool_correction_sent = False
        self.tools_disabled = False
        self.llm_calls = 0
        self.validation = ValidationReport()
        self.response: LLMResponse | None = None
        self.usage = Usage()
        self.tool_results: list[ToolResult] = []

    def note_attempt(self) -> None:
        """记一次已发出的调用。

        发出即计数，而不是成功才计数：一轮跑了三次全部超时的话，"发生过什么"
        才是排障要的信息。只统计成功的话，失败终止的运行显示 0 次调用 0 token，
        看上去像压根没调过模型——而账单上是实实在在扣了钱的。
        """

        self.llm_calls += 1

    def record(self, response: LLMResponse) -> None:
        self.response = response
        self.usage = accumulate_usage(self.usage, response.usage)

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class BaseAgent(ABC):
    """所有 Agent 的基类。

    这个 Agent **固定是什么**用类属性声明，**这次运行要什么**在
    :meth:`prepare` 里准备——两者变化频率相差悬殊，混在一起会让每次调用都重复
    组装本不会变的东西::

        class ChapterWriter(BaseAgent):
            task_type = "chapter_writer"
            outputs = [StructuredOutput("doc", r"<<<DOC>>>(.*?)<<<END>>>")]
            SYSTEM = "你是硬件文档专家。..."   # 几十行的模板放类常量或独立文件

            def prepare(self, ctx: Context, *, chapter: int, topic: str) -> None:
                ctx.system(self.SYSTEM)
                ctx.user(f"请撰写第 {chapter} 章：{topic}")
                ctx.knowledge("datasheet", title="STM32F103", content=ds_text)

        result = ChapterWriter(client, config).run(chapter=1, topic="电源")
    """

    #: 任务类型标识，供应用按任务选模型/超时；框架自身只做透传
    task_type: str = ""
    #: 这一步默认需要多强的模型（档位名，见 ``AgentConfig.model_tiers``）；``None`` 用配置默认。
    model_tier: str | None = None
    #: 本 Agent 固定要提取的结构化输出
    outputs: Sequence[StructuredOutput] = ()
    #: 本 Agent 固定的工具使用策略
    tool_choice: ToolChoice | None = None
    #: 业务规则校验器。Schema 管格式，这里管内容——两者查的是完全不同的问题
    validators: Sequence[Validator] = ()

    def __init__(
        self,
        client: LLMClient | AsyncLLMClient,
        config: AgentConfig,
        *,
        tools: ToolBox | None = None,
        observer: AgentObserver | None = None,
        enable_native_tools: bool = True,
    ) -> None:
        self.client = client
        self.config = config
        if enable_native_tools:
            # 原生工具是每个 Agent 各自的一箱，不存在跨 Agent 的可变全局注册表。
            # 显式工具随后合并，同名时由调用方明确覆盖原生实现。
            self.tools = create_native_toolbox()
            if tools is not None:
                self.tools.extend(tools, replace=True)
        else:
            self.tools = tools or ToolBox()
        # observer 为 None 时取进程级默认：调过 trace.init() 就自动落库，
        # 没调就是 Null 实现、emit 空转。业务代码里一个 observer 参数都不用写。
        self.observer = observer or _default_agent_observer() or NULL_AGENT_OBSERVER

    # ------------------------------ 子类实现 ------------------------------ #
    @abstractmethod
    def prepare(self, ctx: Context, **kwargs: Any) -> None:
        """准备本次运行的上下文。

        往传入的 ``ctx`` 里逐条累积即可，不需要返回值，也**不要修改 ``self``**：
        ``ctx`` 每次运行新建，实例状态则会在并发调用间互相覆盖。
        """

        raise NotImplementedError

    # ------------------------------ 入口 ------------------------------ #
    def session(self) -> Session:
        """开一个新会话，用于多轮对话或需要手动管理上下文的场景。"""

        return Session(self)

    def run(self, **kwargs: Any) -> AgentResult:
        """单轮执行；内部开一次性会话。"""

        return self.session().run(**kwargs)

    async def arun(self, **kwargs: Any) -> AgentResult:
        """:meth:`run` 的异步版本。"""

        return await self.session().arun(**kwargs)

    # ------------------------------ 编排 ------------------------------ #
    def _injected_for(self, call: ToolCall, session: Session) -> Mapping[str, Any]:
        """取出本次会话为**这一个工具**准备的注入值。

        按工具名寻址而非共用一份扁平字典：不同工具需要的东西各不相同，混在一起
        既看不出哪个值给谁，也容易因同名参数互相误伤。
        """

        return session.tool_context.get(call.name, {})

    # ------------------------------ 观测 ------------------------------ #
    def _notify(self, hook: str, event: Any) -> None:
        """向观察者发事件；观测失败不影响主流程，但会计数并告警。"""

        emit_event(self.observer, hook, event)

    def _notify_started(
        self, session: Session, turn: _Turn, inputs: dict[str, Any]
    ) -> None:
        self._notify(
            "on_run_started",
            RunStarted(
                run_id=turn.run_id,
                agent=type(self).__name__,
                task_type=self.task_type,
                turn=session.turns + 1,
                session_id=session.session_id,
                inputs=inputs,
            ),
        )

    def _notify_ended(
        self, session: Session, turn: _Turn, status: AgentStatus, result: AgentResult
    ) -> None:
        usage = turn.usage
        self._notify(
            "on_run_ended",
            RunEnded(
                run_id=turn.run_id,
                status=status.value,
                duration_ms=turn.elapsed_ms,
                iterations=turn.iterations,
                output_retries=turn.output_retries,
                llm_calls=turn.llm_calls,
                **usage_fields(usage),
                tool_calls=len(turn.tool_results),
                tool_failures=sum(1 for t in turn.tool_results if not t.ok),
                repairs=dict(result.repairs),
                output_keys=tuple(result.values),
                error=str(result.error) if result.error else None,
                validation_retries=turn.validation_retries,
                validation_codes=tuple(turn.validation.codes()),
                session_id=session.session_id,
                session_turns=session.turns,
                session_llm_calls=session.llm_calls,
                session_total_tokens=session.usage.total_tokens,
            ),
        )

    def _notify_aborted(
        self, session: Session, turn: _Turn, error: BaseException
    ) -> None:
        """为异常中断的运行补一条收尾事件。

        只有 :class:`LLMError` 会被收敛成 AgentResult；工具外的崩溃、validator 里的
        bug、Ctrl+C / 任务取消都会直接穿出去。若不在这里补 RunEnded，追踪表里就留下
        一条永远 running 的 RunStarted——查起来像是"卡住了"，实际早就死了。
        """

        session.finish_turn()
        status = (
            AgentStatus.CANCELLED
            if isinstance(error, asyncio.CancelledError)
            else AgentStatus.ERROR
        )
        self._notify_ended(
            session,
            turn,
            status,
            AgentResult(status=status, run_id=turn.run_id, error=error),
        )

    def _call_overrides(self, session: Session) -> dict[str, Any]:
        """合成本次调用的参数覆盖。

        优先级：client 默认 < Agent 自身配置 < 模型强度档位 < 本次运行的
        ``ctx.override()`` / ``ctx.model(...)``。档位来源依次取 ``ctx.model(tier)``、
        配置里的 ``tier``、Agent 类上的 ``model_tier``；取到就经 ``model_tiers``
        解析成具体模型。Agent 的 ``config.params`` 必须参与——否则按文档配了模型/
        温度却静默不生效，请求发出去用的还是 client 的默认值，而且没有任何报错。
        """

        overrides = self.config.params.as_overrides()
        tier = session.model_tier or self.config.tier or self.model_tier
        if tier:
            overrides["model"] = self.config.resolve_tier(tier)
        overrides.update(session.overrides)
        return overrides

    def _limit_exceeded(self, turn: _Turn) -> AgentStatus | None:
        """纠错类上限的判定，在每次发起调用前检查。

        用 ``>`` 而不是 ``>=``：这两个计数器是在**发出纠错请求之前**自增的（见
        ``_absorb_correction``），所以计数为 N 时那第 N 次重试还没打出去。写成
        ``>=`` 会少跑一次——配 1 次重试实际一次都不重试。

        工具轮数不在这里判：它记的是"已经跑完几轮"，语义相反，得在**真要执行
        工具时**拦（见 ``_Step.RUN_TOOLS`` 分支）。放在这里会多跑一轮，改成
        ``>=`` 放这里则会连让模型收尾的机会都没有，只能返回没有正文的 TOOL_LIMIT。
        """

        if turn.output_retries > self.config.max_output_retries:
            return AgentStatus.OUTPUT_INVALID
        if turn.validation_retries > self.config.max_validation_retries:
            return AgentStatus.VALIDATION_FAILED
        return None

    def _tool_limit_exceeded(
        self,
        turn: _Turn,
        calls: Sequence[ToolCall],
    ) -> bool:
        """判定当前工具请求是否超出该 Agent 的显式上限。

        ``None`` 是应用对单个 Agent 做出的「不设上限」决策，不影响
        仍使用框架默认保护值的其他 Agent。
        """

        iteration_limit = self.config.max_tool_iterations
        calls_limit = self.config.max_tool_calls_per_iteration
        return (
            iteration_limit is not None
            and turn.iterations >= iteration_limit
        ) or (
            calls_limit is not None
            and len(calls) > calls_limit
        )

    @staticmethod
    def _partition_tool_calls(
        turn: _Turn,
        calls: Sequence[ToolCall],
    ) -> tuple[list[ToolCall], list[ToolCall]]:
        """Split new calls from same-turn semantic duplicates.

        IDs are transport identifiers and may change on every model response;
        duplicate identity is therefore the tool name plus canonical JSON
        arguments.  The local ``seen`` set also removes duplicates inside one
        parallel batch before any tool is executed.
        """

        seen = set(turn.seen_tool_calls)
        novel: list[ToolCall] = []
        duplicates: list[ToolCall] = []
        for call in calls:
            signature = _tool_call_signature(call)
            if signature in seen:
                duplicates.append(call)
                continue
            seen.add(signature)
            novel.append(call)
        return novel, duplicates

    @staticmethod
    def _remember_tool_calls(turn: _Turn, calls: Sequence[ToolCall]) -> None:
        turn.seen_tool_calls.update(_tool_call_signature(call) for call in calls)

    def _absorb_duplicate_tools(
        self,
        session: Session,
        turn: _Turn,
        response: LLMResponse,
        duplicates: Sequence[ToolCall],
        *,
        assistant_recorded: bool,
    ) -> None:
        """Issue the single bounded correction for repeated tool requests."""

        turn.duplicate_tool_correction_sent = True
        # The requested evidence is already present in history.  Make the next
        # model turn text-only so a provider cannot emit the same tool call a
        # third time; a non-compliant provider is still caught by the bounded
        # duplicate guard below.
        turn.tools_disabled = True
        signatures = [_tool_call_signature(call) for call in duplicates]
        self._notify(
            "on_step",
            StepRecorded(
                run_id=turn.run_id,
                kind="correction",
                index=1,
                detail={
                    "errors": ["duplicate_tool_call"],
                    "duplicates": [
                        {"name": name, "arguments": arguments[:500]}
                        for name, arguments in signatures
                    ],
                },
            ),
        )
        if not assistant_recorded and (response.content or response.reasoning):
            session.messages.append(
                AssistantMessage(
                    response.content,
                    reasoning=response.reasoning,
                )
            )
        rows = "\n".join(
            f"- {name}: {arguments[:500]}" for name, arguments in signatures
        )
        session.messages.append(
            UserMessage(
                "## 工具调用纠正\n"
                "你刚才重复请求了本轮已经执行或已经请求过的同一工具调用；"
                "框架没有再次执行，也没有把重复调用写入对话历史。\n"
                f"重复调用：\n{rows}\n"
                "请直接使用已有工具结果完成最终输出。只有确有必要时，才可调用"
                "参数不同且有明确依据的工具；不要再次请求上述调用。"
            )
        )

    def _specs(self, session: Session) -> Sequence[Any]:
        return session.tools.specs() if not session.tools.empty else ()

    def _result(
        self,
        session: Session,
        turn: _Turn,
        status: AgentStatus,
        *,
        values: dict[str, Any] | None = None,
        output_errors: Sequence[Any] = (),
        repairs: dict[str, list[str]] | None = None,
        error: BaseException | None = None,
    ) -> AgentResult:
        session.finish_turn()
        result = AgentResult(
            status=status,
            run_id=turn.run_id,
            values=values or {},
            text=turn.response.content if turn.response else "",
            messages=list(session.messages),
            tool_results=list(turn.tool_results),
            usage=turn.usage,
            output_errors=list(output_errors),
            validation=turn.validation,
            repairs=dict(repairs or {}),
            error=error,
            response=turn.response,
            iterations=turn.iterations,
            llm_calls=turn.llm_calls,
        )
        self._notify_ended(session, turn, status, result)
        return result

    def _absorb_tools(
        self,
        session: Session,
        turn: _Turn,
        response: LLMResponse,
        results: Sequence[ToolResult],
        *,
        calls: Sequence[ToolCall] | None = None,
    ) -> None:
        history_calls = tuple(response.tool_calls if calls is None else calls)
        turn.iterations += 1
        self._notify(
            "on_step",
            StepRecorded(
                run_id=turn.run_id,
                kind="tools",
                index=turn.iterations,
                detail={
                    "tools": [t.name for t in results],
                    "failed": [t.name for t in results if not t.ok],
                },
            ),
        )
        session.messages.append(
            AssistantMessage(
                response.content,
                tool_calls=tuple(
                    _history_safe_tool_call(call)
                    for call in history_calls
                ),
                reasoning=response.reasoning,
            )
        )
        self._remember_tool_calls(turn, history_calls)
        turn.tool_results.extend(results)
        session.tool_results.extend(results)
        for result in results:
            session.messages.append(
                ToolMessage(
                    tool_call_id=result.tool_call_id,
                    content=result.as_text(),
                    name=result.name,
                    is_error=not result.ok,
                )
            )

    def _tool_started(
        self,
        session: Session,
        turn: _Turn,
        call: ToolCall,
        context: ToolExecutionContext,
        *,
        ordinal: int,
    ) -> None:
        self._notify(
            "on_step",
            StepRecorded(
                run_id=turn.run_id,
                kind="tool_started",
                index=turn.iterations + 1,
                detail={
                    "tool_execution_id": context.tool_execution_id,
                    "tool_call_id": call.id,
                    "name": call.name,
                    "iteration": turn.iterations + 1,
                    "ordinal": ordinal,
                    "requested_arguments": session.tools.requested_arguments(call),
                },
            ),
        )

    def _tool_ended(
        self,
        turn: _Turn,
        result: ToolResult,
        *,
        ordinal: int,
    ) -> None:
        kinds: dict[ToolStatus, StepKind] = {
            ToolStatus.SUCCESS: "tool_completed",
            ToolStatus.FAILED: "tool_failed",
            ToolStatus.DENIED: "tool_denied",
            ToolStatus.TIMEOUT: "tool_timeout",
        }
        detail = result.trace_payload()
        detail.update(
            {
                "iteration": turn.iterations + 1,
                "ordinal": ordinal,
                "outcome": result.status.value,
            }
        )
        self._notify(
            "on_step",
            StepRecorded(
                run_id=turn.run_id,
                kind=kinds[result.status],
                index=turn.iterations + 1,
                detail=detail,
            ),
        )

    def _execute_tool(
        self,
        session: Session,
        turn: _Turn,
        call: ToolCall,
        *,
        ordinal: int,
    ) -> ToolResult:
        context = ToolExecutionContext(
            run_id=turn.run_id,
            session_id=session.session_id,
        )
        self._tool_started(session, turn, call, context, ordinal=ordinal)
        result = session.tools.execute(
            call,
            injected=self._injected_for(call, session),
            context=context,
        )
        self._tool_ended(turn, result, ordinal=ordinal)
        return result

    async def _aexecute_tool(
        self,
        session: Session,
        turn: _Turn,
        call: ToolCall,
        *,
        ordinal: int,
    ) -> ToolResult:
        context = ToolExecutionContext(
            run_id=turn.run_id,
            session_id=session.session_id,
        )
        self._tool_started(session, turn, call, context, ordinal=ordinal)
        started = time.monotonic()
        try:
            result = await session.tools.execute_async(
                call,
                injected=self._injected_for(call, session),
                context=context,
            )
        except asyncio.CancelledError:
            self._notify(
                "on_step",
                StepRecorded(
                    run_id=turn.run_id,
                    kind="tool_cancelled",
                    index=turn.iterations + 1,
                    detail={
                        "tool_execution_id": context.tool_execution_id,
                        "tool_call_id": call.id,
                        "name": call.name,
                        "iteration": turn.iterations + 1,
                        "ordinal": ordinal,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "requested_arguments": (
                            session.tools.requested_arguments(call)
                        ),
                        "outcome": "cancelled",
                    },
                ),
            )
            raise
        self._tool_ended(turn, result, ordinal=ordinal)
        return result

    async def _aexecute_tools(
        self,
        session: Session,
        turn: _Turn,
        calls: Sequence[ToolCall],
    ) -> list[ToolResult]:
        """只并行明确声明 ``parallel_safe`` 的连续调用组。

        写入和命令工具会先等前面的读取结束，再独占执行；它之后的读取也不会提前
        越过它。这样既保留模型请求顺序的语义，又不浪费独立检索的并行度。
        """

        results: list[ToolResult | None] = [None] * len(calls)
        safe_group: list[tuple[int, ToolCall]] = []

        async def flush_safe() -> None:
            if not safe_group:
                return
            batch = tuple(safe_group)
            safe_group.clear()
            completed = await asyncio.gather(
                *(
                    self._aexecute_tool(
                        session,
                        turn,
                        call,
                        ordinal=index + 1,
                    )
                    for index, call in batch
                )
            )
            for (index, _), result in zip(batch, completed):
                results[index] = result

        for index, call in enumerate(calls):
            if session.tools.is_parallel_safe(call.name):
                safe_group.append((index, call))
                continue
            await flush_safe()
            results[index] = await self._aexecute_tool(
                session,
                turn,
                call,
                ordinal=index + 1,
            )
        await flush_safe()
        return [result for result in results if result is not None]

    def _validation_context(
        self, session: Session, turn: _Turn, parsed: ParseResult
    ) -> ValidationContext:
        return ValidationContext(
            values=parsed.values,
            text=turn.response.content if turn.response else "",
            messages=tuple(session.messages),
            tool_results=tuple(turn.tool_results),
        )

    def _absorb_validation(
        self,
        session: Session,
        turn: _Turn,
        response: LLMResponse,
        report: ValidationReport,
    ) -> None:
        """把业务规则问题发回给模型自我纠正。"""

        turn.validation_retries += 1
        turn.validation = report
        self._notify(
            "on_step",
            StepRecorded(
                run_id=turn.run_id,
                kind="validation",
                index=turn.validation_retries,
                detail={"codes": [i.code for i in report.errors]},
            ),
        )
        session.messages.append(AssistantMessage(response.content))
        session.messages.append(UserMessage(report.correction_prompt()))

    def _absorb_correction(
        self, session: Session, turn: _Turn, response: LLMResponse, parsed: ParseResult
    ) -> None:
        turn.output_retries += 1
        self._notify(
            "on_step",
            StepRecorded(
                run_id=turn.run_id,
                kind="correction",
                index=turn.output_retries,
                detail={"errors": [e.kind for e in parsed.errors]},
            ),
        )
        session.messages.append(AssistantMessage(response.content))
        for prompt in parsed.correction_prompts():
            session.messages.append(UserMessage(prompt))

    # ------------------------------ 同步 ------------------------------ #
    def _require(self, expected: type, hint: str) -> None:
        """校验 client 与调用方式匹配。

        同步入口配异步 client（或反之）时，原生报错是
        ``object LLMResponse can't be used in 'await' expression`` 之类，
        离根因很远。宁可在这里挑明。
        """

        if not isinstance(self.client, expected):
            raise TypeError(
                f"{type(self).__name__} 的 client 是 {type(self.client).__name__}，{hint}"
            )

    def _execute(self, session: Session) -> AgentResult:
        """跑完一轮。

        模型调用失败（:class:`LLMError`）收敛成 ``status=ERROR`` 的
        :class:`AgentResult`；其余异常是代码 bug 或外部中断，照常抛出——但抛之前
        一定补上收尾事件，不留悬挂的运行记录。
        """

        self._require(
            LLMClient,
            "同步入口 run()/follow_up() 需要 LLMClient；异步请用 arun()/afollow_up()",
        )
        turn = _Turn()
        # 先 set 再发事件：观察者在 on_run_started 里读 current_run_id() 是常规
        # 做法（写库时要拿它当外键），顺序反了就只能读到上一层的 id 或空串。
        token = _current_run.set(turn.run_id)
        self._notify_started(session, turn, session.last_inputs)
        try:
            return self._loop_sync(session, turn)
        except BaseException as error:  # 含 KeyboardInterrupt：留痕后原样抛出
            self._notify_aborted(session, turn, error)
            raise
        finally:
            _current_run.reset(token)

    def _loop_sync(self, session: Session, turn: _Turn) -> AgentResult:
        last_parsed: ParseResult | None = None

        while True:
            exceeded = self._limit_exceeded(turn)
            if exceeded is not None:
                return self._result(
                    session,
                    turn,
                    exceeded,
                    output_errors=last_parsed.errors if last_parsed else (),
                )
            try:
                response = self._call_sync(session, turn)
            except LLMError as error:
                return self._result(session, turn, AgentStatus.ERROR, error=error)
            turn.record(response)
            session.record(response)

            step, parsed = _classify(response, session.outputs)
            if step is _Step.SCHEMA_BROKEN:
                return self._result(
                    session,
                    turn,
                    AgentStatus.ERROR,
                    output_errors=parsed.errors if parsed else (),
                    error=RuntimeError(
                        "结构化输出声明有误："
                        + "；".join(
                            e.message for e in (parsed.errors if parsed else ())
                        )
                    ),
                )
            if step is _Step.FILTERED:
                return self._result(session, turn, AgentStatus.CONTENT_FILTERED)
            if step is _Step.TRUNCATED:
                return self._result(session, turn, AgentStatus.TRUNCATED)
            if step is _Step.DONE:
                report = (
                    run_validators(
                        self.validators, self._validation_context(session, turn, parsed)
                    )
                    if self.validators and parsed
                    else ValidationReport()
                )
                turn.validation = report
                if report.crashes and self.config.strict_validation:
                    # 规则没跑成 ≠ 规则通过。当成通过就是业务校验被悄悄绕过；
                    # 发回给模型也没用——坏的是我们的代码，不是它的输出。
                    return self._result(
                        session,
                        turn,
                        AgentStatus.ERROR,
                        error=RuntimeError(f"校验器执行失败：{report.crash_summary()}"),
                    )
                if not report.ok:
                    self._absorb_validation(session, turn, response, report)
                    continue
                session.messages.append(AssistantMessage(response.content))
                return self._result(
                    session,
                    turn,
                    AgentStatus.SUCCESS,
                    values=(parsed.values if parsed else {}),
                    repairs=(parsed.repairs if parsed else None),
                )
            if step is _Step.RUN_TOOLS:
                calls, duplicates = self._partition_tool_calls(
                    turn,
                    response.tool_calls,
                )
                if duplicates and turn.duplicate_tool_correction_sent:
                    return self._result(session, turn, AgentStatus.TOOL_LIMIT)
                if calls and self._tool_limit_exceeded(turn, calls):
                    if duplicates:
                        self._absorb_duplicate_tools(
                            session,
                            turn,
                            response,
                            duplicates,
                            assistant_recorded=False,
                        )
                        continue
                    return self._result(session, turn, AgentStatus.TOOL_LIMIT)
                if calls:
                    results = [
                        self._execute_tool(
                            session,
                            turn,
                            call,
                            ordinal=index,
                        )
                        for index, call in enumerate(calls, start=1)
                    ]
                    self._absorb_tools(
                        session,
                        turn,
                        response,
                        results,
                        calls=calls,
                    )
                if duplicates:
                    self._absorb_duplicate_tools(
                        session,
                        turn,
                        response,
                        duplicates,
                        assistant_recorded=bool(calls),
                    )
                continue
            last_parsed = parsed
            self._absorb_correction(session, turn, response, parsed)

    def _call_sync(self, session: Session, turn: _Turn) -> LLMResponse:
        """带退避重试的单次调用；只重试可重试的失败。"""

        policy = self.config.retry
        logical_call_id = uuid.uuid4().hex
        for attempt in range(policy.max_attempts):
            turn.note_attempt()
            session.note_attempt()
            try:
                return self.client.call(
                    session.messages,
                    tools=() if turn.tools_disabled else self._specs(session),
                    tool_choice=(
                        ToolChoice.NONE
                        if turn.tools_disabled
                        else session.tool_choice
                    ),
                    stream=self.config.stream,
                    attempt=attempt,
                    max_attempts=policy.max_attempts,
                    logical_call_id=logical_call_id,
                    **self._call_overrides(session),
                )
            except LLMError as error:
                if not error.retryable or attempt == policy.max_attempts - 1:
                    raise
                try:
                    time.sleep(_backoff_delay(policy, attempt))
                except BaseException as interruption:
                    # 失败已被记成 retry_scheduled；若退避期间中断而不显式闭合，
                    # Trace 会永久停在“等待下一次 attempt”。
                    self.client.cancel_logical_call(
                        logical_call_id,
                        attempt=attempt,
                        reason=(
                            "retry backoff interrupted: "
                            f"{type(interruption).__name__}: {interruption}"
                        ),
                    )
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    # ------------------------------ 异步 ------------------------------ #
    async def _aexecute(self, session: Session) -> AgentResult:
        """:meth:`_execute` 的异步版本；决策逻辑完全一致。"""

        self._require(
            AsyncLLMClient,
            "异步入口 arun()/afollow_up() 需要 AsyncLLMClient；同步请用 run()",
        )
        turn = _Turn()
        token = _current_run.set(turn.run_id)
        self._notify_started(session, turn, session.last_inputs)
        try:
            return await self._loop_async(session, turn)
        except BaseException as error:  # 含 CancelledError：留痕后原样抛出
            self._notify_aborted(session, turn, error)
            raise
        finally:
            _current_run.reset(token)

    async def _loop_async(self, session: Session, turn: _Turn) -> AgentResult:
        last_parsed: ParseResult | None = None

        while True:
            exceeded = self._limit_exceeded(turn)
            if exceeded is not None:
                return self._result(
                    session,
                    turn,
                    exceeded,
                    output_errors=last_parsed.errors if last_parsed else (),
                )
            try:
                response = await self._call_async(session, turn)
            except LLMError as error:
                return self._result(session, turn, AgentStatus.ERROR, error=error)
            turn.record(response)
            session.record(response)

            step, parsed = _classify(response, session.outputs)
            if step is _Step.SCHEMA_BROKEN:
                return self._result(
                    session,
                    turn,
                    AgentStatus.ERROR,
                    output_errors=parsed.errors if parsed else (),
                    error=RuntimeError(
                        "结构化输出声明有误："
                        + "；".join(
                            e.message for e in (parsed.errors if parsed else ())
                        )
                    ),
                )
            if step is _Step.FILTERED:
                return self._result(session, turn, AgentStatus.CONTENT_FILTERED)
            if step is _Step.TRUNCATED:
                return self._result(session, turn, AgentStatus.TRUNCATED)
            if step is _Step.DONE:
                report = (
                    await arun_validators(
                        self.validators, self._validation_context(session, turn, parsed)
                    )
                    if self.validators and parsed
                    else ValidationReport()
                )
                turn.validation = report
                if report.crashes and self.config.strict_validation:
                    # 规则没跑成 ≠ 规则通过。当成通过就是业务校验被悄悄绕过；
                    # 发回给模型也没用——坏的是我们的代码，不是它的输出。
                    return self._result(
                        session,
                        turn,
                        AgentStatus.ERROR,
                        error=RuntimeError(f"校验器执行失败：{report.crash_summary()}"),
                    )
                if not report.ok:
                    self._absorb_validation(session, turn, response, report)
                    continue
                session.messages.append(AssistantMessage(response.content))
                return self._result(
                    session,
                    turn,
                    AgentStatus.SUCCESS,
                    values=(parsed.values if parsed else {}),
                    repairs=(parsed.repairs if parsed else None),
                )
            if step is _Step.RUN_TOOLS:
                calls, duplicates = self._partition_tool_calls(
                    turn,
                    response.tool_calls,
                )
                if duplicates and turn.duplicate_tool_correction_sent:
                    return self._result(session, turn, AgentStatus.TOOL_LIMIT)
                if calls and self._tool_limit_exceeded(turn, calls):
                    if duplicates:
                        self._absorb_duplicate_tools(
                            session,
                            turn,
                            response,
                            duplicates,
                            assistant_recorded=False,
                        )
                        continue
                    return self._result(session, turn, AgentStatus.TOOL_LIMIT)
                if calls:
                    results = await self._aexecute_tools(
                        session,
                        turn,
                        calls,
                    )
                    self._absorb_tools(
                        session,
                        turn,
                        response,
                        results,
                        calls=calls,
                    )
                if duplicates:
                    self._absorb_duplicate_tools(
                        session,
                        turn,
                        response,
                        duplicates,
                        assistant_recorded=bool(calls),
                    )
                continue
            last_parsed = parsed
            self._absorb_correction(session, turn, response, parsed)

    async def _call_async(self, session: Session, turn: _Turn) -> LLMResponse:
        policy = self.config.retry
        logical_call_id = uuid.uuid4().hex
        for attempt in range(policy.max_attempts):
            turn.note_attempt()
            session.note_attempt()
            try:
                return await self.client.call(
                    session.messages,
                    tools=() if turn.tools_disabled else self._specs(session),
                    tool_choice=(
                        ToolChoice.NONE
                        if turn.tools_disabled
                        else session.tool_choice
                    ),
                    stream=self.config.stream,
                    attempt=attempt,
                    max_attempts=policy.max_attempts,
                    logical_call_id=logical_call_id,
                    **self._call_overrides(session),
                )
            except LLMError as error:
                if not error.retryable or attempt == policy.max_attempts - 1:
                    raise
                try:
                    await asyncio.sleep(_backoff_delay(policy, attempt))
                except BaseException as interruption:
                    self.client.cancel_logical_call(
                        logical_call_id,
                        attempt=attempt,
                        reason=(
                            "retry backoff interrupted: "
                            f"{type(interruption).__name__}: {interruption}"
                        ),
                    )
                    raise
        raise AssertionError("unreachable")  # pragma: no cover


__all__ = ["BaseAgent"]
