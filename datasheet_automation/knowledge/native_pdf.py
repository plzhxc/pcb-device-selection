"""Fast, explicitly text-only PDF evidence indexing for preparation.

Native text is a source transcription, not an interpretation of diagrams or
tables. Every result carries that limitation in SQL and its indexed text.
"""
from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from .store import KnowledgeStore
_PDFIUM_LOCK = threading.Lock()

PROVENANCE_DDL = """CREATE TABLE IF NOT EXISTS datasheet_ingestion_provenance (
 lichuang_id VARCHAR(64) NOT NULL PRIMARY KEY,
 pdf_sha256 CHAR(64) NOT NULL,
 method VARCHAR(40) NOT NULL,
 qualification VARCHAR(40) NOT NULL,
 total_pages INT NOT NULL,
 text_pages INT NOT NULL,
 missing_text_pages JSON NOT NULL,
 visual_pages JSON NOT NULL,
 source_url VARCHAR(2048) DEFAULT NULL,
 details_json JSON NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def ensure_native_schema(service: Any) -> None:
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor()
        cur.execute(PROVENANCE_DDL)
        cn.commit()
    finally:
        cn.close()


def prepare_native_pdf(service: Any, supplier_part: str, *, product_name: str,
                       pdf_path: str | Path | None = None, source_url: str | None = None,
                       source_provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    with service.preparation_lock(supplier_part):
        return _prepare_native_pdf(service,supplier_part,product_name=product_name,
            pdf_path=pdf_path,source_url=source_url,source_provenance=source_provenance)


def _native_entries(data: bytes, product_name: str) -> tuple:
    import pypdfium2 as pdfium
    entries=[];missing=[];visual=[]
    # PDFium is not thread-safe. Downloads and database writes may be parallel;
    # native document access is serialized within this process.
    with _PDFIUM_LOCK:
        document=pdfium.PdfDocument(data)
        try:
            pages=len(document)
            for index in range(pages):
                page=document[index]
                textpage=page.get_textpage()
                try:
                    text=textpage.get_text_range().strip()
                    if any(obj.type in {pdfium.raw.FPDF_PAGEOBJ_IMAGE,pdfium.raw.FPDF_PAGEOBJ_PATH} for obj in page.get_objects()):
                        visual.append(index+1)
                    if not text:
                        missing.append(index+1)
                        continue
                    entries.append({"type":"text","title":f"{product_name}: PDF page {index+1}",
                        "page_idx":index,"content":"[native_pdf_text: source transcription only; table structure and visual content are unverified]\n"+text})
                finally:
                    textpage.close();page.close()
        finally:
            document.close()
    return entries, pages, missing, visual


def _prepare_native_pdf(service: Any, supplier_part: str, *, product_name: str,
                        pdf_path: str | Path | None, source_url: str | None,
                        source_provenance: dict[str, Any] | None) -> dict[str, Any]:
    """Acquire an exact C-number PDF or index an explicitly reviewed C/S PDF.

    Existing assets are never replaced. Caller should create provenance schema
    once with ensure_native_schema before dispatching a batch.
    """
    from .preparation import IDENTITY, RUNTIME_ROOT
    if not IDENTITY.fullmatch(supplier_part):
        raise ValueError("invalid C/S identity")
    cn = service._mysql("datasheet_assets")
    try:
        cn.start_transaction(readonly=True)
        cur = cn.cursor(dictionary=True)
        cur.execute("SELECT chunk_count,pdf_sha256,pdf_size_bytes FROM datasheet_documents WHERE lichuang_id=%s", (supplier_part,))
        existing = cur.fetchone()
    finally:
        cn.rollback();cn.close()
    mc = service._milvus()
    try:
        count = mc.query(collection_name=service.config.milvus_collection,
            filter="lichuang_id == " + json.dumps(supplier_part), output_fields=["count(*)"],
            consistency_level="Strong")[0]["count(*)"]
    finally:
        mc.close()
    if existing or count:
        complete = bool(existing and count and existing["chunk_count"] == count
                        and existing["pdf_sha256"] and existing["pdf_size_bytes"])
        return {"id":supplier_part,"status":"existing" if complete else "reconciliation_required","milvus_chunks":count}
    directory = RUNTIME_ROOT / "runs" / supplier_part
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    acquisition = source_provenance
    if pdf_path is None:
        cached = list(directory.glob("*.pdf"))
        if len(cached)==1 and cached[0].read_bytes().startswith(b"%PDF"):
            pdf_path = cached[0]
            if (directory/"acquisition.json").is_file():
                acquisition=json.loads((directory/"acquisition.json").read_text())
                source_url=source_url or acquisition.get("pdf_url")
        elif supplier_part.startswith("S"):
            return {"id":supplier_part,"status":"source_required"}
        else:
            from datasheet_automation.lcsc.client import LcscClient
            from datasheet_automation.core.collection import DatasheetCollector
            client=LcscClient()
            candidates=client.search(supplier_part,limit=10)
            selected=DatasheetCollector._select_product(supplier_part,candidates)
            pdf_path=client.download_datasheet(selected,directory)
            source_url=selected.first_datasheet().url
            acquisition={"source":"lcsc_exact_id","product_code":selected.product_code,
                "product_model":selected.product_model,"package":selected.package,
                "item_url":selected.item_url,"pdf_url":source_url}
            (directory/"acquisition.json").write_text(json.dumps(acquisition,ensure_ascii=False,indent=2))
    pdf=Path(pdf_path).resolve()
    data=pdf.read_bytes()
    if not data.startswith(b"%PDF") or len(data)>60*1024*1024:
        raise ValueError("invalid or oversized source PDF")
    entries, pages, missing, visual = _native_entries(data, product_name)
    if not entries:
        return {"id":supplier_part,"status":"ocr_required","total_pages":pages}
    quality={"method":"native_pdf_text","qualification":"text_only","total_pages":pages,
        "text_pages":len(entries),"missing_text_pages":missing,"visual_pages":visual,
        "table_interpretation":"unverified","visual_interpretation":"unverified",
        "pdf_sha256":hashlib.sha256(data).hexdigest(),"source_url":source_url,
        "acquisition":acquisition}
    if acquisition and acquisition.get("identity_qualification"):
        quality["identity_qualification"]=acquisition["identity_qualification"]
    elif acquisition and acquisition.get("manufacturer_identity_confirmed") is False:
        quality["identity_qualification"]="manufacturer_unresolved"
    if acquisition and acquisition.get("original_source_kind"):
        quality["original_source_kind"] = acquisition["original_source_kind"]
    optimized=directory/"optimized"
    optimized.mkdir(mode=0o700,exist_ok=True)
    content=optimized/f"opti_{supplier_part}_content_list.json"
    if content.exists():
        if json.loads(content.read_text()) != entries:
            raise ValueError("existing optimized run requires explicit selection; not overwritten")
    else:
        content.write_text(json.dumps(entries,ensure_ascii=False,indent=2))
    (directory/"native-provenance.json").write_text(json.dumps(quality,ensure_ascii=False,indent=2))
    # Persist qualification before chunks so no concurrent reader can mistake
    # text-only evidence for fully interpreted visual evidence.
    cn=service._mysql("datasheet_assets")
    try:
        cur=cn.cursor()
        cur.execute("SELECT pdf_sha256,method FROM datasheet_ingestion_provenance WHERE lichuang_id=%s",(supplier_part,))
        old=cur.fetchone()
        if old and old != (quality["pdf_sha256"],quality["method"]):
            raise ValueError("existing ingestion provenance differs; explicit reconciliation required")
        if not old:
            cur.execute("INSERT INTO datasheet_ingestion_provenance (lichuang_id,pdf_sha256,method,qualification,total_pages,text_pages,missing_text_pages,visual_pages,source_url,details_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (supplier_part,quality["pdf_sha256"],quality["method"],quality["qualification"],pages,len(entries),json.dumps(missing),json.dumps(visual),source_url,json.dumps(quality,ensure_ascii=False)))
        cn.commit()
    finally:
        cn.close()
    store=KnowledgeStore(service.config)
    try:
        result=store.ingest_run(directory,supplier_part,product_name=product_name,
                                pdf_path=pdf,pdf_url=source_url,replace=False)
    finally:
        if store._milvus is not None:
            store._milvus.close()
    return {"id":supplier_part,"status":"ingested_text_only","quality":quality,"ingest":result}


def _reconciliation_bytes(value: Any) -> bytes:
    """Lossless archive encoding for SQL scalar types, including original bytes."""
    from datetime import date, datetime
    from decimal import Decimal
    def encode(item: Any) -> Any:
        if isinstance(item, (bytes, bytearray, memoryview)):
            return {"sql_binary_hex": bytes(item).hex()}
        if isinstance(item, (date, datetime, Decimal)):
            return {"sql_type": type(item).__name__, "value": str(item)}
        raise TypeError(f"unsupported archive value: {type(item).__name__}")
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=encode).encode()


def _reconciliation_state(service: Any, part: str) -> dict[str, Any]:
    """Snapshot all replaced source rows and unchanged catalog/contract history."""
    tables = {
        "datasheet_assets": ("datasheet_documents", "datasheet_ingestion_provenance", "datasheet_images",
            "datasheet_review_chunks", "datasheet_secondary_chunks", "datasheet_source_documents"),
        "device_parameters": ("devices", "pins", "catalog_field_evidence", "device_pin_contract_versions"),
    }
    state: dict[str, Any] = {"tables": {}}
    for database, names in tables.items():
        cn = service._mysql(database)
        try:
            cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
            for table in names:
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=%s AND table_name=%s", (database, table))
                rows = []
                if cur.fetchone()["n"]:
                    if table == "pins":
                        cur.execute("SELECT * FROM pins WHERE device_id IN (SELECT device_id FROM devices WHERE supplier_part=%s)", (part,))
                    else:
                        key = "lichuang_id" if database == "datasheet_assets" else "supplier_part"
                        cur.execute(f"SELECT * FROM {table} WHERE {key}=%s", (part,))
                    rows = cur.fetchall()
                state["tables"][table] = sorted(rows, key=_reconciliation_bytes)
        finally:
            cn.rollback(); cn.close()
    mc = service._milvus()
    try:
        from collections.abc import Sequence
        from pymilvus import FunctionType
        schema = mc.describe_collection(collection_name=service.config.milvus_collection)
        # Milvus exposes these logical arrays as protobuf repeated containers.
        # Preserve every value as JSON arrays; never stringify opaque objects.
        for function in schema.get("functions", []):
            for key in ("input_field_names", "output_field_names", "input_field_ids", "output_field_ids"):
                if key not in function: continue
                values = function[key]; scalar = str if key.endswith("names") else int
                if (not isinstance(values, Sequence) or isinstance(values, (str, bytes))
                        or any(type(v) is not scalar for v in values)):
                    raise ValueError("invalid Milvus function field array")
                function[key] = list(values)
        if (schema.get("enable_dynamic_field") or not any(
                f.get("name") == "content_bm25" and f.get("input_field_names") == ["content"]
                and f.get("output_field_names") == ["content_sparse_vector"]
                and f.get("type") == FunctionType.BM25 for f in schema.get("functions", []))):
            raise ValueError("reconciliation requires the known reproducible BM25 collection")
        fields = [f["name"] for f in schema["fields"] if f["name"] != "content_sparse_vector"]
        rows = []; iterator = mc.query_iterator(collection_name=service.config.milvus_collection,
            filter="lichuang_id == " + json.dumps(part), output_fields=fields,
            batch_size=1000, consistency_level="Strong")
        try:
            while batch := iterator.next(): rows.extend(batch)
        finally: iterator.close()
        if any(set(row) != set(fields) for row in rows):
            raise ValueError("complete original scalar Milvus rows required for archive")
        state.update(milvus_schema=schema, milvus_rows=sorted(rows, key=lambda row: row["id"]))
    finally: mc.close()
    return state


def _reconciliation_catalog(state: dict[str, Any]) -> dict[str, Any]:
    return {name: state["tables"][name] for name in
        ("devices", "pins", "catalog_field_evidence", "device_pin_contract_versions")}


def _reconciliation_quality(state: dict[str, Any]) -> dict[str, Any]:
    rows = state["tables"]["datasheet_ingestion_provenance"]
    if len(rows) != 1: raise ValueError("one current source provenance row required")
    value = rows[0]["details_json"]
    return json.loads(value) if isinstance(value, str) else dict(value)


def _set_reconciliation_quality(service: Any, part: str, *, expected_document: str,
                                expected_provenance: str, quality: dict[str, Any]) -> None:
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute("SELECT pdf_sha256 FROM datasheet_documents WHERE lichuang_id=%s FOR UPDATE", (part,))
        if cur.fetchone() != {"pdf_sha256": expected_document}:
            raise ValueError("primary changed during reconciliation")
        cur.execute("UPDATE datasheet_ingestion_provenance SET pdf_sha256=%s,method=%s,qualification=%s,total_pages=%s,text_pages=%s,missing_text_pages=%s,visual_pages=%s,source_url=%s,details_json=%s WHERE lichuang_id=%s AND pdf_sha256=%s",
            (quality["pdf_sha256"], quality["method"], quality["qualification"], quality["total_pages"],
             quality["text_pages"], json.dumps(quality["missing_text_pages"]), json.dumps(quality["visual_pages"]),
             quality.get("source_url"), json.dumps(quality, ensure_ascii=False), part, expected_provenance))
        if cur.rowcount != 1: raise ValueError("source provenance changed during reconciliation")
        cn.commit()
    except BaseException:
        cn.rollback(); raise
    finally: cn.close()


def _archive_reconciliation(service: Any, part: str, assets: list[tuple]) -> None:
    from .web_source import SOURCE_DDL
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(dictionary=True); cur.execute(SOURCE_DDL)
        for raw, url, mime, quality in assets:
            sha = hashlib.sha256(raw).hexdigest()
            cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,%s,%s,FALSE,%s)",
                (part, sha, url, mime, raw, json.dumps(quality, ensure_ascii=False)))
            cur.execute("SELECT source_bytes,source_mime,source_url FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s", (part, sha))
            stored = cur.fetchone()
            if (not stored or bytes(stored["source_bytes"]) != raw or stored["source_mime"] != mime
                    or stored["source_url"] != url):
                raise ValueError("immutable source archive differs")
        cn.commit()
    except BaseException:
        cn.rollback(); raise
    finally: cn.close()


def _verify_reconciled_source(service: Any, part: str, sha: str, run: str,
                              entries: list, expected_catalog: dict, old_ids: set,
                              expected_assets: list) -> dict:
    from .store import _split_text
    state = _reconciliation_state(service, part)
    docs = state["tables"]["datasheet_documents"]
    expected = {(i, k): (e["page_idx"], text, len(_split_text(e["content"])))
        for i, e in enumerate(entries) for k, text in enumerate(_split_text(e["content"]))}
    rows = state["milvus_rows"]
    if (len(docs) != 1 or docs[0]["pdf_sha256"] != sha
            or hashlib.sha256(bytes(docs[0]["pdf_bytes"])).hexdigest() != sha
            or docs[0]["chunk_count"] != len(expected) or len(rows) != len(expected)
            or {row["id"] for row in rows} & old_ids
            or _reconciliation_catalog(state) != expected_catalog
            or state["tables"]["datasheet_source_documents"] != expected_assets):
        raise ValueError("reconciliation document/count/catalog/old-ID readback failed")
    actual = {(row["parent_entry_idx"], row["chunk_idx"]):
        (row["page_idx"], row["content"], row["chunk_total"]) for row in rows}
    if (actual != expected or any(row["source_run"] != run or row["content_type"] != "text"
            or row["lichuang_id"] != part for row in rows)):
        raise ValueError("new native chunk content/provenance readback differs")
    return state


def reconcile_wrong_primary_pdf(service: Any, supplier_part: str, *, pdf_path: str | Path,
        source_url: str, source_review: dict[str, Any], expected_current_pdf_sha256: str,
        expected_state_sha256: str | None = None, apply: bool = False) -> dict[str, Any]:
    """Archive a wrong primary, then explicitly select a reviewed real datasheet.

    A dry run returns the whole-state guard. Application requires that guard,
    keeps original bytes/history, and uses the existing related-source denial
    during SQL/Milvus replacement. This does not register pins or electrical
    facts. The bounded first path rejects auxiliary indexed sources/images.
    Failure after staging remains pending; it is never automatically resumed.
    """
    from .preparation import IDENTITY, RUNTIME_ROOT
    from .evidence import normalize_source_text, quote_in_source, primary_source_registry
    import re
    if service.config.mysql_database != "datasheet_assets":
        raise ValueError("reconciliation requires the same datasheet_assets read/write database")
    if (not isinstance(supplier_part, str) or not IDENTITY.fullmatch(supplier_part) or type(apply) is not bool
            or not isinstance(source_url, str) or not source_url.startswith("https://")
            or not isinstance(expected_current_pdf_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_current_pdf_sha256)
            or (apply and not re.fullmatch(r"[0-9a-f]{64}", expected_state_sha256 or ""))):
        raise ValueError("exact identity, HTTPS source, old SHA and apply state guard required")
    raw = Path(pdf_path).read_bytes(); sha = hashlib.sha256(raw).hexdigest()
    if not raw.startswith(b"%PDF") or len(raw) > 60 * 1024 * 1024 or sha == expected_current_pdf_sha256:
        raise ValueError("different bounded original PDF required")
    if (not isinstance(source_review, dict) or source_review.get("pdf_sha256") != sha
            or not isinstance(source_review.get("reviewer"), str) or not source_review["reviewer"].strip()
            or source_review.get("source_origin") not in
                {"manufacturer_original", "distributor_copy_of_manufacturer_datasheet",
                 "public_copy_of_manufacturer_datasheet"}):
        raise ValueError("explicit reviewed new-PDF identity and honest origin required")
    for field in ("model", "manufacturer", "package", "revision_label"):
        if not isinstance(source_review.get(field), str) or not source_review[field].strip():
            raise ValueError("exact manufacturer/model/package/revision review required")
    entries, pages, missing, visual = _native_entries(raw, source_review["model"])
    if missing or not entries or pages > 200:
        raise ValueError("bounded fully native PDF required; scanned correction is unsupported")
    for field in ("model", "manufacturer", "package", "revision_label"):
        if not any(isinstance(w, dict) and w.get("field") == field and type(w.get("page")) is int
                and 1 <= w["page"] <= pages and isinstance(w.get("quote"), str)
                and normalize_source_text(source_review[field]) in normalize_source_text(w["quote"])
                and quote_in_source(w["quote"], entries[w["page"] - 1]["content"])
                for w in source_review.get("support", [])):
            raise ValueError(f"new source lacks original identity witness: {field}")
    review_sha = hashlib.sha256(_reconciliation_bytes({"source_url": source_url, "review": source_review})).hexdigest()
    with service.preparation_lock(supplier_part):
        state = _reconciliation_state(service, supplier_part)
        quality = _reconciliation_quality(state); docs = state["tables"]["datasheet_documents"]
        if len(docs) != 1: raise ValueError("one existing primary required")
        doc = docs[0]
        if any(hashlib.sha256(bytes(a["source_bytes"])).hexdigest() != a["source_sha256"]
                for a in state["tables"]["datasheet_source_documents"]):
            raise ValueError("stored original source asset hash mismatch")
        prior = quality.get("source_reconciliation", {})
        if doc["pdf_sha256"] == sha and prior.get("status") == "complete":
            if (prior.get("previous_pdf_sha256") != expected_current_pdf_sha256
                    or prior.get("expected_state_sha256") != expected_state_sha256
                    or prior.get("source_review_sha256") != review_sha or doc["pdf_url"] != source_url):
                raise ValueError("completed correction differs from requested guards/review")
            archive = next((a for a in state["tables"]["datasheet_source_documents"]
                if a["source_sha256"] == prior.get("archive_sha256")), None)
            if not archive or hashlib.sha256(bytes(archive["source_bytes"])).hexdigest() != prior["archive_sha256"]:
                raise ValueError("completed correction archive unavailable")
            # Compare canonical encodings so SQL date/decimal values retain types.
            saved = json.loads(bytes(archive["source_bytes"]))["state"]
            if hashlib.sha256(_reconciliation_bytes(saved)).hexdigest() != expected_state_sha256:
                raise ValueError("completed correction archive state differs")
            current_catalog = json.loads(_reconciliation_bytes(_reconciliation_catalog(state)))
            old_catalog = {k: saved["tables"][k] for k in current_catalog}
            if current_catalog != old_catalog: raise ValueError("catalog changed after correction; explicit readback required")
            current_assets = {a["source_sha256"]: a for a in state["tables"]["datasheet_source_documents"]}
            if any(current_assets.get(a["source_sha256"]) is None
                    or json.loads(_reconciliation_bytes(current_assets[a["source_sha256"]])) != a
                    for a in saved["tables"]["datasheet_source_documents"]):
                raise ValueError("original source assets changed after correction")
            if any(key not in current_assets or hashlib.sha256(bytes(current_assets[key]["source_bytes"])).hexdigest() != key
                    for key in (sha, expected_current_pdf_sha256)):
                raise ValueError("archived original PDF bytes unavailable")
            _verify_reconciled_source(service, supplier_part, sha, prior["source_run"], entries,
                _reconciliation_catalog(state), {row["id"] for row in saved["milvus_rows"]},
                state["tables"]["datasheet_source_documents"])
            bundle = service.read_primary_source_evidence(supplier_part, limit=200)
            registry = primary_source_registry(bundle)
            if (set(registry) != {f"pdf:{sha}:page:{p}" for p in range(1, pages + 1)}
                    or any(v["source_kind"] != "datasheet" or v["provenance"] != "original_pdf_native" for v in registry.values())):
                raise ValueError("completed original-source registry readback differs")
            return {"supplier_part": supplier_part, "status": "existing", "pdf_sha256": sha,
                "archive_sha256": prior["archive_sha256"]}
        old_raw = bytes(doc["pdf_bytes"])
        if (doc["pdf_sha256"] != expected_current_pdf_sha256
                or hashlib.sha256(old_raw).hexdigest() != expected_current_pdf_sha256
                or state["tables"]["datasheet_ingestion_provenance"][0]["pdf_sha256"] != expected_current_pdf_sha256
                or quality.get("pdf_sha256") != expected_current_pdf_sha256
                or quality.get("source_document_type") != "compliance_certificate"
                or not str(quality.get("original_source_kind", "")).startswith("related_")
                or quality.get("source_reconciliation", {}).get("status") == "pending"):
            raise ValueError("reviewed wrong-type original required; pending correction needs explicit recovery")
        classification = quality.get("source_document_type_review", {})
        if (not classification.get("reviewer") or not re.fullmatch(r"[0-9a-f]{64}", classification.get("audit_sha256", ""))
                or classification.get("source_pdf_sha256") != expected_current_pdf_sha256):
            raise ValueError("original document-type review required")
        devices = state["tables"]["devices"]
        if (len(devices) != 1 or devices[0]["device_name"] != source_review["model"]
                or doc["product_name"] != source_review["model"]):
            raise ValueError("reviewed source model differs from the fixed catalog identity")
        if (any(state["tables"][t] for t in ("datasheet_images", "datasheet_review_chunks", "datasheet_secondary_chunks"))
                or any(a.get("is_primary") for a in state["tables"]["datasheet_source_documents"])
                or doc["image_count"] or doc["chunk_count"] != len(state["milvus_rows"])
                or not isinstance(doc.get("pdf_url"), str) or not doc["pdf_url"]
                or any(row["source_run"] != doc["source_run"] or row["content_type"] != "text" or row["lichuang_id"] != supplier_part
                       for row in state["milvus_rows"])):
            raise ValueError("auxiliary/visual or inconsistent existing chunks require separate reconciliation")
        state_sha = hashlib.sha256(_reconciliation_bytes(state)).hexdigest()
        if expected_state_sha256 is not None and expected_state_sha256 != state_sha:
            raise ValueError("whole original state changed")
        archive_raw = _reconciliation_bytes({"schema": "pcbagent.wrong_primary_archive.v1",
            "supplier_part": supplier_part, "state": state, "source_review": source_review,
            "previous_pdf_sha256": expected_current_pdf_sha256, "selected_pdf_sha256": sha, "selected_source_url": source_url,
            "recovery": "Original rows/bytes/types archived. BM25 vectors regenerate from saved content/schema; retired auto IDs remain historical only."})
        archive_sha = hashlib.sha256(archive_raw).hexdigest()
        if len(archive_raw) > 60 * 1024 * 1024:
            raise ValueError("complete source archive exceeds bounded storage limit")
        result = {"supplier_part": supplier_part, "status": "dry_run", "expected_state_sha256": state_sha,
            "old_pdf_sha256": expected_current_pdf_sha256, "new_pdf_sha256": sha,
            "archive_sha256": archive_sha, "pages": pages, "source_url": source_url, "source_review": source_review,
            "catalog_and_contracts_unchanged": True, "source_facts_automatically_created": False}
        if not apply: return {**result, "archive_record": json.loads(archive_raw)}
        directory = RUNTIME_ROOT / "primary_reconciliation" / supplier_part / archive_sha
        (directory / "optimized").mkdir(mode=0o700, parents=True, exist_ok=True)
        for path, content in ((directory / "source.pdf", raw),
                (directory / "optimized" / f"opti_{supplier_part}_content_list.json", _reconciliation_bytes(entries))):
            if path.exists() and path.read_bytes() != content: raise ValueError("staged immutable file differs")
            if not path.exists(): path.write_bytes(content); path.chmod(0o600)
        _archive_reconciliation(service, supplier_part, [
            (old_raw, doc["pdf_url"], "application/pdf", {"qualification": "wrong_primary_archive", "document_type": "compliance_certificate", "claim_scope": "none"}),
            (archive_raw, doc["pdf_url"], "application/json", {"qualification": "wrong_primary_archive", "claim_scope": "none"}),
            (raw, source_url, "application/pdf", {"qualification": "wrong_primary_selected_original", "source_review": source_review, "claim_scope": "none"})])
        archived = _reconciliation_state(service, supplier_part)
        expected_assets = archived["tables"]["datasheet_source_documents"]
        archived["tables"]["datasheet_source_documents"] = state["tables"]["datasheet_source_documents"]
        if hashlib.sha256(_reconciliation_bytes(archived)).hexdigest() != state_sha:
            raise ValueError("original state changed while archiving; primary unchanged")
        pending = {**quality, "original_source_kind": "related_primary_reconciliation_incomplete",
            "source_reconciliation": {"status": "pending", "previous_pdf_sha256": expected_current_pdf_sha256,
                "selected_pdf_sha256": sha, "archive_sha256": archive_sha, "expected_state_sha256": state_sha,
                "source_review_sha256": review_sha, "source_run": str(directory)}}
        _set_reconciliation_quality(service, supplier_part, expected_document=expected_current_pdf_sha256,
            expected_provenance=expected_current_pdf_sha256, quality=pending)
        if primary_source_registry(service.read_primary_source_evidence(supplier_part, limit=200)):
            raise ValueError("pending source-consumption guard did not hold")
        store = KnowledgeStore(service.config)
        try:
            store.ingest_run(directory, supplier_part, product_name=source_review["model"],
                pdf_path=directory / "source.pdf", pdf_url=source_url, replace=True)
        finally:
            if store._milvus is not None: store._milvus.close()
        old_ids = {row["id"] for row in state["milvus_rows"]}
        _verify_reconciled_source(service, supplier_part, sha, str(directory), entries,
            _reconciliation_catalog(state), old_ids, expected_assets)
        # Confirm the official reader's actual original bodies while the source
        # is still denied. No interpreted chunk or preview becomes an authority.
        preview = service.read_primary_source_evidence(supplier_part, limit=200)
        native_rows = [r for r in preview.get("evidence", []) if r.get("provenance") == "original_pdf_native"]
        actual_pages = {r.get("page_idx"): r for r in native_rows}
        if (len(native_rows) != pages or set(actual_pages) != set(range(pages))
                or any(actual_pages[i].get("document_sha256") != sha
                    or actual_pages[i].get("source_ref") != f"pdf:{sha}:page:{i + 1}"
                    or actual_pages[i].get("content") != e["content"].partition("\n")[2]
                    for i, e in enumerate(entries))):
            raise ValueError("new original-source body readback differs while pending")
        final_quality = {"method": "native_pdf_text", "qualification": "text_only", "pdf_sha256": sha,
            "source_url": source_url, "total_pages": pages, "text_pages": len(entries),
            "missing_text_pages": missing, "visual_pages": visual, "table_interpretation": "unverified",
            "visual_interpretation": "unverified", "source_document_type": "manufacturer_datasheet",
            "acquisition": source_review, "source_reconciliation": {**pending["source_reconciliation"], "status": "complete"}}
        _set_reconciliation_quality(service, supplier_part, expected_document=sha,
            expected_provenance=expected_current_pdf_sha256, quality=final_quality)
        try:
            _verify_reconciled_source(service, supplier_part, sha, str(directory), entries,
                _reconciliation_catalog(state), old_ids, expected_assets)
            bundle = service.read_primary_source_evidence(supplier_part, limit=200)
            registry = primary_source_registry(bundle)
            if (set(registry) != {f"pdf:{sha}:page:{p}" for p in range(1, pages + 1)}
                    or any(v.get("document_sha256") != sha or v["source_kind"] != "datasheet"
                           or v["provenance"] != "original_pdf_native" for v in registry.values())):
                raise ValueError("new original-source registry readback differs")
        except BaseException:
            # Revoke final eligibility if the post-commit readback fails.
            _set_reconciliation_quality(service, supplier_part, expected_document=sha,
                expected_provenance=sha, quality={**pending, "pdf_sha256": sha})
            raise
        return {**result, "status": "source_corrected_native_text_only", "registry_pages": len(registry)}


def restore_cached_acquisition(service: Any, supplier_part: str) -> dict[str, Any]:
    """Fill omitted URLs only from a same-hash, exact-ID acquisition record."""
    from .preparation import IDENTITY, RUNTIME_ROOT
    if not IDENTITY.fullmatch(supplier_part):
        raise ValueError("valid identity required")
    directory = RUNTIME_ROOT / "runs" / supplier_part
    metadata_path = directory / "acquisition.json"
    pdfs = list(directory.glob("*.pdf"))
    if not metadata_path.is_file() or len(pdfs) != 1:
        return {"id": supplier_part, "status": "acquisition_unavailable"}
    acquisition = json.loads(metadata_path.read_text())
    url = acquisition.get("pdf_url")
    if acquisition.get("product_code") != supplier_part or not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return {"id": supplier_part, "status": "acquisition_unverified"}
    sha = hashlib.sha256(pdfs[0].read_bytes()).hexdigest()
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(dictionary=True)
        cur.execute("SELECT pdf_sha256,pdf_url FROM datasheet_documents WHERE lichuang_id=%s FOR UPDATE", (supplier_part,))
        row = cur.fetchone()
        if not row or row["pdf_sha256"] != sha:
            return {"id": supplier_part, "status": "source_hash_mismatch"}
        if row["pdf_url"] and row["pdf_url"] != url:
            return {"id": supplier_part, "status": "existing_url_preserved"}
        cur.execute("SELECT details_json,pdf_sha256 FROM datasheet_ingestion_provenance WHERE lichuang_id=%s FOR UPDATE", (supplier_part,))
        provenance = cur.fetchone()
        if provenance:
            if provenance["pdf_sha256"] != sha:
                raise ValueError("provenance PDF hash mismatch")
            quality = provenance["details_json"]
            quality = json.loads(quality) if isinstance(quality, str) else quality
            quality["source_url"] = quality.get("source_url") or url
            quality["acquisition"] = quality.get("acquisition") or acquisition
            cur.execute("UPDATE datasheet_ingestion_provenance SET source_url=COALESCE(source_url,%s),details_json=%s WHERE lichuang_id=%s AND pdf_sha256=%s", (url, json.dumps(quality, ensure_ascii=False), supplier_part, sha))
        cur.execute("UPDATE datasheet_documents SET pdf_url=COALESCE(pdf_url,%s) WHERE lichuang_id=%s AND pdf_sha256=%s", (url, supplier_part, sha))
        cn.commit()
        return {"id": supplier_part, "status": "source_metadata_restored", "source_url": url, "pdf_sha256": sha}
    except BaseException:
        cn.rollback()
        raise
    finally:
        cn.close()


__all__=["prepare_native_pdf","reconcile_wrong_primary_pdf","ensure_native_schema","PROVENANCE_DDL","restore_cached_acquisition"]
