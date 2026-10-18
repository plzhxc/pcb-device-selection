"""Framework-native, constrained SQL device-catalog tool.

The model can search the known ``devices`` catalog and retrieve associated
pins/parameter rows.  It cannot submit SQL text, select arbitrary tables, or
perform writes.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
import math
import re
from typing import Any, Protocol, runtime_checkable

from agent_framework.tool import BaseTool, ToolCapability, ToolOutput

from .config import KnowledgeConfig


_C_NUMBER = re.compile(r"^C\d{1,20}$", flags=re.IGNORECASE)
_SAFE_CATEGORY = re.compile(r"^[A-Za-z0-9_]{1,100}$")


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<binary:{len(value)} bytes>"
    return str(value)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@runtime_checkable
class DeviceCatalogProvider(Protocol):
    """Backend contract used by SQL and Milvus tools and by tests."""

    name: str

    def search_devices(
        self,
        query: str,
        *,
        limit: int = 5,
        include_pins: bool = False,
        include_parameters: bool = True,
    ) -> list[dict[str, Any]]:
        ...

    def status(self) -> dict[str, object]:
        ...


class MySQLDeviceCatalogProvider:
    """Open a bounded read-only transaction for every catalog request."""

    name = "mysql_device_parameters"

    def __init__(self, config: KnowledgeConfig) -> None:
        self.config = config

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import mysql.connector
        except ImportError as exc:
            raise RuntimeError(
                "SQL knowledge tool requires mysql-connector-python"
            ) from exc

        connection: Any = None
        try:
            connection = mysql.connector.connect(
                host=self.config.mysql_host,
                port=self.config.mysql_port,
                user=self.config.mysql_user,
                password=self.config.read_mysql_password(),
                database=self.config.mysql_database,
                connection_timeout=self.config.mysql_connect_timeout_s,
                autocommit=False,
            )
            connection.start_transaction(readonly=True)
            yield connection
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass

    def _hint(self) -> str:
        return f"/*+ MAX_EXECUTION_TIME({self.config.mysql_query_timeout_ms}) */"

    @staticmethod
    def _cursor(connection: Any) -> Any:
        return connection.cursor(dictionary=True, buffered=True)

    def status(self) -> dict[str, object]:
        try:
            with self._connection() as connection:
                cursor = self._cursor(connection)
                try:
                    cursor.execute(f"SELECT {self._hint()} 1 AS healthy")
                    cursor.fetchone()
                finally:
                    cursor.close()
            return {
                "available": True,
                "read_only": True,
                "database": self.config.mysql_database,
            }
        except Exception as exc:
            return {
                "available": False,
                "read_only": True,
                "database": self.config.mysql_database,
                "error": type(exc).__name__,
            }

    def _pins(
        self,
        connection: Any,
        device_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        if not device_ids:
            return {}
        placeholders = ",".join(["%s"] * len(device_ids))
        cursor = self._cursor(connection)
        try:
            cursor.execute(
                f"SELECT {self._hint()} device_id, pin_index, pin_number, "
                "pin_name, pin_type, pin_description, pin_connection_notes, "
                "pin_impedance, pin_frequency FROM pins "
                f"WHERE device_id IN ({placeholders}) "
                "ORDER BY device_id, pin_index",
                tuple(device_ids),
            )
            grouped: dict[int, list[dict[str, Any]]] = {}
            for raw in cursor.fetchall():
                device_id = int(raw.get("device_id") or 0)
                grouped.setdefault(device_id, []).append(
                    {
                        key: _safe_value(value)
                        for key, value in raw.items()
                        if key != "device_id"
                    }
                )
            return grouped
        finally:
            cursor.close()

    def _parameters(
        self,
        connection: Any,
        category: str,
        device_ids: list[int],
    ) -> dict[int, dict[str, Any]]:
        if not device_ids or not _SAFE_CATEGORY.fullmatch(category):
            return {}
        table = f"params_{category}"
        cursor = self._cursor(connection)
        try:
            cursor.execute(
                "SELECT COUNT(*) AS n FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self.config.mysql_database, table),
            )
            if int((cursor.fetchone() or {}).get("n") or 0) != 1:
                return {}
            placeholders = ",".join(["%s"] * len(device_ids))
            # ``table`` is derived from a catalog category and accepted only by
            # the strict identifier regex plus information_schema check above.
            cursor.execute(
                f"SELECT {self._hint()} * FROM `{table}` "
                f"WHERE device_id IN ({placeholders})",
                tuple(device_ids),
            )
            result: dict[int, dict[str, Any]] = {}
            for raw in cursor.fetchall():
                device_id = int(raw.get("device_id") or 0)
                result[device_id] = {
                    str(key): _safe_value(value)
                    for key, value in raw.items()
                    if key != "device_id" and value not in (None, "")
                }
            return result
        finally:
            cursor.close()

    def search_devices(
        self,
        query: str,
        *,
        limit: int = 5,
        include_pins: bool = False,
        include_parameters: bool = True,
    ) -> list[dict[str, Any]]:
        normalized = str(query).strip()
        if not normalized:
            raise ValueError("query must not be blank")
        if len(normalized) > 160:
            raise ValueError("query exceeds 160 characters")
        if isinstance(limit, bool) or not 1 <= int(limit) <= self.config.mysql_max_results:
            raise ValueError(
                f"limit must be between 1 and {self.config.mysql_max_results}"
            )
        limit = int(limit)
        exact_c = normalized.upper() if _C_NUMBER.fullmatch(normalized) else ""
        exact_name = normalized
        pattern = f"%{_escape_like(normalized)}%"

        with self._connection() as connection:
            cursor = self._cursor(connection)
            try:
                cursor.execute(
                    f"SELECT {self._hint()} device_id, device_name, supplier_part, "
                    "category_level_1, category_level_2, pin_count, price "
                    "FROM devices WHERE supplier_part = %s OR device_name = %s "
                    "OR device_name LIKE %s ESCAPE '\\\\' "
                    "OR category_level_1 LIKE %s ESCAPE '\\\\' "
                    "OR category_level_2 LIKE %s ESCAPE '\\\\' "
                    "ORDER BY CASE WHEN supplier_part = %s THEN 0 "
                    "WHEN device_name = %s THEN 1 ELSE 2 END, device_name ASC "
                    "LIMIT %s",
                    (
                        exact_c or normalized,
                        exact_name,
                        pattern,
                        pattern,
                        pattern,
                        exact_c or normalized,
                        exact_name,
                        limit,
                    ),
                )
                rows = [dict(row) for row in cursor.fetchall()]
            finally:
                cursor.close()

            ids = [int(row["device_id"]) for row in rows]
            pins = self._pins(connection, ids) if include_pins else {}
            parameter_rows: dict[int, dict[str, Any]] = {}
            if include_parameters:
                by_category: dict[str, list[int]] = {}
                for row in rows:
                    by_category.setdefault(
                        str(row.get("category_level_2") or ""), []
                    ).append(int(row["device_id"]))
                for category, category_ids in by_category.items():
                    parameter_rows.update(
                        self._parameters(connection, category, category_ids)
                    )

        results = []
        for row in rows:
            device_id = int(row["device_id"])
            item = {str(key): _safe_value(value) for key, value in row.items()}
            if include_pins:
                item["pins"] = pins.get(device_id, [])
            if include_parameters:
                item["parameters"] = parameter_rows.get(device_id, {})
            results.append(item)
        return results


class DeviceCatalogSQLTool(BaseTool):
    """Search the approved device catalog; never execute model-authored SQL."""

    name = "search_device_catalog"
    description = (
        "查询只读器件目录。输入器件型号或立创 C 编号，返回数据库中真实存在的"
        "device_name、supplier_part、分类、价格、参数；需要核对引脚时可请求 pins。"
        "本工具不接受 SQL 文本。"
    )
    capability = ToolCapability.READ
    parallel_safe = True
    detachable_on_timeout = True
    timeout_s = 15.0
    max_output_chars = None
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "完整器件型号、型号关键词或立创 C 编号。",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 5,
            },
            "include_pins": {
                "type": "boolean",
                "default": False,
                "description": "是否返回候选器件的完整引脚记录。",
            },
            "include_parameters": {
                "type": "boolean",
                "default": True,
                "description": "是否返回器件分类参数表中的非空参数。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, provider: DeviceCatalogProvider) -> None:
        super().__init__()
        if not isinstance(provider, DeviceCatalogProvider):
            raise TypeError("provider must implement DeviceCatalogProvider")
        self.provider = provider

    def call(self, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        limit = min(int(arguments.get("limit", 5)), 20)
        rows = self.provider.search_devices(
            str(arguments.get("query") or ""),
            limit=limit,
            include_pins=bool(arguments.get("include_pins", False)),
            include_parameters=bool(arguments.get("include_parameters", True)),
        )
        return ToolOutput(
            content={
                "query": str(arguments.get("query") or "").strip(),
                "total": len(rows),
                "candidates": rows,
                "evidence_source": self.provider.name,
                "read_only": True,
            },
            metadata={"backend": self.provider.name, "rows": len(rows)},
        )


__all__ = [
    "DeviceCatalogProvider",
    "DeviceCatalogSQLTool",
    "MySQLDeviceCatalogProvider",
]
