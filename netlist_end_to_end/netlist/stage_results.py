"""Input validation, failure artifacts and final netlist result assembly."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import FlowContractError
from ..generation_input import generation_decomposition, validate_generation_input
from ..tools import compact_summary
from ..tools.netlist_evidence.common import core_device_type
from .agents.netlist_linter import NetlistLinterAgent
from .catalog_gate import CatalogBindingGate, CatalogGateViolation
from .helpers import as_mapping


def module_label(row: Any, index: int) -> str:
    module = row.get("module") or row.get("module_info") or {} if isinstance(row, Mapping) else {}
    raw = str(module.get("id") or module.get("name") or f"module_{index + 1}") if isinstance(module, Mapping) else f"module_{index + 1}"
    return "".join(ch if ch.isalnum() or ch in "_-#." else "_" for ch in raw)


def netlist_info(payload: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("netlist_info", "result"):
        candidate = payload.get(key)
        if isinstance(candidate, Mapping):
            return dict(candidate)
    candidate = payload.get("netlist")
    if isinstance(candidate, Mapping):
        result: dict[str, Any] = {"netlist": dict(candidate)}
        for key in ("crossPageNet", "cross_page_net", "notes"):
            if key in payload:
                result[key] = payload[key]
        return result
    return dict(payload)


def lint_ok(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return bool(value)
    for key in ("ok", "passed", "valid"):
        if isinstance(value.get(key), bool):
            return bool(value[key])
    return str(value.get("status") or "").strip().lower() in {"", "ok", "pass", "passed", "success", "skipped"}


def module_failed(value: Mapping[str, Any]) -> bool:
    return str(value.get("status") or "").strip().lower() == "failed"


def knowledge_failed(evidence: Mapping[str, Any] | None) -> bool:
    return evidence is not None and str(evidence.get("status") or "").strip().lower() == "failed"


def failed_artifact(module_info: Mapping[str, Any], reason: str, evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A visible, non-mergeable artifact for one failed module."""

    return {
        "module_info": dict(module_info),
        "netlist_info": {
            "status": "failed",
            "netlist": {},
            "crossPageNet": {"inputs": [], "outputs": []},
            "notes": f"Module netlist generation failed: {reason}",
            "failure": {"reason": reason},
        },
        "cite_info": None,
        "lint": {
            "ok": False,
            "status": "failed",
            "summary": {"devices": 0, "nets": 0, "errors": 1},
            "issues": [{"code": "module_generation_failed", "severity": "error", "message": reason}],
        },
        "strategy": "failed",
        "status": "failed",
        "failure": {"reason": reason},
        "knowledge_evidence": compact_summary(evidence) if evidence else None,
    }


def read_inputs(generation_input: Any, user_input: str = "") -> dict[str, Any]:
    data = validate_generation_input(generation_input)
    return {
        "module_rows": data["modules"],
        "total": len(data["modules"]),
        "requirement_docs": data.get("description") or str(user_input or ""),
        "decomposition": generation_decomposition(data),
    }


def failed_module(reason: str, **kwargs: Any) -> dict[str, Any]:
    selection = kwargs.get("selection") or {}
    module = selection.get("module") or selection.get("module_info") or {} if isinstance(selection, Mapping) else {}
    return failed_artifact(module if isinstance(module, Mapping) else {}, reason)


def finalize(results: list[dict[str, Any]], normalized: Any, fail_on_lint: bool) -> list[dict[str, Any]]:
    """Apply the optional normalizer, run the catalog output gate, enforce fail_on_lint."""

    _require_complete_modules(results)
    gate_context = {
        index: (r.get("_knowledge_evidence"), r.get("_catalog_binding")) for index, r in enumerate(results)
    }
    if isinstance(normalized, Mapping):
        candidate = normalized.get("module_results") or normalized.get("netlist_output") or normalized.get("results")
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
            results = [dict(item) for item in candidate if isinstance(item, Mapping)]

    gated: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        result = dict(result)
        evidence, binding = gate_context.get(index, (None, None))
        evidence = result.pop("_knowledge_evidence", evidence)
        binding = result.pop("_catalog_binding", binding)
        if module_failed(result) or evidence is None:
            gated.append(result)
            continue
        try:
            canonical, _ = CatalogBindingGate.validate_output(
                netlist_info(result), evidence, expected_binding=binding
            )
            result["netlist_info"] = canonical
        except CatalogGateViolation as exc:
            module = result.get("module_info") or result.get("module") or {}
            result = failed_artifact(module, f"{exc.status}: {exc}", evidence)
        gated.append(result)

    _require_complete_modules(gated)
    for result in gated:
        structural = NetlistLinterAgent.execute(netlist_info=netlist_info(result))
        if not structural["ok"]:
            module = as_mapping(result.get("module_info") or result.get("module"))
            codes = ", ".join(issue["code"] for issue in structural["issues"] if issue["severity"] == "error")
            raise FlowContractError(f"invalid module {module.get('id') or module.get('name')}: {codes}")
        result["lint"] = structural
    if fail_on_lint:
        bad = [
            str((r.get("module_info") or {}).get("name") or "unknown")
            for r in gated
            if not module_failed(r) and not lint_ok(r.get("lint"))
        ]
        if bad:
            raise FlowContractError(f"netlist lint failed for modules: {', '.join(bad)}")
    return gated


def _require_complete_modules(results: list[dict[str, Any]]) -> None:
    """A partial board cannot advance to port merging; advisory lint is separate."""
    failures = []
    for result in results:
        info = as_mapping(result.get("netlist_info"))
        if not module_failed(result) and not module_failed(info):
            continue
        module = as_mapping(result.get("module_info") or result.get("module"))
        failure = as_mapping(result.get("failure") or info.get("failure"))
        label = module.get("id") or module.get("name") or "unknown"
        reason = failure.get("reason") or info.get("notes") or "module generation failed"
        failures.append(f"{label}: {reason}")
    if failures:
        raise FlowContractError("incomplete board; top-port merging blocked: " + "; ".join(failures))


def abstractize_common_devices(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep common parts electrical and abstract; never select purchased parts.

    Device keys, designators, types, values and pin-to-net mappings are kept.
    Core IC identity remains catalog-bound; only non-core procurement metadata
    and model labels are removed from the existing props/pins output format.
    """

    bound: list[dict[str, Any]] = []
    for result in results:
        result = dict(result)
        if module_failed(result):
            bound.append(result)
            continue
        info = netlist_info(result.get("netlist_info"))
        rebuilt: dict[str, Any] = {}
        for key, raw_device in info["netlist"].items():
            if not isinstance(raw_device, Mapping):
                rebuilt[key] = raw_device
                continue
            device = dict(raw_device)
            props = dict(as_mapping(raw_device.get("props")))
            is_core = core_device_type(props.get("deviceType"))
            props["is_abstract"] = not is_core
            if not is_core:
                props["supplierPart"] = ""
                for field in ("id", "supplier_part", "lichuang_id", "package", "footprint", "presetBinding",
                              "model", "mpn", "manufacturer", "brand", "price", "price_cny", "manufacturerPartNumber"):
                    props.pop(field, None)
                props["deviceName"] = str(props.get("deviceType") or "other")
                props["description"] = props["deviceName"]
            device["props"] = props
            rebuilt[key] = device
        info["netlist"] = rebuilt
        result["netlist_info"] = info
        result.pop("preset_bindings", None)
        bound.append(result)
    return bound
