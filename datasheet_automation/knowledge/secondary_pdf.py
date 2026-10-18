"""Independent, applicability-reviewed manufacturer PDF source registration."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Any

INTRINSIC_POLICY_REVIEW_SCHEMA = "pcbagent.intrinsic_connection_policy_page_review.v1"
INTRINSIC_POLICY_VALUES = {
    "output_only_open_allowed": {"allow_nc": True, "required_connection": False},
    "input_only_must_not_float": {"allow_nc": False, "required_connection": True},
}
_POLICY_CLAUSES = {
    "output_only_open_allowed": r"Unused output-only pins? (?:can|may) be left (?:unterminated|unconnected|open)(?: safely)?\.",
    "input_only_must_not_float": r"Unused input(?:-only)? pins?(?: on all logic devices?)? must(?: always)? be connected to VCC or GND\.",
}


def intrinsic_policy_reviews(quality: Mapping, bundle: Mapping, source_sha: str) -> list[dict]:
    """Validate explicit guide-page policies against this exact source owner.

    This is a narrow reviewed intrinsic-policy vocabulary, not an inference from
    family names or a waiver of genuinely mode-dependent source conditions.
    Native document-wide scopes never acquire these connection-policy fields.
    """
    rows = quality.get("connection_policy_reviews")
    if rows is None:
        return []
    from .manufacturer_image_review import _identity_digest
    from .evidence import normalize_source_text, primary_document_sha256
    device, pins = bundle.get("device") or {}, bundle.get("pins")
    part = bundle.get("supplier_part")
    if (quality.get("qualification") != "manufacturer_secondary_pdf"
            or quality.get("document_role") != "hardware_design_guide"
            or quality.get("source_kind") != "manufacturer_application_note"
            or quality.get("supplier_part") != part
            or not isinstance(rows, list) or not 1 <= len(rows) <= 32
            or set(quality.get("reviewed_claim_scopes", [])) & {"allow_nc", "required_connection"}
            or device.get("supplier_part") != part or not isinstance(device.get("device_name"), str) or not device["device_name"].strip()
            or not isinstance(pins, list) or type(device.get("pin_count")) is not int
            or device["pin_count"] != len(pins) or not pins
            or quality.get("applicability", {}).get("model") != device["device_name"]):
        raise ValueError("intrinsic guide policies require the exact current owner/model/catalog and bounded page reviews")
    catalog_sha = _identity_digest(bundle)
    numbers = {p.get("pin_number") for p in pins}
    expected = {"schema", "supplier_part", "model", "primary_pdf_sha256", "catalog_identity_sha256",
                "source_pdf_sha256", "page", "pin_numbers", "intrinsic_policy", "fields", "quote", "reviewer", "review_sha256"}
    seen, result = set(), []
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("closed intrinsic policy page review required")
        body = {k: v for k, v in row.items() if k != "review_sha256"}
        sha = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        policy, fields, targets, page = row["intrinsic_policy"], row["fields"], row["pin_numbers"], row["page"]
        if (row["schema"] != INTRINSIC_POLICY_REVIEW_SCHEMA or row["review_sha256"] != sha
                or row["supplier_part"] != part or row["model"] != device["device_name"]
                or row["primary_pdf_sha256"] != primary_document_sha256(bundle)
                or not re.fullmatch(r"[0-9a-f]{64}", str(row["primary_pdf_sha256"]))
                or row["catalog_identity_sha256"] != catalog_sha or row["source_pdf_sha256"] != source_sha
                or type(page) is not int or not 1 <= page <= quality.get("page_count", 0)
                or not isinstance(targets, list) or not targets or len(targets) > 256
                or not all(isinstance(p, str) and p in numbers for p in targets) or len(set(targets)) != len(targets)
                or not isinstance(policy, str) or policy not in INTRINSIC_POLICY_VALUES or (page, policy) in seen
                or not isinstance(fields, dict) or not fields or not set(fields) <= set(INTRINSIC_POLICY_VALUES[policy])
                or any(type(value) is not bool or value is not INTRINSIC_POLICY_VALUES[policy][field] for field, value in fields.items())
                or not isinstance(row["reviewer"], str) or not row["reviewer"].strip()
                or not isinstance(row["quote"], str)
                or not re.fullmatch(_POLICY_CLAUSES[policy], normalize_source_text(row["quote"]), re.I)):
            raise ValueError("intrinsic policy review identity, page, values or complete unconditional clause differs")
        seen.add((page, policy)); result.append(dict(row))
    return result


def secondary_review_scopes(quality: Mapping, bundle: Mapping, source_sha: str, page: int) -> tuple[set, list]:
    """Extra policy fields are available only to an exact page-review receipt."""
    policies = [r for r in intrinsic_policy_reviews(quality, bundle, source_sha) if r["page"] == page]
    scopes = set(quality.get("reviewed_claim_scopes", []))
    for row in policies:
        scopes.update(row["fields"])
    return scopes, policies

SECONDARY_CHUNK_DDL = """CREATE TABLE IF NOT EXISTS datasheet_secondary_chunks (
 lichuang_id VARCHAR(64) NOT NULL,
 pdf_sha256 CHAR(64) NOT NULL,
 chunk_idx INT NOT NULL,
 milvus_chunk_id BIGINT NOT NULL,
 page_number INT NOT NULL,
 content LONGTEXT NOT NULL,
 quality_json JSON NOT NULL,
 PRIMARY KEY (lichuang_id,pdf_sha256,chunk_idx),
 UNIQUE KEY secondary_milvus_id (milvus_chunk_id),
 FULLTEXT KEY secondary_text_search (content)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def secondary_source_kind(quality: dict[str, Any]) -> str:
    """Preserve the exact purpose of a secondary PDF and its field boundary."""
    role = quality.get("document_role")
    kind = quality.get("source_kind", "manufacturer_application_note")
    if role == "manufacturer_datasheet_revision" or kind == "manufacturer_datasheet_revision":
        if (role != kind or quality.get("qualification") != "manufacturer_datasheet_revision_reviewed"
                or not quality.get("applicability_receipt_sha256")
                or quality.get("claim_scope") != "explicitly_reviewed_fields"):
            raise ValueError("revision requires independently reviewed applicability")
        return kind
    if role == "manufacturer_ordering_notice" or kind == "manufacturer_ordering_notice":
        if (role != "manufacturer_ordering_notice" or kind != "manufacturer_ordering_notice"
                or quality.get("claim_scope") != "explicitly_reviewed_fields"
                or quality.get("reviewed_claim_scopes") != ["device_name"]):
            raise ValueError("ordering notices can authorize only exact device_name")
        return "manufacturer_ordering_notice"
    if role not in {"application_note", "evaluation_board_manual", "hardware_design_guide"} or kind != "manufacturer_application_note":
        raise ValueError("unknown secondary manufacturer document role")
    return kind


def pdf_text_pages(raw: bytes) -> list[str]:
    import pypdfium2 as pdfium
    from .native_pdf import _PDFIUM_LOCK
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(raw)
        try:
            result = []
            for index in range(len(doc)):
                page = doc[index]; text = page.get_textpage()
                try: result.append(text.get_text_range().strip())
                finally: text.close(); page.close()
            return result
        finally: doc.close()


def read_secondary_pdf(service: Any, supplier_part: str, sha: str, *, source_selection: Any = None) -> dict[str, Any]:
    from .preparation import IDENTITY
    if not IDENTITY.fullmatch(supplier_part) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("valid source identity and SHA required")
    if source_selection is not None:
        from .datasheet_revision import read_revision_pdf
        return read_revision_pdf(service, supplier_part, sha, source_selection=source_selection)
    cn = service._mysql("datasheet_assets")
    try:
        cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
        cur.execute("SELECT source_bytes,source_url,quality_json FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s AND source_mime='application/pdf' AND is_primary=FALSE", (supplier_part, sha))
        row = cur.fetchone()
    finally: cn.rollback(); cn.close()
    if not row or hashlib.sha256(bytes(row["source_bytes"])).hexdigest() != sha:
        raise ValueError("verified secondary PDF unavailable")
    quality = row.pop("quality_json")
    row["quality"] = json.loads(quality) if isinstance(quality, str) else quality
    if (row["quality"].get("qualification") != "manufacturer_secondary_pdf"
            or row["quality"].get("supplier_part") != supplier_part
            or not row["quality"].get("applicability", {}).get("reviewer")):
        raise ValueError("secondary PDF applicability review unavailable")
    secondary_source_kind(row["quality"])
    row["source_bytes"] = bytes(row["source_bytes"])
    return row


def register_secondary_pdf(service: Any, supplier_part: str, *, pdf_path: str | Path,
                           source_url: str, document_role: str,
                           applicability: dict[str, Any],
                           reviewed_claim_scopes: list[str],
                           connection_policy_reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Persist original bytes and bounded applicability, then index native text.

    Caller must independently inspect the source. Literal applicability quotes
    are checked against their original pages; this is not automatic approval of
    every fact or of diagrams in the registered manual.
    """
    from .preparation import IDENTITY
    from .web_source import SOURCE_DDL
    if not IDENTITY.fullmatch(supplier_part) or not source_url.startswith("https://"):
        raise ValueError("valid identity and HTTPS source URL required")
    if document_role not in {"application_note", "evaluation_board_manual", "hardware_design_guide", "manufacturer_ordering_notice"}:
        raise ValueError("explicit manufacturer document role required")
    if not applicability.get("reviewer") or not applicability.get("model") or not applicability.get("support"):
        raise ValueError("reviewed model applicability with original page support required")
    ordering_notice = document_role == "manufacturer_ordering_notice"
    allowed = ({"device_name"} if ordering_notice else
        {"application_interface_profile", "voltage_domain", "connection_relation", "application_circuit", "passive_value"})
    if not reviewed_claim_scopes or not set(reviewed_claim_scopes) <= allowed:
        raise ValueError("secondary source scopes cannot grant IC pin identity or electrical ratings")
    raw = Path(pdf_path).read_bytes()
    if not raw.startswith(b"%PDF") or len(raw) > 60 * 1024 * 1024:
        raise ValueError("invalid or oversized PDF")
    pages = pdf_text_pages(raw); sha = hashlib.sha256(raw).hexdigest()
    for support in applicability["support"]:
        page, quote = support.get("page"), support.get("quote")
        if type(page) is not int or not 1 <= page <= len(pages) or not isinstance(quote, str) or not quote.strip():
            raise ValueError("valid applicability quote and page required")
        if " ".join(quote.split()) not in " ".join(pages[page - 1].split()):
            raise ValueError("applicability quote absent from original PDF page")
    if ordering_notice and not any(" ".join(str(applicability["model"]).split()) in " ".join(support["quote"].split()) for support in applicability["support"]):
        raise ValueError("ordering notice must literally identify its exact applicable model")
    quality = {"qualification": "manufacturer_secondary_pdf", "supplier_part": supplier_part,
        "source_kind": "manufacturer_ordering_notice" if ordering_notice else "manufacturer_application_note", "document_role": document_role,
        "applicability": applicability, "claim_scope": "explicitly_reviewed_fields",
        "reviewed_claim_scopes": sorted(set(reviewed_claim_scopes)), "page_count": len(pages),
        "native_text_only": True, "unreviewed_visual_content_is_not_fact": True}
    if connection_policy_reviews is not None:
        from .evidence import normalize_source_text
        from validation_platform.conditional_pin_rules import source_clause
        quality["connection_policy_reviews"] = connection_policy_reviews
        current = service.read_primary_source_evidence(supplier_part, pages=[1], limit=1)
        for review in intrinsic_policy_reviews(quality, current, sha):
            clause = source_clause(review["quote"], pages[review["page"] - 1])
            if clause is None or normalize_source_text(clause) != normalize_source_text(review["quote"]):
                raise ValueError("intrinsic policy requires its complete unique original-page clause, without omitted conditions")
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute(SOURCE_DDL); cur.execute(SECONDARY_CHUNK_DDL)
        cur.execute("SELECT quality_json FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s", (supplier_part, sha))
        existing = cur.fetchone()
        if existing and json.loads(existing[0]) != quality:
            raise ValueError("existing secondary source review differs; explicit version reconciliation required")
        cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,'application/pdf',%s,FALSE,%s)", (supplier_part, sha, source_url, raw, json.dumps(quality)))
        cn.commit()
    finally: cn.close()
    indexed = index_secondary_pdf(service, supplier_part, sha)
    return {"supplier_part": supplier_part, "source_sha256": sha, "source_url": source_url,
            "source_mime": "application/pdf", "source_verified": True, "quality": quality, "index": indexed}


def index_secondary_pdf(service: Any, supplier_part: str, sha: str, *, source_selection: Any = None) -> dict[str, Any]:
    from .store import _split_text, _now
    source = read_secondary_pdf(service, supplier_part, sha, source_selection=source_selection)
    chunks = [(page + 1, chunk) for page, text in enumerate(pdf_text_pages(source["source_bytes"])) if text for chunk in _split_text(text)]
    marker = "secondary_pdf:" + sha
    with service.preparation_lock(supplier_part):
        cn = service._mysql("datasheet_assets"); mc = service._milvus(); inserted_ids = []
        try:
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT product_name,pdf_sha256,chunk_count FROM datasheet_documents WHERE lichuang_id=%s FOR UPDATE", (supplier_part,))
            document = cur.fetchone()
            if not document: raise ValueError("primary document metadata required before adding secondary source")
            filt = "lichuang_id == " + json.dumps(supplier_part) + " and source_run == " + json.dumps(marker)
            existing = mc.query(collection_name=service.config.milvus_collection, filter=filt, output_fields=["id", "content", "page_idx", "chunk_idx", "chunk_total"], limit=10000, consistency_level="Strong")
            if existing:
                existing.sort(key=lambda row: row["chunk_idx"])
                if len(existing) != len(chunks) or any(row["content"] != chunks[i][1] or row["page_idx"] != chunks[i][0] - 1 or row["chunk_idx"] != i or row["chunk_total"] != len(chunks) for i, row in enumerate(existing)):
                    raise ValueError("existing secondary chunks differ")
                ids = [row["id"] for row in existing]
            elif chunks:
                rows = [{"lichuang_id": supplier_part, "product_name": document["product_name"], "content_type": "text", "category": secondary_source_kind(source["quality"]), "title": f"Secondary manufacturer PDF page {page}", "content": text, "section_path": marker, "page_idx": page - 1, "parent_entry_idx": -1, "chunk_idx": i, "chunk_total": len(chunks), "image_id": "", "img_path": "", "source_run": marker, "ingested_at": _now()} for i, (page, text) in enumerate(chunks)]
                ids = inserted_ids = list(mc.insert(collection_name=service.config.milvus_collection, data=rows).get("ids", []))
                if len(ids) != len(chunks): raise RuntimeError("secondary chunk insert incomplete")
            else: ids = []
            for i, (page, text) in enumerate(chunks):
                cur.execute("INSERT INTO datasheet_secondary_chunks (lichuang_id,pdf_sha256,chunk_idx,milvus_chunk_id,page_number,content,quality_json) VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE milvus_chunk_id=VALUES(milvus_chunk_id)", (supplier_part, sha, i, ids[i], page, text, json.dumps(source["quality"])))
            actual = mc.query(collection_name=service.config.milvus_collection, filter="lichuang_id == " + json.dumps(supplier_part), output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"]
            if actual < document["chunk_count"] + len(inserted_ids): raise RuntimeError("secondary index did not preserve existing chunks")
            cur.execute("UPDATE datasheet_documents SET chunk_count=%s WHERE lichuang_id=%s AND pdf_sha256=%s", (actual, supplier_part, document["pdf_sha256"]))
            cn.commit()
            return {"status": "indexed" if inserted_ids else "existing", "chunk_count": len(chunks), "device_total_chunks": actual}
        except BaseException:
            cn.rollback()
            if inserted_ids: mc.delete(collection_name=service.config.milvus_collection, ids=inserted_ids)
            raise
        finally: mc.close(); cn.close()


def read_secondary_pdf_evidence(service: Any, supplier_part: str, *, pdf_sha256: str | None = None,
                                pages: list[int] | None = None, query: str | None = None,
                                limit: int = 200) -> list[dict[str, Any]]:
    from .source_evidence import read_source_documents
    if type(limit) is not int or not 1 <= limit <= 1000 or (pages is not None and any(type(p) is not int or p < 1 for p in pages)):
        raise ValueError("bounded limit and one-based pages required")
    evidence = []
    for asset in read_source_documents(service, supplier_part):
        sha = asset["source_sha256"]
        if asset["source_mime"] != "application/pdf" or (pdf_sha256 is not None and sha != pdf_sha256): continue
        if asset["quality"].get("qualification") != "manufacturer_secondary_pdf": continue
        source = read_secondary_pdf(service, supplier_part, sha)
        for index, text in enumerate(pdf_text_pages(source["source_bytes"])):
            page = index + 1
            if not text or (pages is not None and page not in pages): continue
            source_ref = f"pdf:{sha}:page:{page}"
            evidence.append({"id": source_ref, "source_ref": source_ref, "lichuang_id": supplier_part,
                "document_sha256": sha, "source_url": source["source_url"], "page_idx": index,
                "locator": f"page:{page}", "content": text, "content_type": "text",
                "title": f"Manufacturer secondary PDF page {page}", "source_kind": secondary_source_kind(source["quality"]),
                "provenance": "original_secondary_pdf_native", "source_quality": source["quality"]})
    if query:
        terms = query.lower().split()
        evidence.sort(key=lambda row: -sum(row["content"].lower().count(term) for term in terms))
    return evidence[:limit]


def decorate_secondary_chunks(service: Any, supplier_part: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach each additional document's own hash and scopes to RAG hits."""
    ids = [row["id"] for row in rows if type(row.get("id")) is int]
    if not ids: return rows
    cn = service._mysql("datasheet_assets")
    try:
        cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_secondary_chunks'")
        if not cur.fetchone()["n"]: return rows
        cur.execute("SELECT milvus_chunk_id,pdf_sha256,page_number,content,quality_json FROM datasheet_secondary_chunks WHERE lichuang_id=%s AND milvus_chunk_id IN (" + ",".join(["%s"] * len(ids)) + ")", (supplier_part, *ids))
        matches = {row["milvus_chunk_id"]: row for row in cur.fetchall()}
    finally: cn.rollback(); cn.close()
    for row in rows:
        item = matches.get(row.get("id"))
        if not item: continue
        if row["content"] != item["content"]: raise ValueError("secondary SQL and Milvus text differ")
        quality = item["quality_json"]
        quality = json.loads(quality) if isinstance(quality, str) else quality
        sha, page = item["pdf_sha256"], item["page_number"]
        row.update(document_sha256=sha, page_idx=page - 1, locator=f"page:{page}",
            source_ref=f"pdf:{sha}:page:{page}", source_kind=secondary_source_kind(quality),
            provenance="original_secondary_pdf_native", source_quality=quality)
    return rows


__all__ = ["register_secondary_pdf", "read_secondary_pdf_evidence", "read_secondary_pdf", "index_secondary_pdf", "decorate_secondary_chunks", "SECONDARY_CHUNK_DDL"]
