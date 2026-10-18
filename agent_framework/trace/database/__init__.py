"""Trace 数据库控制层。

这里只负责 DB-API 连接、SQLite/MySQL 方言差异、事务辅助和八张表的 Schema。
写入侧与读取侧都依赖本层；本层不依赖它们。
"""

from .control import (
    ConnectionFactory,
    Dialect,
    TraceDatabase,
    TraceDatabaseError,
)

__all__ = [
    "ConnectionFactory",
    "Dialect",
    "TraceDatabase",
    "TraceDatabaseError",
]
