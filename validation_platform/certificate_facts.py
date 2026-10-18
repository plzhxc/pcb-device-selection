"""Strict source consumers for equivalence certificates.

Only source snapshots attached by the reference/generation contract binders are
read. A certificate or permission cannot supply an alternative document bundle.
The application must construct these binders from frozen knowledge snapshots;
hashes detect stale edits, and do not turn caller-controlled data into trust.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from typing import Any

from .pin_contracts import component_contract, pin_fact, pin_binding, evidence_errors, evidence_field_scope_errors

GPIO_FIELDS = ("semantic_role", "electrical_type", "capabilities", "modes", "resources",
               "gpio_remap_safe", "required_connection", "allow_nc", "voltage_min_v", "voltage_max_v")
EFFECT_FIELDS = {
    "connection_policy": ["required_connection", "allow_nc"],
    "voltage_range": ["voltage_min_v", "voltage_max_v"],
    "operating_pin_mode": ["electrical_type", "modes"],
    "pin_selection_constraint": ["capabilities", "modes"],
    "pin_function_role": ["capabilities", "semantic_role"],
    "pin_relation": ["required_connection"],
    "logic_levels": ["logic_levels"],
    "application_interface_profile": ["application_interface_profile"],
}


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def frozen_source_bundle(record: Mapping, supplier_part: str) -> dict:
    """Retain only original-reader fields needed to authenticate source quotes."""
    from datasheet_automation.knowledge.evidence import primary_source_registry
    bundle = {key: deepcopy(record[key]) for key in (
        "document", "source_assets", "evidence", "stored_source_quality", "source_quality",
        "source_version_selection", "selected_source_document") if key in record}
    bundle["supplier_part"] = supplier_part
    if any("connection_policy_reviews" in _map(_map(asset).get("quality")) for asset in record.get("source_assets", [])):
        # Only the explicit new guide policy needs current catalog/model binding.
        # Preserve legacy frozen bundle bytes when that opt-in review is absent.
        bundle.update({key: deepcopy(record.get(key)) for key in ("device", "pins")})
    registry = primary_source_registry(bundle)
    bundle["evidence"] = [row for row in bundle.get("evidence", []) if _map(row).get("source_ref") in registry]
    return {"bundle": bundle, "bundle_sha256": digest(bundle),
            "knowledge_record_sha256": digest(record),
            "origin": "contract_binder_frozen_knowledge_snapshot"}


def _source_proof_matches(ref: Mapping, proof: Mapping | None, *, primary_pdf_sha256: Any) -> bool:
    if not proof:
        return False
    compared = dict(ref)
    # Historical native-PDF v2 citations omitted these two now-derived metadata
    # keys. Registry approval and fresh quote/field authentication precede this
    # comparison. Never overwrite an explicit value or use this for reviewed,
    # secondary, HTML or image evidence.
    page, sha = ref.get("page"), ref.get("document_sha256")
    if (ref.get("source_kind") == proof.get("source_kind") == "datasheet" and
            ref.get("provenance") == proof.get("provenance") == "original_pdf_native" and
            type(ref.get("source_authentication_version")) is int and ref["source_authentication_version"] == 2 and
            proof.get("source_authentication_version") == 2 and sha == primary_pdf_sha256 and
            type(page) is int and page > 0 and ref.get("source_ref") == f"pdf:{sha}:page:{page}" and
            proof.get("locator") == f"page:{page}" and proof.get("claim_scope") == "documented_device_facts"):
        compared.setdefault("locator", proof["locator"])
        compared.setdefault("claim_scope", proof["claim_scope"])
    return all(compared.get(key) == value for key, value in proof.items() if key != "source_url")


def certificate_source_errors(contracts: Mapping | None, supplier_part: str,
                              refs: Any, fields: list[str]) -> list[str]:
    """Authenticate exact document/ref/quote/field against this side's snapshot."""
    from datasheet_automation.knowledge.evidence import primary_source_registry, authenticate_source_quote
    errors = evidence_errors(refs) + evidence_field_scope_errors(refs, fields)
    frozen = _map(_map(_map(contracts).get("source_bundles")).get(supplier_part))
    bundle = _map(frozen.get("bundle"))
    if (frozen.get("origin") != "contract_binder_frozen_knowledge_snapshot" or
            bundle.get("supplier_part") != supplier_part or
            frozen.get("bundle_sha256") != digest(bundle) or
            not isinstance(frozen.get("knowledge_record_sha256"), str) or len(frozen["knowledge_record_sha256"]) != 64):
        return errors + ["independent binder source snapshot is missing or changed"]
    registry = primary_source_registry(bundle)
    components = [cc for cc in _map(_map(contracts).get("components")).values()
                  if _map(cc).get("supplier_part") == supplier_part]
    if not registry or not components:
        return errors + ["contract or frozen original-source registry is missing"]
    for ref in refs if isinstance(refs, list) else []:
        ref = _map(ref)
        source_ref = ref.get("source_ref")
        # A RAG snapshot can contain additional unconsumed pages. Require the
        # consumed reference, not the unrelated whole page inventory, to agree
        # with both the approved contract and current immutable source bundle.
        if (not isinstance(source_ref, str) or source_ref not in registry or any(
                _map(_map(cc).get("source_registry")).get(source_ref) != registry.get(source_ref)
                for cc in components)):
            errors.append("consumed source reference is absent from or differs between approved contract and frozen source registries")
            continue
        candidates = [row for row in bundle.get("evidence", []) if _map(row).get("source_ref") == ref.get("source_ref")]
        proofs = [authenticate_source_quote(bundle, row, ref.get("quote"), fields=fields) for row in candidates]
        # Compare every authenticated identity/scope field, not just a hash and
        # a plausible page number. Images never authorize electrical facts.
        if not any(_source_proof_matches(ref, proof, primary_pdf_sha256=_map(bundle.get("document")).get("pdf_sha256"))
                   for proof in proofs):
            errors.append("citation quote, source identity or field scope does not match original evidence")
    return errors


def effective_gpio_fact(graph: Any, contracts: Mapping | None, cid: str, pid: str) -> tuple[dict, list[str]]:
    """Evaluate independently sourced conditional facts in this side's scenario."""
    from .conditional_pin_rules import evaluate_pin_rules, unconditional_claim_errors
    cc = component_contract(contracts, cid)
    part = graph.components[(graph.source, cid)].anchor_id
    original = pin_fact(contracts, cid, pid)
    binding = pin_binding(contracts, cid, pid)
    errors = []
    if not part or cc.get("supplier_part") != part:
        return {}, ["GPIO contract identity differs from the actual anchored part"]
    for rule in original.get("conditional_rules", []) if isinstance(original.get("conditional_rules", []), list) else []:
        effect = _map(_map(rule).get("effect"))
        fields = [effect.get("check")] if effect.get("kind") == "not_applicable" else EFFECT_FIELDS.get(effect.get("kind"), ["unsupported_effect"])
        if effect.get("kind") == "connection_policy" and "intrinsic_policy" in effect:
            fields = [key for key in ("allow_nc", "required_connection") if key in effect]
        errors.extend(certificate_source_errors(contracts, part, _map(rule).get("evidence"), fields))
    bundle = _map(_map(_map(_map(contracts).get("source_bundles")).get(part)).get("bundle"))
    pages = {_map(row).get("source_ref"): _map(row).get("content") for row in bundle.get("evidence", [])}
    for field in GPIO_FIELDS:
        if original.get(field) is not None:
            refs = _map(original.get("field_evidence")).get(field, original.get("evidence"))
            errors.extend(certificate_source_errors(contracts, part, refs, [field]))
            # Conditional source prose cannot establish an unconditional fact.
            errors.extend(unconditional_claim_errors(field, original[field], refs, source_pages=pages))
    result = evaluate_pin_rules(original, binding, domains=_map(_map(contracts).get("domains")),
        supplies=_map(cc.get("supplies")), component_bindings=_map(cc.get("bindings")),
        supplier_part=part, pin_number=pid,
        pin_nets={n: p.net[1] if p.net else None for n, p in graph.pin_map((graph.source, cid)).items()})
    for diagnostic in result["diagnostics"]:
        if diagnostic.get("status") in {"fail", "unresolved"}:
            errors.append("conditional source fact is not established: " + str(diagnostic.get("rule_id", diagnostic.get("reasons"))))
    fact = result["fact"]
    for field in GPIO_FIELDS:
        if fact.get(field) is not None:
            refs = _map(fact.get("field_evidence")).get(field, fact.get("evidence"))
            errors.extend(certificate_source_errors(contracts, part, refs, [field]))
    return dict(fact), list(dict.fromkeys(errors))


def value_source_errors(rule: Mapping, reference_contracts: Mapping | None, component_type: str) -> list[str]:
    """Source-authenticate a reviewed interval; quote existence is insufficient."""
    part = rule.get("source_supplier_part")
    review = _map(rule.get("source_review"))
    refs = [e for e in rule.get("evidence", []) if _map(e).get("source_kind") != "input"] if isinstance(rule.get("evidence"), list) else []
    errors = []
    if rule.get("component_type") != component_type:
        errors.append("reviewed interval does not apply to this component type")
    if (review.get("method") != "independent_source_review" or not review.get("reviewer") or
            review.get("rule_sha256") != digest({k: v for k, v in rule.items() if k != "source_review"}) or
            review.get("evidence_sha256") != digest(refs)):
        errors.append("interval bounds, conditions, type and source lack an unchanged independent review")
    if not isinstance(part, str) or not part:
        errors.append("interval has no independently bound source device")
    else:
        errors.extend(certificate_source_errors(reference_contracts, part, refs, ["value_interval"]))
    return errors


__all__ = ["frozen_source_bundle", "effective_gpio_fact", "certificate_source_errors", "value_source_errors"]
