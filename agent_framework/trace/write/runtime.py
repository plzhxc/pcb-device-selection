"""trace 的开关与接线：``init()`` 一次，四层自动落库。

## 开关放在哪

放在"有没有调用 :func:`init`"上，而不是一个单独的 ``enabled`` 全局标志：

- 观测要落库就必须有连接，**没连接就没有落库**——两件事本来就是同一件；
  再维护一个开关只会多出"标志说开着、连接却是 None"这种要对账的状态。
- 开关点唯一，在应用启动处；不会散落到 23 个 Agent 和 5 个 Stage 的构造参数里。

三种粒度：

===================  =========================================================
全局开               ``trace.init(connect, dialect="mysql")``
全局关               不调用；或 ``trace.init(..., enabled=False)``
                     （配置已写死、只想临时停的场景）
单个 Agent 关        构造时显式传 ``observer=NULL_AGENT_OBSERVER``
===================  =========================================================

## 为什么用"进程级默认"

与限流器 (:func:`~...llm.rate_limit.get_default_limiter`) 同一个模式：各层构造时
``observer=None`` 就取进程级默认。这样接线只发生在启动处一次，业务代码里一个
observer 参数都不用写；而需要特殊处理的地方仍可显式传入，不失灵活。

## 跨层归属自动接好

四层事件只带本层 id，``stage_id`` / ``agent_id`` 这些外键要从 ContextVar 现取。
这件事以前得由应用自己写三个 lambda，**忘了不报错**——八张表照写，外键全是 NULL，
join 不起来。现在由本模块接，接不接得上不再取决于调用方记性。
"""

from __future__ import annotations

import atexit
import threading
from typing import Any, Callable

from .agent import AgentTraceContext, AgentTraceObserver, AgentTraceWriter
from ..database import Dialect, TraceDatabase
from .llm import LLMTraceObserver, LLMTraceWriter, TraceContext
from .pipeline import current_run_id, current_stage_id, current_workflow_id
from .stage import StageTraceContext, StageTraceObserver, StageTraceWriter
from .workflow import WorkflowTraceContext, WorkflowTraceObserver, WorkflowTraceWriter
from .base import TraceWriteDispatcher

#: 业务请求 id 的来源。默认取当前 workflow id——一次流水线就是一次业务请求；
#: 应用有自己的请求 id（HTTP request id、用户会话 id）时传进来覆盖。
TraceIdSource = Callable[[], str] | str | None


class TraceRuntime:
    """一套已接好线的观测。

    Attributes:
        llm / agent / stage / workflow: 四层的 observer，直接传给对应构造函数。
        enabled: 是否真的落库。``False`` 时四个 observer 为 ``None``，各层退回
            Null 实现，emit 空转。
    """

    __slots__ = (
        "llm",
        "agent",
        "stage",
        "workflow",
        "enabled",
        "_writers",
        "_dispatcher",
        "_shutdown_lock",
        "_closed",
        "connect_factory",
        "dialect",
    )

    def __init__(
        self,
        *,
        llm: Any = None,
        agent: Any = None,
        stage: Any = None,
        workflow: Any = None,
        writers: tuple[Any, ...] = (),
        dispatcher: TraceWriteDispatcher | None = None,
        enabled: bool = True,
        connect_factory: Callable[[], Any] | None = None,
        dialect: Dialect = "mysql",
    ) -> None:
        self.llm = llm
        self.agent = agent
        self.stage = stage
        self.workflow = workflow
        self.enabled = enabled
        self._writers = writers
        self._dispatcher = dispatcher
        self._shutdown_lock = threading.Lock()
        self._closed = False
        #: 留着给观测网页复用——落库和看网页几乎总是同一个库，
        #: 让人再写一遍连接参数只会写错。
        self.connect_factory = connect_factory
        self.dialect = dialect

    @property
    def failed_writes(self) -> int:
        """四层累计写库失败次数。健康检查读它——写库失败不抛异常。"""

        return sum(getattr(w, "failed_writes", 0) for w in self._writers)

    @property
    def last_error(self) -> Exception | None:
        for writer in self._writers:
            error = getattr(writer, "last_error", None)
            if error is not None:
                return error
        return None

    @property
    def pending_writes(self) -> int:
        """尚未完成的数据库事务数。四层共享同一个值。"""

        dispatcher = self._dispatcher
        return dispatcher.pending_writes if dispatcher is not None else 0

    def flush(self, timeout: float | None = None) -> bool:
        """等待调用前已提交的全部 Trace 事件完成。"""

        dispatcher = self._dispatcher
        return True if dispatcher is None else dispatcher.flush(timeout)

    def shutdown(self, timeout: float | None = None) -> bool:
        """先排空队列再停 worker；重复调用安全。"""

        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")
        with self._shutdown_lock:
            if self._closed:
                return True
            # Direct callers must see truthful state immediately. Existing
            # Framework objects may still hold their observer instance, but no
            # newly constructed object should receive it from this runtime.
            self.enabled = False
            self.llm = None
            self.agent = None
            self.stage = None
            self.workflow = None
            dispatcher = self._dispatcher
            if dispatcher is None:
                self._closed = True
                return True
            stopped = dispatcher.shutdown(timeout)
            if stopped:
                self._closed = True
            return stopped


#: 关闭状态的运行时：四个 observer 都是 None，各层自然退回 Null 实现
DISABLED = TraceRuntime(enabled=False)

_lock = threading.Lock()
_runtime: TraceRuntime = DISABLED


def _resolve_trace_id(source: TraceIdSource) -> str:
    if source is None:
        # 默认拿 workflow id 当业务主线：不接 workflow 的单独 Agent 调用则为空串，
        # 此时每条记录各自独立，仍然可查，只是串不成一条链。
        return current_workflow_id() or ""
    if callable(source):
        try:
            return source() or ""
        except Exception:
            return ""
    return str(source)


def init(
    connect_factory: Callable[[], Any] | None = None,
    *,
    dialect: Dialect = "mysql",
    trace_id: TraceIdSource = None,
    enabled: bool = True,
    ensure_schema: bool = True,
) -> TraceRuntime:
    """建立观测运行时，并设为进程级默认。

    Args:
        connect_factory: 零参可调用对象，返回一个 DB-API 连接。启用 Trace 时必须
            提供；禁用时可省略。
        dialect: ``mysql`` 或 ``sqlite``。
        trace_id: 业务请求 id 的来源。默认取当前 workflow id。
        enabled: ``False`` 时返回关闭状态的运行时，一行都不写。
        ensure_schema: 是否建表。生产库由 DBA 管控时设 ``False``。

    Returns:
        已接好线的 :class:`TraceRuntime`；同时成为进程级默认。

    Trace 开关只由此处的 ``enabled`` 参数（通常来自 YAML）决定，不读取环境变量。
    """

    global _runtime

    if dialect not in {"sqlite", "mysql"}:
        raise ValueError("dialect must be 'sqlite' or 'mysql'")
    if enabled and not callable(connect_factory):
        raise ValueError("connect_factory is required when Trace is enabled")

    if not enabled:
        runtime = DISABLED
    else:
        # Build the replacement completely before touching the healthy current
        # runtime. A bad connection or schema must not disable live tracing.
        assert connect_factory is not None
        if ensure_schema:
            TraceDatabase(connect_factory, dialect=dialect).ensure_schema()
        dispatcher = TraceWriteDispatcher()
        try:
            llm_writer = LLMTraceWriter(
                connect_factory,
                dialect=dialect,
                ensure_schema=False,
                dispatcher=dispatcher,
            )
            agent_writer = AgentTraceWriter(
                connect_factory,
                dialect=dialect,
                ensure_schema=False,
                dispatcher=dispatcher,
            )
            stage_writer = StageTraceWriter(
                connect_factory,
                dialect=dialect,
                ensure_schema=False,
                dispatcher=dispatcher,
            )
            workflow_writer = WorkflowTraceWriter(
                connect_factory,
                dialect=dialect,
                ensure_schema=False,
                dispatcher=dispatcher,
            )

            # 四层 context 每次现取，同一个 Agent 在不同 Stage 复用时才不会串线。
            runtime = TraceRuntime(
                llm=LLMTraceObserver(
                    llm_writer,
                    context=lambda: TraceContext(
                        trace_id=_resolve_trace_id(trace_id) or None,
                        flow_id=current_workflow_id(),
                        stage_id=current_stage_id(),
                        agent_id=current_run_id(),
                    ),
                ),
                agent=AgentTraceObserver(
                    agent_writer,
                    context=lambda: AgentTraceContext(
                        trace_id=_resolve_trace_id(trace_id),
                        flow_id=current_workflow_id(),
                        stage_id=current_stage_id(),
                    ),
                ),
                stage=StageTraceObserver(
                    stage_writer,
                    context=lambda: StageTraceContext(
                        trace_id=_resolve_trace_id(trace_id)
                    ),
                ),
                workflow=WorkflowTraceObserver(
                    workflow_writer,
                    context=lambda: WorkflowTraceContext(
                        trace_id=_resolve_trace_id(trace_id)
                    ),
                ),
                writers=(
                    llm_writer,
                    agent_writer,
                    stage_writer,
                    workflow_writer,
                ),
                dispatcher=dispatcher,
                connect_factory=connect_factory,
                dialect=dialect,
            )
        except Exception:
            dispatcher.shutdown()
            raise

    with _lock:
        previous = _runtime
        _runtime = runtime
    # Cut new default bindings over atomically, then drain the old queue.
    previous.shutdown()
    return runtime


def current() -> TraceRuntime:
    """当前进程级观测运行时；未 :func:`init` 时是关闭状态。"""

    return _runtime


def flush(timeout: float | None = None) -> bool:
    """等待当前运行时已提交的所有 Trace 事件落库。"""

    return _runtime.flush(timeout)


def shutdown(timeout: float | None = None) -> bool:
    """关闭观测：先切断新事件，再排空并停止后台 worker。"""

    global _runtime
    if timeout is not None and timeout < 0:
        raise ValueError("timeout must be non-negative or None")
    with _lock:
        previous = _runtime
        _runtime = DISABLED
    return previous.shutdown(timeout)


def enabled() -> bool:
    """观测是否开着。"""

    return _runtime.enabled


# --------------------------------------------------------------------------- #
# 各层取默认 observer 的入口
# --------------------------------------------------------------------------- #
def llm_observer() -> Any:
    return _runtime.llm


def agent_observer() -> Any:
    return _runtime.agent


def stage_observer() -> Any:
    return _runtime.stage


def workflow_observer() -> Any:
    return _runtime.workflow


__all__ = [
    "DISABLED",
    "TraceRuntime",
    "agent_observer",
    "current",
    "enabled",
    "flush",
    "init",
    "llm_observer",
    "shutdown",
    "stage_observer",
    "workflow_observer",
]


atexit.register(shutdown)
