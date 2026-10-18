"""Select whole primary source pages before supplementary semantic retrieval.

Text matches locate evidence; they do not certify a package column, pin mapping,
electrical role, or complete PDF coverage. Existing source authentication remains
the only provenance gate.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re
from typing import Any

from datasheet_automation.knowledge.evidence import primary_document_sha256, primary_source_registry

_PIN_TABLE = re.compile(r"pin\s+(?:description|configuration|function|definition|assignment|overview)|"
    r"functional\s+pin|(?:引脚|管脚|管引脚).{0,5}(?:描述|定义|说明|配置|功能)|"
    r"(?:pin|no\.?|number)\s+(?:name|function)", re.I)
_IDENTITY = re.compile(r"ordering\s+(?:information|code)|package\s*/\s*ordering|"
    r"series\s+comparison|订货信息|订购信息|型号说明|产品命名|型号对照", re.I)
_CONTENTS = re.compile(r"table\s+of\s+contents|^\s*contents\s*$|目\s*录", re.I | re.M)
_DISCLAIMER = re.compile(r"disclaimer|terms\s+of\s+use|attention|免责声明|法律声明", re.I)


def _pin_mentions(text: str, pins: list[Mapping]) -> list[str]:
    found = []
    for pin in pins:
        name = str(pin.get("pin_name") or "").strip()
        if name and re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", text, re.I):
            found.append(str(pin["pin_number"]))
    return found


def _merge_sources(primary: Mapping, additional: Mapping | None) -> dict:
    part, sha = primary.get("supplier_part"), primary_document_sha256(primary)
    result = deepcopy(dict(primary))
    refs = {row.get("source_ref"): row for row in result.get("evidence", []) if row.get("source_ref")}
    if additional:
        if additional.get("supplier_part") != part or primary_document_sha256(additional) != sha:
            raise ValueError("role sources must retain the exact part and frozen primary document")
        for row in additional.get("evidence", []):
            ref = row.get("source_ref")
            if ref in refs and refs[ref].get("content") != row.get("content"):
                raise ValueError("conflicting original text for the same source reference")
            if ref:
                refs.setdefault(ref, deepcopy(row))
        assets = {asset.get("source_sha256"): asset for asset in result.get("source_assets", [])}
        for asset in additional.get("source_assets", []):
            key = asset.get("source_sha256")
            if key in assets and assets[key] != asset:
                raise ValueError("conflicting primary asset provenance")
            assets[key] = deepcopy(asset)
        result["source_assets"] = list(assets.values())
    result["evidence"] = list(refs.values())
    return result


def select_role_sources(primary: Mapping[str, Any], additional: Mapping[str, Any] | None,
                        requested_pins: list[Mapping[str, Any]], *, max_characters: int = 60_000,
                        max_excerpts: int = 32) -> tuple[dict, dict]:
    """Return complete selected excerpts plus an explicit source-coverage report."""
    if type(max_characters) is not int or max_characters < 1 or type(max_excerpts) is not int or max_excerpts < 1:
        raise ValueError("positive role evidence budgets required")
    bundle = _merge_sources(primary, additional)
    registry = primary_source_registry(bundle)
    rows = {row["source_ref"]: row for row in bundle["evidence"] if row.get("source_ref") in registry}
    primary_refs = {row.get("source_ref") for row in primary.get("evidence", [])}
    pin_matches = {ref: _pin_mentions(str(row.get("content", "")), requested_pins) for ref, row in rows.items()}
    pin_tables = {ref for ref, row in rows.items() if ref in primary_refs
                  and _PIN_TABLE.search(str(row.get("content", ""))) and pin_matches[ref]}
    identity = {ref for ref, row in rows.items() if ref in primary_refs
                and _IDENTITY.search(str(row.get("content", "")))}
    contents = {ref for ref, row in rows.items() if ref in primary_refs
                and _CONTENTS.search(str(row.get("content", "")))}
    # Preserve immediate pages around located source tables so a heading on the
    # previous page or a continued package column is not silently discarded.
    table_pages = {(rows[ref].get("document_sha256"), rows[ref].get("page_idx")) for ref in pin_tables}
    neighbors = {ref for ref, row in rows.items() if ref in primary_refs and type(row.get("page_idx")) is int
                 and any(row.get("document_sha256") == sha and type(page) is int
                         and abs(row["page_idx"] - page) == 1 for sha, page in table_pages)}

    def rank(ref: str) -> tuple:
        row = rows[ref]
        priority = 0 if ref in pin_tables else 1 if ref in identity else 2 if ref in neighbors else 3 if ref in contents else 4
        disclaimer = bool(_DISCLAIMER.search(str(row.get("content", ""))[:500]))
        page = row.get("page_idx")
        return (priority, disclaimer, -len(pin_matches[ref]),
                row.get("document_sha256", ""), page if type(page) is int else -1, ref)

    chosen, skipped, size = [], [], 0
    for ref in sorted(rows, key=rank):
        text = str(rows[ref].get("content", ""))
        if not text.strip():
            continue
        if size + len(text) > max_characters or len(chosen) >= max_excerpts:
            skipped.append(ref)
            continue
        chosen.append(ref)
        size += len(text)
    bundle["evidence"] = [deepcopy(rows[ref]) for ref in chosen]
    covered = sorted({pin for ref in chosen for pin in pin_matches[ref]})
    expected = [str(pin["pin_number"]) for pin in requested_pins]
    main_sha = primary_document_sha256(primary)
    returned_pages = sorted({row["page_idx"] + 1 for row in primary.get("evidence", [])
                             if row.get("document_sha256") == main_sha and type(row.get("page_idx")) is int})
    metadata = {key: deepcopy(primary.get(key)) for key in ("native_pdf_page_count", "native_pdf_pages_scanned",
        "native_pdf_evidence_total_before_limit", "native_pdf_evidence_truncated")}
    scanned = metadata["native_pdf_pages_scanned"]
    count = metadata["native_pdf_page_count"]
    full_scan = (type(count) is int and isinstance(scanned, list)
                 and set(scanned) == set(range(1, count + 1)))
    source_complete = full_scan and metadata["native_pdf_evidence_truncated"] is False
    report = {"method": "primary_whole_pages_then_supplementary_retrieval", "supplier_part": primary.get("supplier_part"),
        **metadata, "returned_primary_page_numbers": returned_pages,
        "source_inventory_complete": source_complete, "selected_refs": chosen,
        "located_pin_table_refs": sorted(pin_tables), "located_identity_refs": sorted(identity),
        "located_contents_refs": sorted(contents), "included_neighbor_refs": sorted(neighbors & set(chosen)),
        "pin_name_mentions": covered, "pins_without_name_mentions": sorted(set(expected) - set(covered)),
        "selected_characters": size, "max_characters": max_characters, "max_excerpts": max_excerpts,
        "budget_omitted_refs": skipped, "priority_omitted_refs": sorted(set(skipped) & (pin_tables | identity | neighbors)),
        "unavailable_or_unauthenticated_refs": sorted({row.get("source_ref") for row in primary.get("evidence", [])
            if isinstance(row.get("source_ref"), str)} - registry.keys()),
        "limitation": "Text matching locates possible evidence only; it does not certify pin-number mapping, package columns, roles or electrical correctness."}
    return bundle, report


__all__ = ["select_role_sources"]
