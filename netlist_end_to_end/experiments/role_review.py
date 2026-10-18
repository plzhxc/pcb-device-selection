"""Revalidate stored extraction attempts against original sources without new inference."""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from datasheet_automation.knowledge.evidence import (
    authenticate_source_quote, primary_source_registry, quote_in_source,
)
from validation_platform.conditional_pin_rules import rule_digest

from .inputs import digest
from .records import PinContractStore, RecordStore
from .roles import authenticate_pin_claims


def revalidate_device_roles(part: str, *, service: Any, contracts: PinContractStore,
                            records: RecordStore) -> dict[str, Any]:
    """Replay exact model proposals through the current source/condition guards.

    The original PDF is read again from SQL, and must have the same hash. Existing
    versions are preserved. This operation changes neither input nor model output.
    """
    old = contracts.latest_device(part, source_authentication_version=2)
    if not old:
        return {"supplier_part": part, "status": "not_ready", "reason": "no primary-source extraction version"}
    run_ids = {row["run_id"] for row in old.get("batches", [])}
    events = {}
    for path in (records.root / "events").glob("*.json"):
        event = json.loads(path.read_text(encoding="utf-8"))
        if digest(event) != path.stem:
            raise ValueError("stored experiment event hash mismatch")
        payload = event.get("payload", {})
        if event.get("kind") == "pin_role_agent" and payload.get("run_id") in run_ids and payload.get("supplier_part") == part:
            events[payload["run_id"]] = payload
    if set(events) != run_ids:
        return {"supplier_part": part, "status": "not_ready", "reason": "not all original extraction batches are retained"}
    facts, diagnostics = {}, []
    for batch in old["batches"]:
        event = events[batch["run_id"]]
        supplied = event.get("inputs") or {}
        if digest(supplied) != event.get("input_sha256"):
            raise ValueError("extraction input snapshot hash mismatch")
        excerpts = supplied.get("excerpts", [])
        pages = sorted({row["page_idx"] + 1 for row in excerpts})
        current = service.read_pdf_page_evidence(part, pages=pages, limit=200)
        if (current.get("document") or {}).get("pdf_sha256") != old["document_sha256"]:
            return {"supplier_part": part, "status": "not_ready", "reason": "document version changed; new extraction required"}
        actual = {row["source_ref"]: row for row in current["evidence"]}
        authenticated = []
        for row in excerpts:
            source = actual.get(row.get("source_ref"))
            if not source or source["content"] != row.get("content"):
                raise ValueError("stored extraction source differs from original PDF page")
            authenticated.append({**source, "id": row["id"]})
        current["evidence"] = authenticated
        proposals = event.get("result", {}).get("result", {}).get("pins", []) if event["status"] == "success" else []
        updated, issues = authenticate_pin_claims(proposals, batch["pins"], current)
        facts.update(updated)
        diagnostics.extend(issues)
    if set(facts) != set(old["pins"]):
        raise ValueError("revalidation must preserve the complete pin inventory")
    new = deepcopy(old)
    new.update(pins=facts, diagnostics=diagnostics)
    new["provenance"].update(parent_contract_sha256=digest(old),
        quote_normalization="NFKC_CJK_layout_preserve_numeric_tokens_v1",
        unconditional_policy="source_context_condition_and_negation_guard_v1",
        revalidation="same model proposals and original PDF bytes; no new inference")
    sha = contracts.save_device(new)
    record = records.put("pin-contracts", new)
    result = {"supplier_part": part, "status": "revalidated", "before_sha256": digest(old),
              "after_sha256": sha, "artifact": record,
              "changed_fields": sum(old["pins"][pin].get(field) != fact.get(field)
                                    for pin, fact in facts.items() for field in
                                    ("semantic_role", "required_connection", "allow_nc", "voltage_min_v", "voltage_max_v"))}
    records.event("pin_role_source_revalidation", result)
    return result


def apply_reviewed_corrections(proposal: Mapping, *, service: Any, contracts: PinContractStore,
                               records: RecordStore, reviewer: str) -> dict:
    """Append independently reviewed source corrections to the trusted fact store.

    This administrative operation is never exposed to a generation Agent. It
    authenticates original source bytes and immutable review receipts again;
    callers remain responsible for reviewing the rule's logical entailment.
    A source review may clear a mistaken fact, but cannot select a case scenario.
    """
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise ValueError("an independent accepting reviewer is required")
    part, sha = proposal["supplier_part"], proposal["source_document_sha256"]
    old = contracts.latest_device(part, document_sha256=sha)
    if not old or old.get("provenance", {}).get("source_authentication_version") not in {2, 3}:
        raise ValueError("matching primary-source device contract required")
    source_path = Path(proposal["source_contract_path"]).resolve()
    if not source_path.is_relative_to(records.root) or digest(json.loads(source_path.read_text())) != proposal["source_contract_sha256"]:
        raise ValueError("reviewed parent extraction artifact is unavailable or changed")
    parent = json.loads(source_path.read_text())
    if parent.get("supplier_part") != part or parent.get("document_sha256") != sha or set(parent["pins"]) != set(old["pins"]):
        raise ValueError("source review no longer describes this physical-pin inventory")
    fields_by_effect = {
        "connection_policy": ["allow_nc", "required_connection"],
        "voltage_range": ["voltage_range"], "pin_relation": ["connection_relation"],
        "operating_pin_mode": ["electrical_type", "modes"], "logic_levels": ["logic_levels"],
        "pin_selection_constraint": ["capabilities", "modes"],
        "pin_function_role": ["capabilities", "semantic_role"],
        "application_interface_profile": ["application_interface_profile"],
    }
    refs = []
    for rules in proposal.get("append_conditional_rules", {}).values():
        for rule in rules:
            effect = rule.get("effect", {})
            fields = ([effect.get("check")] if effect.get("kind") == "not_applicable"
                      else fields_by_effect.get(effect.get("kind")))
            if not fields or any(not isinstance(field, str) or not field for field in fields):
                raise ValueError("source review has an unsupported effect claim scope")
            refs.extend((e, fields) for e in rule["evidence"])
    refs.extend((e, ["pin_number", "pin_name"]) for supply in proposal.get("supply_pins_proposals", {}).values() for e in supply["evidence"])
    refs.extend((e, [item["field"]]) for item in proposal.get("clear_unconditional_fields", []) for e in item["evidence"])

    def cited_pages(value: Any) -> set[int]:
        if isinstance(value, Mapping):
            own = {value["page"]} if str(value.get("source_ref", "")).startswith("pdf:") and type(value.get("page")) is int else set()
            return own.union(*(cited_pages(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(cited_pages(item) for item in value))
        return set()

    pages = sorted({e["page"] for e, _ in refs} | cited_pages(old))
    source = service.read_primary_source_evidence(part, pages=pages, limit=200)
    source = {**source, "supplier_part": part}
    if (source.get("document") or {}).get("pdf_sha256") != sha:
        raise ValueError("source PDF changed since independent review")
    excerpts = {row["source_ref"]: row for row in source["evidence"]}
    authenticated = []
    for evidence, fields in refs:
        actual = excerpts.get(evidence.get("source_ref"))
        checked = authenticate_source_quote(source, actual, evidence.get("quote", ""), fields=fields) if actual else None
        if (not checked or evidence.get("source_authentication_version") != checked["source_authentication_version"]
                or evidence.get("provenance") != checked["provenance"] or evidence.get("document_sha256") != checked["document_sha256"]
                or checked.get("page") != evidence.get("page")):
            raise ValueError("review evidence does not match the original PDF or its authenticated visual claim scope")
        authenticated.append(checked)
    new = deepcopy(old)
    for item in proposal.get("clear_unconditional_fields", []):
        pin, field = item["pin"], item["field"]
        if field not in {"allow_nc", "required_connection", "voltage_min_v", "voltage_max_v", "electrical_type", "modes", "resources"} or item["proposed_value"] is not None:
            raise ValueError("source correction permits only clearing a mistaken electrical fact")
        fact = new["pins"][pin]
        if fact.get(field) is not None and (type(fact[field]) is not type(item["old_value"]) or fact[field] != item["old_value"]):
            raise ValueError("a newer fact conflicts with the reviewed correction")
        fact[field] = None
        fact.setdefault("field_evidence", {}).pop(field, None)
    for pin, rules in proposal.get("append_conditional_rules", {}).items():
        target = new["pins"][pin].setdefault("conditional_rules", [])
        for rule in rules:
            review = rule.get("review", {})
            if (rule.get("scope") != {"supplier_part": part, "pin_number": pin}
                    or review.get("method") not in {"bounded_source_parser", "independent_source_review"}
                    or not review.get("reviewer")
                    or review.get("rule_sha256") != rule_digest({k: v for k, v in rule.items() if k != "review"})
                    or review.get("evidence_sha256") != rule_digest(rule["evidence"])):
                raise ValueError("conditional rule review receipt or physical-pin scope is invalid")
            previous = next((r for r in target if r["id"] == rule["id"]), None)
            if previous is not None and previous != rule:
                raise ValueError("a newer conditional rule conflicts with the review")
            if previous is None:
                target.append(deepcopy(rule))
    for symbol, supply in proposal.get("supply_pins_proposals", {}).items():
        if supply.get("pin") not in new["pins"]:
            raise ValueError("reviewed supply must identify a physical pin")
        previous = new.setdefault("supply_pins", {}).get(symbol)
        if previous is not None and previous != supply:
            raise ValueError("conflicting source supply-pin mapping")
        new["supply_pins"][symbol] = deepcopy(supply)
    new["provenance"].update(parent_contract_sha256=digest(old), reviewed_source_correction_sha256=digest(proposal),
                              accepting_reviewer=reviewer)
    if old["provenance"].get("source_authentication_version") == 3 or any(e["source_authentication_version"] == 3 for e in authenticated):
        new["source_registry"] = primary_source_registry(source)
        new["provenance"]["source_authentication_version"] = 3
    sha_new = contracts.save_device(new)
    result = {"supplier_part": part, "status": "source_review_applied", "before_sha256": digest(old),
              "after_sha256": sha_new, "artifact": records.put("pin-contracts", new),
              "proposal": records.put("source-reviews", proposal), "reviewer": reviewer}
    records.event("pin_role_independent_source_review", result)
    return result


__all__ = ["revalidate_device_roles", "apply_reviewed_corrections"]
