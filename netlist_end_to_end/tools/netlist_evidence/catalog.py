"""Exact SQL candidate resolution and bounded device records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from ..sql import DeviceCatalogProvider
from .common import (
    C_NUMBER_PATTERN,
    CATALOG_LIMIT,
    MAX_CANDIDATES,
    MAX_QUERY_CHARS,
    abstract_device_type,
    as_mapping,
    bounded_text,
    issue,
    safe_scalar,
    serialized_size,
)


_C_NUMBER = re.compile(C_NUMBER_PATTERN, re.IGNORECASE)


def _identity_key(device: Mapping[str, Any]) -> tuple[str, str] | None:
    supplier_part = str(device.get("supplier_part") or "").strip()
    if _C_NUMBER.fullmatch(supplier_part):
        return "c_number", supplier_part.upper()
    model = str(device.get("model") or "").strip()
    if model:
        return "model", model.casefold()
    return None


def candidate_inputs(
    module: Mapping[str, Any],
    selection: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Collect at most three identities without ever falling back to ``name``."""

    confirmed = as_mapping(module.get("confirmed_device"))
    confirmed_c = str(confirmed.get("supplier_part") or "").strip()
    selected = selection.get("selected_devices")
    selected_rows = (
        list(selected)
        if isinstance(selected, Sequence) and not isinstance(selected, (str, bytes))
        else []
    )
    ordered: list[tuple[Mapping[str, Any], str]] = []
    confirmed_first = bool(_C_NUMBER.fullmatch(confirmed_c))
    if confirmed_first:
        ordered.append((confirmed, "module.confirmed_device"))
    for index, raw in enumerate(selected_rows):
        wrapper = as_mapping(raw)
        device = as_mapping(wrapper.get("device")) or wrapper
        ordered.append((device, f"selection.selected_devices[{index}].device"))
    if not confirmed_first:
        ordered.append((confirmed, "module.confirmed_device"))

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for device, source_path in ordered:
        if abstract_device_type(device) is not None:
            continue
        key = _identity_key(device)
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "source_path": source_path,
                "model": str(device.get("model") or "").strip(),
                "supplier_part": str(device.get("supplier_part") or "").strip(),
                "confirmed_c_number": source_path == "module.confirmed_device"
                and key[0] == "c_number",
            }
        )
        if len(result) == MAX_CANDIDATES:
            break
    return result


def _compact_pins(
    raw_pins: Any,
    *,
    budget: int,
) -> tuple[list[dict[str, Any]] | None, int, bool]:
    rows = (
        [row for row in raw_pins if isinstance(row, Mapping)]
        if isinstance(raw_pins, Sequence) and not isinstance(raw_pins, (str, bytes))
        else []
    )

    def order(item: tuple[int, Mapping[str, Any]]) -> tuple[int, int]:
        index, row = item
        try:
            return int(row.get("pin_index")), index
        except (TypeError, ValueError):
            return 1_000_000_000, index

    pins: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _index, row in sorted(enumerate(rows), key=order):
        number = bounded_text(row.get("pin_number"), 32)
        if not number or number in seen:
            continue
        seen.add(number)
        pins.append(
            {
                "pin_number": number,
                "pin_name": bounded_text(row.get("pin_name"), 120),
                "pin_type": bounded_text(row.get("pin_type"), 48),
                "pin_description": bounded_text(row.get("pin_description"), 160),
                "pin_connection_notes": bounded_text(
                    row.get("pin_connection_notes"), 180
                ),
                "pin_impedance": bounded_text(row.get("pin_impedance"), 80),
                "pin_frequency": bounded_text(row.get("pin_frequency"), 80),
            }
        )
    if serialized_size(pins) <= budget:
        return pins, len(seen), False
    reduced = [
        {
            "pin_number": pin["pin_number"],
            "pin_name": pin["pin_name"],
            "pin_type": pin["pin_type"],
        }
        for pin in pins
    ]
    return (
        (reduced, len(seen), True)
        if serialized_size(reduced) <= budget
        else (None, len(seen), True)
    )


def _compact_parameters(value: Any, *, budget: int) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, Mapping):
        return {}, False
    result: dict[str, Any] = {}
    truncated = False
    for raw_key, raw_value in value.items():
        if raw_value in (None, ""):
            continue
        if len(result) >= 48:
            truncated = True
            break
        key = bounded_text(raw_key, 96)
        candidate = {**result, key: safe_scalar(raw_value)}
        if serialized_size(candidate) > budget:
            truncated = True
            break
        result = candidate
    return result, truncated


def candidate_failure(candidate: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "reason": reason,
        "source_path": str(candidate.get("source_path") or ""),
        "confirmed_c_number": bool(candidate.get("confirmed_c_number")),
        "requested_model": bounded_text(candidate.get("model"), 160) or None,
        "requested_supplier_part": bounded_text(candidate.get("supplier_part"), 32)
        or None,
    }


def resolve_catalog_candidate(
    catalog: DeviceCatalogProvider,
    candidate: Mapping[str, Any],
    *,
    pin_budget: int,
    parameter_budget: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    source_path = str(candidate.get("source_path") or "")
    supplied_c = str(candidate.get("supplier_part") or "").strip()
    model = str(candidate.get("model") or "").strip()
    issues: list[dict[str, str]] = []
    if _C_NUMBER.fullmatch(supplied_c):
        query = supplied_c.upper()
        basis = "supplier_part_exact"
    elif model:
        query = model
        basis = "device_name_exact"
        if supplied_c:
            issues.append(
                issue(
                    "invalid_supplier_part",
                    "catalog",
                    f"{source_path} has an invalid C/S supplier ID; exact model lookup was used.",
                )
            )
    else:
        return candidate_failure(candidate, "identity_missing"), issues
    base = {
        "source_path": source_path,
        "confirmed_c_number": bool(candidate.get("confirmed_c_number")),
        "query": bounded_text(query, MAX_QUERY_CHARS),
        "match_basis": basis,
        "requested_model": bounded_text(model, MAX_QUERY_CHARS) or None,
        "requested_supplier_part": bounded_text(supplied_c, 32) or None,
    }
    if len(query) > MAX_QUERY_CHARS:
        issues.append(
            issue("query_too_long", "catalog", f"{source_path} query is too long.")
        )
        return {**base, "status": "failed", "reason": "query_too_long"}, issues
    try:
        rows = catalog.search_devices(
            query,
            limit=CATALOG_LIMIT,
            include_pins=True,
            include_parameters=True,
        )
    except Exception as exc:
        issues.append(
            issue(
                "catalog_error",
                "catalog",
                f"{source_path} lookup failed with {type(exc).__name__}.",
            )
        )
        return {
            **base,
            "status": "failed",
            "reason": "catalog_error",
            "error_type": type(exc).__name__,
        }, issues
    if not isinstance(rows, list):
        issues.append(
            issue(
                "catalog_malformed",
                "catalog",
                "Catalog returned a non-list result.",
            )
        )
        return {**base, "status": "failed", "reason": "catalog_malformed"}, issues
    if basis == "supplier_part_exact":
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("supplier_part") or "").strip().upper() == query
        ]
    else:
        matches = [
            row
            for row in rows
            if isinstance(row, Mapping)
            and str(row.get("device_name") or "").strip().casefold() == query.casefold()
        ]
    if len(matches) != 1:
        reason = "catalog_not_found" if not matches else "catalog_ambiguous"
        issues.append(
            issue(
                reason,
                "catalog",
                f"{source_path} produced {len(matches)} exact matches.",
            )
        )
        return {
            **base,
            "status": "failed",
            "reason": reason,
            "exact_match_count": len(matches),
        }, issues
    row = matches[0]
    actual_name = str(row.get("device_name") or "").strip()
    if (
        basis == "supplier_part_exact"
        and model
        and actual_name.casefold() != model.casefold()
    ):
        issues.append(
            issue(
                "catalog_model_canonicalized",
                "catalog",
                f"{source_path} model was replaced by the exact device_name stored for its supplier ID.",
            )
        )
    actual_c = str(row.get("supplier_part") or "").strip().upper()
    if not _C_NUMBER.fullmatch(actual_c):
        issues.append(
            issue(
                "catalog_c_number_missing",
                "catalog",
                "Exact row has no valid C/S supplier ID.",
            )
        )
        return {
            **base,
            "status": "failed",
            "reason": "catalog_c_number_missing",
        }, issues
    try:
        pin_count = int(row.get("pin_count"))
    except (TypeError, ValueError):
        pin_count = -1
    pins, unique_pins, pin_details_truncated = _compact_pins(
        row.get("pins"),
        budget=pin_budget,
    )
    if pin_count < 1 or unique_pins != pin_count:
        issues.append(
            issue(
                "pin_table_incomplete",
                "catalog",
                f"{actual_c} has {unique_pins} unique non-empty pin numbers but pin_count={pin_count}.",
            )
        )
        return {
            **base,
            "status": "failed",
            "reason": "pin_table_incomplete",
            "pin_count": pin_count if pin_count >= 0 else None,
            "unique_pin_numbers": unique_pins,
        }, issues
    if pins is None:
        issues.append(
            issue(
                "pin_evidence_too_large",
                "catalog",
                f"{actual_c} pin evidence exceeds its bound.",
            )
        )
        return {
            **base,
            "status": "failed",
            "reason": "pin_evidence_too_large",
        }, issues
    if pin_details_truncated:
        issues.append(
            issue(
                "pin_details_truncated",
                "catalog",
                f"{actual_c} retained complete pin identity with verbose fields removed.",
            )
        )
    parameters, parameters_truncated = _compact_parameters(
        row.get("parameters"),
        budget=parameter_budget,
    )
    if parameters_truncated:
        issues.append(
            issue(
                "parameters_truncated",
                "catalog",
                f"{actual_c} parameters were bounded.",
            )
        )
    return {
        **base,
        "status": "exact_ready",
        "exact_match_count": 1,
        "device": {
            "device_id": safe_scalar(row.get("device_id"), 48),
            "device_name": bounded_text(actual_name, 160),
            "supplier_part": actual_c,
            "category_level_1": bounded_text(row.get("category_level_1"), 96),
            "category_level_2": bounded_text(row.get("category_level_2"), 96),
            "pin_count": pin_count,
            "pins_complete": True,
            "pins": pins,
            "pin_details_truncated": pin_details_truncated,
            "parameters": parameters,
            "parameters_truncated": parameters_truncated,
        },
    }, issues


__all__ = ["candidate_inputs", "resolve_catalog_candidate"]
