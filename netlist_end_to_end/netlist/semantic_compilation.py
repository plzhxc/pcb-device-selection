"""Compile generated SCL and bind existing electrical checks to repair feedback."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Mapping
import hashlib
import json

from validation_platform.certificate_facts import digest as certificate_digest
from validation_platform.contract_binding import (
    bind_generation_contracts,
    check_declared_port_domains,
    check_top_port_bindings,
)
from validation_platform.netlist_graph import parse_netlist_graph, with_fixed_identity_anchors
from validation_platform.pin_contracts import check_pin_contracts

from .prompts import load_prompt
from .scl import SCLSource, compile_scl
from .semantic_shared import _diagnostic


def _compiled_pin_precheck(generation, knowledge, board_input):
    bound = bind_generation_contracts(generation, knowledge, board_input=board_input)
    graph = parse_netlist_graph(generation["compilation"]["netlist"],
                                source="generation:" + generation["board_id"])
    anchors = {}
    for cid, part in bound["authenticated_identity_anchors"].items():
        identity = bound["components"].get(cid, {}).get("fixed_identity")
        if not isinstance(identity, dict) or identity.get("supplier_part") != part:
            raise ValueError("authenticated generated anchor lacks matching typed identity")
        anchors[cid] = identity
    graph = with_fixed_identity_anchors(graph, anchors)
    checked = check_pin_contracts(graph, bound)
    return checked, _pin_failure_feedback(checked, generation, bound)


def _pin_failure_feedback(checked, generation, bound):
    modules = {m["id"]: m for m in generation["plan"]["modules"]}
    owners = {cid: (c["module"], c["device"]) for cid, c in bound["components"].items()
              if c.get("module") in modules}
    feedback = []
    for raw in checked["diagnostics"]:
        if raw.get("status") != "fail":
            continue
        row = {**deepcopy(raw), "code": raw["rule"], "severity": "error",
               "constraint_origin": "frozen_plan_binding" if raw["rule"] in
               {"PIN_DECLARED_UNUSED", "PIN_DECLARED_USE", "POWER_DOMAIN"} else
               "derived_existing_check" if raw["rule"] == "ARCHITECTURE_OBLIGATION" else
               "existing_checker",
               "repair_hint": load_prompt('semantic_pin_failure_repair.md')}
        endpoints = []
        for cid in [raw.get("component"), *raw.get("components", [])]:
            if cid in owners:
                module, alias = owners[cid]
                endpoints.append({"component": cid, "module": module, "device": alias})
        candidates = {x["module"] for x in endpoints}
        if raw.get("module") in modules:
            candidates.add(raw["module"])
        row["affected_instances"] = endpoints
        row["affected_modules"] = sorted(candidates)
        if len(candidates) == 1:
            module = next(iter(candidates)); row["module"] = module
            row["source"] = generation["board_id"] + ":" + module + ":local"
            if raw.get("component") in owners:
                _, alias = owners[raw["component"]]
                row["device"] = alias
                row["frozen_pin_selection"] = [deepcopy(p) for p in modules[module]["pin_usage"]
                    if p["device"] == alias and p["pin"] == raw.get("pin")]
        else:
            # No text/name heuristic chooses an owner; loop integration must retain
            # this explicitly unassigned or cross-module failure.
            row.pop("module", None); row.pop("source", None)
        feedback.append(row)
    return feedback


def _top_port_diagnostics(diagnostics: list[dict], board_id: str) -> list[dict]:
    """Retain global failures for a scheduled top repair, without assigning blame.

    A domain conflict can involve both local and top-level connections. It must
    be visible to both owners; routing it does not authorize a changed domain,
    deleted required connection, or automatic choice of replacement endpoints.
    """
    return [deepcopy(row) for row in diagnostics
            if row.get("source") == f"{board_id}:top"
            or row.get("code") == "UNCONNECTED_INTERNAL_PORT"
            or not (row.get("source") or row.get("module")
                    or row.get("endpoint", {}).get("module"))]


def _declared_rail_requirements(module_plan: Mapping) -> list[dict]:
    """Expose the frozen rail contract without inferring or creating a wire."""
    return [{"relation": "same_network",
             "port": {"kind": "port", "module": module_plan["id"], "port": pid, "bit": bit},
             "voltage_domain": {"kind": "voltage_domain", "id": port["domain"]},
             "source_refs": deepcopy(port.get("source_refs", []))}
            for pid, port in sorted(module_plan.get("ports", {}).items())
            if port.get("domain_binding") == "rail" and isinstance(port.get("domain"), str)
            and type(port.get("width")) is int and 1 <= port["width"] <= 1024
            for bit in range(port["width"])]


def _rail_failure_feedback(diagnostic: Mapping, module_plan: Mapping, manifest: Mapping) -> dict:
    """Attach actual endpoint groups to an existing failed rail check only."""
    result = deepcopy(dict(diagnostic))
    if diagnostic.get("rule") != "PORT_RAIL_DOMAIN_NET":
        return result
    matching = [row for row in _declared_rail_requirements(module_plan)
                if row["port"]["port"] == diagnostic.get("port") and row["port"]["bit"] == diagnostic.get("bit")
                and row["voltage_domain"]["id"] == diagnostic.get("domain")]
    if len(matching) != 1:
        return result
    result["required_relation"] = matching[0]
    net_ids = set(diagnostic.get("actual_port_nets", [])) | set(diagnostic.get("actual_rail_nets", []))
    result["actual_network_groups"] = [{"id": network["id"],
        "members": deepcopy(network.get("members", [])[:32]),
        "member_count": len(network.get("members", [])),
        "members_truncated": len(network.get("members", [])) > 32}
        for network in manifest.get("networks", []) if network.get("id") in net_ids]
    return result


def _compile_iteration(result: dict, sources: Mapping[str, SCLSource], *, domain_source: SCLSource | None,
                       pool: Mapping, knowledge: Mapping, operating_authority: Mapping,
                       templates: list, repairs: int, retain: bool,
                       planning_validation_mode: str):
    """Compile the current sources and retain the existing electrical audit results."""
    board_id, plan, context = result["board_id"], result["plan"], result["context"]
    domain_record = result["domain_materialization"]
    module_plans = {module["id"]: module for module in plan["modules"]}
    compiled_sources = sorted([*sources.values(), *([domain_source] if domain_source else [])], key=lambda source: source.name)
    result["compiled_sources"] = [asdict(source) for source in compiled_sources]
    compiled = compile_scl(compiled_sources, context)
    compilation = compiled.to_dict()
    compilation["manifest"]["domain_materialization"] = deepcopy(domain_record)
    compilation["manifest"]["fixed_inventory_sha256"] = result["fixed_inventory_sha256"]
    if "architecture_obligations" in result:
        compilation["manifest"]["architecture_obligations_sha256"] = certificate_digest(result["architecture_obligations"])
    if retain:
        compilation["manifest"]["planning_validation_mode"] = planning_validation_mode
    if "operating_selections" in result:
        compilation["manifest"]["operating_selections_sha256"] = hashlib.sha256(json.dumps(
            result["operating_selections"], ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    compilation["manifest"]["normalizations"] = deepcopy(result.get("normalizations", []))
    compile_errors = list(compiled.diagnostics)
    port_diagnostics = [_rail_failure_feedback(diagnostic, module, compiled.manifest)
                        for module in module_plans.values()
                        for diagnostic in check_declared_port_domains(module, compiled.manifest)]
    result["electrical_precheck"] = {"scope": "declared interface rail binding and direct top-level port binding-kind contracts; full pin-role/electrical checking remains required",
        "diagnostics": port_diagnostics,
        "status": "fail" if any(d["status"] == "fail" for d in port_diagnostics) else
                  "unresolved" if any(d["status"] == "unresolved" for d in port_diagnostics) else "checked"}
    if compiled.ok:
        compile_errors.extend(_diagnostic(d["rule"], d["message"],
            source=f"{board_id}:{d['module']}:local",
            **{key:value for key,value in d.items() if key not in {"rule", "message", "source"}})
            for d in port_diagnostics if d["status"] == "fail")
        boundary_diagnostics = check_top_port_bindings(plan, compiled.manifest)
        port_diagnostics.extend(boundary_diagnostics)
        compile_errors.extend(boundary_diagnostics)
        if boundary_diagnostics:
            result["electrical_precheck"]["status"] = "fail"
    from validation_platform.static_voltages import domain_definition
    planned_domains = {d["id"]: domain_definition(d) for d in plan["voltage_domains"]}
    actual_domains = {d["id"]: d for d in compiled.manifest["voltage_domains"]}
    if planned_domains != actual_domains:
        compile_errors.append(_diagnostic("UNAPPROVED_VOLTAGE_DOMAINS", "SCL domain declarations must exactly match the frozen plan; the frozen table supplies definitions, while agent declarations may only repeat them",
            missing=[planned_domains[key] for key in sorted(planned_domains.keys() - actual_domains.keys())],
            extra=[actual_domains[key] for key in sorted(actual_domains.keys() - planned_domains.keys())],
            conflicts=[{"id": key, "planned": planned_domains[key], "actual": actual_domains[key]}
                       for key in sorted(planned_domains.keys() & actual_domains.keys()) if planned_domains[key] != actual_domains[key]]))
    for instance in compilation["manifest"]["instances"]:
        if instance["device"] in pool:
            instance["identity"] = deepcopy(pool[instance["device"]]["identity"])
            instance["pin_space"] = "physical"
            if instance["identity"]["inventory_kind"] != "ic":
                compilation["manifest"].setdefault("identity_overlay", {})[instance["component"]] = deepcopy(instance["identity"])
        if instance["device"] in pool and pool[instance["device"]].get("pin_identity_mappings"):
            instance["pin_identity_mappings"] = deepcopy(pool[instance["device"]]["pin_identity_mappings"])
            instance["pin_space"] = "physical_with_catalog_aliases"
        elif instance["device"] not in pool:
            instance["pin_space"] = "logical"
            specification = module_plans[instance["module"]]["common_devices"][instance["device"]]
            for key in ("terminal_mapping", "parameters"):
                if key in specification:
                    instance[key] = deepcopy(specification[key])
            device = context["modules"][instance["module"]]["devices"][instance["device"]]
            candidates = [t for t in templates if t["device_type"] == device["type"]
                          and {(p["id"], p.get("width")) for p in t["ports"]} == {("a", 1), ("b", 1)}]
            if len(candidates) == 1 and {p["id"] for p in device["pins"]} == {"1", "2"}:
                instance["logical_pin_mapping"] = {"1": "a", "2": "b"}
                instance["logical_mapping_contract"] = "scl.two_terminal.1.0"
    compilation["manifest"]["export_level"] = "mixed_physical_core_abstract_common"
    result.pop("pin_contract_precheck", None)
    if compiled.ok and not compile_errors:
        checked, failures = _compiled_pin_precheck(
            {**result, "compilation": compilation}, knowledge, operating_authority["board_input"])
        result["pin_contract_precheck"] = {
            "scope": "generated graph and frozen input/source/plan only; no reference comparison or qualification grant",
            **checked, "unassigned_failures": [d for d in failures if not d.get("module")]}
        if failures:
            snapshot = {key: deepcopy(value) for key, value in result.items()
                        if key not in {"diagnostic_compiled_candidate", "pin_contract_precheck"}}
            snapshot.update(status="compiled", diagnostics=[], compilation=deepcopy(compilation),
                            sources=[asdict(s) for s in sorted(sources.values(), key=lambda s: s.name)])
            result["diagnostic_compiled_candidate"] = {
                "scope": "retained structural compilation for independent fidelity audit; not a qualified or final successful generation",
                "round": repairs, "qualified": False, "generation": snapshot,
                "pin_contract_check": deepcopy(checked)}
        else:
            result.pop("diagnostic_compiled_candidate", None)
        compile_errors.extend(failures)
    if compile_errors:
        compilation.update(ok=False, netlist=None, diagnostics=compile_errors)
        compilation["manifest"]["status"] = "invalid"
        compilation["manifest"].pop("netlist_sha256", None)
    result.update(sources=[asdict(s) for s in sorted(sources.values(), key=lambda s: s.name)],
                  compilation=compilation, diagnostics=compile_errors)
    return compiled, compilation, compile_errors

