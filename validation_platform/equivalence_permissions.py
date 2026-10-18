"""Bind equivalence permissions to the evaluator's actual complete case input.

Candidate certificates supply identifiers and proposed mappings only. The
evaluation entry point overwrites case_input_snapshot from read_artifact before
calling this module; a self-consistent candidate-provided input is never used.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from .certificate_facts import digest, effective_gpio_fact


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _input_binding(reference_contracts: Mapping | None, generated_contracts: Mapping | None,
                   board_id: str) -> tuple[dict, list[str]]:
    from netlist_end_to_end.experiments.inputs import validate_input
    errors, validated = [], []
    for side, contracts in (("reference", reference_contracts), ("generated", generated_contracts)):
        contracts = _map(contracts)
        snapshot = _map(contracts.get("case_input_snapshot")); record = _map(snapshot.get("record"))
        try:
            data = validate_input(_map(snapshot.get("payload")))
            sha = digest(data)
            if snapshot.get("sha256") != sha or record.get("sha256") != sha or not isinstance(record.get("path"), str) or not record["path"]:
                errors.append(side + " full input snapshot or artifact identity changed")
            boards = [b for b in data["boards"] if b["board_id"] == board_id]
            if len(boards) != 1:
                errors.append(side + " board is absent from the complete input")
            else:
                projection = {"case_id": data["case_id"], **boards[0], "request": data["request"], "requirements": data["requirements"]}
                if contracts.get("input_sha256") != digest(projection):
                    errors.append(side + " board projection does not match the bound input")
            validated.append({"data": data, "sha256": sha, "record": dict(record)})
        except (ValueError, TypeError, KeyError):
            errors.append(side + " has no valid evaluator-frozen complete case input")
    if len(validated) != 2 or validated[0] != validated[1]:
        errors.append("both graphs must use the same complete input artifact")
    return (validated[0] if validated else {}), errors


def _input_evidence(known: Mapping, locator: str, value: Any) -> dict:
    return {"source_kind": "input", "source_ref": f"input:{known['sha256']}:{locator}",
            "document_sha256": known["sha256"], "locator": locator,
            "quote": json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}


def validate_permission_binding(cert: Mapping, permissions: Mapping,
                                reference_contracts: Mapping | None, generated_contracts: Mapping | None) -> list[str]:
    """Reject permissions fabricated from a board hash or candidate-side input."""
    board_id = permissions.get("board_id")
    known, errors = _input_binding(reference_contracts, generated_contracts, board_id)
    if errors:
        return errors
    if permissions.get("input_sha256") != known["sha256"]:
        errors.append("permission header differs from the actual complete input digest")
    rules = permissions.get("rules", [])
    matches = [r for r in rules if isinstance(r, Mapping) and r.get("id") == cert.get("permission_id")] if isinstance(rules, list) else []
    if len(matches) != 1:
        return errors + ["permission must resolve one frozen input rule"]
    rule = matches[0]; expected = None
    if cert.get("kind") == "gpio_permutation":
        if known["data"]["permissions"].get("gpio_remapping") is not True:
            errors.append("actual complete input does not authorize GPIO reassignment")
        expected = _input_evidence(known, "/permissions/gpio_remapping", True)
    elif cert.get("kind") == "value_interval":
        # A generic GPIO permission cannot authorize value substitutions. This
        # separate finite input selection names the device whose reviewed
        # application rule will supply the interval and applicability facts.
        matches = [r for r in known["data"]["requirements"] if r["id"] == rule.get("input_requirement_id")]
        selection = _map(matches[0].get("selection")) if len(matches) == 1 else {}
        if not (selection.get("field") == "requirements.allow_documented_value_alternatives" and
                selection.get("value") is True and selection.get("board_id") == board_id and
                selection.get("device_id") == rule.get("source_supplier_part") and selection.get("applies_to") == "all_instances"):
            errors.append("value alternatives lack an explicit device-scoped input permission")
        else:
            expected = _input_evidence(known, "/requirements/" + matches[0]["id"] + "/selection", selection)
    elif cert.get("kind") in {"connector_contact_permutation", "connector_connection_correspondence"}:
        # Both theorem kinds consume exactly the same finite contact action.
        # The weaker connection claim grants no additional action or value use.
        matches = [p for p in known["data"]["permissions"].get("connector_co_design", [])
                   if p.get("id") == rule.get("input_permission_id") and p.get("board_id") == board_id]
        if len(matches) != 1 or rule.get("connector_permission") != matches[0]:
            errors.append("connector co-design lacks the exact scoped permission from the complete input")
        else:
            expected = _input_evidence(known, "/permissions/connector_co_design/" + matches[0]["id"], matches[0])
    else:
        errors.append("input permission kind is unsupported")
    refs = rule.get("evidence", [])
    if expected is None or not any(_map(ref) == expected for ref in (refs if isinstance(refs, list) else [])):
        errors.append("permission citation does not match the exact input field and value")
    return errors


def bind_equivalence_permissions(reference_graph: Any, reference_contracts: Mapping | None,
                                 generated_contracts: Mapping | None, *, board_id: str) -> dict:
    """Bind source-qualified GPIO and explicitly scoped private connector rules."""
    known, errors = _input_binding(reference_contracts, generated_contracts, board_id)
    result = {"permissions": {"schema": "pcbagent.equivalence_permissions.v1", "board_id": board_id,
              "input_sha256": known.get("sha256"), "rules": [], "context": {}}, "diagnostics": []}
    if errors:
        result["diagnostics"] = [{"rule": "PERMISSION_INPUT_BINDING", "status": "unresolved", "message": e} for e in errors]
        return result
    for key, component in reference_graph.components.items():
        if not component.is_core or known["data"]["permissions"]["gpio_remapping"] is not True:
            continue
        candidates = []
        for pid in reference_graph.pin_map(key):
            fact, issues = effective_gpio_fact(reference_graph, reference_contracts, key[1], pid)
            if not issues and fact.get("semantic_role") in {"logic_in", "logic_out", "logic_io"} and fact.get("gpio_remap_safe") is True and "gpio" in (fact.get("capabilities") or []):
                candidates.append(pid)
        if len(candidates) > 1:
            result["permissions"]["rules"].append({"id": "gpio:" + key[1], "kind": "gpio_permutation",
                "reference_component": key[1], "source_supplier_part": component.anchor_id,
                "applies": True, "pin_ids": sorted(candidates), "allowed_usages": ["gpio"],
                "evidence": [_input_evidence(known, "/permissions/gpio_remapping", True)]})
    from .connector_equivalence import resolve_connector
    for permission in known["data"]["permissions"].get("connector_co_design", []):
        if permission["board_id"] != board_id:
            continue
        key, issues = resolve_connector(reference_graph, permission)
        if issues:
            result["diagnostics"].extend({"rule": "PERMISSION_CONNECTOR_SCOPE", "status": "unresolved", "message": issue}
                                         for issue in issues)
            continue
        result["permissions"]["rules"].append({"id": "connector:" + permission["id"],
            "kind": "connector_contact_permutation", "reference_component": key[1], "applies": True,
            "input_permission_id": permission["id"], "connector_permission": permission,
            "evidence": [_input_evidence(known, "/permissions/connector_co_design/" + permission["id"], permission)]})
    return result


__all__ = ["bind_equivalence_permissions", "validate_permission_binding"]
