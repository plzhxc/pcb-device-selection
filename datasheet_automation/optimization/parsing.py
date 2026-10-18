"""Small response-parsing helpers shared by optimization agents."""

from __future__ import annotations

import json
import re
from typing import Any


_FENCED_RE = re.compile(r"```([a-zA-Z0-9_-]*)\s*(.*?)```", re.DOTALL)


def guess_title(markdown: str) -> str:
    """Return the first visible Markdown line as a bounded fallback title."""

    for raw_line in str(markdown or "").splitlines():
        line = re.sub(r"^#{1,6}\s*", "", raw_line.strip())
        if line:
            return line[:120]
    return "datasheet_text"


def clip_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def extract_fenced_block(text: str, name: str) -> str:
    raw = str(text or "").strip()
    for match in _FENCED_RE.finditer(raw):
        if (match.group(1) or "").strip().lower() == name.lower():
            return match.group(2).strip()
    return raw


def extract_json_payload(text: str) -> Any:
    raw = str(text or "").strip()
    if not raw:
        return None

    candidates: list[str] = []
    fenced = extract_fenced_block(raw, "json")
    if fenced != raw:
        candidates.append(fenced)
    candidates.append(raw)

    decoder = json.JSONDecoder()
    for candidate in candidates:
        for value in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                pass
            for index, char in enumerate(value):
                if char not in "[{":
                    continue
                try:
                    payload, _ = decoder.raw_decode(value[index:])
                    return payload
                except json.JSONDecodeError:
                    continue
    return None


__all__ = [
    "clip_text",
    "extract_fenced_block",
    "extract_json_payload",
    "guess_title",
]
