"""Deterministic parameter pull for user-named devices (SQL catalog + Milvus datasheets).

The requirement report keeps, for every device the user named, the catalog
row and parameter table from MySQL and the datasheet sections from Milvus as
``metadata``.  The pull is keyed by the LCSC C-number, never by name: the name
only serves to find the C-number (device_resolver's confirmed hit, else a
unique exact catalog match), and parameters plus datasheet are then read by
that C-number.  It is a stage dependency (like the netlist prefetcher), not an
Agent tool, so the model never decides whether to query the databases and the
stored values are copied verbatim, never paraphrased.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
import re
from typing import Any, Protocol, runtime_checkable

from .milvus import DatasheetKnowledgeProvider
from .sql import DeviceCatalogProvider


_C_NUMBER = re.compile(r"^[CS][0-9]{1,20}$", flags=re.IGNORECASE)
_CATALOG_LIMIT = 5
_DATASHEET_LIMIT = 8
_MAX_DEVICES = 20
_MAX_WORKERS = 6
_MAX_VALUE_CHARS = 400
_MAX_EXCERPT_CHARS = 240
_CATALOG_FIELDS = ("device_name", "supplier_part", "category_level_1", "category_level_2", "pin_count", "price")


@runtime_checkable
class DeviceKnowledgeFetcher(Protocol):
    """Stage dependency: parameter metadata for a list of resolved devices."""

    name: str

    def fetch(self, devices: Sequence[Mapping[str, Any]]) -> dict[str, Any]: ...


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _normalized(value: Any) -> str:
    return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())


def _pick_row(query: str, rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None, str]:
    """Exact C-number or exact model name only; anything looser is ambiguous."""

    if _C_NUMBER.fullmatch(query):
        hits = [r for r in rows if str(r.get("supplier_part") or "").upper() == query.upper()]
    else:
        hits = [r for r in rows if str(r.get("device_name") or "").casefold() == query.casefold()]
        if not hits:
            hits = [r for r in rows if _normalized(r.get("device_name")) == _normalized(query)]
    if len(hits) == 1:
        return hits[0], "matched"
    return None, ("ambiguous" if len(hits) > 1 else "not_found")


class CatalogMilvusDeviceKnowledge:
    """Pull catalog parameters and datasheet sections for each named device."""

    name = "catalog_milvus_device_knowledge"

    def __init__(self, catalog: DeviceCatalogProvider, datasheets: DatasheetKnowledgeProvider) -> None:
        if not isinstance(catalog, DeviceCatalogProvider):
            raise TypeError("catalog must implement DeviceCatalogProvider")
        if not isinstance(datasheets, DatasheetKnowledgeProvider):
            raise TypeError("datasheets must implement DatasheetKnowledgeProvider")
        self.catalog = catalog
        self.datasheets = datasheets

    def fetch(self, devices: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Return ``{"status", "devices": {model: metadata}}``; one failure never hides the others."""

        queries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for device in devices:
            if not isinstance(device, Mapping):
                continue
            model = str(device.get("model") or device.get("name") or "").strip()
            if not model or model.casefold() in seen:
                continue
            seen.add(model.casefold())
            supplier_part = str(device.get("supplier_part") or "").strip().upper()
            confirmed = str(device.get("verification_status") or "") == "confirmed"
            queries.append((model, supplier_part if confirmed else ""))
            if len(queries) >= _MAX_DEVICES:
                break
        if not queries:
            return {"status": "not_applicable", "devices": {}, "sources": self._sources()}

        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(queries))) as pool:
            results = list(pool.map(lambda item: self._one(*item), queries))
        payload = {model: result for (model, _hint), result in zip(queries, results)}
        statuses = {row["status"] for row in payload.values()}
        return {
            "status": "partial" if "error" in statuses else "ok",
            "devices": payload,
            "sources": self._sources(),
        }

    def _sources(self) -> dict[str, str]:
        return {"catalog": self.catalog.name, "datasheets": self.datasheets.name}

    def _one(self, model: str, hint: str) -> dict[str, Any]:
        """Two steps: name → C-number, then everything by C-number."""

        c_number, resolution, detail = self._resolve_c_number(model, hint)
        if not c_number:
            return {"status": resolution, "model": model, **detail}
        result = self._pull_by_c_number(c_number)
        result.update({"model": model, "c_number": c_number, "resolution": resolution, **detail})
        return result

    def _resolve_c_number(self, model: str, hint: str) -> tuple[str | None, str, dict[str, Any]]:
        """Return (C-number, how it was found, detail).

        A confirmed C-number from device_resolver is taken as is.  Otherwise the
        catalog is searched by name: a unique exact hit resolves directly; several
        hits that are all the same catalog sub-category (the same part in other
        packages or reels) resolve to one representative for the *parameter*
        pull, with every candidate recorded so the C-number itself stays a user
        decision; hits of mixed categories are ambiguous and nothing is pulled.
        """

        if _C_NUMBER.fullmatch(hint):
            return hint.upper(), "device_resolver", {}
        try:
            rows = self.catalog.search_devices(model, limit=_CATALOG_LIMIT, include_pins=False, include_parameters=False)
        except Exception as exc:
            return None, "error", {"error": f"catalog: {type(exc).__name__}"}
        rows = [r for r in rows if isinstance(r, Mapping) and _C_NUMBER.fullmatch(str(r.get("supplier_part") or ""))]
        query = _normalized(model)
        exact = [r for r in rows if _normalized(r.get("device_name")) == query]
        prefix = exact or [r for r in rows if _normalized(r.get("device_name")).startswith(query)] if len(query) >= 4 else exact
        if not prefix:
            return None, "not_found", {"candidates": [str(r.get("device_name") or "") for r in rows][:5]}
        # one C-number per candidate; identical names under several C-numbers are the same part
        by_c: dict[str, Mapping[str, Any]] = {}
        for r in prefix:
            by_c.setdefault(str(r.get("supplier_part")).upper(), r)
        if len(by_c) == 1:
            return next(iter(by_c)), "catalog_name_lookup", {}
        categories = {str(r.get("category_level_2") or "") for r in by_c.values()}
        candidates = sorted(f"{c}: {r.get('device_name')}" for c, r in by_c.items())
        if len(categories) > 1 or len({part[0] for part in by_c}) > 1:
            return None, "ambiguous", {"candidates": candidates}
        representative = min(
            by_c.items(),
            key=lambda kv: (0 if _normalized(kv[1].get("device_name")) == query else 1, len(str(kv[1].get("device_name") or "")), int(kv[0][1:])),
        )[0]
        return representative, "catalog_name_lookup_multi", {"candidates": candidates}

    def _pull_by_c_number(self, c_number: str) -> dict[str, Any]:
        try:
            rows = self.catalog.search_devices(c_number, limit=_CATALOG_LIMIT, include_pins=False, include_parameters=True)
        except Exception as exc:
            return {"status": "error", "error": f"catalog: {type(exc).__name__}"}
        row, status = _pick_row(c_number, [r for r in rows if isinstance(r, Mapping)])
        if row is None:
            return {"status": status}
        catalog = {field: row.get(field) for field in _CATALOG_FIELDS if row.get(field) not in (None, "")}
        parameters = {
            str(key): _text(value, _MAX_VALUE_CHARS)
            for key, value in (row.get("parameters") or {}).items()
            if value not in (None, "")
        }
        result: dict[str, Any] = {"status": "matched", "catalog": catalog, "parameters": parameters}
        try:
            raw = self.datasheets.search(c_number, by="c_number", limit=_DATASHEET_LIMIT)
        except Exception as exc:
            result["datasheet"] = {"status": "error", "error": type(exc).__name__, "sections": []}
            result["status"] = "error"
            return result
        items = [i for i in (raw.get("items") or []) if isinstance(i, Mapping)]
        result["datasheet"] = {
            "status": "found" if items else "not_found",
            "total": int(raw.get("total") or len(items)),
            "sections": [
                {
                    "title": _text(item.get("title"), 96),
                    "section_path": _text(item.get("section_path"), 120) or None,
                    "excerpt": _text(item.get("content"), _MAX_EXCERPT_CHARS),
                }
                for item in items
            ],
        }
        return result


__all__ = ["CatalogMilvusDeviceKnowledge", "DeviceKnowledgeFetcher"]
