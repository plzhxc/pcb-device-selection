"""Bounded batch verification across the device catalog and datasheet store."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
import math
import re
from typing import Any, Callable, TypeVar

from agent_framework.tool import BaseTool, ToolCapability, ToolOutput

from .milvus import DatasheetKnowledgeProvider
from .sql import DeviceCatalogProvider


_C_NUMBER = re.compile(r"^C\d{1,20}$", flags=re.IGNORECASE)
_MAX_MODELS = 20
_MAX_MODEL_CHARS = 160
_MAX_TOTAL_INPUT_CHARS = 3_200
_CATALOG_CANDIDATE_LIMIT = 5
_AMBIGUOUS_CANDIDATE_LIMIT = 3
_DATASHEET_RESULT_LIMIT = 1
_MAX_WORKERS = 8
_MAX_DISPLAY_CHARS = 96
_MAX_OUTPUT_CHARS = 24_000

_T = TypeVar("_T")
_R = TypeVar("_R")


def _bounded_text(value: Any, maximum: int = _MAX_DISPLAY_CHARS) -> str:
    """Return one compact, single-line display value."""

    text = " ".join(str(value or "").split())
    if len(text) <= maximum:
        return text
    return text[: maximum - 1] + "…"


def _safe_scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _bounded_text(value)
    return _bounded_text(value)


def _run_bounded_parallel(
    values: list[_T],
    operation: Callable[[_T], _R],
) -> dict[int, tuple[_R | None, str | None]]:
    """Execute independent provider reads without letting one failure abort peers."""

    if not values:
        return {}
    results: dict[int, tuple[_R | None, str | None]] = {}
    workers = min(_MAX_WORKERS, len(values))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures: dict[Future[_R], int] = {
            executor.submit(operation, value): index
            for index, value in enumerate(values)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = (future.result(), None)
            except Exception as exc:  # each token is an independent evidence read
                results[index] = (None, type(exc).__name__)
    return results


def _candidate_identity(row: Mapping[str, Any]) -> tuple[str, str]:
    supplier_part = str(row.get("supplier_part") or "").strip().upper()
    if supplier_part:
        return ("supplier_part", supplier_part)
    device_id = str(row.get("device_id") or "").strip()
    if device_id:
        return ("device_id", device_id)
    return ("device_name", str(row.get("device_name") or "").strip().casefold())


def _normalized_model(value: Any) -> str:
    """Normalize catalog punctuation without turning substring matches into hits."""

    return "".join(character for character in str(value).casefold() if character.isalnum())


def _matching_candidates(
    model: str,
    rows: Any,
) -> tuple[list[Mapping[str, Any]], str | None]:
    if not isinstance(rows, list):
        raise TypeError("catalog provider must return a list")
    query_is_c_number = bool(_C_NUMBER.fullmatch(model))
    expected = model.upper() if query_is_c_number else model.casefold()

    def unique_matches(predicate: Callable[[Mapping[str, Any]], bool]) -> list[Mapping[str, Any]]:
        candidates: list[Mapping[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, Mapping) or not predicate(row):
                continue
            identity = _candidate_identity(row)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(row)
        return candidates

    exact = unique_matches(
        lambda row: (
            str(row.get("supplier_part") or "").strip().upper()
            if query_is_c_number
            else str(row.get("device_name") or "").strip().casefold()
        )
        == expected
    )
    if exact or query_is_c_number:
        return exact, "exact" if exact else None

    normalized_query = _normalized_model(model)
    if len(normalized_query) < 4:
        return [], None
    prefix = unique_matches(
        lambda row: _normalized_model(row.get("device_name")).startswith(
            normalized_query
        )
    )
    return prefix, "normalized_prefix" if prefix else None


def _catalog_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "supplier_part": _bounded_text(row.get("supplier_part"), 32) or None,
        "device_name": _bounded_text(row.get("device_name")) or None,
    }


def _catalog_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    category = row.get("category_level_2") or row.get("category_level_1")
    return {
        "category": _bounded_text(category, 64) or None,
        "pin_count": _safe_scalar(row.get("pin_count")),
    }


def _not_checked_datasheet(reason: str) -> dict[str, Any]:
    return {
        "status": "not_checked",
        "exists": None,
        "reason": reason,
    }


class BatchDeviceEvidenceTool(BaseTool):
    """Verify explicit device models in SQL and Milvus with one bounded call."""

    name = "verify_device_evidence_batch"
    description = (
        "批量核验明确的器件型号或立创 C 编号。一次传入 models 数组；工具会去重并"
        "并发查询只读器件目录，只对唯一明确命中的器件核验数据手册。返回紧凑的"
        "型号、C 编号、分类、引脚数及数据手册标题/章节证据，不返回正文。"
    )
    capability = ToolCapability.READ
    parallel_safe = True
    detachable_on_timeout = True
    timeout_s = 35.0
    max_output_chars = _MAX_OUTPUT_CHARS
    parameters_schema = {
        "type": "object",
        "properties": {
            "models": {
                "type": "array",
                "minItems": 1,
                "maxItems": _MAX_MODELS,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": _MAX_MODEL_CHARS,
                },
                "description": "明确的完整器件型号或立创 C 编号；不要传描述句。",
            }
        },
        "required": ["models"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        catalog: DeviceCatalogProvider,
        datasheets: DatasheetKnowledgeProvider,
    ) -> None:
        super().__init__()
        if not isinstance(catalog, DeviceCatalogProvider):
            raise TypeError("catalog must implement DeviceCatalogProvider")
        if not isinstance(datasheets, DatasheetKnowledgeProvider):
            raise TypeError("datasheets must implement DatasheetKnowledgeProvider")
        self.catalog = catalog
        self.datasheets = datasheets

    @staticmethod
    def _models(arguments: Mapping[str, Any]) -> tuple[list[str], int]:
        raw_models = arguments.get("models")
        if not isinstance(raw_models, list):
            raise ValueError("models must be an array")
        if not 1 <= len(raw_models) <= _MAX_MODELS:
            raise ValueError(f"models must contain between 1 and {_MAX_MODELS} items")

        models: list[str] = []
        seen: set[str] = set()
        total_chars = 0
        for index, raw in enumerate(raw_models):
            if not isinstance(raw, str):
                raise ValueError(f"models[{index}] must be a string")
            model = raw.strip()
            if not model:
                raise ValueError(f"models[{index}] must not be blank")
            if len(model) > _MAX_MODEL_CHARS:
                raise ValueError(
                    f"models[{index}] exceeds {_MAX_MODEL_CHARS} characters"
                )
            total_chars += len(model)
            if total_chars > _MAX_TOTAL_INPUT_CHARS:
                raise ValueError(
                    f"models exceed {_MAX_TOTAL_INPUT_CHARS} total characters"
                )
            key = model.casefold()
            if key in seen:
                continue
            seen.add(key)
            models.append(model)
        return models, len(raw_models) - len(models)

    def _catalog_lookup(self, model: str) -> list[dict[str, Any]]:
        return self.catalog.search_devices(
            model,
            limit=_CATALOG_CANDIDATE_LIMIT,
            include_pins=False,
            include_parameters=False,
        )

    def _datasheet_lookup(self, supplier_part: str) -> dict[str, Any]:
        return self.datasheets.search(
            supplier_part,
            by="c_number",
            limit=_DATASHEET_RESULT_LIMIT,
        )

    @staticmethod
    def _datasheet_summary(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise TypeError("datasheet provider must return an object")
        raw_items = raw.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        evidence: Mapping[str, Any] | None = next(
            (item for item in items if isinstance(item, Mapping)),
            None,
        )
        try:
            reported_total = int(raw.get("total") or 0)
        except (TypeError, ValueError):
            reported_total = 0
        evidence_count = min(1_000_000, max(0, reported_total, len(items)))
        exists = evidence_count > 0
        return {
            "status": "found" if exists else "not_found",
            "exists": exists,
            "title": (
                _bounded_text(evidence.get("title")) or None if evidence else None
            ),
            "section": (
                _bounded_text(evidence.get("section_path")) or None
                if evidence
                else None
            ),
            "evidence_count": evidence_count,
        }

    def call(self, arguments: dict[str, Any], **kwargs: Any) -> ToolOutput:
        models, duplicates_removed = self._models(arguments)
        catalog_results = _run_bounded_parallel(models, self._catalog_lookup)

        results: list[dict[str, Any]] = []
        datasheet_targets: list[tuple[int, str]] = []
        catalog_errors = 0
        matched = 0
        ambiguous = 0
        not_found = 0

        for index, model in enumerate(models):
            raw_rows, error_type = catalog_results[index]
            base: dict[str, Any] = {
                "model": model,
                "supplier_part": None,
                "device_name": None,
            }
            if error_type is not None:
                catalog_errors += 1
                results.append(
                    {
                        **base,
                        "match_status": "error",
                        "error_stage": "catalog",
                        "error_type": error_type,
                        "datasheet": _not_checked_datasheet("catalog_error"),
                    }
                )
                continue

            try:
                candidates, match_basis = _matching_candidates(model, raw_rows)
            except Exception as exc:
                catalog_errors += 1
                results.append(
                    {
                        **base,
                        "match_status": "error",
                        "error_stage": "catalog",
                        "error_type": type(exc).__name__,
                        "datasheet": _not_checked_datasheet("catalog_error"),
                    }
                )
                continue
            if not candidates:
                not_found += 1
                results.append(
                    {
                        **base,
                        "match_status": "not_found",
                        "datasheet": _not_checked_datasheet("catalog_not_found"),
                    }
                )
                continue
            if len(candidates) > 1:
                ambiguous += 1
                results.append(
                    {
                        **base,
                        "match_status": "ambiguous",
                        "match_basis": match_basis,
                        "candidate_count": len(candidates),
                        "candidates": [
                            _catalog_identity(candidate)
                            for candidate in candidates[:_AMBIGUOUS_CANDIDATE_LIMIT]
                        ],
                        "datasheet": _not_checked_datasheet("ambiguous_catalog_match"),
                    }
                )
                continue

            candidate = candidates[0]
            identity = _catalog_identity(candidate)
            supplier_part = str(identity["supplier_part"] or "")
            matched += 1
            results.append(
                {
                    **base,
                    **identity,
                    "match_status": "matched",
                    "match_basis": match_basis,
                    "catalog": _catalog_summary(candidate),
                    "datasheet": _not_checked_datasheet("missing_c_number"),
                }
            )
            if _C_NUMBER.fullmatch(supplier_part):
                datasheet_targets.append((index, supplier_part.upper()))

        datasheet_results = _run_bounded_parallel(
            datasheet_targets,
            lambda target: self._datasheet_lookup(target[1]),
        )
        datasheet_errors = 0
        for target_index, (result, error_type) in datasheet_results.items():
            model_index, _supplier_part = datasheet_targets[target_index]
            if error_type is not None:
                datasheet_errors += 1
                results[model_index]["datasheet"] = {
                    "status": "error",
                    "exists": None,
                    "error_type": error_type,
                }
                continue
            try:
                results[model_index]["datasheet"] = self._datasheet_summary(result)
            except Exception as exc:
                datasheet_errors += 1
                results[model_index]["datasheet"] = {
                    "status": "error",
                    "exists": None,
                    "error_type": type(exc).__name__,
                }

        partial = bool(catalog_errors or datasheet_errors)
        content = {
            "status": "partial" if partial else "ok",
            "requested_count": len(models) + duplicates_removed,
            "unique_count": len(models),
            "duplicates_removed": duplicates_removed,
            "summary": {
                "matched": matched,
                "ambiguous": ambiguous,
                "not_found": not_found,
                "catalog_errors": catalog_errors,
                "datasheet_errors": datasheet_errors,
            },
            "results": results,
            "evidence_sources": {
                "catalog": self.catalog.name,
                "datasheets": self.datasheets.name,
            },
            "read_only": True,
        }
        return ToolOutput(
            content=content,
            metadata={
                "catalog_backend": self.catalog.name,
                "datasheet_backend": self.datasheets.name,
                "requested_models": len(models) + duplicates_removed,
                "unique_models": len(models),
                "partial": partial,
            },
        )


__all__ = ["BatchDeviceEvidenceTool"]
