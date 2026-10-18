"""Project complete cited device context for architecture planning."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .prompts import load_prompt
from .semantic_presentation import ARCHITECTURE_CONTEXT_PROFILES, architecture_device_view


def _knowledge_summary(knowledge: Mapping, *, profile: str = "legacy_full_contracts",
                       frozen_knowledge_sha256: str | None = None) -> dict:
    """Present existing interface contracts with their complete cited chunks.

    Keep conditions, unknown fields, provenance and application limitations.
    Resolve citations across the already supplied device bundles while retaining
    each chunk's actual owner. This does not retrieve, authenticate or infer a
    fact, and it does not copy a source registry as if all pages were consumed.
    Uncited original pages remain available to subsequent peripheral planning;
    their omission here is explicit rather than reported as missing knowledge.
    """
    if profile not in ARCHITECTURE_CONTEXT_PROFILES:
        raise ValueError("unknown architecture context profile")
    displayed = ({part: architecture_device_view(record, part=part,
                    frozen_knowledge_sha256=frozen_knowledge_sha256)
                  for part, record in knowledge.items()} if profile == "stage_boundary_v1" else knowledge)

    def references(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "source_registry":
                    continue
                if key == "source_ref" and isinstance(item, str):
                    found.add(item)
                elif key == "source_refs" and isinstance(item, list):
                    found.update(ref for ref in item if isinstance(ref, str))
                else:
                    found.update(references(item))
        elif isinstance(value, list):
            for item in value:
                found.update(references(item))
        return found

    by_part = {part: references({key: value for key, value in record.get("contracts", {}).items()
                                if profile != "stage_boundary_v1" or key != "terminal_role_evidence"}) |
               references(record.get("canonical_supply_symbols", {})) |
               references(knowledge[part].get("application_source_context", {}))
               for part, record in displayed.items()}
    requested = set().union(*by_part.values()) if by_part else set()
    included, available, resolved = {}, {}, set()
    for part, record in knowledge.items():
        included[part], available[part] = [], []
        for chunk in record.get("evidence", []):
            if not isinstance(chunk, Mapping):
                continue
            aliases = {str(chunk[key]) for key in ("id", "original_id", "source_ref")
                       if chunk.get(key) is not None}
            if chunk.get("id") is not None:
                aliases.add(f"evidence:{chunk['id']}")
            available[part].append({"source_ref": f"evidence:{chunk['id']}"
                                   if chunk.get("id") is not None else chunk.get("source_ref"),
                                   **{key: deepcopy(chunk.get(key)) for key in
                                      ("document_sha256", "page_idx", "title", "section_path")}})
            if aliases & requested:
                included[part].append(deepcopy(chunk))
                resolved.update(aliases & requested)
    result = {}
    for part, record in knowledge.items():
        base = deepcopy(displayed[part]) if profile == "stage_boundary_v1" else {
            "device": deepcopy(record["device"]),
            "pins": [{"pin_number": p["pin_number"], "pin_name": p.get("pin_name"),
                      **{key: deepcopy(record.get("contracts", {}).get("pins", {}).get(p["pin_number"], {}).get(key))
                         for key in ("semantic_role", "capabilities", "modes")}}
                     for p in record["pins"]],
            "contracts": deepcopy(record.get("contracts", {})),
            "canonical_supply_symbols": deepcopy(record.get("canonical_supply_symbols", {}))}
        if "application_source_context" in record:
            base["application_source_context"] = deepcopy(record["application_source_context"])
        result[part] = {**base, "interface_evidence": included[part],
            "evidence_coverage": {
                "selection": load_prompt('architecture_application_evidence_selection.md' if 'application_source_context' in record else 'architecture_evidence_selection.md'),
                "supplied_chunk_count": len(available[part]),
                "included_chunk_count": len(included[part]),
                "available_chunk_index": available[part],
                "unresolved_document_refs": sorted(ref for ref in by_part[part] - resolved
                    if ref.startswith(("evidence:", "pdf:"))),
                "remaining_sources": load_prompt('architecture_remaining_sources.md'),
                "electrical_authentication_granted": False}}
    return result


def _source_subset(index: Mapping, parts: set[str]) -> dict:
    return {key: deepcopy(value) for key, value in index.items()
            if value.get("device_id") is None or value["device_id"] in parts}

