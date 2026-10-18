"""FastAPI composition for the read-only Agent Tracer."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ...database import Dialect
from .. import TraceReader
from .api_routes import router as api_router
from .execution_views import router as execution_router
from .flow_views import router as flow_router
from .module_view import router as module_router
from .common import STATIC_ROOT


def create_app(
    connect_factory: Callable[[], Any],
    *,
    dialect: Dialect = "mysql",
    title: str = "Agent Tracer",
) -> FastAPI:
    """创建只读 Trace Web 应用。

    数据库连接由调用方提供；页面和 API 共享同一个 :class:`TraceReader`，不会创建
    Writer，也不会执行任何写语句。
    """

    reader = TraceReader(connect_factory, dialect=dialect)
    app = FastAPI(
        title=f"{title} API",
        description=(
            "Read-only Workflow → Stage → Agent → LLM trace queries backed by "
            "the eight current trace tables."
        ),
        version="1.0",
    )
    app.state.trace_reader = reader
    app.state.trace_title = title
    app.mount(
        "/assets",
        StaticFiles(directory=str(STATIC_ROOT)),
        name="trace-assets",
    )
    app.include_router(api_router)
    app.include_router(flow_router)
    app.include_router(module_router)
    app.include_router(execution_router)
    return app


__all__ = ["create_app"]
