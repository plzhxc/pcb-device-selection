"""启动观测网页。

``run()`` 阻塞、``serve()`` 后台线程。两个都有的原因：脚本里想让它挡住进程，
Notebook 或已有服务里想让它在旁边跑着。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ...database import Dialect
from .app import create_app


def _resolve_connection(
    connect_factory: Callable[[], Any] | None, dialect: Dialect | None
) -> tuple[Callable[[], Any], Dialect]:
    """校验读侧连接；读侧不会反向读取写入运行时。"""

    if connect_factory is None:
        raise RuntimeError(
            "read.web 需要显式 connect_factory；若要复用 trace.init() 的连接，"
            "请调用公共入口 trace.run() 或 trace.serve()。"
        )
    resolved = dialect or "mysql"
    if resolved not in {"sqlite", "mysql"}:
        raise ValueError("dialect must be 'sqlite' or 'mysql'")
    return connect_factory, resolved


def run(
    connect_factory: Callable[[], Any] | None = None,
    *,
    dialect: Dialect | None = None,
    host: str = "0.0.0.0",
    port: int = 8765,
    title: str = "Agent Tracer",
    log_level: str = "warning",
) -> None:
    """起网页并阻塞当前线程。

    Args:
        connect_factory: 只读数据库连接工厂。直接调用本模块时必须传入；
            公共入口 ``trace.run()`` 才会负责复用写入运行时。
        dialect: ``mysql`` 或 ``sqlite``。
        host: 监听地址。默认 ``0.0.0.0`` —— 这个服务通常跑在开发机上，
            要让同事从自己电脑打开。
        port: 端口。
        title: 页面标题，多环境并存时用来区分。
    """

    import uvicorn

    factory, resolved = _resolve_connection(connect_factory, dialect)
    app = create_app(factory, dialect=resolved, title=title)
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print(f"观测网页：http://{shown}:{port}    （库：{resolved}）")
    uvicorn.run(app, host=host, port=port, log_level=log_level)


def serve(
    connect_factory: Callable[[], Any] | None = None,
    *,
    dialect: Dialect | None = None,
    host: str = "0.0.0.0",
    port: int = 8765,
    title: str = "Agent Tracer",
    log_level: str = "warning",
) -> threading.Thread:
    """在后台线程里起网页，立即返回。

    线程是 daemon：主程序退出时它跟着退，不会把进程挂住。
    """

    import uvicorn

    factory, resolved = _resolve_connection(connect_factory, dialect)
    app = create_app(factory, dialect=resolved, title=title)
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level=log_level)
    )
    thread = threading.Thread(target=server.run, daemon=True, name="agent-trace-ui")
    thread.start()
    shown = "localhost" if host in ("0.0.0.0", "::") else host
    print(f"观测网页（后台）：http://{shown}:{port}    （库：{resolved}）")
    return thread


__all__ = ["run", "serve"]
