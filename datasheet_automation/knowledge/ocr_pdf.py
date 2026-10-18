"""Evidence-preserving OCR fallback through the existing MinerU boundary."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .native_pdf import ensure_native_schema
from .store import KnowledgeStore


def prepare_ocr_pdf(service: Any, supplier_part: str, *, product_name: str,
                    pdf_path: str | Path, source_url: str | None = None,
                    timeout_s: int = 1200,
                    source_provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    with service.preparation_lock(supplier_part):
        return _prepare_ocr_pdf(service,supplier_part,product_name=product_name,
            pdf_path=pdf_path,source_url=source_url,timeout_s=timeout_s,
            source_provenance=source_provenance)


def _prepare_ocr_pdf(service: Any, supplier_part: str, *, product_name: str,
                     pdf_path: str | Path, source_url: str | None,
                     timeout_s: int, source_provenance: dict[str, Any] | None) -> dict[str, Any]:
    """Index raw OCR text with qualification=ocr_only; never trust it as reviewed.

    This does not invoke an LLM to complete missing cells or describe diagrams.
    Original PDF bytes remain the authority for later visual verification.
    """
    from .preparation import IDENTITY,RUNTIME_ROOT
    from datasheet_automation.parsing.protocol import MinerURequest
    from datasheet_automation.parsing.transaction import parse_mineru
    if not IDENTITY.fullmatch(supplier_part):raise ValueError("valid identity required")
    old=service.knowledge(supplier_part,evidence_limit=1)
    if old["document"] or old["evidence"]:
        return {"id":supplier_part,"status":"existing_requires_review"}
    pdf=Path(pdf_path).resolve();data=pdf.read_bytes()
    if not data.startswith(b"%PDF") or len(data)>60*1024*1024:raise ValueError("invalid source PDF")
    directory=RUNTIME_ROOT/"runs"/supplier_part/"mineru_ocr"
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    raw=parse_mineru(MinerURequest(pdf_path=pdf,output_dir=directory/"parsed",method="ocr",
        lang="ch",table=True,formula=False,timeout_s=timeout_s))
    items=json.loads(raw.content_list_json.read_text());entries=[];seen_pages=set()
    for row in items:
        page=row.get("page_idx")
        if type(page) is not int or page<0:continue
        text=row.get("text") if row.get("type")=="text" else row.get("table_body")
        if not isinstance(text,str) or not text.strip():continue
        seen_pages.add(page+1)
        entries.append({"type":"text","title":f"{product_name}: unverified OCR page {page+1}",
            "page_idx":page,"content":"[mineru_ocr: unverified transcription; verify labels, numeric values and table structure visually against original PDF]\n"+text})
    if not entries:return {"id":supplier_part,"status":"ocr_produced_no_text"}
    import pypdfium2 as pdfium
    from .native_pdf import _PDFIUM_LOCK
    with _PDFIUM_LOCK:
        document=pdfium.PdfDocument(data)
        try:total=len(document)
        finally:document.close()
    quality={"method":"mineru_ocr","qualification":"ocr_only","pdf_sha256":hashlib.sha256(data).hexdigest(),
        "source_url":source_url,"total_pages":total,"text_pages":len(seen_pages),
        "missing_text_pages":sorted(set(range(1,total+1))-seen_pages),"visual_pages":list(range(1,total+1)),
        "table_interpretation":"unverified","visual_interpretation":"unverified","llm_completion":False}
    if source_provenance:
        quality["acquisition"] = dict(source_provenance)
        for key in ("identity_qualification", "original_source_kind"):
            if key in source_provenance:
                quality[key] = source_provenance[key]
    optimized=directory/"optimized";optimized.mkdir(mode=0o700,exist_ok=True)
    path=optimized/f"opti_{supplier_part}_content_list.json"
    if path.exists() and json.loads(path.read_text())!=entries:raise ValueError("OCR entries changed; explicit review required")
    path.write_text(json.dumps(entries,ensure_ascii=False,indent=2));path.chmod(0o600)
    ensure_native_schema(service);cn=service._mysql("datasheet_assets")
    try:
        cur=cn.cursor();cur.execute("SELECT pdf_sha256,method FROM datasheet_ingestion_provenance WHERE lichuang_id=%s",(supplier_part,));existing=cur.fetchone()
        if existing and existing!=(quality["pdf_sha256"],"mineru_ocr"):raise ValueError("existing provenance requires explicit review")
        if not existing:cur.execute("INSERT INTO datasheet_ingestion_provenance (lichuang_id,pdf_sha256,method,qualification,total_pages,text_pages,missing_text_pages,visual_pages,source_url,details_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (supplier_part,quality["pdf_sha256"],"mineru_ocr","ocr_only",total,len(seen_pages),json.dumps(quality["missing_text_pages"]),json.dumps(quality["visual_pages"]),source_url,json.dumps(quality)))
        cn.commit()
    finally:cn.close()
    store=KnowledgeStore(service.config)
    try:result=store.ingest_run(directory,supplier_part,product_name=product_name,pdf_path=pdf,pdf_url=source_url,replace=False)
    finally:
        if store._milvus is not None:store._milvus.close()
    return {"id":supplier_part,"status":"ingested_ocr_only","quality":quality,"ingest":result}


__all__=["prepare_ocr_pdf"]
