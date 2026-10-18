"""Store explicit visual reviews of individual original PDF pages."""
from __future__ import annotations

import hashlib
import io
import json
from typing import Any

REVIEW_CHUNK_DDL = """CREATE TABLE IF NOT EXISTS datasheet_review_chunks (
 lichuang_id VARCHAR(64) NOT NULL,
 receipt_sha256 CHAR(64) NOT NULL,
 chunk_idx INT NOT NULL,
 chunk_total INT NOT NULL,
 milvus_chunk_id BIGINT NOT NULL,
 source_ref VARCHAR(255) NOT NULL,
 pdf_sha256 CHAR(64) NOT NULL,
 page_number INT NOT NULL,
 content LONGTEXT NOT NULL,
 quality_json JSON NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (lichuang_id,receipt_sha256,chunk_idx),
 UNIQUE KEY unique_review_milvus_id (milvus_chunk_id),
 FULLTEXT KEY review_text_search (content)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def _secondary_review_source(service: Any, supplier_part: str, sha: str, *,
                             source_selection: Any = None,
                             revision_identity_origin_sha256: str | None = None) -> dict:
    if revision_identity_origin_sha256 is not None:
        if source_selection is not None:
            raise ValueError("identity-only review cannot also claim a selected revision")
        from .datasheet_revision import read_revision_identity_pdf
        return read_revision_identity_pdf(service, supplier_part, sha,
            manufacturer_page_sha256=revision_identity_origin_sha256)
    from .secondary_pdf import read_secondary_pdf
    return read_secondary_pdf(service, supplier_part, sha, source_selection=source_selection)


def _secondary_scopes(service: Any, supplier_part: str, sha: str, page: int,
                      quality: dict, transcript: str, fields: list[str]) -> set:
    allowed = set(quality.get("reviewed_claim_scopes", []))
    if "connection_policy_reviews" in quality:
        from .secondary_pdf import secondary_review_scopes
        from .evidence import quote_in_source
        current = service.read_primary_source_evidence(supplier_part, pages=[1], limit=1)
        allowed, policies = secondary_review_scopes(quality, current, sha, page)
        for field in set(fields) & {"allow_nc", "required_connection"}:
            if not any(field in p["fields"] and quote_in_source(p["quote"], transcript) for p in policies):
                raise ValueError("policy page transcript must retain the exact reviewed intrinsic clause")
    return allowed


def _render(service: Any, supplier_part: str, page: int, *, source_pdf_sha256: str | None = None,
            source_selection: Any = None,
            revision_identity_origin_sha256: str | None = None) -> tuple[bytes, str]:
    from .preparation import IDENTITY
    if not IDENTITY.fullmatch(supplier_part) or type(page) is not int or page < 1:
        raise ValueError("valid identity and positive one-based page required")
    if revision_identity_origin_sha256 is not None and not source_pdf_sha256:
        raise ValueError("identity-only review requires an explicit original revision SHA")
    cn = service._mysql("datasheet_assets")
    try:
        cn.start_transaction(readonly=True)
        cur = cn.cursor()
        cur.execute("SELECT pdf_bytes,pdf_sha256 FROM datasheet_documents WHERE lichuang_id=%s", (supplier_part,))
        row = cur.fetchone()
    finally:
        cn.rollback(); cn.close()
    if revision_identity_origin_sha256 is not None or (source_pdf_sha256 and (not row or row[1] != source_pdf_sha256)):
        source = _secondary_review_source(service, supplier_part, source_pdf_sha256,
            source_selection=source_selection, revision_identity_origin_sha256=revision_identity_origin_sha256)
        raw = source["source_bytes"]; sha = source_pdf_sha256
    else:
        if not row or not row[0]:
            raise ValueError("original PDF unavailable")
        raw = bytes(row[0]); sha = hashlib.sha256(raw).hexdigest()
        if sha != row[1]:
            raise ValueError("original PDF hash mismatch")
    import pypdfium2 as pdfium
    from .native_pdf import _PDFIUM_LOCK
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(raw)
        try:
            if page > len(doc):
                raise ValueError("page exceeds original PDF")
            pdf_page = doc[page - 1]
            try:
                bitmap = pdf_page.render(scale=2)
                try:
                    stream = io.BytesIO(); bitmap.to_pil().save(stream, format="PNG")
                    return stream.getvalue(), sha
                finally: bitmap.close()
            finally: pdf_page.close()
        finally: doc.close()


def render_pdf_page(service: Any, supplier_part: str, page: int, *, source_pdf_sha256: str | None = None,
                    source_selection: Any = None,
                    revision_identity_origin_sha256: str | None = None) -> dict[str, Any]:
    from .preparation import RUNTIME_ROOT
    data, sha = _render(service, supplier_part, page, source_pdf_sha256=source_pdf_sha256,
        source_selection=source_selection, revision_identity_origin_sha256=revision_identity_origin_sha256)
    image_sha = hashlib.sha256(data).hexdigest()
    directory = RUNTIME_ROOT / "review_images" / supplier_part
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{sha}-page-{page}-{image_sha}.png"
    if path.exists() and path.read_bytes() != data:
        raise ValueError("existing review rendering differs")
    if not path.exists():
        path.write_bytes(data); path.chmod(0o600)
    return {"path": str(path), "supplier_part": supplier_part, "page": page,
            "pdf_sha256": sha, "rendered_image_sha256": image_sha, "render_scale": 2}


def register_pdf_page_review(service: Any, supplier_part: str, page: int, *,
                             transcript: str, reviewed_claim_scopes: list[str],
                             reviewer: str, expected_pdf_sha256: str,
                             expected_rendered_image_sha256: str,
                             source_pdf_sha256: str | None = None, source_selection: Any = None,
                             revision_identity_origin_sha256: str | None = None) -> dict[str, Any]:
    """Record caller's actual visual review, never automatically approve OCR.

    The caller must inspect the rendering, transcribe precisely, and list only
    the fields reviewed. Different transcriptions create separate receipts.
    """
    if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 100000:
        raise ValueError("nonempty bounded reviewed transcript required")
    if not reviewer or not reviewed_claim_scopes or any(not isinstance(scope, str) or not scope for scope in reviewed_claim_scopes):
        raise ValueError("reviewer and explicit reviewed claim scopes required")
    image, pdf_sha = _render(service, supplier_part, page, source_pdf_sha256=source_pdf_sha256,
        source_selection=source_selection, revision_identity_origin_sha256=revision_identity_origin_sha256)
    image_sha = hashlib.sha256(image).hexdigest()
    if (pdf_sha, image_sha) != (expected_pdf_sha256, expected_rendered_image_sha256):
        raise ValueError("reviewed PDF or rendered image changed")
    if source_pdf_sha256 is not None:
        cn = service._mysql("datasheet_assets")
        try:
            cur = cn.cursor(); cur.execute("SELECT pdf_sha256 FROM datasheet_documents WHERE lichuang_id=%s", (supplier_part,))
            primary = cur.fetchone()
        finally: cn.rollback(); cn.close()
        if not primary or primary[0] != source_pdf_sha256:
            quality = _secondary_review_source(service, supplier_part, source_pdf_sha256,
                source_selection=source_selection,
                revision_identity_origin_sha256=revision_identity_origin_sha256)["quality"]
            if not set(reviewed_claim_scopes) <= _secondary_scopes(
                    service, supplier_part, source_pdf_sha256, page, quality, transcript, reviewed_claim_scopes):
                raise ValueError("secondary review exceeds source claim scopes")
    transcript_sha = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    receipt = {"schema": "pcbagent.pdf_page_review.v1", "supplier_part": supplier_part,
        "pdf_sha256": pdf_sha, "page": page, "rendered_image_sha256": image_sha,
        "transcript_sha256": transcript_sha, "transcript": transcript,
        "reviewed_claim_scopes": sorted(set(reviewed_claim_scopes)), "reviewer": reviewer,
        "review_method": "explicit_visual_inspection_of_original_pdf_render", "render_scale": 2}
    if source_selection is not None:
        if (pdf_sha != source_selection.get("selected_document_sha256")
                or not set(reviewed_claim_scopes) <= set(source_selection.get("fields", []))):
            raise ValueError("PDF revision review exceeds the explicit selection")
        receipt["source_version_selection"] = dict(source_selection)
    if revision_identity_origin_sha256 is not None:
        receipt["revision_identity_origin_sha256"] = revision_identity_origin_sha256
    raw = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode("utf-8")
    receipt_sha = hashlib.sha256(raw).hexdigest()
    from .web_source import SOURCE_DDL
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute(SOURCE_DDL)
        for sha, mime, source, data in [
            (receipt_sha, "application/json", f"pdf_review:{receipt_sha}", raw),
            (image_sha, "image/png", f"pdf:{pdf_sha}:page:{page}", image)]:
            quality = {"source_kind": "pdf_page_review" if mime == "application/json" else "original_pdf_render",
                       "pdf_sha256": pdf_sha, "page": page}
            if mime == "application/json":
                quality["receipt_sha256"] = receipt_sha
            cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,%s,%s,FALSE,%s)",
                (supplier_part, sha, source, mime, data, json.dumps(quality)))
        cn.commit()
    finally: cn.close()
    indexed = index_pdf_page_review(service, supplier_part, receipt_sha)
    return {**receipt, "receipt_sha256": receipt_sha, "index": indexed}


def read_pdf_page_reviews(service: Any, supplier_part: str, pdf_sha256: str) -> list[dict[str, Any]]:
    cn = service._mysql("datasheet_assets")
    try:
        cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
        cur.execute("SELECT source_sha256,source_bytes FROM datasheet_source_documents WHERE lichuang_id=%s AND source_mime='application/json'", (supplier_part,))
        rows = cur.fetchall()
    finally: cn.rollback(); cn.close()
    result = []
    for row in rows:
        raw = bytes(row["source_bytes"])
        if hashlib.sha256(raw).hexdigest() != row["source_sha256"]:
            raise ValueError("review receipt hash mismatch")
        record = json.loads(raw)
        if record.get("schema") != "pcbagent.pdf_page_review.v1" or record.get("pdf_sha256") != pdf_sha256:
            continue
        if record.get("supplier_part") != supplier_part or hashlib.sha256(record["transcript"].encode("utf-8")).hexdigest() != record["transcript_sha256"]:
            raise ValueError("review receipt identity or transcription hash mismatch")
        result.append({**record, "receipt_sha256": row["source_sha256"]})
    return result


def index_pdf_page_review(service: Any, supplier_part: str, receipt_sha256: str) -> dict[str, Any]:
    """Append a verified review to both search stores, idempotently by receipt."""
    from .preparation import IDENTITY
    from .store import _split_text, _now
    import re
    if not IDENTITY.fullmatch(supplier_part) or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256):
        raise ValueError("valid identity and receipt SHA256 required")
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute(REVIEW_CHUNK_DDL); cn.commit()
    finally: cn.close()
    with service.preparation_lock(supplier_part):
        cn = service._mysql("datasheet_assets"); mc = service._milvus(); inserted_ids = []
        try:
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT product_name,pdf_sha256,chunk_count FROM datasheet_documents WHERE lichuang_id=%s FOR UPDATE", (supplier_part,))
            document = cur.fetchone()
            if not document or not document["pdf_sha256"]:
                raise ValueError("original PDF unavailable")
            cur.execute("SELECT source_bytes FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s AND source_mime='application/json'", (supplier_part, receipt_sha256))
            stored = cur.fetchone()
            if not stored:
                raise ValueError("review receipt unavailable")
            raw = bytes(stored["source_bytes"])
            if hashlib.sha256(raw).hexdigest() != receipt_sha256:
                raise ValueError("review receipt hash mismatch")
            receipt = json.loads(raw)
            if receipt.get("schema") != "pcbagent.pdf_page_review.v1" or receipt.get("supplier_part") != supplier_part:
                raise ValueError("review receipt source identity mismatch")
            source_quality = {}
            if receipt.get("pdf_sha256") != document["pdf_sha256"]:
                source_quality = _secondary_review_source(service, supplier_part, receipt.get("pdf_sha256", ""),
                    source_selection=receipt.get("source_version_selection"),
                    revision_identity_origin_sha256=receipt.get("revision_identity_origin_sha256"))["quality"]
                if not set(receipt["reviewed_claim_scopes"]) <= _secondary_scopes(
                        service, supplier_part, receipt["pdf_sha256"], receipt["page"], source_quality,
                        receipt["transcript"], receipt["reviewed_claim_scopes"]):
                    raise ValueError("secondary review exceeds source claim scopes")
            if hashlib.sha256(receipt["transcript"].encode()).hexdigest() != receipt["transcript_sha256"]:
                raise ValueError("review transcript hash mismatch")
            chunks = _split_text(receipt["transcript"])
            marker = "pdf_review:" + receipt_sha256
            filt = "lichuang_id == " + json.dumps(supplier_part) + " and source_run == " + json.dumps(marker)
            existing = mc.query(collection_name=service.config.milvus_collection, filter=filt,
                output_fields=["id", "content", "chunk_idx", "chunk_total"], limit=1000, consistency_level="Strong")
            if existing:
                existing.sort(key=lambda row: row["chunk_idx"])
                if len(existing) != len(chunks) or any(row["content"] != chunks[i] or row["chunk_idx"] != i or row["chunk_total"] != len(chunks) for i, row in enumerate(existing)):
                    raise ValueError("existing review search chunks differ; reconciliation required")
                ids = [row["id"] for row in existing]
            else:
                rows = [{"lichuang_id": supplier_part, "product_name": document["product_name"],
                    "content_type": "text", "category": "reviewed_pdf_transcription",
                    "title": f"Original PDF page {receipt['page']}: visually reviewed transcription",
                    "content": content, "section_path": marker, "page_idx": receipt["page"] - 1,
                    "parent_entry_idx": -1, "chunk_idx": i, "chunk_total": len(chunks),
                    "image_id": receipt["rendered_image_sha256"], "img_path": "",
                    "source_run": marker, "ingested_at": _now()} for i, content in enumerate(chunks)]
                inserted = mc.insert(collection_name=service.config.milvus_collection, data=rows)
                ids = inserted_ids = list(inserted.get("ids", []))
                if len(ids) != len(chunks):
                    raise RuntimeError("review chunk insert was not acknowledged completely")
            source_ref = f"pdf:{receipt['pdf_sha256']}:page:{receipt['page']}:review:{receipt_sha256}"
            quality = {**source_quality, "qualification": "reviewed_pdf_page_transcription", "provenance": "reviewed_original_pdf_transcription",
                "review_receipt_sha256": receipt_sha256, "rendered_image_sha256": receipt["rendered_image_sha256"],
                "transcript_sha256": receipt["transcript_sha256"], "reviewed_claim_scopes": receipt["reviewed_claim_scopes"],
                "reviewer": receipt["reviewer"], "review_method": receipt["review_method"]}
            for i, content in enumerate(chunks):
                cur.execute("INSERT INTO datasheet_review_chunks (lichuang_id,receipt_sha256,chunk_idx,chunk_total,milvus_chunk_id,source_ref,pdf_sha256,page_number,content,quality_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE milvus_chunk_id=VALUES(milvus_chunk_id)",
                    (supplier_part, receipt_sha256, i, len(chunks), ids[i], source_ref, receipt["pdf_sha256"], receipt["page"], content, json.dumps(quality)))
            actual = mc.query(collection_name=service.config.milvus_collection,
                filter="lichuang_id == " + json.dumps(supplier_part), output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"]
            if actual < document["chunk_count"] + len(inserted_ids):
                raise RuntimeError("review read-back count did not preserve existing chunks")
            cur.execute("UPDATE datasheet_documents SET chunk_count=%s WHERE lichuang_id=%s AND pdf_sha256=%s", (actual, supplier_part, document["pdf_sha256"]))
            cn.commit()
            return {"status": "indexed" if inserted_ids else "existing", "milvus_chunk_ids": ids,
                    "chunk_count": len(chunks), "device_total_chunks": actual, "source_ref": source_ref}
        except BaseException:
            cn.rollback()
            if inserted_ids:
                mc.delete(collection_name=service.config.milvus_collection, ids=inserted_ids)
            raise
        finally: mc.close(); cn.close()


def decorate_review_chunks(service: Any, supplier_part: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach authoritative SQL review metadata to identity-filtered RAG hits."""
    from .manufacturer_image_review import decorate_image_review_chunks
    rows = decorate_image_review_chunks(service, supplier_part, rows)
    ids = [row["id"] for row in rows if type(row.get("id")) is int]
    if not ids:
        return rows
    cn = service._mysql("datasheet_assets")
    try:
        cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_review_chunks'")
        if not cur.fetchone()["n"]:
            return rows
        cur.execute("SELECT milvus_chunk_id,source_ref,pdf_sha256,page_number,content,quality_json FROM datasheet_review_chunks WHERE lichuang_id=%s AND milvus_chunk_id IN (" + ",".join(["%s"] * len(ids)) + ")", (supplier_part, *ids))
        mapping = {row["milvus_chunk_id"]: row for row in cur.fetchall()}
    finally: cn.rollback(); cn.close()
    for row in rows:
        evidence = mapping.get(row.get("id"))
        if not evidence:
            continue
        if row["content"] != evidence["content"]:
            raise ValueError("reviewed SQL and Milvus text differ")
        quality = evidence["quality_json"]
        quality = json.loads(quality) if isinstance(quality, str) else quality
        row.update(source_ref=evidence["source_ref"], document_sha256=evidence["pdf_sha256"],
            page_idx=evidence["page_number"] - 1, locator=f"page:{evidence['page_number']}",
            source_kind=quality.get("source_kind", "datasheet"), provenance="reviewed_original_pdf_transcription", source_quality=quality)
    return rows


__all__ = ["render_pdf_page", "register_pdf_page_review", "read_pdf_page_reviews", "index_pdf_page_review", "decorate_review_chunks", "REVIEW_CHUNK_DDL"]
