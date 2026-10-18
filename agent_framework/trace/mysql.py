"""MySQL deployment adapter for Agent Framework Trace.

The core Trace runtime remains database-agnostic.  This module owns optional
environment parsing, pooled MySQL connections, schema setup, health checks,
and the native read-only Web surface.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from importlib import import_module
from threading import Lock
from typing import Any

from .database import TraceDatabase
from .read import TraceReader
from .write.runtime import TraceRuntime, init as _init_trace_runtime


TRACE_ENV_PREFIX = "AGENT_FRAMEWORK_TRACE_"

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_MYSQL_DATABASE_NAME = re.compile(r"[A-Za-z0-9_]{1,64}")


class TraceConfigurationError(ValueError):
    """Raised when the dedicated Trace target is missing or unsafe."""


def _read_bool(
    source: Mapping[str, str],
    key: str,
    *,
    default: bool,
) -> bool:
    raw = source.get(key)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise TraceConfigurationError(
        f"{key} must be one of: " f"{', '.join(sorted(_TRUE_VALUES | _FALSE_VALUES))}"
    )


def _read_int(
    source: Mapping[str, str],
    key: str,
    *,
    default: int,
) -> int:
    raw = source.get(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise TraceConfigurationError(f"{key} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class MySQLTraceConfig:
    """MySQL storage and Web settings for one Framework Trace runtime."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "agent_trace"
    password: str = field(default="", repr=False)
    database: str = "agent_trace"
    ensure_schema: bool = True
    connection_timeout_s: int = 10
    pool_size: int = 4
    web_host: str = "127.0.0.1"
    web_port: int = 8767

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        prefix: str = TRACE_ENV_PREFIX,
        defaults: "MySQLTraceConfig | None" = None,
    ) -> "MySQLTraceConfig":
        """Parse one caller-selected environment namespace."""

        source = os.environ if environ is None else environ
        base = defaults or cls()

        def value(name: str, default: str) -> str:
            return source.get(f"{prefix}{name}", default).strip()

        password = source.get(f"{prefix}MYSQL_PASSWORD", base.password)

        return cls(
            enabled=_read_bool(
                source,
                f"{prefix}ENABLED",
                default=base.enabled,
            ),
            host=value("MYSQL_HOST", base.host),
            port=_read_int(
                source,
                f"{prefix}MYSQL_PORT",
                default=base.port,
            ),
            user=value("MYSQL_USER", base.user),
            password=password,
            database=value("MYSQL_DATABASE", base.database),
            ensure_schema=_read_bool(
                source,
                f"{prefix}ENSURE_SCHEMA",
                default=base.ensure_schema,
            ),
            connection_timeout_s=_read_int(
                source,
                f"{prefix}MYSQL_CONNECTION_TIMEOUT_S",
                default=base.connection_timeout_s,
            ),
            pool_size=_read_int(
                source,
                f"{prefix}MYSQL_POOL_SIZE",
                default=base.pool_size,
            ),
            web_host=value("WEB_HOST", base.web_host),
            web_port=_read_int(
                source,
                f"{prefix}WEB_PORT",
                default=base.web_port,
            ),
        )

    def validate(self, *, require_password: bool | None = None) -> None:
        """Validate database and Web connectivity settings."""

        for name, text_value in (
            ("host", self.host),
            ("user", self.user),
            ("database", self.database),
            ("web_host", self.web_host),
        ):
            if not text_value.strip():
                raise TraceConfigurationError(f"Trace {name} must not be empty")
        for name, port_value in (
            ("port", self.port),
            ("web_port", self.web_port),
        ):
            if not 1 <= port_value <= 65535:
                raise TraceConfigurationError(
                    f"Trace {name} must be between 1 and 65535"
                )
        if self.connection_timeout_s <= 0:
            raise TraceConfigurationError(
                "Trace connection_timeout_s must be greater than zero"
            )
        if not 1 <= self.pool_size <= 32:
            raise TraceConfigurationError("Trace pool_size must be between 1 and 32")

        if _MYSQL_DATABASE_NAME.fullmatch(self.database) is None:
            raise TraceConfigurationError(
                "Trace database must contain only ASCII letters, digits, and "
                "underscores, with a maximum length of 64 characters"
            )

        password_required = (
            self.enabled if require_password is None else require_password
        )
        if password_required and not self.password:
            raise TraceConfigurationError("Trace MySQL password is required")

    def public_target(self) -> dict[str, Any]:
        """Return connection metadata that is safe to print or log."""

        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "database": self.database,
            "web_url": f"http://{self.web_host}:{self.web_port}",
        }


def mysql_connection_factory(config: MySQLTraceConfig) -> Callable[[], Any]:
    """Build a zero-argument DB-API factory for the dedicated database."""

    config.validate(require_password=True)
    pool: Any = None
    pool_lock = Lock()

    def connect() -> Any:
        nonlocal pool
        if pool is None:
            with pool_lock:
                if pool is None:
                    try:
                        pooling = import_module("mysql.connector.pooling")
                    except ImportError as exc:
                        raise RuntimeError(
                            "mysql-connector-python is required for "
                            "Agent Framework Trace"
                        ) from exc
                    pool = pooling.MySQLConnectionPool(
                        pool_size=config.pool_size,
                        pool_reset_session=True,
                        host=config.host,
                        port=config.port,
                        user=config.user,
                        password=config.password,
                        database=config.database,
                        connection_timeout=config.connection_timeout_s,
                        autocommit=False,
                    )
        return pool.get_connection()

    return connect


def _ensure_mysql_database_exists(config: MySQLTraceConfig) -> None:
    """Create the configured database before opening database-bound connections.

    The bootstrap connection deliberately omits ``database`` because this is the
    path used when the dedicated database does not exist yet.  The strict
    identifier validation in :meth:`MySQLTraceConfig.validate` makes the required
    DDL interpolation safe; credentials remain connector parameters.
    """

    config.validate(require_password=True)
    try:
        connector = import_module("mysql.connector")
    except ImportError as exc:
        raise RuntimeError(
            "mysql-connector-python is required for Agent Framework Trace"
        ) from exc

    connection: Any = None
    cursor: Any = None
    try:
        connection = connector.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            connection_timeout=config.connection_timeout_s,
            autocommit=True,
        )
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{config.database}` "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    finally:
        if cursor is not None:
            with suppress(Exception):
                cursor.close()
        if connection is not None:
            with suppress(Exception):
                connection.close()


def init_mysql_trace(
    config: MySQLTraceConfig,
    *,
    trace_id: str | Callable[[], str] | None = None,
) -> TraceRuntime:
    """Initialize all four default observers before Framework objects exist."""

    config.validate(require_password=config.enabled)
    if not config.enabled:
        return _init_trace_runtime(
            None,
            dialect="mysql",
            trace_id=trace_id,
            enabled=False,
            ensure_schema=False,
        )
    return _init_trace_runtime(
        mysql_connection_factory(config),
        dialect="mysql",
        trace_id=trace_id,
        enabled=True,
        ensure_schema=config.ensure_schema,
    )


def mysql_trace_health(
    config: MySQLTraceConfig,
    *,
    connect_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Read the isolated Trace schema health without mutating it."""

    config.validate(require_password=True)
    reader = TraceReader(
        connect_factory or mysql_connection_factory(config),
        dialect="mysql",
    )
    return {
        **reader.health(),
        "target": config.public_target(),
    }


def setup_mysql_trace_database(
    config: MySQLTraceConfig,
    *,
    connect_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Explicitly create the database and schema, then verify all eight tables.

    This maintenance operation is independent of the writer ``enabled`` flag.
    """

    _ensure_mysql_database_exists(config)
    factory = connect_factory or mysql_connection_factory(config)
    TraceDatabase(factory, dialect="mysql").ensure_schema()
    return mysql_trace_health(config, connect_factory=factory)


def run_mysql_trace_web(
    config: MySQLTraceConfig,
    *,
    title: str = "Agent Framework Trace",
) -> None:
    """Prepare storage, then run the native read-only Trace Web app."""

    config.validate(require_password=True)
    factory = mysql_connection_factory(config)
    if config.ensure_schema:
        health = setup_mysql_trace_database(config, connect_factory=factory)
    else:
        health = mysql_trace_health(config, connect_factory=factory)
    if not health.get("ok"):
        raise RuntimeError("Agent Framework Trace schema health check failed")

    from .read.web import run as run_trace_web

    run_trace_web(
        factory,
        dialect="mysql",
        host=config.web_host,
        port=config.web_port,
        title=title,
    )


__all__ = [
    "MySQLTraceConfig",
    "TraceConfigurationError",
    "init_mysql_trace",
    "mysql_connection_factory",
    "mysql_trace_health",
    "run_mysql_trace_web",
    "setup_mysql_trace_database",
]
