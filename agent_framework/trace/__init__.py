"""Trace 公共入口。

实现被明确分为三个代码区：

``write → database ← read``

根模块只做懒加载兼容导出与 Web 组合。单独导入 ``trace.read`` 不会加载写入侧，
单独导入 ``trace.write`` 也不会加载读取或 Web 依赖。
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable


_WRITE_EXPORTS = {
    "DISABLED",
    "AgentContextSource",
    "AgentEventType",
    "AgentRunSummary",
    "AgentTraceContext",
    "AgentTraceObserver",
    "AgentTraceWriter",
    "BaseTraceWriter",
    "ContextSource",
    "LLMCallConflict",
    "LLMCallNotFound",
    "LLMCallTerminal",
    "LLMRunRecord",
    "LLMTraceObserver",
    "LLMTraceWriter",
    "LLMWriteError",
    "RecordConflict",
    "RecordLevel",
    "RunEventType",
    "StageTraceContext",
    "StageTraceObserver",
    "StageTraceWriter",
    "TokenSource",
    "TokenUsage",
    "TraceContext",
    "TraceRuntime",
    "TraceWriteDispatcher",
    "TraceWriteFuture",
    "TraceWriterClosed",
    "WorkflowTraceContext",
    "WorkflowTraceObserver",
    "WorkflowTraceWriter",
    "WriteReceipt",
    "current",
    "current_run_id",
    "current_stage_id",
    "current_workflow_id",
    "digest",
    "emit",
    "enabled",
    "flush",
    "init",
    "observer_failures",
    "reset_observer_failures",
    "shutdown",
}
_READ_EXPORTS = {"TraceReader"}
_MYSQL_EXPORTS = {
    "MySQLTraceConfig",
    "TraceConfigurationError",
    "init_mysql_trace",
    "mysql_connection_factory",
    "mysql_trace_health",
    "run_mysql_trace_web",
    "setup_mysql_trace_database",
}


def __getattr__(name: str) -> Any:
    """按需提供旧的根级公开符号，不提前耦合三块实现。"""

    if name in _WRITE_EXPORTS:
        value = getattr(import_module(".write", __name__), name)
    elif name in _READ_EXPORTS:
        value = getattr(import_module(".read", __name__), name)
    elif name in _MYSQL_EXPORTS:
        value = getattr(import_module(".mysql", __name__), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(
        {*globals(), *_WRITE_EXPORTS, *_READ_EXPORTS, *_MYSQL_EXPORTS, "run", "serve"}
    )


def _web_connection(
    connect_factory: Callable[[], Any] | None,
    dialect: str | None,
) -> tuple[Callable[[], Any], str]:
    """在公共组合层决定 Web 使用显式连接还是当前写入连接。"""

    if connect_factory is not None:
        resolved = dialect or "mysql"
        if resolved not in {"sqlite", "mysql"}:
            raise ValueError("dialect must be 'sqlite' or 'mysql'")
        return connect_factory, resolved

    runtime = import_module(".write.runtime", __name__).current()
    factory = runtime.connect_factory
    if factory is None:
        raise RuntimeError(
            "没有可用的 Trace 数据库连接：先 trace.init(connect, dialect=...)，"
            "或给 trace.run()/trace.serve() 显式传 connect_factory。"
        )
    if dialect is not None and dialect != runtime.dialect:
        raise ValueError(
            "dialect does not match the current Trace runtime; pass an "
            "explicit connect_factory to inspect another database"
        )
    return factory, runtime.dialect


def run(
    connect_factory: Callable[[], Any] | None = None,
    *,
    dialect: str | None = None,
    **kwargs: Any,
) -> None:
    """启动只读可视化并阻塞；未传连接时复用当前写入运行时。"""

    factory, resolved = _web_connection(connect_factory, dialect)
    web = import_module(".read.web", __name__)
    web.run(factory, dialect=resolved, **kwargs)


def serve(
    connect_factory: Callable[[], Any] | None = None,
    *,
    dialect: str | None = None,
    **kwargs: Any,
) -> Any:
    """在后台线程启动只读可视化；未传连接时复用当前写入运行时。"""

    factory, resolved = _web_connection(connect_factory, dialect)
    web = import_module(".read.web", __name__)
    return web.serve(factory, dialect=resolved, **kwargs)


__all__ = sorted([*_WRITE_EXPORTS, *_READ_EXPORTS, *_MYSQL_EXPORTS, "run", "serve"])
