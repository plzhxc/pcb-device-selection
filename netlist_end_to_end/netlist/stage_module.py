"""Module knowledge prefetch, catalog binding and planned-result handling."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import FlowContractError
from ..tools import NetlistKnowledgePrefetcher, compact_summary
from .catalog_gate import CatalogBindingGate, CatalogGateViolation
from .stage_results import knowledge_failed, netlist_info
from .prompts import load_prompt


def module_of(selection: Any, index: int) -> dict[str, Any]:
    module = selection.get("module") or selection.get("module_info") or {} if isinstance(selection, Mapping) else None
    if not isinstance(module, Mapping):
        raise FlowContractError(f"netlist module row {index + 1} has invalid module metadata")
    return dict(module)


async def prefetch_knowledge(
    prefetcher: NetlistKnowledgePrefetcher | None, module: Mapping[str, Any], selection: Mapping[str, Any]
) -> dict[str, Any]:
    """Resolve catalog identity and topology evidence once per module.

    Returns ``{"evidence": raw | None, "for_agents": raw | {}}``; a failed
    catalog lookup raises so the module falls back to a failed artifact.
    """

    if prefetcher is None:
        return {"evidence": None, "for_agents": {}, "summary": None}
    try:
        raw = await asyncio.to_thread(prefetcher.prefetch, module, selection)
        if not isinstance(raw, Mapping):
            raise TypeError("knowledge prefetcher must return a mapping")
        evidence = dict(raw)
    except Exception as exc:
        evidence = {
            "status": "failed",
            "catalog": {"status": "error", "reason": "catalog_prefetch_failed", "error_type": type(exc).__name__},
            "topology": {"status": "not_checked", "reason": "catalog_prefetch_failed"},
            "issues": [{"code": "catalog_prefetch_failed", "reason": type(exc).__name__}],
            "summary": {"catalog_queries": 0, "catalog_matches": 0, "topology_queries": 0, "topology_hits": 0},
            "read_only": True,
        }
    if knowledge_failed(evidence):
        catalog = evidence.get("catalog") if isinstance(evidence.get("catalog"), Mapping) else {}
        reason = str(catalog.get("reason") or "").strip()
        if not reason:
            issues = evidence.get("issues")
            first = next(iter(issues), None) if isinstance(issues, Sequence) and not isinstance(issues, (str, bytes)) else None
            if isinstance(first, Mapping):
                reason = str(first.get("reason") or first.get("message") or first.get("code") or "").strip()
        raise FlowContractError(
            f"{catalog.get('status') or 'knowledge_failed'}: {reason or 'required SQL catalog evidence is unavailable'}"
        )
    return {"evidence": evidence, "for_agents": evidence, "summary": compact_summary(evidence)}


def prebuild_matched(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    if isinstance(payload.get("matched"), bool):
        return bool(payload["matched"])
    return bool(payload.get("netlist_info") or payload.get("netlist"))


def assemble_module(module: Mapping[str, Any], prebuilt: Any, planned: Any, knowledge: Mapping[str, Any]) -> dict[str, Any]:
    evidence = knowledge.get("evidence")
    if planned is None and prebuild_matched(prebuilt):
        return {
            "module_info": dict(module),
            "netlist_info": netlist_info(prebuilt),
            "cite_info": prebuilt.get("cite_info"),
            "lint": prebuilt.get("lint") or {"ok": True, "skipped": True},
            "strategy": "prebuilt",
            "knowledge_evidence": knowledge.get("summary"),
            "_knowledge_evidence": evidence,
            "_catalog_binding": None,
        }
    if not isinstance(planned, Mapping):
        raise FlowContractError("netlist module produced neither a prebuilt nor a planned netlist")
    return {
        "module_info": dict(module),
        "netlist_info": planned["netlist_info"],
        "cite_info": planned.get("cite_info"),
        "lint": planned["lint"],
        "strategy": "planned",
        "knowledge_evidence": knowledge.get("summary"),
        "_knowledge_evidence": evidence,
        "_catalog_binding": planned.get("binding"),
    }


def plan_gate(plan_payload: Mapping[str, Any], knowledge: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the plan, bind its main chip to one exact catalog record, list sub-tasks."""

    candidate = plan_payload.get("plan") or plan_payload.get("master_plan") or plan_payload.get("netlist_plan") or plan_payload
    if not isinstance(candidate, Mapping):
        raise FlowContractError("netlist planner must return a mapping")
    plan = dict(candidate)
    binding = None
    evidence = knowledge.get("evidence")
    if evidence is not None:
        try:
            plan, binding = CatalogBindingGate.bind_plan(plan, evidence)
        except CatalogGateViolation as exc:
            raise FlowContractError(f"{exc.status}: {exc}") from exc
    raw = plan.get("sub_tasks") or plan.get("tasks")
    tasks = [dict(t) for t in raw if isinstance(t, Mapping)] if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else []
    if not tasks:
        tasks = [{"id": "direct_generation", "description": load_prompt("stage_direct_generation.md")}]
    return {"plan": plan, "binding": dict(binding) if binding else None, "sub_tasks": tasks}


def merge_fallback(reason: str, **kwargs: Any) -> dict[str, Any]:
    """Deterministic merge when the merger agent is unavailable or fails."""

    plan, sub_results = kwargs.get("plan") or {}, list(kwargs.get("sub_results") or [])
    if len(sub_results) == 1:
        return {"netlist_info": netlist_info(sub_results[0])}
    return {"netlist_info": {"plan": dict(plan), "sub_results": [dict(r) for r in sub_results]}}


def merged_view(merge: Mapping[str, Any], guidance: Any) -> dict[str, Any]:
    cite = merge.get("cite_info")
    if cite is None and isinstance(guidance, Mapping):
        cite = guidance.get("cite_info")
    return {"netlist_info": netlist_info(merge), "cite_info": cite}


def lint_fallback(reason: str, **kwargs: Any) -> dict[str, Any]:
    return {"ok": True, "status": "skipped", "issues": []}


def repaired_view(repair: Mapping[str, Any]) -> dict[str, Any]:
    return {"netlist_info": netlist_info(repair)}


def finalize_planned(rounds: int, **outputs: Any) -> dict[str, Any]:
    """Pick the latest netlist and lint that actually ran."""

    info = outputs["merged"]["netlist_info"]
    lint = outputs["lint_0"]
    for k in range(1, rounds + 1):
        repaired = outputs.get(f"repaired_{k}")
        if not isinstance(repaired, Mapping):
            break
        info = repaired["netlist_info"]
        lint = outputs.get(f"lint_{k}") or lint
    return {
        "netlist_info": info,
        "cite_info": outputs["merged"].get("cite_info"),
        "lint": lint,
        "binding": outputs.get("binding"),
    }
