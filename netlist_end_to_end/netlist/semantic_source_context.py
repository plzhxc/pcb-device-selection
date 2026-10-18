"""Resolve explicitly referenced source bodies without inferring new facts."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any

from .prompts import load_prompt


def _same_json(left: Any, right: Any) -> bool:
    try:
        return json.dumps(left, ensure_ascii=False, sort_keys=True, allow_nan=False) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return False



def with_referenced_source_context(payload: Mapping, *, knowledge: Mapping,
                                   source_index: Mapping, source_bundles: Mapping) -> dict:
    """Present only foreign bodies actually cited by this stage's supplied data.

    Resolve exact aliases through the existing global index, then require one
    unchanged prepared chunk and its original-source attribution. This does not
    authenticate a new field, grant an operating selection, or fetch any source.
    No foreign reference means no payload change, including no extra message.
    """
    from hashlib import sha256
    from datasheet_automation.knowledge.evidence import authenticate_source_quote

    requested: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key in {"source_registry", "source_index", "referenced_source_context"}:
                    continue
                if key == "source_ref" and isinstance(item, str):
                    requested.add(item)
                elif key == "source_refs" and isinstance(item, list):
                    requested.update(ref for ref in item if isinstance(ref, str))
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    for key in ("knowledge", "core_pool", "context", "module_plan", "previous_plan",
                "frozen_architecture", "voltage_domains", "planning_obligations"):
        if key == "knowledge":
            # Only the device's raw corpus is not a request. Structured fact or
            # profile evidence below contracts remains an explicit reference.
            for record in payload.get(key, {}).values():
                if isinstance(record, Mapping):
                    collect({name: value for name, value in record.items() if name != "evidence"})
        else:
            collect(payload.get(key))
    own_parts = set(payload.get("knowledge", {}))
    entries, unresolved, needed_keys = {}, [], {}

    def chunks(record: Mapping) -> list:
        rows = record.get("evidence", [])
        return rows if isinstance(rows, list) else []

    for ref in sorted(requested):
        matches = [(key, row) for key, row in source_index.items() if isinstance(row, Mapping)
                   and row.get("kind") == "datasheet_chunk"
                   and (key == ref or row.get("source_ref") == ref)]
        if not matches:
            if ref.startswith(("evidence:", "pdf:", "manufacturer_page:", "manufacturer_image:")):
                unresolved.append({"requested_ref": ref, "reason": "source_index_missing"})
            continue
        if len(matches) != 1:
            unresolved.append({"requested_ref": ref, "reason": "source_index_ambiguous"})
            continue
        key, indexed = matches[0]
        owner = indexed.get("device_id")
        candidates = [(part, row) for part, record in knowledge.items() if isinstance(record, Mapping)
                      for row in chunks(record) if isinstance(row, Mapping)
                      and (row.get("id") == indexed.get("id") or row.get("source_ref") == indexed.get("source_ref"))]
        reason = None
        if len(candidates) != 1:
            reason = "prepared_chunk_missing" if not candidates else "prepared_chunk_ambiguous"
        else:
            part, chunk = candidates[0]
            if (not isinstance(owner, str) or owner != part or chunk.get("lichuang_id") != owner
                    or indexed.get("id") is None or key != f"evidence:{indexed['id']}"
                    or any(not _same_json(chunk.get(field), indexed.get(field)) for field in
                           ("id", "source_ref", "document_sha256", "page_idx"))):
                reason = "source_identity_mismatch"
            elif not isinstance(chunk.get("content"), str) or not chunk["content"].strip():
                reason = "source_body_missing"
            elif owner in own_parts:
                continue
            elif key in payload.get("source_index", {}) and not _same_json(payload["source_index"][key], indexed):
                reason = "stage_source_index_mismatch"
            else:
                bundle = source_bundles.get(owner, {})
                bundle = bundle if isinstance(bundle, Mapping) else {}
                originals = [row for row in chunks(bundle) if isinstance(row, Mapping)
                             and row.get("source_ref") == chunk.get("source_ref")]
                if bundle.get("supplier_part") != owner or len(originals) != 1:
                    reason = "original_source_missing_or_ambiguous"
                elif any(originals[0].get(field) != chunk.get(field) for field in
                         ("id", "source_ref", "lichuang_id", "document_sha256", "page_idx", "content", "provenance", "source_kind")):
                    reason = "prepared_source_changed"
                else:
                    # Empty fields asks only for attribution, never expands the
                    # original claim scope or treats body delivery as a fact.
                    quality = originals[0].get("source_quality") or {}
                    hashes = [originals[0].get("content_sha256")]
                    if isinstance(quality, Mapping):
                        hashes.append(quality.get("transcript_sha256"))
                    content_sha = sha256(chunk["content"].encode()).hexdigest()
                    try:
                        attribution = (authenticate_source_quote(bundle, originals[0], chunk["content"], fields=[])
                            if all(value == content_sha for value in hashes if value is not None)
                            else None)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        attribution = None
                    if attribution is None:
                        reason = "original_source_unverified"
        if reason is not None:
            unresolved.append({"requested_ref": ref, "source_index_key": key, "source_owner": owner,
                               "reason": reason})
            continue
        if key not in entries:
            entries[key] = {"source_index_key": key, "source_owner": owner, "requested_refs": [],
                "evidence": deepcopy(chunk), "content_sha256": sha256(chunk["content"].encode()).hexdigest(),
                "source_attribution": {name: deepcopy(value) for name, value in attribution.items() if name != "quote"}}
            needed_keys[key] = deepcopy(indexed)
        entries[key]["requested_refs"].append(ref)
    if not entries and not unresolved:
        return dict(payload)
    result = deepcopy(dict(payload))
    result["referenced_source_context"] = {
        "schema": "pcbagent.stage_referenced_source_context.v1",
        "authority": load_prompt('presentation_referenced_source_authority.md'),
        "entries": list(entries.values()), "unresolved": unresolved,
        "coverage": {"included_body_count": len(entries), "unresolved_reference_count": len(unresolved),
                     "status": "partial" if entries and unresolved else "unresolved" if unresolved else "complete"},
        "electrical_authentication_granted": False}
    if needed_keys:
        # Reuse original keys verbatim. Never manufacture a page or source alias.
        result["source_index"] = {**deepcopy(dict(payload.get("source_index", {}))), **needed_keys}
    return result

