"""Strict YAML/mapping configuration for the unified datasheet pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


OPERATIONS = {"search", "parse", "collect_and_parse", "optimize", "full"}
REQUEST_SCHEMA = "datasheet-automation.request.v1"
_TOP_KEYS = {"schema_version", "operation", "payload", "options", "logging", "trace"}


@dataclass
class PlatformConfig:
    operation: str
    payload: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    logging: dict[str, Any] = field(default_factory=dict)
    trace: dict[str, Any] = field(default_factory=dict)


def default_config(operation: str, **payload: Any) -> dict[str, Any]:
    """Return a complete, loadable request with code-derived defaults."""

    if operation not in OPERATIONS:
        raise ValueError(
            f"unsupported operation {operation!r}; expected one of {sorted(OPERATIONS)}"
        )
    from .collection import CollectionConfig
    from datasheet_automation.parsing.protocol import MinerURequest

    mineru_request = MinerURequest(pdf_path=Path("."), output_dir=Path("."))
    collection = CollectionConfig()
    mineru = {
        "backend": mineru_request.backend,
        "method": mineru_request.method,
        "lang": mineru_request.lang,
        "formula": mineru_request.formula,
        "table": mineru_request.table,
        "timeout_s": mineru_request.timeout_s,
    }
    optimization = {
        "output_subdir": "optimized",
        "chunk_size": 5000,
        "agent_mode": "required",
        "use_vlm": True,
        "llm_model": "qwen-plus",
        "vlm_model": "qwen-vl-plus",
        "reasoning": None,
        "concurrency": 6,
    }

    if operation == "search":
        options: dict[str, Any] = {"candidate_limit": collection.candidate_limit}
    elif operation == "parse":
        options = {
            "reuse": True,
            "execute": True,
            "overwrite": collection.overwrite,
            "mineru": mineru,
        }
    elif operation == "optimize":
        options = {"optimization": optimization}
    else:
        options = {
            "overwrite": collection.overwrite,
            "reuse": collection.reuse_mineru_output,
            "candidate_limit": collection.candidate_limit,
            "price_ladder": collection.fetch_price_ladder,
            "mineru": mineru,
        }
        if operation == "full":
            options["optimization"] = optimization
    return {
        "schema_version": REQUEST_SCHEMA,
        "operation": operation,
        "payload": dict(payload),
        "options": options,
        "logging": {"level": "INFO"},
    }


def load_config(source: str | Path | Mapping[str, Any]) -> PlatformConfig:
    """Load and validate a request from YAML or an in-memory mapping."""

    if isinstance(source, Mapping):
        raw: Mapping[str, Any] = source
    else:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"config file not found: {path}")
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise ValueError(
                f"config must be a mapping, got {type(loaded).__name__}: {path}"
            )
        raw = loaded

    unknown = sorted(set(raw) - _TOP_KEYS)
    if unknown:
        raise ValueError(f"unknown config key(s): {', '.join(unknown)}")

    schema = raw.get("schema_version")
    if schema is not None and schema != REQUEST_SCHEMA:
        raise ValueError(
            f"schema_version must be {REQUEST_SCHEMA!r}; observed {schema!r}"
        )

    operation = str(raw.get("operation") or "").strip()
    if operation not in OPERATIONS:
        raise ValueError(
            f"unsupported operation {operation!r}; expected one of {sorted(OPERATIONS)}"
        )
    for name in ("payload", "options", "logging", "trace"):
        value = raw.get(name)
        if value is not None and not isinstance(value, Mapping):
            raise TypeError(f"config.{name} must be a mapping")

    return PlatformConfig(
        operation=operation,
        payload=dict(raw.get("payload") or {}),
        options=dict(raw.get("options") or {}),
        logging=dict(raw.get("logging") or {}),
        trace=dict(raw.get("trace") or {}),
    )
