"""Milvus + MySQL writer for the datasheet knowledge base (schema v2)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import mysql.connector
from pymilvus import CollectionSchema, DataType, FieldSchema, Function, FunctionType, MilvusClient

from datasheet_automation.core.app_logging import get_logger

log = get_logger()

CONTENT_LIMIT = 20_000          # Milvus VARCHAR max for ``content`` (bytes ≈ chars for CJK ×3 → we split by chars conservatively)
CONTENT_SPLIT = 6_000           # split long text entries into chunks of at most this many characters
PDF_LIMIT_BYTES = 60 * 1024 * 1024  # under MySQL max_allowed_packet (64 MiB)
_DEVICE_ID = re.compile(r"[CS][0-9]+")


@dataclass(frozen=True)
class KnowledgeStoreConfig:
    """Connection settings.  Env: ``PCBAGENT_MILVUS_URL`` / ``PCBAGENT_MILVUS_TOKEN`` and ``PCBAGENT_MYSQL_*``."""

    milvus_url: str = "http://127.0.0.1:19530"
    milvus_token: str | None = None
    milvus_database: str = "DataSheetBaseV2"
    milvus_collection: str = "DataSheet"
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3307
    mysql_user: str = "root"
    mysql_password: str = ""
    mysql_database: str = "datasheet_assets"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "KnowledgeStoreConfig":
        env = os.environ if environ is None else environ
        return cls(
            milvus_url=env.get("PCBAGENT_MILVUS_URL", cls.milvus_url).strip() or cls.milvus_url,
            milvus_token=env.get("PCBAGENT_MILVUS_TOKEN", "").strip() or None,
            milvus_database=env.get("PCBAGENT_MILVUS_DATABASE", cls.milvus_database).strip() or cls.milvus_database,
            milvus_collection=env.get("PCBAGENT_MILVUS_COLLECTION", cls.milvus_collection).strip() or cls.milvus_collection,
            mysql_host=env.get("PCBAGENT_MYSQL_HOST", cls.mysql_host).strip() or cls.mysql_host,
            mysql_port=int(env.get("PCBAGENT_MYSQL_PORT", cls.mysql_port)),
            mysql_user=env.get("PCBAGENT_MYSQL_USER", cls.mysql_user).strip() or cls.mysql_user,
            mysql_password=env.get("PCBAGENT_MYSQL_PASSWORD", ""),
            mysql_database=env.get("PCBAGENT_ASSETS_MYSQL_DATABASE", cls.mysql_database).strip() or cls.mysql_database,
        )


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
def milvus_schema() -> CollectionSchema:
    fields = [
        FieldSchema("id", DataType.INT64, is_primary=True, auto_id=True),
        FieldSchema("lichuang_id", DataType.VARCHAR, max_length=64),
        FieldSchema("product_name", DataType.VARCHAR, max_length=256),
        FieldSchema("content_type", DataType.VARCHAR, max_length=32),      # text | table | image
        FieldSchema("category", DataType.VARCHAR, max_length=64),          # VLM class for image/table, "" for text
        FieldSchema("title", DataType.VARCHAR, max_length=2048),
        FieldSchema("content", DataType.VARCHAR, max_length=CONTENT_LIMIT, enable_analyzer=True,
                    analyzer_params={"type": "chinese"}),
        FieldSchema("content_sparse_vector", DataType.SPARSE_FLOAT_VECTOR),
        FieldSchema("section_path", DataType.VARCHAR, max_length=2048),
        FieldSchema("page_idx", DataType.INT32),
        FieldSchema("parent_entry_idx", DataType.INT32),
        FieldSchema("chunk_idx", DataType.INT32),
        FieldSchema("chunk_total", DataType.INT32),
        FieldSchema("image_id", DataType.VARCHAR, max_length=64),          # sha256 of the image bytes, "" for text
        FieldSchema("img_path", DataType.VARCHAR, max_length=512),         # name inside the run's images/ dir, for provenance only
        FieldSchema("source_run", DataType.VARCHAR, max_length=512),
        FieldSchema("ingested_at", DataType.VARCHAR, max_length=32),
    ]
    schema = CollectionSchema(fields, description="datasheet chunks v2: every image row carries a VLM description; bytes live in MySQL datasheet_assets")
    schema.add_function(
        Function(
            name="content_bm25",
            function_type=FunctionType.BM25,
            input_field_names=["content"],
            output_field_names=["content_sparse_vector"],
        )
    )
    return schema


MYSQL_DDL = (
    """CREATE TABLE IF NOT EXISTS datasheet_documents (
  lichuang_id      VARCHAR(64)  NOT NULL,
  product_name     VARCHAR(256) NOT NULL DEFAULT '',
  pdf_sha256       CHAR(64)     DEFAULT NULL,
  pdf_bytes        LONGBLOB     DEFAULT NULL,
  pdf_size_bytes   INT UNSIGNED DEFAULT NULL,
  pdf_url          VARCHAR(1024) DEFAULT NULL,
  source_run       VARCHAR(512) NOT NULL,
  chunk_count      INT UNSIGNED NOT NULL DEFAULT 0,
  image_count      INT UNSIGNED NOT NULL DEFAULT 0,
  ingested_at      TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (lichuang_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS datasheet_images (
  image_id      CHAR(64)      NOT NULL COMMENT 'sha256 of bytes',
  lichuang_id   VARCHAR(64)   NOT NULL,
  img_path      VARCHAR(512)  NOT NULL,
  mime          VARCHAR(64)   NOT NULL,
  width         INT UNSIGNED  DEFAULT NULL,
  height        INT UNSIGNED  DEFAULT NULL,
  size_bytes    INT UNSIGNED  NOT NULL,
  page_idx      INT           DEFAULT NULL,
  title         VARCHAR(2048) NOT NULL DEFAULT '',
  category      VARCHAR(64)   NOT NULL DEFAULT '',
  description   MEDIUMTEXT,
  bytes         MEDIUMBLOB    NOT NULL,
  source_run    VARCHAR(512)  NOT NULL,
  created_at    TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (image_id, lichuang_id),
  KEY idx_lichuang (lichuang_id),
  CONSTRAINT fk_images_document FOREIGN KEY (lichuang_id) REFERENCES datasheet_documents (lichuang_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _mime(name: str) -> str:
    ext = Path(name).suffix.lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "application/octet-stream")


def _split_text(text: str, limit: int = CONTENT_SPLIT) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    parts: list[str] = []
    for para in re.split(r"\n{2,}", text):
        if parts and len(parts[-1]) + len(para) + 2 <= limit:
            parts[-1] = f"{parts[-1]}\n\n{para}"
        elif len(para) <= limit:
            parts.append(para)
        else:
            parts.extend(para[i : i + limit] for i in range(0, len(para), limit))
    return [p for p in parts if p.strip()]


def _clip(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_content_list(run_dir: Path, lichuang_id: str) -> Path:
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run directory does not exist: {run_dir}")
    candidates = [
        run_dir / "optimized" / f"opti_{lichuang_id}_content_list.json",
        *sorted(run_dir.rglob(f"opti_{lichuang_id}_content_list.json")),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f"no optimized content list for {lichuang_id} under {run_dir}")


def _resolve_image(run_dir: Path, entry: Mapping[str, Any]) -> Path | None:
    for key in ("absolute_img_path", "image_path"):
        raw = str(entry.get(key) or "").strip()
        if raw and Path(raw).is_file():
            return Path(raw)
    rel = str(entry.get("img_path") or "").strip()
    if not rel:
        return None
    for base in (run_dir, run_dir / "optimized", *sorted(p for p in run_dir.iterdir() if p.is_dir())):
        candidate = base / rel
        if candidate.is_file():
            return candidate
        candidate = base / "images" / Path(rel).name
        if candidate.is_file():
            return candidate
    return None


def _section_path(entries: list[Mapping[str, Any]], index: int) -> str:
    """Nearest preceding text title (the optimizer emits one entry per section)."""

    for cursor in range(index, -1, -1):
        title = str(entries[cursor].get("title") or "").strip()
        if title and entries[cursor].get("type") == "text":
            return _clip(title, 2000)
    return ""


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class KnowledgeStore:
    def __init__(self, config: KnowledgeStoreConfig | None = None) -> None:
        self.config = config or KnowledgeStoreConfig.from_env()
        self._milvus: MilvusClient | None = None

    # -- connections -------------------------------------------------------
    @property
    def milvus(self) -> MilvusClient:
        if self._milvus is None:
            client = MilvusClient(uri=self.config.milvus_url, token=self.config.milvus_token or "", alias="knowledge_store_" + uuid4().hex)
            if self.config.milvus_database not in client.list_databases():
                client.create_database(self.config.milvus_database)
            client.using_database(self.config.milvus_database)
            self._milvus = client
        return self._milvus

    def _mysql(self, database: str | None = None) -> Any:
        cfg = self.config
        return mysql.connector.connect(
            host=cfg.mysql_host, port=cfg.mysql_port, user=cfg.mysql_user, password=cfg.mysql_password,
            database=database, charset="utf8mb4", autocommit=False,
        )

    # -- schema ------------------------------------------------------------
    def ensure_schema(self) -> dict[str, Any]:
        """Create the Milvus collection (+ BM25 index) and MySQL tables if missing.  Idempotent."""

        created: dict[str, Any] = {"milvus_collection": False, "mysql_tables": False}
        client = self.milvus
        name = self.config.milvus_collection
        if not client.has_collection(name):
            index_params = client.prepare_index_params()
            index_params.add_index(field_name="content_sparse_vector", index_type="SPARSE_INVERTED_INDEX",
                                   metric_type="BM25", params={"inverted_index_algo": "DAAT_MAXSCORE"})
            index_params.add_index(field_name="lichuang_id", index_type="INVERTED")
            index_params.add_index(field_name="content_type", index_type="INVERTED")
            client.create_collection(collection_name=name, schema=milvus_schema(), index_params=index_params)
            created["milvus_collection"] = True
        client.load_collection(name)

        cn = self._mysql()
        try:
            cur = cn.cursor()
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.config.mysql_database}` DEFAULT CHARSET utf8mb4")
            cur.execute(f"USE `{self.config.mysql_database}`")
            cur.execute("SHOW TABLES")
            before = {row[0] for row in cur.fetchall()}
            for ddl in MYSQL_DDL:
                cur.execute(ddl)
            cn.commit()
            created["mysql_tables"] = not {"datasheet_documents", "datasheet_images"} <= before
        finally:
            cn.close()
        return created

    # -- ingest ------------------------------------------------------------
    def ingest_run(
        self,
        run_dir: str | Path,
        lichuang_id: str,
        *,
        product_name: str = "",
        pdf_path: str | Path | None = None,
        pdf_url: str | None = None,
        replace: bool = True,
    ) -> dict[str, Any]:
        """Load one finished run directory for ``lichuang_id`` into Milvus + MySQL.

        Validate inputs before writes. With ``replace=True``, verify new chunks
        before retiring old IDs. Insertion failures roll back MySQL and remove
        acknowledged new chunks; old chunks are never deleted on that path.
        """

        run_dir = Path(run_dir).resolve()
        lichuang_id = lichuang_id.strip().upper()
        if not _DEVICE_ID.fullmatch(lichuang_id) or len(lichuang_id) > 64:
            raise ValueError(f"lichuang_id must be a C-number or confirmed custom S-number, got {lichuang_id!r}")
        content_list = _find_content_list(run_dir, lichuang_id)
        entries = json.loads(content_list.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or not entries or not all(isinstance(e, Mapping) for e in entries):
            raise ValueError("optimized content list must be a nonempty list of objects")
        stamp = _now()
        source_run = _clip(str(run_dir), 500)

        rows: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        missing_images: list[str] = []
        empty_visuals: list[str] = []
        for index, entry in enumerate(entries):
            etype = str(entry.get("type") or "text")
            title = _clip(entry.get("title") or "", 2000)
            content = str(entry.get("content") or "")
            page = entry.get("page_idx")
            page_idx = int(page) if isinstance(page, int) else -1
            base = {
                "lichuang_id": lichuang_id,
                "product_name": _clip(product_name, 250),
                "title": title,
                "section_path": _section_path(entries, index),
                "page_idx": page_idx,
                "parent_entry_idx": index,
                "source_run": source_run,
                "ingested_at": stamp,
            }
            if etype in {"image", "chart", "table"}:
                image_id = ""
                img_rel = str(entry.get("img_path") or "").strip()
                source = _resolve_image(run_dir, entry)
                if source is not None:
                    data = source.read_bytes()
                    from PIL import Image

                    try:
                        with Image.open(io.BytesIO(data)) as decoded:
                            decoded.verify()
                        with Image.open(io.BytesIO(data)) as decoded:
                            decoded.load()
                            width, height = decoded.size
                    except Exception as exc:
                        raise ValueError(f"unreadable visual bytes: {source}") from exc
                    image_id = hashlib.sha256(data).hexdigest()
                    if entry.get("image_id") and entry["image_id"] != image_id:
                        raise ValueError(f"visual SHA256 mismatch: {source}")
                    images.append({
                        "image_id": image_id, "lichuang_id": lichuang_id, "img_path": _clip(img_rel or source.name, 500),
                        "mime": _mime(source.name), "width": width, "height": height, "size_bytes": len(data),
                        "page_idx": None if page_idx < 0 else page_idx, "title": title,
                        "category": _clip(entry.get("category") or "", 60), "description": content, "bytes": data,
                        "source_run": source_run,
                    })
                elif img_rel or etype in {"image", "chart"}:
                    missing_images.append(img_rel or f"entry_{index}")
                body = content.partition("description:\n")[2].strip() if "description:\n" in content else content.strip()
                if (not body or body.endswith("未执行视觉内容推断。") or body.endswith("未提供对应视觉条目。")
                        or body == img_rel or re.fullmatch(r"!?\[[^\]]*\]\([^)]+\)", body)
                        or re.fullmatch(r"\S+\.(?:png|jpe?g|webp|gif)", body, re.I)):
                    empty_visuals.append(img_rel or f"entry_{index}")
                if len(content.encode("utf-8")) > CONTENT_LIMIT:
                    raise ValueError(f"visual content exceeds Milvus byte limit at entry {index}; split it before ingestion")
                rows.append({
                    **base,
                    "content_type": "table" if etype == "table" else "image",
                    "category": _clip(entry.get("category") or "", 60),
                    "content": content,
                    "chunk_idx": 0, "chunk_total": 1,
                    "image_id": image_id, "img_path": _clip(img_rel, 500),
                })
                continue
            if etype != "text":
                raise ValueError(f"unsupported optimized entry type: {etype!r}")
            pieces = _split_text(content) or ([title] if title else [])
            for k, piece in enumerate(pieces):
                rows.append({
                    **base, "content_type": "text", "category": "", "content": _clip(piece, CONTENT_LIMIT - 1),
                    "chunk_idx": k, "chunk_total": len(pieces), "image_id": "", "img_path": "",
                })

        if not rows or missing_images or empty_visuals:
            raise ValueError(f"incomplete optimized run: chunks={len(rows)}, missing_images={missing_images}, visuals_without_description={empty_visuals}")
        # Milvus VARCHAR limits are bytes, not Python character counts.
        limits = {"lichuang_id": 64, "product_name": 256, "content_type": 32,
                  "category": 64, "title": 2048, "content": CONTENT_LIMIT,
                  "section_path": 2048, "image_id": 64, "img_path": 512,
                  "source_run": 512, "ingested_at": 32}
        for row in rows:
            for key, maximum in limits.items():
                if len(row[key].encode("utf-8")) > maximum:
                    raise ValueError(f"optimized {key} exceeds Milvus byte limit {maximum}")

        pdf_bytes = None
        pdf_sha = None
        pdf_size = None
        pdf_candidates = sorted(run_dir.glob("*.pdf"))
        if not pdf_path and len(pdf_candidates) > 1:
            raise ValueError("multiple source PDFs in run directory; supply pdf_path explicitly")
        pdf_file = Path(pdf_path) if pdf_path else next(iter(pdf_candidates), None)
        if pdf_path and not pdf_file.is_file():
            raise FileNotFoundError(f"source PDF does not exist: {pdf_file}")
        if pdf_file is not None and pdf_file.is_file():
            data = pdf_file.read_bytes()
            if not data.startswith(b"%PDF-"):
                raise ValueError(f"source is not a PDF: {pdf_file}")
            pdf_sha, pdf_size = hashlib.sha256(data).hexdigest(), len(data)
            if pdf_size > PDF_LIMIT_BYTES:
                raise ValueError(f"source PDF exceeds storage limit: {pdf_file}")
            pdf_bytes = data

        # Snapshot old IDs before writes; never delete by the broad device filter.
        client = self.milvus
        name = self.config.milvus_collection
        old_ids: list[int] = []
        if replace:
            iterator = client.query_iterator(collection_name=name, filter=f'lichuang_id == "{lichuang_id}"',
                                             output_fields=["id"], batch_size=1000, consistency_level="Strong")
            try:
                while batch := iterator.next():
                    old_ids.extend(row["id"] for row in batch)
            finally:
                iterator.close()
        new_ids: list[int] = []
        cn = self._mysql(self.config.mysql_database)
        try:
            cur = cn.cursor()
            if replace:
                cur.execute("DELETE FROM datasheet_images WHERE lichuang_id = %s", (lichuang_id,))
            cur.execute(
                "INSERT INTO datasheet_documents (lichuang_id, product_name, pdf_sha256, pdf_bytes, pdf_size_bytes, pdf_url, source_run, chunk_count, image_count) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE product_name=VALUES(product_name), pdf_sha256=COALESCE(VALUES(pdf_sha256),pdf_sha256), "
                "pdf_bytes=COALESCE(VALUES(pdf_bytes),pdf_bytes), pdf_size_bytes=COALESCE(VALUES(pdf_size_bytes),pdf_size_bytes), pdf_url=COALESCE(VALUES(pdf_url),pdf_url), source_run=VALUES(source_run), "
                "chunk_count=VALUES(chunk_count), image_count=VALUES(image_count)",
                (lichuang_id, _clip(product_name, 250), pdf_sha, pdf_bytes, pdf_size, pdf_url, source_run, len(rows), len({i["image_id"] for i in images})),
            )
            seen: set[str] = set()
            for image in images:
                if image["image_id"] in seen:
                    continue
                seen.add(image["image_id"])
                cur.execute(
                    "INSERT INTO datasheet_images (image_id, lichuang_id, img_path, mime, width, height, size_bytes, page_idx, title, category, description, bytes, source_run) "
                    "VALUES (%(image_id)s,%(lichuang_id)s,%(img_path)s,%(mime)s,%(width)s,%(height)s,%(size_bytes)s,%(page_idx)s,%(title)s,%(category)s,%(description)s,%(bytes)s,%(source_run)s) "
                    "ON DUPLICATE KEY UPDATE img_path=VALUES(img_path), title=VALUES(title), category=VALUES(category), description=VALUES(description), source_run=VALUES(source_run)",
                    image,
                )
            for start in range(0, len(rows), 200):
                batch = rows[start : start + 200]
                result = client.insert(collection_name=name, data=batch)
                ids = list(result.get("ids") or [])
                new_ids.extend(ids)
                if result.get("insert_count") != len(batch) or len(ids) != len(batch):
                    raise RuntimeError("Milvus did not acknowledge every inserted chunk")
            # Strong reads confirm acknowledged WAL writes without forcing a
            # flush per device, which fragments segments and trips server quotas.
            for start in range(0, len(new_ids), 200):
                ids = new_ids[start : start + 200]
                count = client.query(collection_name=name, filter=f"id in {json.dumps(ids)}",
                                     output_fields=["count(*)"], consistency_level="Strong")
                if not count or count[0].get("count(*)") != len(ids):
                    raise RuntimeError("Milvus read-back did not confirm every new chunk")
            cn.commit()
        except Exception:
            cn.rollback()
            # Insert/acknowledgement failures may leave unknown new IDs; retain
            # the original data and fail explicitly instead of claiming success.
            for start in range(0, len(new_ids), 200):
                try:
                    client.delete(collection_name=name, ids=new_ids[start : start + 200])
                except Exception:
                    log.error("knowledge ingest | %s | failed to remove new chunks; reconciliation required", lichuang_id)
            raise
        finally:
            cn.close()
        for start in range(0, len(old_ids), 200):
            client.delete(collection_name=name, ids=old_ids[start : start + 200])
        if replace:
            count = client.query(collection_name=name, filter=f'lichuang_id == "{lichuang_id}"',
                                 output_fields=["count(*)"], consistency_level="Strong")
            if not count or count[0].get("count(*)") != len(rows):
                raise RuntimeError("replacement count mismatch; new data committed, reconcile retained chunks")
        inserted = len(new_ids)

        report = {
            "lichuang_id": lichuang_id,
            "product_name": product_name,
            "run_dir": str(run_dir),
            "content_list": str(content_list),
            "entries": len(entries),
            "milvus_rows": inserted,
            "rows_by_type": {t: sum(1 for r in rows if r["content_type"] == t) for t in ("text", "table", "image")},
            "images_stored": len({i["image_id"] for i in images}),
            "images_missing_file": missing_images,
            "visuals_without_description": empty_visuals,
            "pdf_stored": pdf_bytes is not None,
            "existing_pdf_preserved_if_missing": pdf_bytes is None,
            "pdf_size_bytes": pdf_size,
        }
        log.info("knowledge ingest | %s | rows=%d images=%d missing=%d undesc=%d", lichuang_id, inserted, len(images), len(missing_images), len(empty_visuals))
        return report

    # -- read-back ---------------------------------------------------------
    def devices(self) -> list[dict[str, Any]]:
        cn = self._mysql(self.config.mysql_database)
        try:
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT lichuang_id, product_name, chunk_count, image_count, pdf_size_bytes, ingested_at FROM datasheet_documents ORDER BY lichuang_id")
            return list(cur.fetchall())
        finally:
            cn.close()

    def search(self, query: str, *, lichuang_id: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        expr = f'lichuang_id == "{lichuang_id.strip().upper()}"' if lichuang_id else ""
        hits = self.milvus.search(
            collection_name=self.config.milvus_collection, data=[query], anns_field="content_sparse_vector",
            limit=limit, filter=expr, search_params={"metric_type": "BM25"},
            output_fields=["lichuang_id", "content_type", "category", "title", "content", "section_path", "page_idx", "image_id"],
        )
        return [{"score": h["distance"], **h["entity"]} for h in (hits[0] if hits else [])]

    def image(self, image_id: str) -> dict[str, Any] | None:
        cn = self._mysql(self.config.mysql_database)
        try:
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT image_id, lichuang_id, img_path, mime, width, height, size_bytes, page_idx, title, category, description, bytes FROM datasheet_images WHERE image_id = %s LIMIT 1", (image_id,))
            return cur.fetchone()
        finally:
            cn.close()


__all__ = ["KnowledgeStore", "KnowledgeStoreConfig", "MYSQL_DDL", "milvus_schema"]
