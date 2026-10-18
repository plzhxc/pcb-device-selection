"""Shared bounds, projections, and public prefetch protocol."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import Any, Protocol, runtime_checkable

from validation_platform.taxonomy import DEVICE_TYPE_TABLE, canonical_type_name


# Catalog identities include both LCSC parts and project-assigned supplier IDs.
C_NUMBER_PATTERN = r"^[CS][0-9]{1,20}$"
MAX_CANDIDATES = 3
CATALOG_LIMIT = 5
DATASHEET_LIMIT = 6
MAX_QUERY_CHARS = 160
MAX_OUTPUT_CHARS = 24_000
PIN_BUDGET = 10_000
PARAMETER_BUDGET = 3_000
TOPOLOGY_BUDGET = 6_000


def core_device_type(value: Any) -> bool:
    kind = canonical_type_name(value)
    spec = DEVICE_TYPE_TABLE.get(kind)
    return str(value or "").strip().lower() == "core_ic" or (spec is not None and spec.role == "core_ic")


def abstract_device_type(device: Mapping[str, Any]) -> str | None:
    """Validate an explicit common-device identity before bypassing SQL.

    A boolean flag alone cannot turn an IC into a common device. Supplier
    identity aliases must all be empty, including legacy netlist spellings.
    """

    flag = device.get("is_abstract")
    if "is_abstract" in device and type(flag) is not bool:
        raise ValueError("is_abstract must be boolean")
    if flag is not True:
        return None
    kinds = [str(device[key]).strip().lower().replace("-", "_").replace(" ", "_")
             for key in ("type", "device_type", "deviceType") if device.get(key)]
    if not kinds or any(core_device_type(kind) or kind == "common_device"
                        or re.fullmatch(r"[a-z][a-z0-9_]*", kind) is None for kind in kinds):
        raise ValueError("an abstract device requires a concrete non-IC type")
    if any(device.get(key) not in (None, "")
           for key in ("id", "supplier_part", "supplierPart", "lichuang_id")):
        raise ValueError("an abstract device must not carry a supplier identity")
    return kinds[0]


def bounded_text(value: Any, limit: int) -> str:
    result = " ".join(str(value or "").split())
    return result if len(result) <= limit else result[: limit - 1] + "…"


def serialized_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    )


def safe_scalar(value: Any, limit: int = 240) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return bounded_text(value, limit)


def as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def issue(code: str, stage: str, message: str) -> dict[str, str]:
    return {"code": code, "stage": stage, "message": bounded_text(message, 220)}


def compact_summary(evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return a small Trace-safe summary of a full prefetch result."""

    catalog = as_mapping(evidence.get("catalog"))
    topology = as_mapping(evidence.get("topology"))
    resolved = catalog.get("resolved_candidates")
    resolved_rows = resolved if isinstance(resolved, list) else []
    candidates = catalog.get("candidate_results")
    candidate_rows = candidates if isinstance(candidates, list) else []
    items = topology.get("items")
    topology_items = items if isinstance(items, list) else []
    raw_statuses = topology.get("candidate_statuses")
    topology_statuses = raw_statuses if isinstance(raw_statuses, list) else []
    issues = evidence.get("issues")
    issue_rows = issues if isinstance(issues, list) else []
    return {
        "status": str(evidence.get("status") or "failed"),
        "catalog_status": str(catalog.get("status") or "not_checked"),
        "topology_status": str(topology.get("status") or "not_checked"),
        "candidate_count": len(candidate_rows),
        "resolved_count": len(resolved_rows),
        "catalog_queries": len(candidate_rows),
        "resolved_devices": [
            {
                "device_name": bounded_text(
                    as_mapping(row.get("device")).get("device_name"), 96
                ),
                "supplier_part": bounded_text(
                    as_mapping(row.get("device")).get("supplier_part"), 32
                ),
                "pin_count": as_mapping(row.get("device")).get("pin_count"),
            }
            for row in resolved_rows[:MAX_CANDIDATES]
        ],
        "topology_item_count": len(topology_items),
        "topology_queries": len(topology_statuses),
        "topology_hits": sum(
            int(as_mapping(row).get("item_count") or 0) for row in topology_statuses
        ),
        "topology_sections": [
            {
                "c_number": bounded_text(as_mapping(row).get("c_number"), 32),
                "title": bounded_text(as_mapping(row).get("title"), 140) or None,
                "section_path": bounded_text(as_mapping(row).get("section_path"), 180)
                or None,
                "score": safe_scalar(as_mapping(row).get("score"), 32),
            }
            for row in topology_items[:6]
        ],
        "retrieval_modes": list(
            dict.fromkeys(
                bounded_text(as_mapping(row).get("retrieval_mode"), 64)
                for row in topology_statuses
                if as_mapping(row).get("retrieval_mode")
            )
        ),
        "degraded_reasons": list(
            dict.fromkeys(
                bounded_text(as_mapping(row).get("degraded_reason"), 64)
                for row in topology_statuses
                if as_mapping(row).get("degraded_reason")
            )
        ),
        "issue_codes": [
            bounded_text(as_mapping(row).get("code"), 64)
            for row in issue_rows[:12]
            if as_mapping(row).get("code")
        ],
    }


@runtime_checkable
class NetlistKnowledgePrefetcher(Protocol):
    name: str

    def prefetch(
        self,
        module: Mapping[str, Any],
        selection: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    def compact_summary(self, evidence: Mapping[str, Any]) -> dict[str, Any]: ...


__all__ = ["NetlistKnowledgePrefetcher", "compact_summary"]
