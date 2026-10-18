"""Explicit, connection-free hand-off to netlist generation.

The caller supplies module metadata and already selected devices. These
functions validate and project that input; they never infer requirements,
choose devices, read references, or invoke a model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
import json
import re
from typing import Any

from validation_platform.taxonomy import DEVICE_TYPE_TABLE, canonical_type_name

from .contracts import FlowContractError

SCHEMA = "pcbagent.netlist_input.v1"
MAX_INPUT_BYTES = 1_048_576
MAX_MODULES = 128
_ANSWER_FIELDS = frozenset({"netlist", "netlist_info", "reference_netlist", "reference_content",
                           "connections", "pin_nets", "prebuilt_netlist", "template_payload"})


def _is_core_type(value: Any) -> bool:
    return (str(value or "").strip().lower() == "core_ic"
            or DEVICE_TYPE_TABLE[canonical_type_name(value)].role == "core_ic")


def _reject_answers(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in _ANSWER_FIELDS:
                raise FlowContractError(f"generation_input must not include answer field {key!r}")
            _reject_answers(child)
    elif isinstance(value, list):
        for child in value:
            _reject_answers(child)


def validate_generation_input(value: Any) -> dict[str, Any]:
    """Return an independent JSON-safe input or fail before any model call."""
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise FlowContractError(f"generation_input.schema must be {SCHEMA!r}")
    unknown = set(value) - {"schema", "modules", "description", "system", "links"}
    if unknown:
        raise FlowContractError(f"unsupported generation_input fields: {sorted(unknown)}")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=False, allow_nan=False)
        data = json.loads(encoded)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise FlowContractError("generation_input must contain finite JSON values") from exc
    if len(encoded.encode("utf-8")) > MAX_INPUT_BYTES:
        raise FlowContractError("generation_input exceeds the 1 MiB limit")
    try:
        _reject_answers(data)
    except RecursionError as exc:
        raise FlowContractError("generation_input nesting is too deep") from exc
    rows = data.get("modules")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MODULES:
        raise FlowContractError(f"generation_input.modules must contain 1..{MAX_MODULES} modules")
    seen: set[str] = set()
    for index, row in enumerate(rows):
        prefix = f"generation_input.modules[{index}]"
        if not isinstance(row, dict):
            raise FlowContractError(f"{prefix} must be an object")
        module = row.get("module")
        if not isinstance(module, dict):
            raise FlowContractError(f"{prefix}.module must contain explicit module metadata")
        for key in ("id", "name"):
            if not isinstance(module.get(key), str) or not module[key].strip():
                raise FlowContractError(f"{prefix}.module.{key} must be nonempty text")
        module_id = module["id"]
        if module_id != module_id.strip() or module_id in seen:
            raise FlowContractError("module IDs must be unique and have no surrounding whitespace")
        seen.add(module_id)
        devices = row.get("selected_devices")
        if not isinstance(devices, list) or not devices:
            raise FlowContractError(f"{prefix}.selected_devices must contain already selected devices")
        for device_index, wrapper in enumerate(devices):
            if not isinstance(wrapper, dict):
                raise FlowContractError(f"{prefix}.selected_devices[{device_index}] must be an object")
            device = wrapper.get("device", wrapper)
            if not isinstance(device, dict):
                raise FlowContractError(f"{prefix}.selected_devices[{device_index}].device must be an object")
            if "is_abstract" in device and type(device["is_abstract"]) is not bool:
                raise FlowContractError("selected device is_abstract must be boolean")
            if device.get("is_abstract") is True:
                device_type = device.get("type")
                if (not isinstance(device_type, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", device_type)
                        or _is_core_type(device_type) or device_type == "common_device"):
                    raise FlowContractError("an abstract device requires an explicit non-IC component type")
                for owner in (wrapper, device):
                    for key in ("type", "deviceType", "device_type"):
                        if _is_core_type(owner.get(key)):
                            raise FlowContractError("IC type aliases cannot be used for abstract devices")
                    if any(owner.get(key) not in (None, "")
                           for key in ("id", "supplier_part", "supplierPart", "lichuang_id")):
                        raise FlowContractError("an abstract device must not carry a catalog identity")
            elif not any(isinstance(device.get(key), str) and device[key].strip()
                         for key in ("model", "supplier_part")):
                raise FlowContractError(f"{prefix}.selected_devices[{device_index}] requires model or supplier_part")
            for owner in (wrapper, device):
                if "quantity" in owner and (type(owner["quantity"]) is not int or owner["quantity"] < 1):
                    raise FlowContractError("selected device quantity must be a positive integer")
    if not isinstance(data.get("description", ""), str):
        raise FlowContractError("generation_input.description must be text")
    if not isinstance(data.get("system", {}), dict):
        raise FlowContractError("generation_input.system must be an object")
    links = data.get("links", [])
    if not isinstance(links, list):
        raise FlowContractError("generation_input.links must be an array")
    for link in links:
        if (not isinstance(link, dict)
                or not isinstance(link.get("source"), str)
                or not isinstance(link.get("target"), str)
                or link["source"] not in seen or link["target"] not in seen):
            raise FlowContractError("every declared link must reference existing source/target module IDs")
    return data


def build_generation_input(
    module_rows: Sequence[Mapping[str, Any]], *, description: str = "",
    system: Mapping[str, Any] | None = None, links: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Accept prepared module rows without executing any upstream stage."""
    if isinstance(module_rows, (str, bytes)) or not isinstance(module_rows, Sequence):
        raise FlowContractError("module_rows must be an array of prepared modules")
    value: dict[str, Any] = {"schema": SCHEMA, "modules": deepcopy(list(module_rows)), "description": description}
    if system is not None:
        value["system"] = deepcopy(dict(system))
    if links is not None:
        value["links"] = deepcopy(list(links))
    return validate_generation_input(value)


def generation_decomposition(value: Any) -> dict[str, Any]:
    """Project only explicitly supplied metadata for local/top-port agents."""
    data = validate_generation_input(value)
    return {"system": deepcopy(data.get("system", {})),
            "modules": [deepcopy(row["module"]) for row in data["modules"]],
            "links": deepcopy(data.get("links", []))}


__all__ = ["SCHEMA", "build_generation_input", "validate_generation_input", "generation_decomposition"]
