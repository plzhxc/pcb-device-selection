"""Read original source evidence without promoting interpreted RAG to facts."""
from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import json
from typing import Any


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def read_source_documents(service: Any, supplier_part: str) -> list[dict[str, Any]]:
    """Verify SQL source bytes and expose text/metadata, never case connections.

    Binary images remain in SQL. Their digest and review status are sufficient
    to bind reviewed transcriptions; callers cannot mistake image bytes for PDF.
    """
    from .preparation import IDENTITY
    if not IDENTITY.fullmatch(supplier_part):
        raise ValueError("valid identity required")
    cn = service._mysql("datasheet_assets")
    try:
        cn.start_transaction(readonly=True)
        cur = cn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_source_documents'")
        if not cur.fetchone()["n"]:
            return []
        cur.execute("SELECT source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json FROM datasheet_source_documents WHERE lichuang_id=%s ORDER BY source_sha256", (supplier_part,))
        rows = cur.fetchall()
    finally:
        cn.rollback()
        cn.close()
    image_review_parents = set()
    for stored in rows:
        if stored["source_mime"] == "application/json":
            record = json.loads(bytes(stored["source_bytes"]))
            if record.get("schema") == "pcbagent.manufacturer_image_field_review.v1":
                image_review_parents.add(record.get("parent_html_sha256"))
    result = []
    for row in rows:
        raw = bytes(row.pop("source_bytes"))
        if hashlib.sha256(raw).hexdigest() != row["source_sha256"]:
            raise ValueError("stored source hash mismatch")
        quality = row.pop("quality_json")
        row["quality"] = json.loads(quality) if isinstance(quality, str) else quality
        row["source_verified"] = True
        if row["source_mime"] == "text/html":
            parser = _VisibleText()
            parser.feed(raw.decode("utf-8", errors="replace"))
            row["text"] = "\n".join(parser.parts)
            row["locator"] = "visible_text"
            if row["quality"].get("document_role") == "manufacturer_datasheet_revision" or row["source_sha256"] in image_review_parents:
                # Exact manufacturer-page href bytes bind revision applicability.
                row["original_html_text"] = raw.decode("utf-8")
        elif row["source_mime"] == "application/json":
            record = json.loads(raw)
            if record.get("schema") in {"pcbagent.identity_evidence.v1", "pcbagent.model_display_alias.v1", "pcbagent.connection_free_device_inventory.v1", "pcbagent.pdf_page_review.v1", "pcbagent.local_symbol_mapping.v1", "pcbagent.cad_terminal_position_source.v1", "pcbagent.manufacturer_datasheet_revision_applicability.v1", "pcbagent.manufacturer_image_field_review.v1"}:
                row["record"] = record
            if row["quality"].get("qualification") in {"public_cad_library_original_json", "public_lcsc_std_component_original_json"}:
                # Position validation rehashes the original serialized response;
                # this asset never becomes manufacturer electrical evidence.
                row["original_json_text"] = raw.decode("utf-8")
        result.append(row)
    return result


def read_primary_source_evidence(service: Any, supplier_part: str, *,
                                 pages: list[int] | None = None,
                                 query: str | None = None, limit: int = 20,
                                 include_symbol_mappings: bool = True) -> dict[str, Any]:
    """Return original PDF, raw manufacturer HTML, and reviewed image evidence.

    CAD mappings are separate, restricted to physical numbering/label claims.
    Legacy VLM chunks and unreviewed images never become primary evidence.
    """
    bundle = service.read_pdf_page_evidence(supplier_part, pages=pages, query=query, limit=limit)
    bundle["evidence"] = [row for row in bundle["evidence"] if row["content"].strip()]
    assets = read_source_documents(service, supplier_part)
    bundle["source_assets"] = [{k: v for k, v in row.items() if k != "text"} for row in assets]
    pdf_sha = (bundle.get("document") or {}).get("pdf_sha256")
    pdf_sources = [(pdf_sha, {}, "datasheet")] if pdf_sha else []
    from .secondary_pdf import secondary_source_kind
    pdf_sources.extend((asset["source_sha256"], asset["quality"], secondary_source_kind(asset["quality"]))
        for asset in assets if asset["source_mime"] == "application/pdf"
        and asset["quality"].get("qualification") == "manufacturer_secondary_pdf")
    if assets:
        from .pdf_review import read_pdf_page_reviews
        asset_hashes = {asset["source_sha256"] for asset in assets}
        reviews = [(review, sha, quality, kind) for sha, quality, kind in pdf_sources
                   for review in read_pdf_page_reviews(service, supplier_part, sha)]
        for review, reviewed_pdf_sha, parent_quality, source_kind in reviews:
            page = review["page"]
            if pages is not None and page not in pages:
                continue
            if review["rendered_image_sha256"] not in asset_hashes:
                raise ValueError("reviewed rendering source unavailable")
            source_ref = f"pdf:{reviewed_pdf_sha}:page:{page}:review:{review['receipt_sha256']}"
            bundle["evidence"].append({"id": f"pdf_review:{review['receipt_sha256']}",
                "source_ref": source_ref, "lichuang_id": supplier_part,
                "title": f"Visually reviewed original PDF page {page}",
                "content": review["transcript"], "content_type": "text",
                "source_kind": "related_document" if source_kind == "datasheet" and str((bundle.get("source_quality") or {}).get("original_source_kind", "")).startswith("related_") else source_kind,
                "document_sha256": reviewed_pdf_sha, "page_idx": page - 1,
                "locator": f"page:{page}",
                "provenance": "reviewed_original_pdf_transcription",
                "source_quality": {**parent_quality, "qualification": "reviewed_pdf_page_transcription",
                    "review_receipt_sha256": review["receipt_sha256"],
                    "rendered_image_sha256": review["rendered_image_sha256"],
                    "transcript_sha256": review["transcript_sha256"],
                    "reviewed_claim_scopes": review["reviewed_claim_scopes"],
                    "reviewer": review["reviewer"], "review_method": review["review_method"]}})
    if any(kind in {"manufacturer_application_note", "manufacturer_ordering_notice"} for _, _, kind in pdf_sources):
        bundle["evidence"].extend(service.read_secondary_pdf_evidence(supplier_part, pages=pages, query=query, limit=limit))
    original_quality = bundle.get("stored_source_quality") or bundle.get("source_quality") or {}
    reviewed = set(original_quality.get("reviewed_image_hashes", []))
    for asset in assets:
        if not asset["is_primary"] or asset["source_mime"] != "text/html":
            continue
        sha = asset["source_sha256"]
        source_ref = f"manufacturer_page:{sha}:visible_text"
        bundle["evidence"].append({"id": source_ref, "source_ref": source_ref,
            "lichuang_id": supplier_part, "content": asset["text"],
            "title": "Original manufacturer product page", "source_kind": "manufacturer_page",
            "source_url": asset["source_url"], "document_sha256": sha,
            "locator": "visible_text", "content_type": "text", "page_idx": None,
            "provenance": "original_manufacturer_html", "source_quality": asset["quality"]})
    if reviewed:
        rows = service.knowledge(supplier_part, evidence_limit=200)["evidence"]
        image_assets = {asset["source_sha256"]: asset for asset in assets
                        if asset["source_mime"].startswith("image/")}
        for row in rows:
            sha = row.get("image_id")
            if sha not in reviewed or sha not in image_assets:
                continue
            locator = original_quality.get("reviewed_pinout_locator") or "reviewed_image"
            source_ref = f"manufacturer_image:{sha}:{locator}"
            bundle["evidence"].append({**row, "id": source_ref, "source_ref": source_ref,
                "document_sha256": sha, "page_idx": None, "source_kind": "manufacturer_image",
                "source_url": image_assets[sha]["source_url"], "locator": locator,
                "provenance": "reviewed_original_manufacturer_image",
                "source_quality": {"qualification": "reviewed_image_transcription",
                    "review_method": original_quality.get("review_method"),
                    "reviewed_image_sha256": sha, "claim_scope": "pin_number_and_label_only"}})
    from .manufacturer_image_review import image_review_evidence
    bundle["evidence"].extend(image_review_evidence(bundle))
    bundle["symbol_mappings"] = service.read_symbol_mappings(supplier_part) if include_symbol_mappings else []
    bundle["source_scope"] = "catalog_and_original_sources_with_separate_cad_numbering"
    return bundle


__all__ = ["read_source_documents", "read_primary_source_evidence"]
