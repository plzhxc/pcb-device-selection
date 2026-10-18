"""Prepare fixed device inventory and authenticated supplied source facts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping
import hashlib
import json
import re

from datasheet_automation.knowledge.evidence import (
    authenticate_source_quote,
    normalize_source_text,
)
from validation_platform.conditional_pin_rules import (
    bind_frozen_input_conditions,
    evaluate_pin_rules,
)
from validation_platform.fixed_inventory import PHYSICAL_DEVICE_TYPES, iter_fixed_devices
from validation_platform.operating_selections import option_ref, reviewed_operating_options
from validation_platform.physical_catalog import authenticate_physical_catalog
from validation_platform.pin_contracts import evidence_errors

from ..common_devices import TemplateRegistry
from .semantic_presentation import catalog_pin_prompt_view
from .semantic_selection import authenticated_selection_constraints
from .semantic_shared import MAX_CORE_INSTANCES, _IDENTITY, _diagnostic


def _template_facts() -> list[dict]:
    registry = TemplateRegistry.load_builtin()
    result = []
    for entry in registry.list_templates():
        if entry["kind"] != "primitive":
            continue
        definition = registry.get(entry["template_id"], entry["version"])
        result.append({key: definition.get(key) for key in
                       ("template_id", "version", "device_type", "description", "ports", "parameters_schema", "value_spec")})
        result[-1]["definition_sha256"] = registry.definition_sha256(entry["template_id"], entry["version"])
    return result


def _contracts(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = record.get("contracts") or {}
    if isinstance(raw, Mapping):
        rows = raw.get("pins")
        if raw.get("supplier_part") == record.get("device", {}).get("supplier_part") and isinstance(rows, Mapping):
            return {str(key): dict(row) for key, row in rows.items() if isinstance(row, Mapping)}
    return {}


def _normal_quote(value: str) -> str:
    return normalize_source_text(value)


def _citation_refs(citation: Mapping, evidence: list[Mapping]) -> list[str]:
    """Resolve a quote to its identified original source, never a similar page."""
    quote = citation.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return []
    refs = []
    for chunk in evidence:
        if _normal_quote(quote) not in _normal_quote(chunk["content"]):
            continue
        sha = citation.get("document_sha256")
        if sha and chunk.get("document_sha256") != sha:
            continue
        source_ref = citation.get("source_ref")
        identities = {str(chunk.get(key)) for key in ("id", "original_id", "source_ref") if chunk.get(key) is not None}
        if source_ref and str(source_ref) not in identities:
            continue
        if citation.get("page") is not None:
            if type(citation["page"]) is not int or chunk.get("page_idx") != citation["page"] - 1:
                continue
        refs.append(f"evidence:{chunk['id']}")
    return sorted(set(refs))


def _compact_contract(value: Any, evidence: list[Mapping]) -> Any:
    """Render repeated citations as refs; keep all facts and unmatched evidence.

    The complete original chunks and their source IDs remain in knowledge, and
    the caller's full contract snapshot is never mutated. This is a prompt
    presentation transform, not a replacement for electrical authentication.
    """
    if isinstance(value, list):
        return [_compact_contract(item, evidence) for item in value]
    if not isinstance(value, Mapping):
        return value
    if "quote" in value and "source_ref" in value:
        refs = _citation_refs(value, evidence)
        return {"source_refs": refs} if refs else deepcopy(value)
    result = {}
    for key, item in value.items():
        if key == "evidence" and isinstance(item, list):
            refs, unresolved = set(), []
            for citation in item:
                found = _citation_refs(citation, evidence) if isinstance(citation, Mapping) else []
                if found:
                    refs.update(found)
                else:
                    unresolved.append(deepcopy(citation))
            if refs:
                result["source_refs"] = sorted(refs | set(value.get("source_refs", [])))
            if unresolved:
                result["evidence"] = unresolved
        elif key == "source_refs" and key in result:
            result[key] = sorted(set(result[key]) | set(item))
        else:
            result[key] = _compact_contract(item, evidence)
    return result


def _open_allowed(fact: Mapping, source_bundle: Mapping) -> bool:
    citations = fact.get("evidence")
    if (fact.get("allow_nc") is not True or fact.get("required_connection") is True
            or evidence_errors(citations)):
        return False
    for citation in citations:
        if not any(chunk.get("source_ref") == citation["source_ref"]
                   and chunk.get("document_sha256") == citation["document_sha256"]
                   and authenticate_source_quote(source_bundle, chunk, citation["quote"], fields=["allow_nc"])
                   for chunk in source_bundle.get("evidence", [])):
            return False
    return True


def _input_conditions(board_input: Mapping, requirements: Any, part: str, source_index: Mapping) -> tuple[dict, dict]:
    selected = {}
    frozen_input = {"board_id": board_input.get("board_id"), "requirements": requirements}
    checked = bind_frozen_input_conditions(board_input=frozen_input, supplier_part=part, source_index=source_index)
    bad_fields = {d.get("field") for d in checked["diagnostics"]}
    for category, values in checked["conditions"].items():
        for name, fact in values.items():
            field = f"{category}.{name}"
            if field in bad_fields or fact.get("value") is None:
                continue
            selected[field] = {"value": fact["value"], "source_refs": sorted({e["locator"] for e in fact.get("evidence", [])})}
    return checked["conditions"], selected


def _pin_nc_policy(fact: Mapping, binding: Mapping, selected: Mapping, source_bundle: Mapping,
                   evidence: list[Mapping], *, part: str, pin: str) -> dict:
    evaluated = evaluate_pin_rules(fact, binding, supplier_part=part, pin_number=pin)
    effective = evaluated["fact"]
    unresolved_policy = any(row.get("status") == "unresolved" and
        (row.get("effect_kind") == "connection_policy" or row.get("field") in {"allow_nc", "required_connection"})
        for row in evaluated["diagnostics"])
    ids = set(evaluated["derived_fields"].get("allow_nc", []))
    rules = [rule for rule in fact.get("conditional_rules", []) if isinstance(rule, Mapping) and rule.get("id") in ids]
    field_evidence = fact.get("field_evidence") or {}
    proof = ([citation for rule in rules for citation in rule.get("evidence", [])] if ids
             else field_evidence.get("allow_nc", fact.get("evidence")))
    allowed = (not unresolved_policy and _open_allowed({**effective, "evidence": proof}, source_bundle)
               and all(_citation_refs(citation, evidence) for citation in proof))
    conditions = {}
    if allowed:
        for rule in rules:
            for predicate in rule["when"]["all"]:
                field = predicate["field"]
                if field in selected:
                    conditions[field] = deepcopy(selected[field])
    return {"allowed": allowed, "method": "reviewed_conditional_rules" if ids else "unconditional_source_fact",
            "rule_ids": sorted(ids) if allowed else [], "conditions": conditions}


def _prepare(board_input: Mapping[str, Any], knowledge: Mapping[str, Any], requirements: Any) -> tuple[dict, dict, dict, list]:
    errors, pool, selected = [], {}, {}
    sources: dict[str, Any] = {"request": {"kind": "user_request"}}
    if isinstance(requirements, list):
        for index, requirement in enumerate(requirements):
            key = str(requirement.get("id", index)) if isinstance(requirement, Mapping) else str(index)
            sources[f"requirement:{key}"] = {"kind": "requirement", "value": requirement}
    elif requirements:
        sources["requirements"] = {"kind": "requirements", "value": requirements}
    if "static_environment" in board_input:
        from validation_platform.static_voltages import validate_environment
        try:
            validate_environment(board_input["static_environment"], requirements=requirements if isinstance(requirements, list) else [])
        except (ValueError, TypeError) as exc:
            return pool, selected, sources, [_diagnostic("STATIC_ENVIRONMENT_INVALID", str(exc))]
    core_rows = board_input.get("core_devices")
    if not isinstance(core_rows, list):
        return pool, selected, sources, [_diagnostic("INVALID_BOM", "core_devices must be an array")]
    seen = set()
    try:
        fixed_rows = list(iter_fixed_devices(board_input))
    except ValueError as exc:
        return pool, selected, sources, [_diagnostic("INVALID_BOM", str(exc))]
    for inventory_kind, row in fixed_rows:
        if not isinstance(row, Mapping):
            errors.append(_diagnostic("INVALID_BOM", "core device rows must be objects"))
            continue
        is_module = inventory_kind == "functional_module"
        is_physical = inventory_kind == "physical_device"
        is_non_ic = is_module or is_physical
        component_type = row.get("component_type") if is_non_ic else "ic"
        if is_physical and (set(row) != {"device_id", "model", "quantity", "component_type"}
                            or component_type not in PHYSICAL_DEVICE_TYPES):
            errors.append(_diagnostic("INVALID_PHYSICAL_BOM", "physical rows require an exact supported type, model, identity and quantity"))
            continue
        if is_module and (set(row) != {"device_id", "model", "quantity", "component_type"}
                          or row.get("component_type") != "module"):
            errors.append(_diagnostic("INVALID_FUNCTIONAL_BOM", "functional rows require only exact identity, model, quantity and original module component_type"))
            continue
        part, quantity = row.get("device_id"), row.get("quantity")
        if not isinstance(part, str) or not _IDENTITY.fullmatch(part) or type(quantity) is not int or not 1 <= quantity <= MAX_CORE_INSTANCES:
            errors.append(_diagnostic("INVALID_BOM", "core rows require exact C/S device_id and positive bounded quantity"))
            continue
        if part in seen:
            errors.append(_diagnostic("DUPLICATE_BOM_IDENTITY", f"aggregate duplicate BOM identity {part!r} before generation"))
            continue
        seen.add(part)
        if len(pool) + quantity > MAX_CORE_INSTANCES:
            errors.append(_diagnostic("RESOURCE_LIMIT", "too many core instances"))
            continue
        raw = knowledge.get(part)
        if not isinstance(raw, Mapping):
            errors.append(_diagnostic("MISSING_DEVICE_KNOWLEDGE", f"no supplied knowledge for {part}", device_id=part))
            continue
        device, document = raw.get("device"), raw.get("document") or {}
        quality = raw.get("source_quality") or (document.get("source_quality", {}) if isinstance(document, Mapping) else {})
        document_hash = (document.get("pdf_sha256") or document.get("document_sha256") or quality.get("document_sha256")) if isinstance(document, Mapping) and isinstance(quality, Mapping) else None
        pins, evidence = raw.get("pins"), raw.get("evidence")
        if not isinstance(device, Mapping) or device.get("supplier_part") != part:
            errors.append(_diagnostic("DEVICE_IDENTITY_MISMATCH", f"catalog does not confirm {part}", device_id=part))
            continue
        model_matches = row.get("model") == device.get("device_name")
        if is_physical:
            from datasheet_automation.knowledge.model_identity import authenticate_model_display
            model_matches = authenticate_model_display(raw, supplier_part=part, component_type=component_type,
                display_name=row.get("model"), canonical_model=device.get("device_name"))["status"] in {"exact", "authenticated_alias"}
        if is_non_ic and (not isinstance(row.get("model"), str) or not row["model"].strip() or not model_matches):
            errors.append(_diagnostic("PHYSICAL_MODEL_IDENTITY_UNRESOLVED" if is_physical else "FUNCTIONAL_MODEL_IDENTITY_UNRESOLVED", "input and catalog fixed non-IC models need exact equality or an independently authenticated physical display alias; equivalent pin count or a guessed subtype cannot establish identity",
                device_id=part, input_model=row.get("model"), catalog_model=device.get("device_name")))
            continue
        if (not isinstance(document, Mapping) or (document.get("lichuang_id") or raw.get("supplier_part")) != part
                or not re.fullmatch(r"[0-9a-f]{64}", str(document_hash or ""))):
            errors.append(_diagnostic("MISSING_DATASHEET", f"no identified source document for {part}", device_id=part))
            continue
        document = {**document, "document_sha256": document_hash,
                    "source_format": "pdf" if document.get("pdf_sha256") else "manufacturer_source",
                    "source_quality": quality}
        if (not isinstance(pins, list) or not pins or type(device.get("pin_count")) is not int
                or device["pin_count"] != len(pins) or any(not isinstance(p, Mapping) or not isinstance(p.get("pin_number"), str) or not p["pin_number"] for p in pins)
                or len({p["pin_number"] for p in pins}) != len(pins)):
            errors.append(_diagnostic("INCOMPLETE_PHYSICAL_PINS", f"catalog pin_count and unique physical pins do not agree for {part}", device_id=part))
            continue
        valid_evidence = [e for e in evidence if isinstance(e, Mapping) and e.get("id") is not None
                          and e.get("lichuang_id") == part and isinstance(e.get("content"), str) and e["content"].strip()] if isinstance(evidence, list) else []
        if not valid_evidence:
            errors.append(_diagnostic("MISSING_RAG_EVIDENCE", f"no identified datasheet chunks for {part}", device_id=part))
            continue
        if is_physical:
            authenticated = authenticate_physical_catalog(raw, supplier_part=part, component_type=component_type)
            if authenticated["status"] != "authenticated":
                errors.extend(_diagnostic(d["rule"], d["message"], **{k:v for k,v in d.items() if k not in {"rule", "message"}}) for d in authenticated["diagnostics"])
                continue
        if is_module:
            from validation_platform.module_catalog import authenticate_module_catalog
            authenticated = authenticate_module_catalog(raw, supplier_part=part)
            if authenticated["status"] == "unresolved":
                errors.extend(_diagnostic(d["rule"], d["message"], **{k:v for k,v in d.items() if k not in {"rule", "message"}}) for d in authenticated["diagnostics"])
                continue
        contract_rows = _contracts(raw)
        safe_device = {k: device.get(k) for k in ("supplier_part", "device_name", "pin_count", "category_level_1", "category_level_2")}
        safe_pins, catalog_annotations, catalog_coverage = catalog_pin_prompt_view(raw, supplier_part=part)
        safe_document = {k: document.get(k) for k in ("lichuang_id", "product_name", "pdf_sha256", "pdf_url", "chunk_count", "document_sha256", "source_format", "source_quality")}
        safe_evidence = [{**{k: e.get(k) for k in ("id", "original_id", "source_ref", "lichuang_id", "title", "content", "content_type", "page_idx", "section_path", "image_id", "source_kind", "provenance")},
                          "document_sha256": e.get("document_sha256") or document_hash} for e in valid_evidence]
        compact_contracts = {}
        for number, fact in contract_rows.items():
            compact_contracts[number] = _compact_contract(fact, safe_evidence)
        identity_mappings = deepcopy(raw.get("pin_identity_mappings") or [])
        if not isinstance(identity_mappings, list) or any(not isinstance(mapping, Mapping)
                or mapping.get("supplier_part") != part or mapping.get("status") != "verified"
                or mapping.get("catalog_pin_number") not in {p["pin_number"] for p in pins}
                for mapping in identity_mappings):
            errors.append(_diagnostic("PIN_IDENTITY_MAPPING_SCOPE", "catalog alias mappings must identify this exact device and an existing catalog pin", device_id=part))
            continue
        selected[part] = {"device": safe_device, "pins": safe_pins, "document": safe_document,
                          "pin_identity_mappings": identity_mappings,
                          "evidence": safe_evidence, "contracts": {"supplier_part": part, "pins": compact_contracts}}
        selected[part]["catalog_annotations"] = catalog_annotations
        selected[part]["catalog_annotation_coverage"] = catalog_coverage
        from .application_context import project_application_context
        try:
            application_context = project_application_context(raw, safe_evidence)
        except (TypeError, ValueError, KeyError) as exc:
            errors.append(_diagnostic("APPLICATION_SOURCE_INVALID", str(exc), device_id=part))
            continue
        if application_context is not None:
            selected[part]["application_source_context"] = application_context
        if "device_electrical" in (raw.get("contracts") or {}):
            selected[part]["contracts"]["device_electrical"] = _compact_contract(
                raw["contracts"]["device_electrical"], safe_evidence)
        options = reviewed_operating_options(raw, part)
        if options["options"]:
            selected[part]["contracts"]["selection_options"] = [
                {**_compact_contract(option, safe_evidence), "selection_source_ref": option_ref(part, oid)}
                for oid, option in sorted(options["options"].items())]
            for oid, option in options["options"].items():
                sources[option_ref(part, oid)] = {"kind": "source_operating_option", "device_id": part,
                    "option_id": oid, "sha256": hashlib.sha256(json.dumps(option, ensure_ascii=False,
                        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
        if options["diagnostics"]:
            # Invalid optional source entries confer no authority; unrelated
            # legacy input-only planning can continue without these options.
            selected[part]["operating_option_diagnostics"] = options["diagnostics"]
        sources[f"catalog:{part}"] = {"kind": "catalog", "device_id": part,
            "claim_scope": "inventory_identity_only", "electrical_authentication_granted": False}
        sources[f"document:{part}"] = {"kind": document["source_format"], "device_id": part, "sha256": document_hash}
        for evidence_row in safe_evidence:
            sources[f"evidence:{evidence_row['id']}"] = {"kind": "datasheet_chunk", "device_id": part,
                **{key: evidence_row.get(key) for key in ("id", "source_ref", "document_sha256", "page_idx", "image_id")}}
        binding, input_selections = _input_conditions(board_input, requirements, part, sources)
        selection = authenticated_selection_constraints(raw, part, binding)
        canonical_supplies = {symbol: {"pin_numbers": value["pin_numbers"],
            "source_refs": sorted({ref for citation in value["evidence"] for ref in _citation_refs(citation, safe_evidence)})}
            for symbol,value in selection["supply_symbols"].items()}
        selected[part]["canonical_supply_symbols"] = canonical_supplies
        nc_policies = {}
        role_provenance = (raw.get("contracts") or {}).get("provenance") or {}
        if role_provenance.get("source_authentication_version") in {2, 3}:
            for pin in pins:
                nc_policies[pin["pin_number"]] = _pin_nc_policy(contract_rows.get(pin["pin_number"], {}),
                    binding, input_selections, {**raw, "supplier_part": part}, safe_evidence, part=part, pin=pin["pin_number"])
        for instance_index in range(1, quantity + 1):
            prefix = "D" if is_physical else "M" if is_module else "U"
            alias = f"{prefix}{sum(spec['identity']['inventory_kind'] == inventory_kind for spec in pool.values()) + 1}"
            allowances = {pin["pin_number"]: nc_policies.get(pin["pin_number"], {}).get("allowed", False) for pin in pins}
            pool[alias] = {"device": {"name": device.get("device_name") or row.get("model") or part,
                                     "designator": alias, "type": component_type, "is_abstract": is_non_ic,
                                     "value": None, "description": "Fixed catalog device" if is_non_ic else None, "id": None if is_non_ic else part,
                                     "pins": [{"id": p["pin_number"], "name": p.get("pin_name")} for p in pins]},
                           "allow_nc": allowances}
            pool[alias]["identity"] = {"supplier_part": part, "inventory_kind": inventory_kind,
                "component_type": component_type, "input_model": row.get("model"),
                "catalog_model": device.get("device_name"), "instance_index": instance_index,
                "catalog_pin_count": len(pins), "source_document_sha256": document_hash,
                "terminal_scope": "catalog_pins_only"}
            pool[alias]["selection_constraints"] = deepcopy(selection["pins"])
            pool[alias]["canonical_supply_symbols"] = deepcopy(canonical_supplies)
            if identity_mappings:
                pool[alias]["pin_identity_mappings"] = deepcopy(identity_mappings)
            conditional = {pin: policy for pin, policy in nc_policies.items() if policy["allowed"] and policy["conditions"]}
            if conditional:
                pool[alias]["conditional_nc"] = deepcopy(conditional)
    return pool, selected, sources, errors

