"""Shared limits, deterministic digests and semantic diagnostic records."""

from __future__ import annotations

from typing import Any
import hashlib
import json
import re


MAX_REPAIRS = 2


MAX_CORE_INSTANCES = 1000


MAX_MODULES = 128


MAX_COMMON_DEVICES = 10000


_IDENTITY = re.compile(r"[CS][0-9]+")


def _diagnostic(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"severity": "error", "code": code, "message": message, **extra}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

