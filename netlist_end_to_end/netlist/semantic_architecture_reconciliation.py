"""Collect boundary proposals and preserve architecture ownership invariants."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy

from .prompts import load_prompt
from .semantic_architecture_validation import _error


_RECONCILIATION_CONSTRAINTS = (
    load_prompt('architecture_reconciliation.md')
)


_PROJECTED_RECONCILIATION_CONSTRAINTS = (
    load_prompt('architecture_projected_reconciliation.md')
)


def final_port_reconciliation(result: Mapping) -> dict | None:
    """Ask the boundary owner about an observed exhausted failure; never pick a peer."""
    from .scl import SCLSource, compile_scl
    if (result.get("planning_strategy") != "architecture"
            or any(row.get("stage") == "netlist_architecture_reconciliation"
                   for row in result.get("repair_history", []))):
        return None
    architecture, context = result.get("architecture"), result.get("context")
    if not isinstance(architecture, Mapping) or not isinstance(context, Mapping):
        return None
    try:
        checked = compile_scl([SCLSource(**row) for row in result["compiled_sources"]], context)
    except (KeyError, TypeError, ValueError):
        return None
    from .planner_projection import frozen_boundary_observations
    from validation_platform.contract_binding import check_top_port_bindings
    diagnostics = [*checked.diagnostics, *(check_top_port_bindings(architecture, checked.manifest) if checked.ok else [])]
    if any(row.get("code") == "PORT_BINDING_KIND_CONFLICT" for row in diagnostics):
        from .planner_projection import owned_fields_match
        try:
            if not owned_fields_match(result["plan"], architecture, "netlist"):
                return None
        except (KeyError, TypeError, AttributeError):
            return None
    if result.get("diagnostics") != diagnostics:
        return None
    observations = frozen_boundary_observations(architecture, context, checked.manifest, diagnostics)
    if not observations:
        return None
    modules = {row["id"]: row for row in architecture.get("modules", [])}
    plans = result.get("peripheral_plans", [])
    pending = {}
    for plan in plans:
        rows = plan.get("modules", []) if isinstance(plan, Mapping) else []
        if (len(rows) != 1 or rows[0].get("id") not in modules
                or rows[0]["id"] in pending or not isinstance(plan.get("unresolved"), list)
                or any(not isinstance(text, str) for text in plan["unresolved"])):
            return None
        pending[rows[0]["id"]] = deepcopy(plan["unresolved"])
    if not observations or set(pending) != set(modules):
        return None
    return {"origin": "final_internal_port", "architecture": deepcopy(architecture),
            "observations": observations, "peripheral_unresolved": pending,
            "discarded_peripheral_plans": deepcopy(plans),
            "construction_diagnostics": deepcopy(diagnostics)}


def _boundary_reconciliation(result: Mapping) -> dict | None:
    """Return exact observed boundary proposals, without adopting any of them."""
    issues = result.get("diagnostics")
    architecture = result.get("architecture")
    if not isinstance(issues, list) or not issues or not isinstance(architecture, Mapping):
        return None
    for issue in issues:
        if (not isinstance(issue, Mapping) or
                not (issue.get("code") == "PERIPHERAL_DOMAINS_CHANGED" or
                     issue.get("code") == "PERIPHERAL_BOUNDARY_REQUEST" and isinstance(issue.get("request"), Mapping) or
                     issue.get("code") == "PERIPHERAL_BOUNDARY_CHANGED" and issue.get("field") == "ports")):
            return None
    modules = {row["id"]: row for row in architecture.get("modules", [])}
    proposals = {}
    for plan in result.get("peripheral_plans", []):
        rows = plan.get("modules") if isinstance(plan, Mapping) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            return None
        mid = rows[0].get("id")
        pending = plan.get("unresolved")
        if (mid not in modules or mid in proposals or not isinstance(pending, list)
                or any(not isinstance(text, str) for text in pending)):
            return None
        proposals[mid] = plan
    if set(proposals) != set(modules):
        return None
    observations = []
    for issue in issues:
        mid = issue.get("module")
        if mid not in proposals:
            return None
        if issue["code"] == "PERIPHERAL_BOUNDARY_REQUEST":
            for field in ("ports", "voltage_domains"):
                if field in issue["request"]:
                    observations.append({"module": mid, "field": field,
                        "expected": deepcopy(architecture[field] if field == "voltage_domains" else modules[mid][field]),
                        "proposed": deepcopy(issue["request"][field]), "diagnostic": deepcopy(issue)})
            continue
        field = "voltage_domains" if issue["code"] == "PERIPHERAL_DOMAINS_CHANGED" else "ports"
        expected = architecture[field] if field == "voltage_domains" else modules[mid][field]
        proposed = proposals[mid].get(field) if field == "voltage_domains" else proposals[mid]["modules"][0].get(field)
        observations.append({"module": mid, "field": field, "expected": deepcopy(expected),
                             "proposed": deepcopy(proposed), "diagnostic": deepcopy(issue)})
    return {"architecture": deepcopy(architecture), "observations": observations,
            "peripheral_unresolved": {mid: deepcopy(plan["unresolved"]) for mid, plan in proposals.items()}}


def _reconciliation_invariants(candidate: Mapping, original: Mapping, *, peripheral_unresolved: Mapping | None = None) -> list[dict]:
    """Boundary reconsideration cannot erase device ownership or pending work."""
    issues = []
    before = {row["id"]: row for row in original["modules"]}
    after = {row["id"]: row for row in candidate["modules"]}
    if set(before) != set(after):
        issues.append(_error("ARCHITECTURE_RECONCILIATION_MODULES", "Reconciliation must preserve all original module identifiers"))
    for mid in before.keys() & after.keys():
        for field in ("label", "intent", "core_instances"):
            old, new = before[mid][field], after[mid][field]
            equal = Counter(old) == Counter(new) if field == "core_instances" else old == new
            if not equal:
                issues.append(_error("ARCHITECTURE_RECONCILIATION_OWNERSHIP", "Reconciliation cannot change module ownership or intent",
                                     module=mid, field=field, expected=deepcopy(old)))
    by_id = {row["id"]: row for row in candidate.get("obligations", [])}
    for obligation in original.get("obligations", []):
        if by_id.get(obligation["id"]) != obligation:
            issues.append(_error("ARCHITECTURE_RECONCILIATION_OBLIGATION", "Existing obligations must remain unchanged",
                                 obligation=obligation["id"], expected=deepcopy(obligation)))
    required_pending = set(original["unresolved"])
    for statements in (peripheral_unresolved or {}).values():
        required_pending.update(statements)
    missing = sorted(required_pending - set(candidate["unresolved"]))
    if missing:
        issues.append(_error("ARCHITECTURE_RECONCILIATION_PENDING", "Boundary repair cannot remove unresolved source or design statements",
                             expected=missing))
    return issues

