"""数据手册库（Milvus）只读访问。

用的是 Milvus 的 REST 接口（`/v2/vectordb/...`），这样本机不需要装 pymilvus。
连接参数与 chenxin 的 requirements_studio 保持一致：

* 地址：`PCBAGENT_MILVUS_URL`，默认 `http://127.0.0.1:19530`
* 库：`DataSheetBaseV2` 优先，回退 `DataSheetBase`
* 集合：`DataSheet`，按 `lichuang_id`（立创 C 编号）过滤
* 稀疏检索字段：`content_sparse_vector`，metric 为 BM25

实测两个库的覆盖面不同：`DataSheetBaseV2` 有 12848 条，`DataSheetBase` 有
669212 条；不少器件只在后者里（例如 LM358 的 C5252902、AMS1117 的 C347222）。
所以必须按 V2 → Base 的顺序回退。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any, Iterable, Sequence
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:19530"
DEFAULT_DATABASES = ("DataSheetBaseV2", "DataSheetBase")
COLLECTION = "DataSheet"

_FIELDS = ["id", "lichuang_id", "title", "content", "content_type"]


@dataclass(frozen=True, slots=True)
class Span:
    """手册里的一段文本。"""

    span_id: str
    c_number: str
    title: str
    content: str
    database: str
    page: int | None
    mode: str  # scalar | bm25

    def as_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "c_number": self.c_number,
            "title": self.title,
            "page": self.page,
            "database": self.database,
            "mode": self.mode,
            "locator": f"{self.database}.{COLLECTION}:{self.span_id}",
        }


class MilvusUnavailable(RuntimeError):
    """连不上手册库。"""


class DatasheetLibrary:
    """按立创 C 编号读手册片段。"""

    def __init__(
        self,
        url: str | None = None,
        *,
        databases: Sequence[str] = DEFAULT_DATABASES,
        timeout: float = 8.0,
        max_spans: int = 6,
    ) -> None:
        self.url = (url or os.environ.get("PCBAGENT_MILVUS_URL") or DEFAULT_URL).rstrip("/")
        self.databases = tuple(databases)
        self.timeout = timeout
        self.max_spans = max_spans
        self._cache: dict[tuple[str, str], list[Span]] = {}
        self.last_error = ""

    # ------------------------------------------------------------------ 底层
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise MilvusUnavailable(self.last_error) from exc

    def available(self) -> dict[str, Any]:
        try:
            result = self._post("/v2/vectordb/databases/list", {})
        except MilvusUnavailable as exc:
            return {"ok": False, "url": self.url, "error": str(exc)}
        return {
            "ok": bool(result.get("code") == 0),
            "url": self.url,
            "databases": result.get("data"),
            "collection": COLLECTION,
        }

    def _rows(
        self,
        database: str,
        *,
        c_number: str,
        query_text: str = "",
        limit: int,
    ) -> tuple[list[dict[str, Any]], str]:
        expr = "lichuang_id == " + json.dumps(c_number)
        if query_text:
            payload = {
                "dbName": database,
                "collectionName": COLLECTION,
                "data": [query_text],
                "annsField": "content_sparse_vector",
                "filter": expr,
                "searchParams": {"metricType": "BM25"},
                "outputFields": _FIELDS,
                "limit": limit,
            }
            result = self._post("/v2/vectordb/entities/search", payload)
            rows = []
            for hit in result.get("data") or []:
                row = dict(hit)
                row.setdefault("id", hit.get("id"))
                rows.append(row)
            return rows, "bm25"
        payload = {
            "dbName": database,
            "collectionName": COLLECTION,
            "filter": expr,
            "outputFields": _FIELDS,
            "limit": limit,
        }
        result = self._post("/v2/vectordb/entities/query", payload)
        return [dict(row) for row in (result.get("data") or [])], "scalar"

    # ------------------------------------------------------------------ 对外
    def spans(self, c_number: str, *, query_text: str = "", limit: int | None = None) -> list[Span]:
        """取某个器件的片段；先查 V2，查不到再回退旧库。"""

        c_number = str(c_number or "").strip().upper()
        if not c_number:
            return []
        key = (c_number, f"{query_text}|{limit}")
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        want = int(limit or self.max_spans)
        collected: list[Span] = []
        seen: set[str] = set()
        for database in self.databases:
            try:
                rows, mode = self._rows(
                    database, c_number=c_number, query_text=query_text, limit=want
                )
            except MilvusUnavailable:
                continue
            for row in rows:
                span_id = str(row.get("id"))
                if span_id in seen:
                    continue
                content = str(row.get("content") or "")
                if not content.strip():
                    continue
                seen.add(span_id)
                page = row.get("page_idx")
                collected.append(
                    Span(
                        span_id=span_id,
                        c_number=str(row.get("lichuang_id") or c_number),
                        title=str(row.get("title") or ""),
                        content=content,
                        database=database,
                        page=(int(page) + 1) if isinstance(page, int) else None,
                        mode=mode,
                    )
                )
            if collected:
                break
        self._cache[key] = collected
        return collected

    def identity(self, c_number: str) -> dict[str, Any]:
        """某个 C 编号在手册库里有没有、在哪个库。"""

        c_number = str(c_number or "").strip().upper()
        found: dict[str, Any] = {"c_number": c_number, "databases": {}}
        for database in self.databases:
            try:
                rows, _ = self._rows(database, c_number=c_number, limit=1)
            except MilvusUnavailable:
                rows = []
            found["databases"][database] = len(rows)
        found["ok"] = any(v > 0 for v in found["databases"].values())
        return found
