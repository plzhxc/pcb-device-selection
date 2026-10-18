"""Bounded Milvus retrieval for peripheral-topology evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..milvus import DatasheetKnowledgeProvider
from .common import (
    DATASHEET_LIMIT,
    bounded_text,
    issue,
    safe_scalar,
    serialized_size,
)


def _datasheet_rows(raw: Any) -> list[Mapping[str, Any]]:
    if isinstance(raw, Mapping):
        for key in ("items", "chunks", "results", "evidence", "snippets"):
            rows = raw.get(key)
            if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
                return [row for row in rows if isinstance(row, Mapping)]
        if any(
            key in raw for key in ("content", "content_preview", "excerpt", "guidance")
        ):
            return [raw]
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [row for row in raw if isinstance(row, Mapping)]
    return []


def fetch_topology(
    datasheets: DatasheetKnowledgeProvider,
    c_number: str,
    *,
    budget: int,
) -> tuple[dict[str, Any], dict[str, str] | None]:
    method_name = "search_peripheral_guidance"
    method = getattr(datasheets, method_name, None)
    try:
        if callable(method):
            try:
                raw = method(c_number, limit=DATASHEET_LIMIT)
            except TypeError:
                raw = method(c_number)
        else:
            method_name = "search"
            raw = datasheets.search(
                c_number,
                by="c_number",
                limit=DATASHEET_LIMIT,
            )
    except Exception as exc:
        return {
            "status": "error",
            "c_number": c_number,
            "source_method": method_name,
            "error_type": type(exc).__name__,
            "items": [],
        }, issue(
            "topology_lookup_error",
            "topology",
            f"{c_number} Milvus lookup failed with {type(exc).__name__}.",
        )

    raw_mapping = raw if isinstance(raw, Mapping) else {}
    retrieval_mode = str(
        raw_mapping.get("retrieval_mode")
        or ("scalar_exact_c" if method_name == "search" else "provider_guidance")
    ).strip()
    degraded = bool(raw_mapping.get("degraded")) or method_name == "search"
    degraded_reason = str(
        raw_mapping.get("fallback_reason")
        or ("provider_has_no_guidance_search" if method_name == "search" else "")
    ).strip()
    items: list[dict[str, Any]] = []
    truncated = False
    for row in _datasheet_rows(raw):
        reported_c = str(
            row.get("lichuang_id")
            or row.get("supplier_part")
            or row.get("c_number")
            or ""
        ).strip()
        if reported_c and reported_c.upper() != c_number:
            continue
        content = next(
            (
                row.get(key)
                for key in (
                    "excerpt",
                    "content_preview",
                    "content",
                    "text",
                    "guidance",
                )
                if row.get(key) not in (None, "")
            ),
            "",
        )
        item = {
            "id": bounded_text(row.get("id"), 96) or None,
            "c_number": c_number,
            "title": bounded_text(row.get("title"), 140) or None,
            "section_path": bounded_text(row.get("section_path"), 180) or None,
            "content_type": bounded_text(row.get("content_type"), 48) or None,
            "chunk_idx": safe_scalar(row.get("chunk_idx"), 24),
            "chunk_total": safe_scalar(row.get("chunk_total"), 24),
            "excerpt": bounded_text(content, 900),
            "score": safe_scalar(row.get("score"), 32),
        }
        if len(items) >= DATASHEET_LIMIT or serialized_size([*items, item]) > budget:
            truncated = True
            break
        items.append(item)
    if not items:
        return {
            "status": "not_found",
            "c_number": c_number,
            "source_method": method_name,
            "retrieval_mode": retrieval_mode,
            "degraded": degraded,
            "degraded_reason": degraded_reason or None,
            "item_count": 0,
            "truncated": False,
            "items": [],
        }, issue(
            "topology_not_found",
            "topology",
            f"No Milvus guidance was found for {c_number}.",
        )
    topology_issue = None
    if degraded:
        topology_issue = issue(
            "topology_retrieval_degraded",
            "topology",
            f"{c_number} used {retrieval_mode} instead of vector guidance"
            + (f" ({degraded_reason})" if degraded_reason else "."),
        )
    elif truncated:
        topology_issue = issue(
            "topology_truncated",
            "topology",
            f"{c_number} Milvus guidance was bounded.",
        )
    return {
        "status": "found",
        "c_number": c_number,
        "source_method": method_name,
        "retrieval_mode": retrieval_mode,
        "degraded": degraded,
        "degraded_reason": degraded_reason or None,
        "truncated": truncated,
        "item_count": len(items),
        "items": items,
    }, topology_issue


__all__ = ["fetch_topology"]
