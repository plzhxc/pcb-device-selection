"""只读数据库访问。

每个查询开一个只读事务，结束即回滚。公开的 query() 只接受 SELECT / SHOW /
DESCRIBE，杜绝任何写库路径——器件目录不属于本阶段，改数据是别人的事。
"""

from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import datetime as _dt
from typing import Any, Iterator, Sequence

from .config import DBConfig

_READONLY_PREFIXES = ("select", "show", "describe", "desc", "explain", "with")
# 无论写什么 SQL，这些词一旦出现就直接拒绝——包括注释里藏花招的情况。
_FORBIDDEN = (
    "insert ",
    "update ",
    "delete ",
    "replace ",
    "drop ",
    "alter ",
    "create ",
    "truncate ",
    "grant ",
    "revoke ",
    "load data",
    "into outfile",
    "into dumpfile",
    " for update",
    "lock in share mode",
)


class UnsafeQueryError(ValueError):
    """调用了非只读 SQL。"""


def _plain(value: Any) -> Any:
    """把驱动返回值收敛成可 JSON 序列化的标量。"""

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat(sep=" ")
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - decode 已经兜底
            return ""
    return value


def assert_readonly(sql: str) -> str:
    normalized = " ".join(str(sql or "").split()).lower()
    if not normalized:
        raise UnsafeQueryError("SQL must not be blank")
    if not normalized.startswith(_READONLY_PREFIXES):
        raise UnsafeQueryError("only SELECT / SHOW / DESCRIBE / EXPLAIN are allowed")
    for token in _FORBIDDEN:
        if token in normalized:
            raise UnsafeQueryError(f"statement contains forbidden token {token.strip()!r}")
    if ";" in normalized.rstrip(";"):
        raise UnsafeQueryError("only one statement per call is allowed")
    return sql


class Database:
    """一个薄封装：连接、只读事务、参数化查询。"""

    def __init__(self, config: DBConfig | None = None) -> None:
        self.config = config or DBConfig.from_env()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import mysql.connector
        except ImportError as exc:  # pragma: no cover - 本机已装
            raise RuntimeError("需要 mysql-connector-python") from exc

        connection = mysql.connector.connect(**self.config.connect_kwargs())
        try:
            connection.start_transaction(readonly=True)
            yield connection
        finally:
            try:
                connection.rollback()
            except Exception:
                pass
            try:
                connection.close()
            except Exception:
                pass

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        assert_readonly(sql)
        with self._connection() as connection:
            cursor = connection.cursor(dictionary=True, buffered=True)
            try:
                cursor.execute(sql, tuple(params or ()))
                rows = cursor.fetchall()
                return [
                    {str(key): _plain(value) for key, value in dict(row).items()}
                    for row in rows
                ]
            finally:
                cursor.close()

    def one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def status(self) -> dict[str, Any]:
        """健康检查：给出连接身份和规模，不带密码。"""

        info = self.config.describe()
        try:
            row = self.one(
                "SELECT VERSION() AS version, DATABASE() AS db, USER() AS account"
            )
            counts = self.one(
                "SELECT (SELECT COUNT(*) FROM dsearch_v_device_search_profiled_incremental) AS devices,"
                " (SELECT COUNT(*) FROM dsearch_v_device_attribute_priority_incremental) AS attribute_rows"
            )
            return {"ok": True, "config": info, "server": row, "counts": counts}
        except Exception as exc:
            return {"ok": False, "config": info, "error": f"{type(exc).__name__}: {exc}"}
