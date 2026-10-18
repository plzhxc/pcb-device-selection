"""Configuration for the standard read-only device knowledge tools.

Credentials deliberately do not live on the configuration object.  They are
read from the process environment only when a connection is opened, so config
snapshots and Trace payloads cannot accidentally serialize them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import os
from typing import Final
from urllib.parse import urlsplit


_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})
_MYSQL_PASSWORD_ENV: Final = "PCBAGENT_MYSQL_PASSWORD"
_MILVUS_TOKEN_ENV: Final = "PCBAGENT_MILVUS_TOKEN"


def _text(
    environ: Mapping[str, str],
    name: str,
    default: str,
    *,
    maximum: int = 255,
) -> str:
    raw = environ.get(name)
    value = str(default if raw is None or not str(raw).strip() else raw).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return value


def _integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name)
    candidate = default if raw is None or not str(raw).strip() else raw
    try:
        value = int(str(candidate).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _floating(
    environ: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = environ.get(name)
    candidate = default if raw is None or not str(raw).strip() else raw
    try:
        value = float(str(candidate).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(
    environ: Mapping[str, str],
    name: str,
    default: bool,
) -> bool:
    raw = environ.get(name)
    fallback = "true" if default else "false"
    value = (
        str(fallback if raw is None or not str(raw).strip() else raw).strip().lower()
    )
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class KnowledgeConfig:
    """Validated connection metadata for SQL and Milvus knowledge reads."""

    enabled: bool = True
    # Match the established PCBAgent device-parameter service.  Deployments
    # can still override each leaf through the shared PCBAGENT_MYSQL_* env.
    mysql_host: str = "10.0.0.102"
    mysql_port: int = 3307
    mysql_user: str = "root"
    mysql_database: str = "device_parameters"
    mysql_connect_timeout_s: int = 5
    mysql_query_timeout_ms: int = 5_000
    mysql_max_results: int = 20
    milvus_url: str = "http://127.0.0.1:19530"
    milvus_database: str = "DataSheetBaseV2"
    milvus_collection: str = "DataSheet"
    milvus_timeout_s: float = 5.0
    milvus_max_results: int = 20

    def __post_init__(self) -> None:
        if not self.mysql_host.strip() or not self.mysql_user.strip():
            raise ValueError("MySQL host and user must not be empty")
        if not self.mysql_database.strip():
            raise ValueError("MySQL database must not be empty")
        if not 1 <= self.mysql_port <= 65_535:
            raise ValueError("MySQL port must be between 1 and 65535")
        if not 1 <= self.mysql_connect_timeout_s <= 30:
            raise ValueError("MySQL connect timeout must be between 1 and 30")
        if not 100 <= self.mysql_query_timeout_ms <= 30_000:
            raise ValueError("MySQL query timeout must be between 100 and 30000")
        if not 1 <= self.mysql_max_results <= 100:
            raise ValueError("MySQL max results must be between 1 and 100")
        parsed = urlsplit(self.milvus_url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Milvus URL must not embed credentials")
        if not self.milvus_url.strip():
            raise ValueError("Milvus URL must not be empty")
        if not self.milvus_database.strip() or not self.milvus_collection.strip():
            raise ValueError("Milvus database and collection must not be empty")
        if (
            not math.isfinite(self.milvus_timeout_s)
            or not 0.1 <= self.milvus_timeout_s <= 30
        ):
            raise ValueError("Milvus timeout must be between 0.1 and 30")
        if not 1 <= self.milvus_max_results <= 100:
            raise ValueError("Milvus max results must be between 1 and 100")

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "KnowledgeConfig":
        env = os.environ if environ is None else environ
        return cls(
            enabled=_boolean(env, "PCBAGENT_V2_KNOWLEDGE_ENABLED", True),
            mysql_host=_text(env, "PCBAGENT_MYSQL_HOST", "10.0.0.102"),
            mysql_port=_integer(
                env, "PCBAGENT_MYSQL_PORT", 3307, minimum=1, maximum=65_535
            ),
            mysql_user=_text(env, "PCBAGENT_MYSQL_USER", "root"),
            mysql_database=_text(env, "PCBAGENT_MYSQL_DATABASE", "device_parameters"),
            mysql_connect_timeout_s=_integer(
                env,
                "PCBAGENT_V2_KNOWLEDGE_MYSQL_CONNECT_TIMEOUT_S",
                5,
                minimum=1,
                maximum=30,
            ),
            mysql_query_timeout_ms=_integer(
                env,
                "PCBAGENT_V2_KNOWLEDGE_MYSQL_QUERY_TIMEOUT_MS",
                5_000,
                minimum=100,
                maximum=30_000,
            ),
            mysql_max_results=_integer(
                env,
                "PCBAGENT_V2_KNOWLEDGE_SQL_MAX_RESULTS",
                20,
                minimum=1,
                maximum=100,
            ),
            milvus_url=_text(
                env,
                "PCBAGENT_MILVUS_URL",
                "http://127.0.0.1:19530",
                maximum=2_048,
            ),
            milvus_database=_text(env, "PCBAGENT_MILVUS_DATABASE", "DataSheetBaseV2"),
            milvus_collection=_text(env, "PCBAGENT_MILVUS_COLLECTION", "DataSheet"),
            milvus_timeout_s=_floating(
                env,
                "PCBAGENT_V2_KNOWLEDGE_MILVUS_TIMEOUT_S",
                5.0,
                minimum=0.1,
                maximum=30.0,
            ),
            milvus_max_results=_integer(
                env,
                "PCBAGENT_V2_KNOWLEDGE_MILVUS_MAX_RESULTS",
                20,
                minimum=1,
                maximum=100,
            ),
        )

    @staticmethod
    def read_mysql_password(
        environ: Mapping[str, str] | None = None,
    ) -> str:
        env = os.environ if environ is None else environ
        value = str(env.get(_MYSQL_PASSWORD_ENV, ""))
        if len(value) > 16_384:
            raise ValueError("PCBAGENT_MYSQL_PASSWORD is too long")
        return value

    @staticmethod
    def read_milvus_token(
        environ: Mapping[str, str] | None = None,
    ) -> str | None:
        env = os.environ if environ is None else environ
        value = str(env.get(_MILVUS_TOKEN_ENV, "")).strip()
        if len(value) > 16_384:
            raise ValueError("PCBAGENT_MILVUS_TOKEN is too long")
        return value or None

    def public_config(self) -> dict[str, object]:
        """Return a credential-free snapshot suitable for diagnostics."""

        return {
            "enabled": self.enabled,
            "mysql_host": self.mysql_host,
            "mysql_port": self.mysql_port,
            "mysql_database": self.mysql_database,
            "mysql_max_results": self.mysql_max_results,
            "milvus_url": self.milvus_url,
            "milvus_database": self.milvus_database,
            "milvus_collection": self.milvus_collection,
            "milvus_max_results": self.milvus_max_results,
        }


__all__ = ["KnowledgeConfig"]
