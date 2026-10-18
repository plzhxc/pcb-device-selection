"""Store reviewed manufacturer web evidence with original bytes and provenance."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .store import KnowledgeStore

SOURCE_DDL = """CREATE TABLE IF NOT EXISTS datasheet_source_documents (
 lichuang_id VARCHAR(64) NOT NULL,
 source_sha256 CHAR(64) NOT NULL,
 source_url VARCHAR(2048) NOT NULL,
 source_mime VARCHAR(100) NOT NULL,
 source_bytes LONGBLOB NOT NULL,
 is_primary BOOLEAN NOT NULL DEFAULT FALSE,
 quality_json JSON NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY (lichuang_id,source_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def prepare_manufacturer_web(service: Any, supplier_part: str, *, product_name: str,
                             html_path: str | Path, source_url: str,
                             entries: list[dict[str, Any]],
                             images: list[dict[str, Any]],
                             quality: dict[str, Any]) -> dict[str, Any]:
    with service.preparation_lock(supplier_part):
        return _prepare_manufacturer_web(service,supplier_part,product_name=product_name,
            html_path=html_path,source_url=source_url,entries=entries,images=images,quality=quality)


def _prepare_manufacturer_web(service: Any, supplier_part: str, *, product_name: str,
                              html_path: str | Path, source_url: str,
                              entries: list[dict[str, Any]], images: list[dict[str, Any]],
                              quality: dict[str, Any]) -> dict[str, Any]:
    """Index caller-reviewed excerpts/images; never infer model or pin facts.

    Every image must include a local path and its original URL. Entries are
    reviewed transcriptions, not generated application circuit instructions.
    A webpage is explicitly not labeled as an original PDF datasheet.
    """
    from .preparation import IDENTITY, RUNTIME_ROOT
    if not IDENTITY.fullmatch(supplier_part) or not source_url.startswith(("http://", "https://")):
        raise ValueError("valid identity and manufacturer URL required")
    if not entries or quality.get("qualification") != "manufacturer_web_excerpt":
        raise ValueError("reviewed entries and explicit web-excerpt qualification required")
    current=service.knowledge(supplier_part,evidence_limit=1)
    if current["document"] or current["evidence"]:
        return {"id":supplier_part,"status":"existing_requires_review"}
    directory=RUNTIME_ROOT/"runs"/supplier_part/"manufacturer_web"
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    (directory/"optimized").mkdir(mode=0o700,exist_ok=True)
    (directory/"images").mkdir(mode=0o700,exist_ok=True)
    html=Path(html_path).read_bytes()
    source_hash=hashlib.sha256(html).hexdigest()
    quality={**quality,"source_url":source_url,"document_sha256":source_hash,
             "method":"manufacturer_web_capture","original_pdf":False}
    assets=[(source_hash,source_url,"text/html",html,True)]
    for row in images:
        path=Path(row["path"]);data=path.read_bytes()
        if not row["source_url"].startswith(("http://","https://")):
            raise ValueError("image source URL required")
        assets.append((hashlib.sha256(data).hexdigest(),row["source_url"],"image/png",data,False))
        target=directory/"images"/path.name
        if target.exists() and target.read_bytes()!=data:
            raise ValueError("existing source image differs")
        if not target.exists():
            shutil.copyfile(path,target);target.chmod(0o600)
    content=directory/"optimized"/f"opti_{supplier_part}_content_list.json"
    if content.exists() and json.loads(content.read_text())!=entries:
        raise ValueError("existing reviewed web entries differ")
    content.write_text(json.dumps(entries,ensure_ascii=False,indent=2));content.chmod(0o600)
    cn=service._mysql("datasheet_assets")
    try:
        cur=cn.cursor();cur.execute(SOURCE_DDL)
        for sha,url,mime,data,primary in assets:
            cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (supplier_part,sha,url,mime,data,primary,json.dumps(quality,ensure_ascii=False)))
        cn.commit()
    finally:
        cn.close()
    store=KnowledgeStore(service.config)
    try:
        result=store.ingest_run(directory,supplier_part,product_name=product_name,replace=False)
    finally:
        if store._milvus is not None:store._milvus.close()
    return {"id":supplier_part,"status":"ingested_manufacturer_web_excerpt", "quality":quality,"ingest":result}


__all__=["prepare_manufacturer_web","SOURCE_DDL","decorate_retained_web_chunks"]


def decorate_retained_web_chunks(metadata: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep older web excerpts attached to their own source after first-PDF selection.

    The original HTML and Milvus rows remain unchanged. The explicit ingestion
    receipt names each retained chunk and its exact content digest; it grants no
    PDF page, electrical interpretation, or manufacturer variant equivalence.
    """
    retained = (metadata.get("source_quality") or {}).get("retained_web_chunks", [])
    if not retained:
        return rows
    assets = {a.get("source_sha256"): a for a in metadata.get("source_assets", [])}
    index = {}
    for item in retained:
        asset = assets.get(item.get("document_sha256"))
        if (type(item.get("id")) is not int or item["id"] in index or not asset
                or asset.get("source_mime") != "text/html"
                or (asset.get("quality") or {}).get("qualification") != "manufacturer_web_excerpt"
                or item.get("source_url") != asset.get("source_url")):
            raise ValueError("retained web chunk provenance is invalid")
        index[item["id"]] = item
    for row in rows:
        item = index.get(row.get("id"))
        if item is None:
            continue
        if hashlib.sha256(row["content"].encode()).hexdigest() != item.get("content_sha256"):
            raise ValueError("retained web chunk content changed")
        sha = item["document_sha256"]
        row.update(document_sha256=sha, page_idx=None, locator="visible_text",
            source_ref=f"manufacturer_page:{sha}:visible_text", source_kind="manufacturer_page",
            source_url=item["source_url"], provenance="retained_manufacturer_web_excerpt",
            source_quality=assets[sha]["quality"])
    return rows
