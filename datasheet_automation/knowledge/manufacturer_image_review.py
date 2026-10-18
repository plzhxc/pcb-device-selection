"""Explicit, scoped visual reviews of registered manufacturer web images.

Preparation-only writes; frozen consumers only revalidate stored source proofs.
Image facts never inherit authority from filenames, OCR, or the enclosing HTML.
"""
from __future__ import annotations

from collections.abc import Mapping
from html.parser import HTMLParser
import hashlib
import json
import re
from urllib.parse import urljoin

SCHEMA = "pcbagent.manufacturer_image_field_review.v1"
PROVENANCE = "reviewed_manufacturer_image_fields"
METHOD = "independent_visual_inspection_of_original_manufacturer_image"
ALLOWED_SCOPES = frozenset({"semantic_role", "capabilities"})
_SHA = re.compile(r"[0-9a-f]{64}")
_MARKER = "manufacturer_image_review:"
CHUNK_DDL = """CREATE TABLE IF NOT EXISTS datasheet_manufacturer_image_review_chunks (
 lichuang_id VARCHAR(64) NOT NULL, receipt_sha256 CHAR(64) NOT NULL,
 chunk_idx INT NOT NULL, chunk_total INT NOT NULL, milvus_chunk_id BIGINT NOT NULL,
 source_ref VARCHAR(255) NOT NULL, image_sha256 CHAR(64) NOT NULL,
 content LONGTEXT NOT NULL, quality_json JSON NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (lichuang_id,receipt_sha256,chunk_idx),
 UNIQUE KEY unique_image_review_milvus_id (milvus_chunk_id),
 FULLTEXT KEY image_review_text_search (content)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def _sha(data):
    return hashlib.sha256(data).hexdigest()


class _ImageLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "img":
            self.images.append(dict(attrs))


def _identity_digest(bundle):
    pins = [{"pin_number": row.get("pin_number"), "pin_name": row.get("pin_name")}
            for row in bundle.get("pins", [])]
    if not pins or len({row["pin_number"] for row in pins}) != len(pins):
        raise ValueError("complete unique current catalog identities required")
    return _sha(_encoded(sorted(pins, key=lambda row: row["pin_number"])))


def _identity_bundle(service, supplier_part):
    # Do not recursively call knowledge(): it invokes the RAG decorator below.
    cn = service._mysql()
    try:
        cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
        cur.execute("SELECT device_id,supplier_part,device_name,pin_count FROM devices WHERE supplier_part=%s", (supplier_part,))
        device = cur.fetchone()
        if not device: raise ValueError("existing exact catalog identity required")
        cur.execute("SELECT pin_number,pin_name FROM pins WHERE device_id=%s ORDER BY pin_index", (device["device_id"],))
        pins = cur.fetchall()
        return {"supplier_part": supplier_part, "device": device, "pins": pins}
    finally: cn.rollback(); cn.close()


def _image_evidence(receipt, receipt_sha):
    ref = f"manufacturer_image:{receipt['image_sha256']}:review:{receipt_sha}"
    return {"id": ref, "source_ref": ref, "lichuang_id": receipt["supplier_part"],
        "document_sha256": receipt["image_sha256"], "page_idx": None,
        "locator": receipt["image_locator"], "source_kind": "manufacturer_image",
        "source_url": receipt["image_url"], "content_type": "text",
        "title": "Independently reviewed original manufacturer image",
        "content": receipt["transcript"], "provenance": PROVENANCE,
        "source_quality": {"qualification": "manufacturer_image_field_review",
            "claim_scope": "explicitly_reviewed_fields",
            "reviewed_claim_scopes": receipt["reviewed_claim_scopes"],
            "review_receipt_sha256": receipt_sha,
            "transcript_sha256": receipt["transcript_sha256"],
            "parent_html_sha256": receipt["parent_html_sha256"],
            "review_method": METHOD}}


def validate_image_review(bundle, receipt, receipt_sha256, *, require_registered=True):
    """Validate an exact stored review against original frozen source assets.

    This authenticates scoped attribution, not the truth of arbitrary claim
    values. Caller/independent review remains responsible for entailment.
    """
    if (not isinstance(receipt, Mapping) or receipt.get("schema") != SCHEMA
            or not _SHA.fullmatch(str(receipt_sha256))
            or _sha(_encoded(receipt)) != receipt_sha256):
        raise ValueError("image review receipt hash/schema mismatch")
    part = bundle.get("supplier_part")
    if not re.fullmatch(r"[CS][0-9]+", str(part)) or receipt.get("supplier_part") != part:
        raise ValueError("image review source owner mismatch")
    scopes = receipt.get("reviewed_claim_scopes")
    if (not isinstance(scopes, list) or not scopes or any(not isinstance(x, str) for x in scopes)
            or scopes != sorted(set(scopes)) or not set(scopes) <= ALLOWED_SCOPES):
        raise ValueError("image review cannot authorize electrical, NC, or identity fields")
    reviewers = receipt.get("reviewers")
    if (not isinstance(reviewers, list) or len(reviewers) != 2
            or any(not isinstance(x, str) or not x.strip() for x in reviewers)
            or reviewers[0].strip() == reviewers[1].strip()
            or receipt.get("review_method") != METHOD):
        raise ValueError("two distinct explicit visual reviewers required")
    transcript = receipt.get("transcript")
    if (not isinstance(transcript, str) or not transcript.strip() or len(transcript) > 100000
            or _sha(transcript.encode()) != receipt.get("transcript_sha256")
            or not isinstance(receipt.get("image_locator"), str) or not receipt["image_locator"].strip()):
        raise ValueError("image review transcription/locator mismatch")
    model = receipt.get("model")
    identity = receipt.get("catalog_identity")
    if (not isinstance(model, str) or not model.strip()
            or not isinstance(receipt.get("package_scope"), str) or not receipt["package_scope"].strip()
            or not isinstance(identity, list)
            or receipt.get("catalog_identity_sha256") != _identity_digest({"pins": identity})):
        raise ValueError("image review exact model/package applicability mismatch")
    # The independent certificate source projection intentionally omits catalog
    # records. It rechecks the immutable registered identity inventory instead.
    # Full source readers and preparation still guard the actual current catalog.
    if "device" in bundle or "pins" in bundle:
        device = bundle.get("device") or {}
        if (model != device.get("device_name")
                or receipt["catalog_identity_sha256"] != _identity_digest(bundle)
                or device.get("pin_count") != len(bundle.get("pins", []))):
            raise ValueError("image review current catalog identity changed")
    assets = {a.get("source_sha256"): a for a in bundle.get("source_assets", [])
              if a.get("source_verified") is True}
    parent = assets.get(receipt.get("parent_html_sha256"), {})
    image = assets.get(receipt.get("image_sha256"), {})
    quality = parent.get("quality") or {}
    raw_html = parent.get("original_html_text")
    if (parent.get("source_mime") != "text/html" or not parent.get("is_primary")
            or quality.get("qualification") != "manufacturer_web_excerpt"
            or not quality.get("manufacturer") or not isinstance(raw_html, str)
            or _sha(raw_html.encode()) != receipt.get("parent_html_sha256")
            or parent.get("source_url") != receipt.get("parent_html_url")):
        raise ValueError("registered original manufacturer HTML is required")
    if (not re.search(r"(?<![A-Za-z0-9])" + re.escape(model) + r"(?![A-Za-z0-9])", raw_html)
            or not re.search(r"(?<![A-Za-z0-9])" + re.escape(receipt["package_scope"]) + r"(?![A-Za-z0-9])", raw_html)
            or not str(image.get("source_mime", "")).startswith("image/")
            or image.get("source_url") != receipt.get("image_url")
            or not _SHA.fullmatch(str(receipt.get("image_sha256")))):
        raise ValueError("original manufacturer model/image mismatch")
    parser = _ImageLinks(); parser.feed(raw_html)
    links = [tag for tag in parser.images if tag.get("src") == receipt.get("html_image_src")
             and urljoin(parent["source_url"], tag["src"]) == image["source_url"]]
    if len(links) != 1:
        raise ValueError("exact original manufacturer HTML image link required")
    # Honor known excluded figures. Do not allow a caller-supplied locator to
    # rename a wrong-device image that the original source already excludes.
    labels = " ".join(str(links[0].get(k, "")) for k in ("alt", "title"))
    ordinals = set(re.findall(r"(?:figure|图)\s*(\d+)", labels, re.I))
    exclusions = quality.get("excluded_images") or {}
    for excluded in exclusions:
        excluded_numbers = set(re.findall(r"(?:figure|图)\s*(\d+)", str(excluded), re.I))
        if (str(excluded) in {receipt["image_sha256"], image["source_url"], receipt["html_image_src"]}
                or ordinals & excluded_numbers):
            raise ValueError("original source explicitly excludes this conflicting image")
    if require_registered:
        stored = assets.get(receipt_sha256, {})
        if (stored.get("source_mime") != "application/json" or stored.get("is_primary")
                or stored.get("record") != dict(receipt)
                or (stored.get("quality") or {}).get("source_kind") != "manufacturer_image_field_review"
                or (stored.get("quality") or {}).get("supplier_part") != part):
            raise ValueError("independently registered image review receipt required")
    return _image_evidence(receipt, receipt_sha256)


def image_review_evidence(bundle):
    result = []
    for asset in bundle.get("source_assets", []):
        receipt = asset.get("record") or {}
        if receipt.get("schema") == SCHEMA:
            result.append(validate_image_review(bundle, receipt, asset["source_sha256"]))
    return result


def register_manufacturer_image_review(service, supplier_part, *, model, package_scope,
        parent_html_sha256, image_sha256, html_image_src, image_locator, transcript,
        reviewed_claim_scopes, reviewer, independent_reviewer):
    """Preparation-only registration after two actual independent visual reviews.

    Existing original image/HTML assets are immutable. This function appends a
    new receipt and review chunks; it never auto-approves model generated text.
    """
    from .source_evidence import read_source_documents
    from .web_source import SOURCE_DDL
    with service.preparation_lock(supplier_part):
        bundle = _identity_bundle(service, supplier_part)
        bundle["supplier_part"] = supplier_part
        bundle["source_assets"] = read_source_documents(service, supplier_part)
        assets = {x["source_sha256"]: x for x in bundle["source_assets"]}
        parent = assets.get(parent_html_sha256, {})
        image = assets.get(image_sha256, {})
        # Bootstrap reads exact raw HTML directly, before a receipt exists to
        # request its inclusion in frozen original-source snapshots.
        cn = service._mysql("datasheet_assets")
        try:
            cur = cn.cursor(); cur.execute("SELECT source_bytes FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s AND source_mime='text/html'", (supplier_part, parent_html_sha256))
            row = cur.fetchone()
            if not row or _sha(bytes(row[0])) != parent_html_sha256:
                raise ValueError("original HTML bytes changed")
            parent["original_html_text"] = bytes(row[0]).decode("utf-8")
        finally: cn.close()
        receipt = {"schema": SCHEMA, "supplier_part": supplier_part, "model": model,
            "package_scope": package_scope, "catalog_identity_sha256": _identity_digest(bundle),
            "catalog_identity": [{"pin_number": p["pin_number"], "pin_name": p["pin_name"]} for p in bundle["pins"]],
            "parent_html_sha256": parent_html_sha256,
            "parent_html_url": parent.get("source_url"), "image_sha256": image_sha256,
            "image_url": image.get("source_url"), "html_image_src": html_image_src,
            "image_locator": image_locator, "transcript": transcript,
            "transcript_sha256": _sha(transcript.encode()),
            "reviewed_claim_scopes": sorted(set(reviewed_claim_scopes)),
            "reviewers": [reviewer, independent_reviewer], "review_method": METHOD}
        raw = _encoded(receipt); sha = _sha(raw)
        validate_image_review(bundle, receipt, sha, require_registered=False)
        cn = service._mysql("datasheet_assets")
        try:
            cur = cn.cursor(); cur.execute(SOURCE_DDL)
            quality = {"source_kind": "manufacturer_image_field_review", "supplier_part": supplier_part,
                "receipt_sha256": sha, "image_sha256": image_sha256}
            cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,'application/json',%s,FALSE,%s)",
                (supplier_part, sha, _MARKER + sha, raw, json.dumps(quality)))
            cn.commit()
        finally: cn.close()
    indexed = index_manufacturer_image_review(service, supplier_part, sha)
    return {**receipt, "receipt_sha256": sha, "index": indexed}


def index_manufacturer_image_review(service, supplier_part, receipt_sha256):
    """Idempotent SQL text/Milvus index; a failed attempt retains its receipt."""
    from .source_evidence import read_source_documents
    from .store import _split_text, _now
    bundle = _identity_bundle(service, supplier_part)
    bundle.update(supplier_part=supplier_part, source_assets=read_source_documents(service, supplier_part))
    asset = next((x for x in bundle["source_assets"] if x["source_sha256"] == receipt_sha256), {})
    evidence = validate_image_review(bundle, asset.get("record"), receipt_sha256)
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute(CHUNK_DDL); cn.commit()
    finally: cn.close()
    with service.preparation_lock(supplier_part):
        cn = service._mysql("datasheet_assets"); mc = service._milvus(); inserted_ids = []
        try:
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT product_name,chunk_count FROM datasheet_documents WHERE lichuang_id=%s FOR UPDATE", (supplier_part,))
            document = cur.fetchone()
            if not document: raise ValueError("existing source document required")
            chunks = _split_text(evidence["content"]); marker = _MARKER + receipt_sha256
            filt = "lichuang_id == " + json.dumps(supplier_part) + " and source_run == " + json.dumps(marker)
            existing = mc.query(collection_name=service.config.milvus_collection, filter=filt,
                output_fields=["id", "content", "chunk_idx", "chunk_total"], limit=1000, consistency_level="Strong")
            if existing:
                existing.sort(key=lambda x: x["chunk_idx"])
                if len(existing) != len(chunks) or any(x["content"] != chunks[i] or x["chunk_idx"] != i or x["chunk_total"] != len(chunks) for i,x in enumerate(existing)):
                    raise ValueError("existing image review chunks differ")
                ids = [x["id"] for x in existing]
            else:
                rows = [{"lichuang_id": supplier_part, "product_name": document["product_name"],
                    "content_type": "text", "category": "reviewed_manufacturer_image_fields",
                    "title": evidence["title"], "content": content, "section_path": marker,
                    "page_idx": -1, "parent_entry_idx": -1, "chunk_idx": i, "chunk_total": len(chunks),
                    "image_id": evidence["document_sha256"], "img_path": "", "source_run": marker,
                    "ingested_at": _now()} for i,content in enumerate(chunks)]
                inserted = mc.insert(collection_name=service.config.milvus_collection, data=rows)
                ids = inserted_ids = list(inserted.get("ids", []))
                if len(ids) != len(chunks): raise RuntimeError("image review insert acknowledgement incomplete")
            for i,content in enumerate(chunks):
                cur.execute("INSERT INTO datasheet_manufacturer_image_review_chunks (lichuang_id,receipt_sha256,chunk_idx,chunk_total,milvus_chunk_id,source_ref,image_sha256,content,quality_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE milvus_chunk_id=VALUES(milvus_chunk_id)",
                    (supplier_part,receipt_sha256,i,len(chunks),ids[i],evidence["source_ref"],evidence["document_sha256"],content,json.dumps(evidence["source_quality"])))
            cur.execute("SELECT chunk_idx,chunk_total,milvus_chunk_id,source_ref,image_sha256,content FROM datasheet_manufacturer_image_review_chunks WHERE lichuang_id=%s AND receipt_sha256=%s ORDER BY chunk_idx", (supplier_part,receipt_sha256))
            stored_rows = cur.fetchall()
            if len(stored_rows) != len(chunks) or any(
                    row["chunk_idx"] != i or row["chunk_total"] != len(chunks)
                    or row["milvus_chunk_id"] != ids[i] or row["content"] != chunks[i]
                    or row["source_ref"] != evidence["source_ref"] or row["image_sha256"] != evidence["document_sha256"]
                    for i,row in enumerate(stored_rows)):
                raise ValueError("SQL image review index readback differs")
            actual = mc.query(collection_name=service.config.milvus_collection,
                filter="lichuang_id == " + json.dumps(supplier_part), output_fields=["count(*)"], consistency_level="Strong")[0]["count(*)"]
            if actual < document["chunk_count"] + len(inserted_ids):
                raise RuntimeError("image review index lost previous chunks")
            cur.execute("UPDATE datasheet_documents SET chunk_count=%s WHERE lichuang_id=%s", (actual,supplier_part)); cn.commit()
            return {"status": "indexed" if inserted_ids else "existing", "milvus_chunk_ids": ids,
                "chunk_count": len(chunks), "device_total_chunks": actual, "source_ref": evidence["source_ref"]}
        except BaseException:
            cn.rollback()
            if inserted_ids: mc.delete(collection_name=service.config.milvus_collection, ids=inserted_ids)
            raise
        finally: mc.close(); cn.close()


def decorate_image_review_chunks(service, supplier_part, rows):
    targets = [row for row in rows if str(row.get("section_path", "")).startswith(_MARKER)]
    if not targets: return rows
    from .source_evidence import read_source_documents
    bundle = _identity_bundle(service, supplier_part)
    bundle.update(supplier_part=supplier_part, source_assets=read_source_documents(service, supplier_part))
    for row in targets:
        sha = row["section_path"][len(_MARKER):]
        asset = next((x for x in bundle["source_assets"] if x["source_sha256"] == sha), {})
        evidence = validate_image_review(bundle, asset.get("record"), sha)
        cn = service._mysql("datasheet_assets")
        try:
            cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
            cur.execute("SELECT content,source_ref,image_sha256 FROM datasheet_manufacturer_image_review_chunks WHERE lichuang_id=%s AND receipt_sha256=%s AND milvus_chunk_id=%s", (supplier_part,sha,row.get("id")))
            stored = cur.fetchone()
        finally: cn.rollback(); cn.close()
        if (not stored or stored["content"] != row.get("content")
                or stored["source_ref"] != evidence["source_ref"]
                or stored["image_sha256"] != evidence["document_sha256"]):
            raise ValueError("image review RAG content/receipt mismatch")
        row.update({k:v for k,v in evidence.items() if k not in {"id","content"}})
    return rows
