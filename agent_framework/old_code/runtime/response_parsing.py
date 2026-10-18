"""Parse imperfect LLM text and JSON at the framework runtime boundary."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_text_block(pattern: str, text: str | None) -> str | None:
    if text is None:
        return None
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    content = match.group(1).strip()
    return content or None


def safe_json_loads(value: str) -> Any:
    """Parse common imperfect LLM JSON without raising on malformed input."""

    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        if not isinstance(value, str):
            return None
    without_comments = re.sub(r"//.*", "", value)
    repaired = re.sub(r'\\(?![\\/"bfnrt]|u[0-9a-fA-F]{4})', r"\\\\", without_comments)
    repaired = re.sub(r"\\\\\\", r"\\\\", repaired)
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        return None


__all__ = ["extract_text_block", "safe_json_loads"]
