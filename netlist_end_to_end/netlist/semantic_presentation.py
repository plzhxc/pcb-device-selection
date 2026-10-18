"""Prompt projections retain original facts; catalog prose requires source receipts."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any

from .prompts import load_prompt, render_prompt
from .semantic_source_context import _same_json, with_referenced_source_context


SOURCE_POINTER_CONSTRAINT = (
    load_prompt('presentation_source_pointer.md')
)


def prompt_json_text(value: Any) -> str:
    """Serialize complete semantic inputs without indentation-token overhead.

    This changes whitespace outside JSON strings only. Source text, numerical
    signs and units, conditions, arrays and all provenance remain present.
    Runtime snapshots and evidence hashes are never calculated from this view.
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str, allow_nan=False)


def present_scl_repair(ctx: Any, payload: Mapping, *, module: str | None) -> None:
    """Keep the full input, then repeat bounded actual errors and matching lines.

    The caller supplies the name of the stored previous SCLSource; a diagnostic
    cannot supply that identity itself. No connection or replacement is inferred.
    """
    from agent_framework.message import UserMessage

    previous = payload.get("previous_scl")
    diagnostics = payload.get("diagnostics")
    if not isinstance(previous, str) or not previous or not isinstance(diagnostics, list):
        ctx.user(prompt_json_text(payload))
        return
    source = payload.get("previous_scl_source")
    selected = []
    for position, row in enumerate(diagnostics):
        if not isinstance(row, Mapping):
            continue
        endpoint = row.get("endpoint")
        owner = row.get("module") or (endpoint.get("module") if isinstance(endpoint, Mapping) else None)
        same_source = isinstance(source, str) and bool(source) and row.get("source") == source
        # Module repairs receive the complete compiler diagnostic list. Repeat
        # only their own errors and unowned global errors. Top errors have
        # already been routed by the caller; never attach another source's line.
        if module is not None and (owner not in (None, module)
                or row.get("source") and not same_source):
            continue
        selected.append((position, row, same_source and owner in (None, module)))
    if not selected:
        ctx.user(prompt_json_text(payload))
        return

    def excerpt(text: str, limit: int) -> dict:
        return {"text": text[:limit], "characters": len(text), "truncated": len(text) > limit}

    # Match Python parser physical newlines. str.splitlines also splits Unicode
    # separators inside comments/strings, which would shift compiler line numbers.
    lines = previous.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    shown = []
    for position, row, same_source in selected[:16]:
        item = {"diagnostic_index": position, "diagnostic_json": excerpt(prompt_json_text(row), 1600)}
        line = row.get("line")
        if same_source and type(line) is int and 1 <= line <= len(lines):
            item["previous_scl_line"] = {"line": line, **excerpt(lines[line - 1], 800)}
        shown.append(item)
    focus = {"diagnostic_count": len(diagnostics), "relevant_diagnostic_count": len(selected),
             "focus_truncated": len(selected) > len(shown), "errors": shown}
    # Context.user is a setter. Keep the original complete input as its own
    # message before replacing the final user turn with this display-only focus.
    ctx.message(UserMessage(prompt_json_text(payload)))
    ctx.user(render_prompt('presentation_scl_repair.md', focus=prompt_json_text(focus)))


CATALOG_ANNOTATION_CONSTRAINT = (
    load_prompt('presentation_catalog_annotation.md')
)


def catalog_pin_prompt_view(record: Mapping, *, supplier_part: str) -> tuple[list, dict, dict]:
    """Separate exact-current, source-authenticated annotations from raw prose.

    Stored catalog receipts are independent input artifacts, never model-owned.
    Their source chains establish attribution and field scope, not entailment.
    Unreceipted values remain only in the immutable knowledge snapshot; no raw
    text is copied into a supposedly untrusted section of the Agent prompt.
    This does not change pin identity, source documents, contracts or DB rows.
    """
    from datasheet_automation.knowledge.evidence import authenticate_source_quote
    from validation_platform.certificate_facts import _source_proof_matches
    fields = ("pin_type", "pin_description", "pin_connection_notes", "pin_impedance", "pin_frequency")
    pins, annotations, withheld = [], {}, []
    receipts = record.get("catalog_field_evidence", [])
    receipts = receipts if isinstance(receipts, list) else []
    evidence = record.get("evidence", [])
    evidence = evidence if isinstance(evidence, list) else []
    owner_ok = (record.get("supplier_part") == supplier_part
                and isinstance(record.get("device"), Mapping)
                and record["device"].get("supplier_part") == supplier_part)
    for pin in record.get("pins", []):
        number = pin["pin_number"]
        pins.append({"pin_number": number, "pin_name": pin.get("pin_name")})
        for field in fields:
            value = pin.get(field)
            if value is None or value == "":
                continue
            proofs = []
            for receipt in receipts if owner_ok else []:
                if not isinstance(receipt, Mapping):
                    continue
                action = receipt.get("action")
                asserted = receipt.get("value")
                if action == "correct":
                    asserted = asserted.get("new") if isinstance(asserted, Mapping) and set(asserted) == {"old", "new"} else None
                if (action not in {"insert", "fill", "correct", "retained"}
                        or receipt.get("supplier_part") != supplier_part or receipt.get("entity") != "pin"
                        or receipt.get("pin_number") != number or receipt.get("field_name") != field
                        or pin.get("pin_index") is None or str(receipt.get("entity_key")) != str(pin["pin_index"])
                        or not _same_json(receipt.get("current_value"), value) or not _same_json(asserted, value)):
                    continue
                citation = receipt.get("evidence")
                if (not isinstance(citation, Mapping)
                        or citation.get("source_supplier_part", supplier_part) != supplier_part):
                    continue
                for chunk in evidence:
                    if (not isinstance(chunk, Mapping) or chunk.get("lichuang_id") != supplier_part
                            or chunk.get("id") is None or chunk.get("source_ref") != citation.get("source_ref")
                            or chunk.get("document_sha256") != citation.get("document_sha256")):
                        continue
                    proof = authenticate_source_quote(record, chunk, citation.get("quote"), fields=[field])
                    if _source_proof_matches(citation, proof, primary_pdf_sha256=record.get("document", {}).get("pdf_sha256")):
                        proofs.append({"source_ref": "evidence:" + str(chunk["id"]),
                            "document_sha256": proof["document_sha256"], "page": proof.get("page"),
                            "claim_scope": proof.get("claim_scope"), "reviewed_claim_scopes": proof.get("reviewed_claim_scopes"),
                            "source_kind": proof["source_kind"], "provenance": proof["provenance"]})
            if proofs:
                annotations.setdefault(number, {})[field] = {"value": deepcopy(value),
                    "source_refs": sorted({p["source_ref"] for p in proofs}),
                    "source_scope": [json.loads(s) for s in sorted({prompt_json_text(p) for p in proofs})],
                    "status": "source_quote_authenticated_not_entailment_or_typed_fact"}
            else:
                withheld.append({"pin_number": number, "field": field, "reason": "no_current_owner_field_source_chain"})
    coverage = {"authenticated_field_count": sum(len(v) for v in annotations.values()),
                "withheld_field_count": len(withheld), "withheld_fields": withheld,
                "raw_values_location": "immutable_frozen_knowledge_only",
                "electrical_authentication_granted": False}
    return pins, annotations, coverage


def source_index_prompt_view(payload: Mapping) -> tuple[dict, list[dict]]:
    """Replace only exact copies of existing request/unique-ID requirements.

    All source keys and other metadata remain unchanged. No quote, conditional
    rule, device fact, or input is shortened. The returned list identifies the
    local pointers, not new sources; consumers validate the original payload.
    """
    result = deepcopy(dict(payload))
    index = result.get("source_index")
    if not isinstance(index, dict):
        return result, []
    requirements = result.get("requirements")
    rows = requirements if isinstance(requirements, list) else []
    ids = Counter(row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str))
    candidates = {f"requirement:{row['id']}": (position, row)
                  for position, row in enumerate(rows)
                  if isinstance(row, dict) and isinstance(row.get("id"), str) and ids[row["id"]] == 1}
    replacements = []
    for key, source in index.items():
        if not isinstance(source, dict) or "value" not in source or "value_pointer" in source:
            continue
        pointer = None
        if source.get("kind") == "requirement" and key in candidates:
            position, requirement = candidates[key]
            if _same_json(source["value"], requirement):
                pointer = f"/requirements/{position}"
        elif (key == "request" and source.get("kind") == "user_request"
              and isinstance(result.get("request"), str) and _same_json(source["value"], result["request"])):
            pointer = "/request"
        if pointer is not None:
            source.pop("value")
            source["value_pointer"] = pointer
            replacements.append({"key": key, "value_pointer": pointer})
    return result, replacements


ARCHITECTURE_CONTEXT_PROFILES = ("legacy_full_contracts", "stage_boundary_v1")


STAGE_BOUNDARY_CONSTRAINT = (
    load_prompt('presentation_stage_boundary.md')
)


def architecture_device_view(record: Mapping, *, part: str,
                             frozen_knowledge_sha256: str | None = None) -> dict:
    """Stage display only; full source records remain the authority elsewhere.

    Retain each physical contact and every supply/rule/application contract.
    Do not select values, resolve uncertainty, authenticate facts or create NC
    permissions from the compact terminal overview.
    """
    from validation_platform.pin_contracts import POWER_ROLES
    contracts = record.get("contracts", {})
    facts = contracts.get("pins", {})
    supply, rules, role_evidence = {}, {}, {}
    statuses = Counter()
    for number, fact in facts.items():
        statuses[str(fact.get("status", "not_reported"))] += 1
        if (fact.get("semantic_role") in POWER_ROLES
                or fact.get("electrical_type") in ("power_in", "power_out", "ground")):
            supply[number] = deepcopy(fact)
        if "conditional_rules" in fact:
            rules[number] = deepcopy(fact["conditional_rules"])
        support = {key: deepcopy(value) for key, value in fact.get("field_evidence", {}).items()
                   if key in ("semantic_role", "capabilities")}
        if support:
            role_evidence[number] = support
    shown_contracts = {key: deepcopy(value) for key, value in contracts.items()
                       if key in ("supplier_part", "device_electrical", "selection_options")}
    shown_contracts.update(supply_pin_contracts=supply, conditional_rules_by_pin=rules,
                           terminal_role_evidence=role_evidence)
    import hashlib
    bundle_sha = hashlib.sha256(prompt_json_text(record).encode()).hexdigest()
    result = {"device": deepcopy(record["device"]),
        "pins": [{"pin_number": pin["pin_number"], "pin_name": pin.get("pin_name"),
                  **{key: deepcopy(facts.get(pin["pin_number"], {}).get(key))
                     for key in ("semantic_role", "capabilities")}}
                 for pin in record["pins"]],
        "contracts": shown_contracts,
        "canonical_supply_symbols": deepcopy(record.get("canonical_supply_symbols", {})),
        "fact_coverage": {"profile": "stage_boundary_v1", "supplier_part": part,
            "supplied_device_bundle_sha256": bundle_sha,
            "full_fact_pointer": "/" + part + "/contracts",
            "pin_status_counts": dict(statuses),
            "supply_pins_with_complete_contracts": list(supply),
            "non_supply_detail_fields_not_displayed": ["electrical_type", "voltage_min_v", "voltage_max_v",
                "allow_nc", "required_connection", "gpio_remap_safe", "resources", "modes", "uncertainties"],
            "remaining_facts": load_prompt('presentation_remaining_facts.md'),
            "electrical_authentication_granted": False}}
    if frozen_knowledge_sha256 is not None:
        result["fact_coverage"]["frozen_knowledge_sha256"] = frozen_knowledge_sha256
    return result


def architecture_configuration_choices(requirements: Any, knowledge: Mapping) -> dict:
    """Separate exact input selections from actual source-owned internal options."""
    selected = []
    for index, requirement in enumerate(requirements if isinstance(requirements, list) else []):
        if isinstance(requirement, Mapping) and isinstance(requirement.get("selection"), Mapping):
            selected.append({"requirement_id": requirement.get("id"),
                "requirement_pointer": f"/requirements/{index}",
                "selection": deepcopy(requirement["selection"]), "authority": "input_requirement"})
    internal = []
    for part, record in knowledge.items():
        options = record.get("contracts", {}).get("selection_options")
        if options:
            internal.append({"supplier_part": part,
                "options_pointer": f"/knowledge_summary/{part}/contracts/selection_options"})
    return {"already_selected_input": selected, "source_internal_options": internal,
            "authority_note": load_prompt('presentation_configuration_authority.md')}


__all__ = ["source_index_prompt_view", "SOURCE_POINTER_CONSTRAINT", "prompt_json_text", "present_scl_repair",
    "with_referenced_source_context",
    "ARCHITECTURE_CONTEXT_PROFILES", "STAGE_BOUNDARY_CONSTRAINT",
    "architecture_device_view", "architecture_configuration_choices"]

