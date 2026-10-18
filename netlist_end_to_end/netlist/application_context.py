"""Display explicitly reviewed application text without creating pin facts.

Source preparation registers ordinary original-PDF review receipts with the
presentation-only ``application_context`` scope. This module has no write or
retrieval API and does not interpret prose, select a circuit, or award proof.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .prompts import load_prompt

from datasheet_automation.knowledge.evidence import authenticate_source_quote, primary_document_sha256
from datasheet_automation.knowledge.terminal_inventory import _authenticate_reviewed_receipt

SCOPE = "application_context"
SECONDARY_SCOPE = "application_circuit"
AUTHORITY = (
    load_prompt('application_authority.md')
)


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _selected(scopes: Any, scope: str = SCOPE) -> bool:
    return scope in scopes if isinstance(scopes, (list, tuple, set)) else scopes == scope


def _receipt_sources(record: Mapping) -> list[dict]:
    # Selected revisions currently grant only identity/terminal-role purposes.
    # Merely retaining an old primary receipt cannot activate its application.
    if record.get("source_version_selection") is not None:
        return []
    primary = primary_document_sha256(record)
    part = record.get("supplier_part")
    result = []
    for asset in record.get("source_assets", []):
        receipt = _map(_map(asset).get("record"))
        if receipt.get("schema") != "pcbagent.pdf_page_review.v1":
            continue
        document = receipt.get("pdf_sha256")
        scope, kind = SCOPE, "datasheet"
        if document != primary:
            parents = [a for a in record.get("source_assets", []) if _map(a).get("source_sha256") == document]
            parent = _map(parents[0]) if len(parents) == 1 else {}
            quality = _map(parent.get("quality"))
            applicability = _map(quality.get("applicability"))
            if (parent.get("source_verified") is not True or parent.get("source_mime") != "application/pdf"
                    or parent.get("is_primary") is True or quality.get("qualification") != "manufacturer_secondary_pdf"
                    or quality.get("supplier_part") != part
                    or quality.get("document_role") not in {"application_note", "evaluation_board_manual", "hardware_design_guide"}
                    or quality.get("source_kind", "manufacturer_application_note") != "manufacturer_application_note"
                    or not applicability.get("reviewer") or not applicability.get("model") or not applicability.get("support")):
                continue
            scope, kind = SECONDARY_SCOPE, "manufacturer_application_note"
        if not _selected(receipt.get("reviewed_claim_scopes"), scope):
            continue
        sha, page = _map(asset).get("source_sha256"), receipt.get("page")
        ref = f"pdf:{document}:page:{page}:review:{sha}"
        quality = {"qualification": "reviewed_pdf_page_transcription",
            "review_receipt_sha256": sha, **{key: receipt.get(key) for key in (
                "rendered_image_sha256", "transcript_sha256", "reviewed_claim_scopes", "reviewer", "review_method")}}
        chunk = {"source_ref": ref, "document_sha256": document, "lichuang_id": part,
            "page_idx": page - 1 if type(page) is int else None,
            "source_kind": kind, "provenance": "reviewed_original_pdf_transcription",
            "content": receipt.get("transcript"), "source_quality": quality}
        proof = {"document_sha256": document, "page": page, "source_ref": ref,
            "review_receipt_sha256": sha, "rendered_image_sha256": quality["rendered_image_sha256"],
            "reviewed_claim_scopes": quality["reviewed_claim_scopes"]}
        # Reuse full existing receipt/asset checks, not a caller's claimed scope.
        _authenticate_reviewed_receipt(chunk, proof, record, required_scopes={scope})
        source_bundle = {**record, "evidence": [chunk]}
        if authenticate_source_quote(source_bundle, chunk, chunk["content"], fields=[scope]) is None:
            raise ValueError("application context requires an applicable original manufacturer source")
        result.append({"source_ref": ref, "source_supplier_part": part, "page": page,
            "document_sha256": document, "review_receipt_sha256": sha, "selection_scope": scope})
    refs = [row["source_ref"] for row in result]
    if len(refs) != len(set(refs)):
        raise ValueError("application review receipts must be unique")
    return sorted(result, key=lambda row: (row["document_sha256"], row["page"], row["source_ref"]))


def application_source_requirements(record: Mapping) -> list[dict]:
    """Locate only exact existing reviews; the normal collector fetches pages."""
    return _receipt_sources(record)


def project_application_context(record: Mapping, evidence: list[Mapping]) -> dict | None:
    """Reauthenticate full bodies and reference their existing displayed chunks.

    No text is truncated, paraphrased, or added here. Invalid explicitly tagged
    source preparation is rejected; untouched legacy records return None.
    """
    if record.get("source_version_selection") is not None:
        return None
    expected = application_source_requirements(record)
    advertised = {row.get("source_ref") for row in record.get("evidence", []) if isinstance(row, Mapping)
                  and (_selected(_map(row.get("source_quality")).get("reviewed_claim_scopes"))
                       and row.get("document_sha256") == primary_document_sha256(record)
                       or row.get("source_kind") == "manufacturer_application_note"
                       and row.get("provenance") == "reviewed_original_pdf_transcription"
                       and _selected(_map(row.get("source_quality")).get("reviewed_claim_scopes"), SECONDARY_SCOPE))}
    if advertised - {row["source_ref"] for row in expected}:
        raise ValueError("application scope lacks its unchanged reviewed source receipt")
    if not expected:
        return None
    sources = []
    for requirement in expected:
        ref = requirement["source_ref"]
        original = [row for row in record.get("evidence", []) if _map(row).get("source_ref") == ref]
        shown = [row for row in evidence if _map(row).get("source_ref") == ref]
        if len(original) != 1 or len(shown) != 1:
            raise ValueError("each application context requires its exact complete original source chunk")
        row, display = original[0], shown[0]
        quality = _map(row.get("source_quality"))
        proof = {**requirement, "rendered_image_sha256": quality.get("rendered_image_sha256"),
                 "reviewed_claim_scopes": quality.get("reviewed_claim_scopes")}
        _authenticate_reviewed_receipt(row, proof, record, required_scopes={requirement["selection_scope"]})
        checked = authenticate_source_quote(record, row, row["content"], fields=[requirement["selection_scope"]])
        if (checked is None or checked.get("source_kind") not in {"datasheet", "manufacturer_application_note"}
                or display.get("content") != row["content"] or display.get("id") is None
                or display.get("lichuang_id") != record.get("supplier_part")
                or display.get("document_sha256") != row["document_sha256"]
                or display.get("page_idx") != row.get("page_idx")):
            raise ValueError("application source content, owner, document, page and display alias must agree")
        sources.append({"source_refs": [f"evidence:{display['id']}"], "original_source_ref": ref,
            "supplier_part": record.get("supplier_part"),
            "selection_scope": requirement["selection_scope"],
            "provenance": {key: deepcopy(value) for key, value in checked.items() if key != "quote"},
            "transcript_sha256": quality["transcript_sha256"],
            "character_count": len(row["content"]), "body_location": "complete matching source chunk content"})
    return {"schema": "pcbagent.application_source_context.v1", "authority": AUTHORITY,
        "sources": sources, "source_count": len(sources),
        "character_count": sum(row["character_count"] for row in sources),
        "electrical_authentication_granted": False}


__all__ = ["SCOPE", "AUTHORITY", "application_source_requirements", "project_application_context"]
