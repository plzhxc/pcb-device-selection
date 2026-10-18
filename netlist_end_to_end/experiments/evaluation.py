"""Evaluation-only reference access and four-score case qualification."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any

from validation_platform.consistency import compare_consistency
from validation_platform.contract_binding import bind_generation_contracts
from validation_platform.reference_binding import bind_reference_contracts, authenticate_physical_pin_inventory
from validation_platform.equivalence_permissions import bind_equivalence_permissions
from validation_platform.gpio_configuration import propose_equivalence_certificates
from validation_platform.netlist_graph import (parse_netlist_graph, _unique_object, with_functional_identity_anchors,
    with_fixed_identity_anchors, PHYSICAL_DEVICE_TYPES)

from .inputs import digest, validate_input, reference_parse_options
from .records import PinContractStore, RecordStore

METRICS = ("raw_connection", "raw_value", "adjusted_connection", "adjusted_value")
MINIMUM_SCORE = 0.80


def evaluation_implementation_sha256() -> str:
    """Freeze the evaluator and its source-owned graph/contract dependencies."""
    root = Path(__file__).resolve().parents[1]
    source_root = root.parent
    paths = [Path(__file__).resolve(), root / "experiments" / "inputs.py"]
    for package in (source_root / "validation_platform", root / "common_devices"):
        paths.extend(p for p in package.rglob("*") if p.is_file() and p.suffix in {".py", ".json", ".yaml", ".yml"} and "__pycache__" not in p.parts)
    # Evidence validation imports the terminal/position and source-asset helpers;
    # their changes must invalidate a frozen evaluation configuration as well.
    paths.extend(p for p in (source_root / "datasheet_automation" / "knowledge").rglob("*.py")
                 if p.is_file() and "__pycache__" not in p.parts)
    return digest({str(p.relative_to(source_root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))})


_LOADED_EVALUATION_IMPLEMENTATION = evaluation_implementation_sha256()


def read_artifact(record: Mapping[str, str]) -> Any:
    value = json.loads(Path(record["path"]).read_text(encoding="utf-8"))
    if digest(value) != record["sha256"]:
        raise ValueError("artifact content does not match its frozen digest")
    return value


def aggregate_metrics(reports: list[Mapping[str, Any]]) -> dict[str, dict]:
    """Sum fixed per-board units; missing boards must have explicit zero matches."""
    result = {}
    for name in METRICS:
        rows = [r["metrics"][name] for r in reports]
        fields = {field: sum(row[field] for row in rows)
                  for field in ("matched", "reference_total", "generated_total")}
        rt, gt, match = (fields[k] for k in ("reference_total", "generated_total", "matched"))
        score = 2 * match / (rt + gt) if rt + gt else None
        if score is not None and not 0 <= score <= 1:
            raise ValueError("invalid metric contributions; duplicated credit or denominator error")
        result[name] = {**fields, "score": score, "percentage": 100 * score if score is not None else None,
            "precision": match / gt if gt else None, "recall": match / rt if rt else None,
            "aggregation": "micro F1 over all original boards; empty strata are not automatic matches"}
    return result


def _source_locations(value: Mapping[str, str] | None) -> dict[str, str]:
    """An explicit evaluator-only full-path map; no search or prefix rewrite."""
    if value is None:
        return {}
    if not isinstance(value, Mapping) or any(
            not isinstance(old, str) or not isinstance(new, str)
            or not old.strip() or not new.strip() or "\0" in old or "\0" in new
            or not Path(old).is_absolute() or not Path(new).is_absolute()
            for old, new in value.items()):
        raise ValueError("source_locations must map full original paths to full resolved paths")
    return dict(value)


def _read_frozen_source(original: str, expected_sha256: str, *, locations: Mapping[str, str],
                        consumed: list[dict] | None, kind: str) -> bytes:
    if (not isinstance(original, str) or not original.strip()
            or not isinstance(expected_sha256, str) or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)):
        raise ValueError("a frozen original source path and SHA256 are required")
    resolved = locations.get(original, original)
    try:
        content = Path(resolved).read_bytes()
    except FileNotFoundError as error:
        raise FileNotFoundError(
            "frozen source path is missing: " + original
            + "; for renamed cleaned netlists, provide --source-locations mapping the old full path "
            "to the current <case>-cleaned.json (the frozen SHA256 is still checked), "
            "or rebuild the case input and reference manifest"
        ) from error
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256:
        raise ValueError("frozen source bytes changed: " + original)
    entry = {"kind": kind, "original_path": original, "resolved_path": resolved, "sha256": actual}
    if consumed is not None and entry not in consumed:
        consumed.append(entry)
    return content


def _physical_reference_anchors(raw: Mapping, ref: Mapping, *, case_id: str,
                                source_locations: Mapping[str, str] | None = None,
                                consumed_sources: list[dict] | None = None) -> dict:
    """Authenticate identity-only CAD inventory citations, never infer pin maps."""
    anchors = {}
    locations = _source_locations(source_locations)
    resolutions = ref.get("physical_identity_resolutions", {})
    if not isinstance(resolutions, Mapping):
        raise ValueError("physical identity resolutions must be an explicit mapping")
    for cid, resolution in resolutions.items():
        if not isinstance(resolution, Mapping):
            raise ValueError("physical identity resolution must be an object")
        source = resolution.get("source", {})
        if (cid not in raw or raw[cid]["type"] not in PHYSICAL_DEVICE_TYPES or raw[cid].get("is_abstract") is not True
                or raw[cid]["id"] is not None or resolution.get("component_type") != raw[cid]["type"]
                or resolution.get("model_name") != raw[cid]["name"] or resolution.get("reference_sha256") != ref["sha256"]
                or not isinstance(source, Mapping) or source.get("scope") != "physical_device"
                or source.get("kind") != "original_cad_bom_identity" or source.get("cleaned_component") != cid):
            raise ValueError("invalid reference-side typed physical identity resolution")
        identity_record = source.get("identity_inventory")
        if not isinstance(identity_record, Mapping):
            raise ValueError("physical reference identity lacks its original CAD inventory artifact")
        inventory = read_artifact(identity_record)
        if (inventory.get("schema") != "pcbagent.connection_free_device_inventory.v1" or inventory.get("case_id") != case_id
                or inventory.get("source_sha256") != source.get("original_source_sha256")):
            raise ValueError("physical reference source inventory or original CAD identity changed")
        _read_frozen_source(inventory.get("source_path"), inventory.get("source_sha256"),
            locations=locations, consumed=consumed_sources, kind="physical_identity_original_cad")
        hits = [(index, row) for index, row in enumerate(inventory.get("instances", [])) if isinstance(row, Mapping)
                and row.get("source_component") == source.get("original_component")
                and row.get("device_id") == resolution.get("supplier_part") and row.get("model") == resolution.get("model_name")
                and all(row.get(field) == source.get(field) for field in ("manufacturer", "package", "datasheet_url"))]
        if len(hits) != 1 or resolution.get("source_ref") != f"artifact:{identity_record['sha256']}#/instances/{hits[0][0]}":
            raise ValueError("physical reference identity citation does not select one exact original inventory row")
        if resolution.get("status") in {"confirmed", "project_scoped"}:
            anchors[cid] = {"supplier_part": resolution["supplier_part"], "component_type": raw[cid]["type"],
                            "inventory_kind": "physical_device"}
    return anchors


def _generated_fixed_anchors(bound: Mapping) -> dict:
    result = {}
    for cid, part in bound.get("authenticated_identity_anchors", {}).items():
        identity = bound.get("components", {}).get(cid, {}).get("fixed_identity")
        if not isinstance(identity, Mapping) or identity.get("supplier_part") != part:
            raise ValueError("authenticated generated anchor lacks the corresponding frozen typed identity")
        result[cid] = identity
    return result


def _physical_inventory_gate(reference: Any, generated: Any, board: Mapping, reference_bound: Mapping,
                             generated_bound: Mapping, knowledge: Mapping, reference_anchors: Mapping) -> dict:
    expected = Counter()
    for device in board.get("physical_devices", []):
        expected[(device["component_type"], device["device_id"])] += device["quantity"]
    physical_reference = {k: c for k, c in reference.components.items() if c.type in PHYSICAL_DEVICE_TYPES}
    physical_generated = {k: c for k, c in generated.components.items() if c.type in PHYSICAL_DEVICE_TYPES}
    actual = Counter((c.type, c.anchor_id) for c in physical_generated.values() if c.identity_anchor is not None)
    reference_count = Counter((c.type, c.anchor_id) for c in physical_reference.values() if c.identity_anchor is not None)
    count_ok = (actual == expected == reference_count and len(physical_reference) == len(reference_anchors) == sum(expected.values())
                and len(physical_generated) == sum(expected.values()))
    source_checks = {}
    for key, component in physical_generated.items():
        source_checks[key[1]] = authenticate_physical_pin_inventory(generated, key, knowledge.get(component.anchor_id, {}))
    source_ok = (all(row["authenticated"] for row in source_checks.values()) and all(
        reference_bound.get("components", {}).get(key[1], {}).get("source_identity_authenticated") is True
        for key in physical_reference))
    frozen_ok = (not expected and not physical_generated or
                 generated_bound.get("fixed_inventory_authentication", {}).get("status") == "authenticated")
    return {"pass": count_ok and source_ok and frozen_ok, "typed_counts": count_ok,
            "source_pin_inventory": source_ok, "fixed_inventory": frozen_ok,
            "source_diagnostics": source_checks}


def evaluate_case_run(run: Mapping[str, Any], manifest: Mapping[str, Any], *,
                      records: RecordStore, contracts: PinContractStore | None = None,
                      reference_contracts: Mapping[str, Any] | None = None,
                      permissions: Mapping[str, Any] | None = None,
                      certificates: Mapping[str, list] | None = None,
                      source_locations: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Validate generated data first, then compare each board to its own reference.

    A failed generation contributes no matches and retains every reference unit.
    Database role writes identify the exact run and cannot alter device facts.
    Reference facts are derived independently from frozen source knowledge.
    Supplied reference annotations cannot replace those facts. Certificates
    remain proposals until the independent consistency consumer verifies them.
    Optional source_locations changes only where frozen reference/CAD bytes are
    read. Original paths remain graph identities; frozen input/artifacts are not
    relocated or edited and every consumed source is checked against its SHA256.
    """
    if run.get("schema") != "pcbagent.semantic_case_run.v1" or manifest.get("schema") != "pcbagent.case_reference_manifest.v1":
        raise ValueError("explicit generation run and reference manifest required")
    locations = _source_locations(source_locations)
    implementation_before = evaluation_implementation_sha256()
    data = validate_input(read_artifact(run["input"]))
    if data["case_id"] != run["case_id"] or data["case_id"] != manifest["case_id"]:
        raise ValueError("case identity mismatch")
    if data["provenance"]["source_manifest_sha256"] != digest(manifest):
        raise ValueError("input was derived from a different reference manifest")
    boards = {b["board_id"]: b for b in data["boards"]}
    outputs = {b["board_id"]: b for b in run["boards"]}
    refs = {b["board_id"]: b for b in manifest["references"]}
    if len(outputs) != len(run["boards"]) or set(outputs) - boards.keys() or len(refs) != len(manifest["references"]) or set(refs) != set(boards):
        raise ValueError("board inventory mismatch or duplicated board result")
    reports, summaries, consumed_sources = [], [], []
    for board_id, ref in refs.items():
        path = Path(ref["path"])
        board_sources = []
        content = _read_frozen_source(ref["path"], ref.get("sha256"), locations=locations,
            consumed=board_sources, kind="reference_netlist")
        # Parse exactly the authenticated bytes with the existing duplicate-key
        # guard; do not reopen the relocated path after its hash was checked.
        raw = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        for cid, resolution in ref.get("identity_resolutions", {}).items():
            if resolution.get("reference_sha256") != ref["sha256"] or not resolution.get("source_ref"):
                raise ValueError("invalid reference-side identity resolution")
            if raw[cid]["id"] and raw[cid]["id"] != resolution["supplier_part"]:
                raise ValueError("identity resolution attempts to overwrite a confirmed ID")
            raw[cid]["id"] = resolution["supplier_part"]
        reference = parse_netlist_graph(raw, source=str(path), **reference_parse_options(ref))
        functional_anchors = {}
        for cid, resolution in ref.get("functional_identity_resolutions", {}).items():
            if (cid not in raw or raw[cid]["type"] != "module"
                    or raw[cid].get("is_abstract") is not True or raw[cid]["id"] is not None
                    or resolution.get("reference_sha256") != ref["sha256"]
                    or not resolution.get("source_ref") or resolution.get("source", {}).get("scope") != "functional_module"):
                raise ValueError("invalid reference-side functional-module identity resolution")
            if resolution.get("status") in {"confirmed", "project_scoped"}:
                functional_anchors[cid] = resolution["supplier_part"]
        reference = with_functional_identity_anchors(reference, functional_anchors)
        physical_anchors = _physical_reference_anchors(raw, ref, case_id=data["case_id"],
            source_locations=locations, consumed_sources=board_sources)
        reference = with_fixed_identity_anchors(reference, physical_anchors)
        output_record = outputs.get(board_id, {})
        output = read_artifact(output_record["output"]) if output_record.get("output") else {}
        knowledge = read_artifact(output_record["knowledge"]) if output_record.get("knowledge") else {}
        compilation = output.get("compilation") or {}
        structurally_valid = output.get("status") == "compiled" and compilation.get("ok") is True
        generated = parse_netlist_graph(compilation["netlist"] if structurally_valid else {}, source=run["run_id"] + ":" + board_id)
        board_input = {"case_id": data["case_id"], **boards[board_id], "request": data["request"], "requirements": data["requirements"]}
        bound = bind_generation_contracts(output, knowledge, board_input=board_input)
        # Only this evaluator has read and verified the complete input record.
        # The board projection omits permissions and cannot authorize changes.
        input_snapshot = {"record": deepcopy(run["input"]), "sha256": digest(data), "payload": deepcopy(data)}
        bound["case_input_snapshot"] = deepcopy(input_snapshot)
        generated = with_fixed_identity_anchors(generated, _generated_fixed_anchors(bound))
        # This explicit check precedes comparison and is persisted independently.
        from validation_platform.pin_contracts import check_pin_contracts
        electrical = check_pin_contracts(generated, bound)
        # Rebuilt by the independent binder/checker, never generator-declared closure.
        pending_checks = [row for row in electrical["diagnostics"]
                          if row.get("rule") == "PLANNING_PENDING_FACT"]
        planning_pass = not pending_checks
        ref_bound = bind_reference_contracts(reference, knowledge, board_input=board_input,
            reference_annotations=(reference_contracts or {}).get(board_id))
        ref_bound["case_input_snapshot"] = deepcopy(input_snapshot)
        permission_proposal = bind_equivalence_permissions(reference, ref_bound, bound, board_id=board_id)
        selected_permissions = deepcopy((permissions or {}).get(board_id, permission_proposal["permissions"]))
        supplied_certificates = (certificates or {}).get(board_id)
        certificate_proposal = (propose_equivalence_certificates(reference, generated, ref_bound, bound, selected_permissions)
            if structurally_valid and supplied_certificates is None else
            {"certificates": [], "diagnostics": [], "states": 0, "search_complete": None})
        selected_certificates = deepcopy(supplied_certificates if supplied_certificates is not None else certificate_proposal["certificates"])
        report = compare_consistency(generated, reference, reference_contracts=ref_bound,
            generated_contracts=bound, permissions=selected_permissions, certificates=selected_certificates)
        actual_cores = Counter(c.anchor_id for c in generated.components.values() if c.is_core and c.type == "ic")
        expected_cores = Counter()
        for device in boards[board_id]["core_devices"]:
            expected_cores[device["device_id"]] += device["quantity"]
        actual_functional = Counter(c.anchor_id for c in generated.components.values() if c.identity_anchor is not None and c.type == "module")
        expected_functional = Counter()
        for device in boards[board_id].get("functional_devices", []):
            expected_functional[device["device_id"]] += device["quantity"]
        reference_functional_count = sum(c.type == "module" and bool(c.name and c.name.strip()) for c in reference.components.values())
        functional_gate = (actual_functional == expected_functional and None not in expected_functional
                           and sum(expected_functional.values()) == reference_functional_count
                           and len(functional_anchors) == reference_functional_count)
        physical_gate = _physical_inventory_gate(reference, generated, boards[board_id], ref_bound, bound, knowledge, physical_anchors)
        gate = (structurally_valid and planning_pass and electrical["status"] == "pass" and actual_cores == expected_cores
                and functional_gate and physical_gate["pass"])
        if contracts and structurally_valid:
            contracts.save_bindings(run_id=run["run_id"], case_id=data["case_id"], board_id=board_id,
                                    source_kind="generated", payload=bound)
            contracts.save_bindings(run_id=run["run_id"], case_id=data["case_id"], board_id=board_id,
                                    source_kind="reference", payload=ref_bound)
        report.update(board_id=board_id, generation_status=output.get("status", output_record.get("status", "missing")),
            gates={"structural": structurally_valid, "electrical": electrical["status"],
                   "planning_pending": "unresolved" if pending_checks else "none",
                   "core_inventory": actual_cores == expected_cores, "functional_inventory": functional_gate,
                   "physical_inventory": physical_gate["pass"], "pass": gate},
            physical_inventory_details=physical_gate, consumed_sources=board_sources,
            reference_sha256=ref["sha256"], generated_bindings_sha256=digest(bound),
            reference_bindings_sha256=digest(ref_bound),
            equivalence_proposals={"permission_diagnostics": permission_proposal["diagnostics"],
                "certificate_diagnostics": certificate_proposal["diagnostics"],
                "states": certificate_proposal["states"], "search_complete": certificate_proposal["search_complete"],
                "origin": "explicit_evaluator_proposals" if supplied_certificates is not None else "bounded_source_qualified_search",
                "submitted_count": len(selected_certificates)})
        reports.append(report)
        consumed_sources.extend(row for row in board_sources if row not in consumed_sources)
        summaries.append({"board_id": board_id, "gates": report["gates"], "metrics": report["metrics"],
                          "coverage": report["coverage"], "report": records.put("board-evaluations", report),
                          "bindings": records.put("case-bindings", bound),
                          "reference_bindings": records.put("reference-bindings", ref_bound),
                          "permissions": records.put("equivalence-permissions", selected_permissions),
                          "certificates": records.put("equivalence-certificates", selected_certificates)})
    metrics = aggregate_metrics(reports)
    evaluation_stable = implementation_before == _LOADED_EVALUATION_IMPLEMENTATION == evaluation_implementation_sha256()
    candidate_pass = evaluation_stable and run.get("configuration_stable") is True and all(r["gates"]["pass"] for r in reports) and all(
        metrics[key]["score"] is not None and metrics[key]["score"] >= MINIMUM_SCORE
        for key in ("adjusted_connection", "adjusted_value"))
    result = {"schema": "pcbagent.case_evaluation.v1", "case_id": data["case_id"],
        "run_id": run["run_id"], "input_sha256": digest(data), "reference_manifest_sha256": digest(manifest),
        "configuration_sha256": run["configuration_sha256"],
        "evaluation_implementation_sha256": _LOADED_EVALUATION_IMPLEMENTATION,
        "evaluation_configuration_stable": evaluation_stable,
        "knowledge_sha256": digest({key: row.get("knowledge", {}).get("sha256") for key, row in sorted(outputs.items())}),
        "consumed_sources": consumed_sources,
        "metrics": metrics, "boards": summaries, "minimum_adjusted_score": MINIMUM_SCORE,
        "candidate_pass": candidate_pass, "qualified": False,
        "qualification_reason": "frozen repeatability runs still required" if candidate_pass else "one or more validity or score gates failed"}
    result["record"] = records.put("case-evaluations", result)
    return result


def qualify_repeats(evaluations: list[Mapping[str, Any]]) -> dict[str, Any]:
    if len(evaluations) < 3 or len({e["run_id"] for e in evaluations}) != len(evaluations):
        raise ValueError("at least three distinct frozen generation runs are required")
    if any(not e.get("evaluation_implementation_sha256") for e in evaluations):
        raise ValueError("qualification requires an explicit evaluator implementation digest for every run")
    if any(e.get("evaluation_configuration_stable") is not True for e in evaluations):
        raise ValueError("qualification requires an unchanged loaded evaluator throughout each run")
    if any(len({e[field] for e in evaluations}) != 1 for field in
           ("case_id", "input_sha256", "reference_manifest_sha256", "configuration_sha256", "knowledge_sha256", "evaluation_implementation_sha256")):
        raise ValueError("repeatability runs must use the same case, input, reference, knowledge and configuration")
    scores = {name: min(e["metrics"][name]["score"] for e in evaluations)
              if all(e["metrics"][name]["score"] is not None for e in evaluations) else None for name in METRICS}
    good = all(e["candidate_pass"] for e in evaluations) and all(scores[n] is not None and scores[n] >= MINIMUM_SCORE
        for n in ("adjusted_connection", "adjusted_value"))
    return {"schema": "pcbagent.case_qualification.v1", "case_id": evaluations[0]["case_id"],
            "qualified": good, "minimum_scores": scores, "run_ids": [e["run_id"] for e in evaluations],
            "input_sha256": evaluations[0]["input_sha256"], "threshold": MINIMUM_SCORE}


__all__ = ["evaluate_case_run", "aggregate_metrics", "qualify_repeats", "read_artifact"]
