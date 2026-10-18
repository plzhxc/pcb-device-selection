"""Validate architecture inventories, domains and frozen module boundaries."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from validation_platform.static_voltages import voltage_domain_profile_valid
from validation_platform.planning_obligations import OBLIGATIONS_SCHEMA, inventory_from_pool, validate_obligations

from .prompts import load_prompt
from .planner_projection import INVENTORY_REPAIR_SCHEMA
from .semantic_contracts import DOMAIN, PORT, STRING, STRINGS, obj
from .semantic_domains import port_domain_selection_valid, external_source_port_valid


ARCH_MODULE = obj({"id": STRING, "label": STRING, "intent": STRING,
    "core_instances": STRINGS, "ports": {"type": "object", "additionalProperties": PORT}})


ARCHITECTURE_SCHEMA = obj({"modules": {"type": "array", "minItems": 1, "maxItems": 128, "items": ARCH_MODULE},
    "voltage_domains": {"type": "array", "items": DOMAIN}, "assumptions": STRINGS, "unresolved": STRINGS,
    "obligations": OBLIGATIONS_SCHEMA}, ["modules", "voltage_domains", "assumptions", "unresolved"])


def _error(code: str, message: str, **extra: Any) -> dict:
    return {"severity": "error", "code": code, "message": message, **extra}


def _source_reference_details(refs: list[str], source_index: Mapping) -> dict:
    """Explain invalid keys without rewriting citations or authorizing facts."""
    invalid = sorted({ref for ref in refs if ref not in source_index})
    suggestions = {ref: ref[1:] for ref in invalid
                   if ref.startswith("/") and ref[1:] in source_index}
    owners = {match.group(1) for ref in invalid
              if (match := re.match(r"^(?:evidence|catalog|document):([CS][0-9]+)(?::|$)", ref))}
    available = {}
    for owner in sorted(owners):
        keys = sorted(key for key, entry in source_index.items()
                      if isinstance(entry, Mapping) and entry.get("device_id") == owner)
        if keys:
            available[owner] = {"keys": keys[:32], "total": len(keys), "truncated": len(keys) > 32}
    return {"invalid_refs": invalid, "known_key_suggestions": suggestions,
            "available_keys_by_device": available,
            "source_reference_rule": load_prompt('architecture_source_reference_rule.md')}


def _domain_reference_details(domain: Mapping, domains: Mapping) -> dict:
    """Explain a missing reference without inferring a physical ground."""
    return {"declared_voltage_v": domain["voltage_v"],
            "actual_reference": domain["reference"],
            "other_declared_domain_ids": sorted(key for key in domains if key != domain["id"]),
            "required_action": load_prompt('architecture_domain_reference_action.md'),
            "authority_limit": load_prompt('architecture_domain_authority_limit.md')}


def validate_architecture(plan: Any, pool: Mapping, source_index: Mapping, *, max_module_core_pins: int,
                          static_environment: Mapping | None = None) -> list[dict]:
    """Validate inventory and boundary declarations before any module model call."""
    errors = [_error("ARCHITECTURE_SCHEMA", e.message, path=".".join(map(str, e.absolute_path)))
              for e in Draft202012Validator(ARCHITECTURE_SCHEMA).iter_errors(plan)]
    if errors:
        return errors
    counts, modules = Counter(), set()
    for module in plan["modules"]:
        mid = module["id"]
        if mid in modules or mid == "__top__":
            errors.append(_error("ARCHITECTURE_MODULE_ID", "module ids must be unique and cannot use __top__", module=mid))
        modules.add(mid)
        counts.update(module["core_instances"])
        own = [alias for alias in module["core_instances"] if alias in pool]
        pin_count = sum(len(pool[alias]["device"]["pins"]) for alias in own)
        if pin_count > max_module_core_pins and len(own) != 1:
            errors.append(_error("ARCHITECTURE_MODULE_TOO_LARGE", "split the functional group into smaller modules; one oversized core must stand alone",
                module=mid, pin_count=pin_count, max_module_core_pins=max_module_core_pins))
        for port, definition in module["ports"].items():
            if not definition["source_refs"] or any(ref not in source_index for ref in definition["source_refs"]):
                errors.append(_error("ARCHITECTURE_PORT_SOURCE", "module interface needs known source references", module=mid, port=port,
                    **_source_reference_details(definition["source_refs"], source_index)))
    if set(counts) != set(pool) or any(value != 1 for value in counts.values()):
        errors.append(_error("ARCHITECTURE_CORE_INVENTORY", "every fixed core alias must be assigned exactly once",
            missing=sorted(set(pool)-set(counts)), extra=sorted(set(counts)-set(pool)),
            duplicate=sorted(alias for alias,count in counts.items() if count != 1)))
    domains = {}
    for domain in plan["voltage_domains"]:
        if domain["id"] in domains:
            errors.append(_error("ARCHITECTURE_DUPLICATE_DOMAIN", "global voltage domain ids must be unique", domain=domain["id"]))
        domains[domain["id"]] = domain
        if not voltage_domain_profile_valid(domain, static_environment):
            errors.append(_error("ARCHITECTURE_DOMAIN_PROFILE", "profile_ref requires a frozen input source, explicit reference and voltage_v=null; repair before freezing module boundaries",
                domain=domain["id"], selected_profile_ref=deepcopy(domain.get("profile_ref")),
                selected_voltage_v=domain["voltage_v"], selected_reference=domain["reference"],
                available_source_ids=sorted((static_environment or {}).get("sources", {})),
                required_action=load_prompt('architecture_domain_profile_action.md')))
        if not domain["source_refs"] or any(ref not in source_index for ref in domain["source_refs"]):
            errors.append(_error("ARCHITECTURE_DOMAIN_SOURCE", "voltage domain requires known sources", domain=domain["id"],
                **_source_reference_details(domain["source_refs"], source_index)))
    for module in plan["modules"]:
        for port_id, port in module["ports"].items():
            if not external_source_port_valid(port, static_environment):
                errors.append(_error("ARCHITECTURE_STATIC_SOURCE_PORT", "external source terminal must be a scalar actual rail port of a frozen input source; repair before freezing module boundaries",
                    module=module["id"], port=port_id, selected_source=deepcopy(port.get("external_source")),
                    available_source_ids=sorted((static_environment or {}).get("sources", {})),
                    source_authority="board.static_environment only; prose does not create a source identifier"))
            if not port_domain_selection_valid(port, domains):
                errors.append(_error("ARCHITECTURE_PORT_DOMAIN", "port domain/binding must both be null or name a declared domain with rail/logic semantics; repair before freezing module boundaries",
                    module=module["id"], port=port_id, selected_domain=port["domain"],
                    selected_binding=port["domain_binding"], allowed_domains=sorted(domains)))
    for identifier, domain in domains.items():
        reference = domain["reference"]
        if reference is not None and reference not in domains:
            errors.append(_error("ARCHITECTURE_DOMAIN_REFERENCE", "reference must be a declared global domain", domain=identifier,
                **_domain_reference_details(domain, domains)))
        if domain["voltage_v"] not in (0, None) and reference is None:
            errors.append(_error("ARCHITECTURE_DOMAIN_REFERENCE", "nonzero voltage requires an explicit reference domain", domain=identifier,
                **_domain_reference_details(domain, domains)))
        seen, current = set(), identifier
        while current in domains and current not in seen:
            seen.add(current); current = domains[current]["reference"]
        if current in seen:
            errors.append(_error("ARCHITECTURE_DOMAIN_CYCLE", "voltage references cannot form a cycle", domain=identifier))
    errors.extend(validate_obligations(plan, inventory_from_pool(pool), source_index)["diagnostics"])
    return errors


def architecture_inventory_repair_targets(plan: Any, pool: Mapping, source_index: Mapping,
                                          issues: list[dict], *, max_module_core_pins: int,
                                          static_environment: Mapping | None = None) -> list[str]:
    """Only a schema-valid, otherwise-valid missing-inventory failure is eligible."""
    if not issues or any(row.get("code") != "ARCHITECTURE_CORE_INVENTORY" for row in issues):
        return []
    actual = validate_architecture(plan, pool, source_index, max_module_core_pins=max_module_core_pins, static_environment=static_environment)
    if len(actual) != 1 or actual[0]["code"] != "ARCHITECTURE_CORE_INVENTORY":
        return []
    row = actual[0]
    return (list(row["missing"]) if 0 < len(row["missing"]) <= INVENTORY_REPAIR_SCHEMA["properties"]["assignments"]["maxItems"]
            and not row["extra"] and not row["duplicate"] else [])


def merge_architecture_inventory_repair(plan: Any, patch: Any, pool: Mapping, source_index: Mapping,
                                       *, max_module_core_pins: int, static_environment: Mapping | None = None) -> tuple[Any, list[dict]]:
    """Apply only exact missing-alias additions; full validation remains mandatory."""
    errors = [_error("ARCHITECTURE_INVENTORY_REPAIR_SCHEMA", error.message)
              for error in Draft202012Validator(INVENTORY_REPAIR_SCHEMA).iter_errors(patch)]
    if errors:
        return deepcopy(plan), errors
    missing = architecture_inventory_repair_targets(plan, pool, source_index,
        [{"code": "ARCHITECTURE_CORE_INVENTORY"}], max_module_core_pins=max_module_core_pins, static_environment=static_environment)
    assignments = patch["assignments"]
    counts = Counter(row["core_instance"] for row in assignments)
    modules = {row["id"] for row in plan.get("modules", [])} if isinstance(plan, Mapping) else set()
    if (not missing or patch["unresolved"] or set(counts) != set(missing)
            or any(count != 1 for count in counts.values())
            or any(row["module_id"] not in modules for row in assignments)):
        return deepcopy(plan), [_error("ARCHITECTURE_INVENTORY_REPAIR_INCOMPLETE",
            "assign every actual missing alias once to an existing module; no unrelated edits or unresolved placement",
            missing=missing, unresolved=deepcopy(patch["unresolved"]))]
    merged = deepcopy(plan)
    by_module = {row["id"]: row for row in merged["modules"]}
    for row in assignments:
        by_module[row["module_id"]]["core_instances"].append(row["core_instance"])
    errors = validate_architecture(merged, pool, source_index, max_module_core_pins=max_module_core_pins, static_environment=static_environment)
    if errors:
        return deepcopy(plan), errors
    return merged, []


def _boundary_errors(plan: Any, architecture: Mapping, module: Mapping,
                     reserved_core_aliases: set[str] | None = None) -> list[dict]:
    if not isinstance(plan, Mapping) or not isinstance(plan.get("modules"), list) or len(plan["modules"]) != 1:
        return [_error("PERIPHERAL_MODULE_COUNT", "return exactly one planned module with the frozen architecture boundary", module=module["id"])]
    actual = plan["modules"][0]
    if not isinstance(actual, Mapping):
        return [_error("PERIPHERAL_MODULE_SCHEMA", "planned module must be an object", module=module["id"])]
    errors = []
    for field in ("id", "label", "intent", "ports"):
        if actual.get(field) != module[field]:
            errors.append(_error("PERIPHERAL_BOUNDARY_CHANGED", f"frozen architecture {field} must be preserved", module=module["id"], field=field, expected=deepcopy(module[field])))
    if Counter(actual.get("core_instances", [])) != Counter(module["core_instances"]):
        errors.append(_error("PERIPHERAL_CORE_CHANGED", "peripheral planning must preserve this module's exact core aliases", module=module["id"], expected=module["core_instances"]))
    common = actual.get("common_devices")
    if isinstance(common, Mapping):
        for alias in sorted(set(common) & (reserved_core_aliases or set())):
            errors.append(_error("CORE_ALIAS_REUSE", "common alias conflicts with the board-wide fixed core pool; choose a different common alias and update its local pin usages before freezing",
                module=module["id"], device=alias, reserved_fixed_aliases=sorted(reserved_core_aliases)))
    if plan.get("voltage_domains") != architecture["voltage_domains"]:
        errors.append(_error("PERIPHERAL_DOMAINS_CHANGED", "global voltage domains must be copied from the frozen architecture", module=module["id"], expected=architecture["voltage_domains"]))
    return errors

