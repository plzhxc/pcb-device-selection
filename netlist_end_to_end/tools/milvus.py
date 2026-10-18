"""Framework-native Milvus datasheet knowledge tool."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from uuid import uuid4
from typing import Any, Protocol, runtime_checkable

from agent_framework.tool import BaseTool, ToolCapability, ToolOutput

from .config import KnowledgeConfig
from .sql import DeviceCatalogProvider


_C_NUMBER = re.compile(r"^[CS][0-9]{1,20}$", flags=re.IGNORECASE)
_OUTPUT_FIELDS = (
    "id",
    "lichuang_id",
    "content_type",
    "title",
    "content",
    "chunk_idx",
    "chunk_total",
    "section_path",
)

_PERIPHERAL_GUIDANCE_QUERY = (
    "power supply decoupling bypass typical application circuit feedback "
    "compensation startup enable reset clock interface protection layout "
    "unused pin connection recommendations"
)


@runtime_checkable
class DatasheetKnowledgeProvider(Protocol):
    name: str

    def search(
        self,
        query: str,
        *,
        by: str = "auto",
        limit: int = 12,
    ) -> dict[str, Any]: ...

    def status(self) -> dict[str, object]: ...


class MilvusDatasheetProvider:
    """Read full approved datasheet chunks from the configured collection."""

    name = "milvus_datasheet"

    def __init__(
        self,
        config: KnowledgeConfig,
        catalog: DeviceCatalogProvider,
    ) -> None:
        self.config = config
        self.catalog = catalog

    def _client(self) -> Any:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "Milvus knowledge tool requires pymilvus>=2.5,<2.6"
            ) from exc
        kwargs: dict[str, Any] = {
            "uri": self.config.milvus_url,
            "db_name": self.config.milvus_database,
            "timeout": self.config.milvus_timeout_s,
            "alias": "knowledge_read_" + uuid4().hex,
        }
        token = self.config.read_milvus_token()
        if token:
            kwargs["token"] = token
        return MilvusClient(**kwargs)

    @staticmethod
    def _close(client: Any) -> None:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def status(self) -> dict[str, object]:
        client: Any = None
        try:
            client = self._client()
            exists = bool(
                client.has_collection(
                    collection_name=self.config.milvus_collection,
                    timeout=self.config.milvus_timeout_s,
                )
            )
            return {
                "available": exists,
                "read_only": True,
                "database": self.config.milvus_database,
                "collection": self.config.milvus_collection,
                "code": "ok" if exists else "collection_missing",
            }
        except Exception as exc:
            return {
                "available": False,
                "read_only": True,
                "database": self.config.milvus_database,
                "collection": self.config.milvus_collection,
                "error": type(exc).__name__,
            }
        finally:
            if client is not None:
                self._close(client)

    def _resolve_c_numbers(self, query: str, by: str, limit: int) -> list[str]:
        if by == "c_number":
            if not _C_NUMBER.fullmatch(query):
                raise ValueError("device identity must match ^[CS][0-9]{1,20}$")
            return [query.upper()]
        rows = self.catalog.search_devices(
            query,
            limit=min(5, limit),
            include_pins=False,
            include_parameters=False,
        )
        result: list[str] = []
        for row in rows:
            value = str(row.get("supplier_part") or "").strip().upper()
            if _C_NUMBER.fullmatch(value) and value not in result:
                result.append(value)
        return result

    def search(
        self,
        query: str,
        *,
        by: str = "auto",
        limit: int = 12,
    ) -> dict[str, Any]:
        normalized = str(query).strip()
        if not normalized:
            raise ValueError("query must not be blank")
        if len(normalized) > 160:
            raise ValueError("query exceeds 160 characters")
        mode = str(by).strip().lower()
        if mode not in {"auto", "c_number", "name"}:
            raise ValueError("by must be auto, c_number, or name")
        if mode == "auto":
            mode = "c_number" if _C_NUMBER.fullmatch(normalized) else "name"
        if (
            isinstance(limit, bool)
            or not 1 <= int(limit) <= self.config.milvus_max_results
        ):
            raise ValueError(
                f"limit must be between 1 and {self.config.milvus_max_results}"
            )
        limit = int(limit)
        c_numbers = self._resolve_c_numbers(normalized, mode, limit)
        if not c_numbers:
            return {
                "query": normalized,
                "by": mode,
                "resolved_c_numbers": [],
                "total": 0,
                "items": [],
            }

        expression = (
            f"lichuang_id == {json.dumps(c_numbers[0])}"
            if len(c_numbers) == 1
            else "lichuang_id in "
            + json.dumps(c_numbers, ensure_ascii=True, separators=(",", ":"))
        )
        client: Any = None
        try:
            client = self._client()
            if not client.has_collection(
                collection_name=self.config.milvus_collection,
                timeout=self.config.milvus_timeout_s,
            ):
                raise RuntimeError(
                    f"Milvus collection {self.config.milvus_collection!r} is missing"
                )
            rows = client.query(
                collection_name=self.config.milvus_collection,
                filter=expression,
                output_fields=list(_OUTPUT_FIELDS),
                limit=limit,
                timeout=self.config.milvus_timeout_s,
            )
        finally:
            if client is not None:
                self._close(client)

        allowed = set(c_numbers)
        items = []
        for raw in rows or []:
            if not isinstance(raw, Mapping):
                continue
            actual = str(raw.get("lichuang_id") or "").strip().upper()
            if actual not in allowed:
                continue
            items.append(
                {field: raw.get(field) for field in _OUTPUT_FIELDS if field in raw}
            )
        order = {value: index for index, value in enumerate(c_numbers)}
        items.sort(
            key=lambda item: (
                order.get(str(item.get("lichuang_id") or ""), len(order)),
                int(item.get("chunk_idx") or 0),
                str(item.get("id") or ""),
            )
        )
        return {
            "query": normalized,
            "by": mode,
            "resolved_c_numbers": c_numbers,
            "total": len(items),
            "items": items,
        }

    def search_peripheral_guidance(
        self,
        c_number: str,
        *,
        query: str = _PERIPHERAL_GUIDANCE_QUERY,
        limit: int = 8,
    ) -> dict[str, Any]:
        """Retrieve peripheral-circuit evidence with the collection's BM25 index.

        The catalog identity has already been resolved before this method is
        called, so the C number is an exact filter rather than a model-authored
        search term.  Deployments without the sparse index fall back to the
        bounded scalar lookup and report that downgrade explicitly.
        """

        normalized_c = str(c_number).strip().upper()
        if not _C_NUMBER.fullmatch(normalized_c):
            raise ValueError("C number must match ^C\\d{1,20}$")
        normalized_query = " ".join(str(query or "").split())
        if not normalized_query:
            normalized_query = _PERIPHERAL_GUIDANCE_QUERY
        if len(normalized_query) > 2_000:
            raise ValueError("peripheral guidance query exceeds 2000 characters")
        if (
            isinstance(limit, bool)
            or not 1 <= int(limit) <= self.config.milvus_max_results
        ):
            raise ValueError(
                f"limit must be between 1 and {self.config.milvus_max_results}"
            )
        limit = int(limit)

        client: Any = None
        try:
            client = self._client()
            if not client.has_collection(
                collection_name=self.config.milvus_collection,
                timeout=self.config.milvus_timeout_s,
            ):
                raise RuntimeError(
                    f"Milvus collection {self.config.milvus_collection!r} is missing"
                )
            raw_groups = client.search(
                collection_name=self.config.milvus_collection,
                data=[normalized_query],
                anns_field="content_sparse_vector",
                filter=f"lichuang_id == {json.dumps(normalized_c)}",
                limit=limit,
                output_fields=list(_OUTPUT_FIELDS),
                search_params={"metric_type": "BM25"},
                timeout=self.config.milvus_timeout_s,
            )
            raw_hits = raw_groups[0] if raw_groups else []
            items: list[dict[str, Any]] = []
            for raw_hit in raw_hits or []:
                getter = getattr(raw_hit, "get", None)
                entity = getter("entity", {}) if callable(getter) else {}
                if not isinstance(entity, Mapping):
                    entity = getattr(raw_hit, "entity", {})
                if not isinstance(entity, Mapping):
                    continue
                actual = str(entity.get("lichuang_id") or "").strip().upper()
                if actual != normalized_c:
                    continue
                item = {
                    field: entity.get(field)
                    for field in _OUTPUT_FIELDS
                    if field in entity
                }
                score = getter("distance") if callable(getter) else None
                if score is None:
                    score = getattr(raw_hit, "distance", None)
                if isinstance(score, (int, float)) and math.isfinite(float(score)):
                    item["score"] = float(score)
                items.append(item)
            return {
                "query": normalized_query,
                "by": "c_number",
                "resolved_c_numbers": [normalized_c],
                "retrieval_mode": "bm25_sparse_vector",
                "degraded": False,
                "total": len(items),
                "items": items,
            }
        except Exception as exc:
            fallback = self.search(normalized_c, by="c_number", limit=limit)
            return {
                **fallback,
                "guidance_query": normalized_query,
                "retrieval_mode": "scalar_fallback",
                "degraded": True,
                "fallback_reason": type(exc).__name__,
            }
        finally:
            if client is not None:
                self._close(client)


class DatasheetMilvusTool(BaseTool):
    """Look up full datasheet evidence by exact C number or model name."""

    name = "search_datasheet_knowledge"
    description = (
        "查询内部 Milvus 数据手册知识库。可传器件型号或立创 C 编号；型号会先经"
        "SQL 器件目录解析。返回带 C 编号、章节路径和完整内容的证据片段。"
    )
    capability = ToolCapability.READ
    parallel_safe = True
    detachable_on_timeout = True
    timeout_s = 20.0
    max_output_chars = None
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
                "description": "完整器件型号或立创 C 编号。",
            },
            "by": {
                "type": "string",
                "enum": ["auto", "c_number", "name"],
                "default": "auto",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": 12,
                "description": "最多返回的数据手册片段数。",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, provider: DatasheetKnowledgeProvider) -> None:
        super().__init__()
        if not isinstance(provider, DatasheetKnowledgeProvider):
            raise TypeError("provider must implement DatasheetKnowledgeProvider")
        self.provider = provider

    def call(self, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        result = self.provider.search(
            str(arguments.get("query") or ""),
            by=str(arguments.get("by") or "auto"),
            limit=min(int(arguments.get("limit", 12)), 20),
        )
        return ToolOutput(
            content={
                **result,
                "evidence_source": self.provider.name,
                "read_only": True,
            },
            metadata={
                "backend": self.provider.name,
                "rows": int(result.get("total") or 0),
            },
        )


__all__ = [
    "DatasheetKnowledgeProvider",
    "DatasheetMilvusTool",
    "MilvusDatasheetProvider",
]
