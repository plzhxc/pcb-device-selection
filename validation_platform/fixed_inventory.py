"""Authenticate fixed IC/module instances against input and knowledge snapshots.

Hashes detect snapshot changes; input/catalog/physical-pin equality establishes
the identity chain. Module names, topology and pin counts alone are insufficient.
This module never reads reference wiring, a database, or generation internals.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
import re
from typing import Any

IDENTITY_FIELDS = frozenset({"supplier_part", "inventory_kind", "component_type", "input_model", "catalog_model",
    "instance_index", "catalog_pin_count", "source_document_sha256", "terminal_scope"})
_IDENTITY = re.compile(r"^[CS][0-9]+$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_LIMIT = 1000
PHYSICAL_DEVICE_TYPES = frozenset({"mosfet", "transistor", "microphone", "antenna"})
FIXED_COLLECTIONS = (("core_devices", "ic"), ("functional_devices", "functional_module"),
                     ("physical_devices", "physical_device"))


def iter_fixed_devices(board: Mapping):
    """Iterate explicit inventories without inferring devices from prose/types."""
    for collection, kind in FIXED_COLLECTIONS:
        rows = board.get(collection, [])
        if not isinstance(rows, list):
            raise ValueError(f"{collection} must be an array")
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError(f"{collection} rows must be objects")
            yield kind, row


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _pin_inventory(value: Any, *, catalog: bool = False, closed: bool = False) -> dict[str, Any] | None:
    if not isinstance(value, list) or not value or len(value) > 10000:
        return None
    result = {}
    for raw in value:
        row = _map(raw)
        number, name = (row.get("pin_number"), row.get("pin_name")) if catalog else (row.get("id"), row.get("name"))
        if (not isinstance(number, str) or not number or number in result
                or name is not None and not isinstance(name, str)
                or closed and set(row) != {"id", "name"}):
            return None
        result[number] = name
    return result


def authenticate_fixed_inventory(generation: Mapping, knowledge: Mapping, *, board_input: Mapping | None = None) -> dict:
    """Return authenticated instance identities and module-only graph anchors.

Old IC-only artifacts retain their original explicit C/S netlist identities.
Any physical module requires the complete new fixed-inventory contract. A
failed global inventory check exports no partial module anchors.
"""
    from datasheet_automation.knowledge.evidence import primary_document_sha256

    generation, knowledge, board_input = _map(generation), _map(knowledge), _map(board_input)
    compilation = _map(generation.get("compilation")); manifest = _map(compilation.get("manifest"))
    netlist = _map(compilation.get("netlist")); context = _map(generation.get("context"))
    context_modules = _map(context.get("modules")); plan = _map(generation.get("plan"))
    inventory = generation.get("fixed_inventory")
    diagnostics = []
    result = {"schema": "pcbagent.fixed_inventory_authentication.v1", "status": "unresolved",
        "components": {}, "authenticated_identity_anchors": {}, "diagnostics": diagnostics,
        "fixed_inventory_sha256": None}
    def add(rule: str, message: str, status: str = "fail", **detail: Any) -> None:
        diagnostics.append({"rule": rule, "status": status, "message": message, **detail})

    modules_present = any(_map(device).get("type") == "module" for device in netlist.values())
    declared_modules = bool(board_input.get("functional_devices") or board_input.get("physical_devices"))
    if inventory is None:
        if modules_present or declared_modules or manifest.get("identity_overlay"):
            add("FIXED_INVENTORY_MISSING", "Physical functional modules require an independently checkable frozen inventory", "unresolved")
        else:
            result["status"] = "legacy_ic_only"
        return result
    if not isinstance(inventory, Mapping) or len(inventory) > _LIMIT:
        add("FIXED_INVENTORY_SCHEMA", "A bounded fixed-inventory object is required")
        return result
    try:
        sha = _digest(inventory)
    except (TypeError, ValueError):
        add("FIXED_INVENTORY_SCHEMA", "Inventory must contain canonical finite JSON values")
        return result
    result["fixed_inventory_sha256"] = sha
    if generation.get("fixed_inventory_sha256") != sha or manifest.get("fixed_inventory_sha256") != sha:
        add("FIXED_INVENTORY_DIGEST", "Generation and compilation must bind the exact same frozen inventory")
    if compilation.get("ok") is not True or manifest.get("status") != "compiled":
        add("FIXED_INVENTORY_COMPILATION", "Only a successful compilation can authenticate physical instances", "unresolved")
    try:
        if manifest.get("context_sha256") != _digest(context) or manifest.get("netlist_sha256") != _digest(netlist):
            add("FIXED_INVENTORY_COMPILATION_DIGEST", "Frozen context or actual netlist differs from the compilation digest")
    except (TypeError, ValueError):
        add("FIXED_INVENTORY_COMPILATION_DIGEST", "Context and netlist must be canonical finite JSON")

    expected = {}
    for collection, kind in FIXED_COLLECTIONS:
        rows = board_input.get(collection, [] if collection != "core_devices" else None)
        if not isinstance(rows, list):
            add("FIXED_INVENTORY_INPUT", "The actual frozen input must contain typed device inventories", "unresolved", collection=collection)
            continue
        for raw in rows:
            row = _map(raw); part = row.get("device_id"); quantity = row.get("quantity")
            if (not isinstance(part, str) or not _IDENTITY.fullmatch(part) or part in expected
                    or type(quantity) is not int or not 1 <= quantity <= _LIMIT):
                add("FIXED_INVENTORY_INPUT", "Input device identities must be unique with bounded positive integer quantities", collection=collection)
                continue
            if kind == "functional_module" and (set(row) != {"device_id", "model", "quantity", "component_type"}
                    or row.get("component_type") != "module" or not isinstance(row.get("model"), str) or not row["model"]):
                add("FIXED_INVENTORY_INPUT", "Functional input rows must specify exact module model, identity and quantity", part=part)
                continue
            if kind == "physical_device" and (set(row) != {"device_id", "model", "quantity", "component_type"}
                    or row.get("component_type") not in PHYSICAL_DEVICE_TYPES
                    or not isinstance(row.get("model"), str) or not row["model"].strip()):
                add("FIXED_INVENTORY_INPUT", "Physical input requires an exact supported component type, model, identity and quantity", part=part)
                continue
            expected[part] = (row, kind)
    expected_count = sum(row[0]["quantity"] for row in expected.values())
    if expected_count > _LIMIT or expected_count != len(inventory):
        add("FIXED_INVENTORY_QUANTITY", "Frozen instance count must equal the complete input inventory", expected=expected_count, actual=len(inventory))

    by_alias = {}; indices = {}
    for alias, raw in inventory.items():
        item = _map(raw); identity = _map(item.get("identity")); part = identity.get("supplier_part")
        if (not isinstance(alias, str) or not alias or set(item) != {"identity", "pins"}
                or set(identity) != IDENTITY_FIELDS or not isinstance(part, str) or part not in expected):
            add("FIXED_INSTANCE_SCHEMA", "Each fixed alias requires the closed identity and complete pin contract", device=alias)
            continue
        row, kind = expected[part]
        record = _map(knowledge.get(part)); catalog = _map(record.get("device"))
        source_hash = primary_document_sha256(record)
        numbers = _pin_inventory(record.get("pins"), catalog=True)
        frozen_numbers = _pin_inventory(item.get("pins"), closed=True)
        index = identity.get("instance_index")
        component_type = row.get("component_type") if kind != "ic" else "ic"
        model_matches = row.get("model") == catalog.get("device_name")
        if kind == "physical_device":
            from datasheet_automation.knowledge.model_identity import authenticate_model_display
            model_matches = authenticate_model_display(record, supplier_part=part, component_type=component_type,
                display_name=row.get("model"), canonical_model=catalog.get("device_name"))["status"] in {"exact", "authenticated_alias"}
        if (identity.get("inventory_kind") != kind or identity.get("component_type") != component_type
                or identity.get("input_model") != row.get("model") or identity.get("catalog_model") != catalog.get("device_name")
                or catalog.get("supplier_part") != part
                or kind != "ic" and not model_matches
                or type(index) is not int or not 1 <= index <= row["quantity"]
                or identity.get("terminal_scope") != "catalog_pins_only"):
            add("FIXED_INSTANCE_IDENTITY", "Input, catalog and frozen instance identities must agree exactly", device=alias, supplier_part=part)
            continue
        if not isinstance(source_hash, str) or not _SHA.fullmatch(source_hash) or identity.get("source_document_sha256") != source_hash:
            add("FIXED_INSTANCE_DOCUMENT", "Frozen identity must reference the actual identified primary source", device=alias, supplier_part=part)
        if (numbers is None or frozen_numbers != numbers or type(catalog.get("pin_count")) is not int
                or catalog["pin_count"] != len(numbers or {}) or type(identity.get("catalog_pin_count")) is not int
                or identity["catalog_pin_count"] != len(numbers or {})):
            add("FIXED_INSTANCE_PINS", "Frozen pin numbers and labels must equal the complete real catalog inventory", device=alias, supplier_part=part)
        if kind == "physical_device":
            from .physical_catalog import authenticate_physical_catalog
            checked = authenticate_physical_catalog(record, supplier_part=part, component_type=component_type)
            diagnostics.extend({**d, "device": alias} for d in checked["diagnostics"])
        if kind == "functional_module":
            from .module_catalog import authenticate_module_catalog
            checked = authenticate_module_catalog(record, supplier_part=part)
            diagnostics.extend({**d, "device": alias} for d in checked["diagnostics"])
        indices.setdefault(part, []).append(index)
        by_alias[alias] = (identity, frozen_numbers)
    for part, (row, _) in expected.items():
        if sorted(indices.get(part, [])) != list(range(1, row["quantity"] + 1)):
            add("FIXED_INSTANCE_INDICES", "Every requested physical instance must occur exactly once", supplier_part=part)

    assigned = []
    assignments = set()
    module_ids = set()
    for raw in _rows(plan.get("modules")):
        module = _map(raw); mid = module.get("id")
        fixed = module.get("core_instances")
        if not isinstance(mid, str) or not isinstance(fixed, list) or any(not isinstance(alias, str) for alias in fixed):
            add("FIXED_INSTANCE_ASSIGNMENT", "Frozen module plan requires explicit fixed-instance assignments")
            continue
        if mid in module_ids:
            add("FIXED_INSTANCE_ASSIGNMENT", "Frozen module IDs must be unique", module=mid)
        module_ids.add(mid)
        assigned.extend(fixed)
        assignments.update((mid, alias) for alias in fixed)
        if set(fixed) & set(_map(module.get("common_devices"))):
            add("FIXED_INSTANCE_COMMON_COLLISION", "A fixed device cannot also be generated as a common template", module=mid)
    if Counter(assigned) != Counter({alias: 1 for alias in inventory}):
        add("FIXED_INSTANCE_ASSIGNMENT", "Every fixed alias must be assigned to exactly one module")
    actual_assignments = {(mid, alias) for mid, module in context_modules.items()
        for alias, device in _map(_map(module).get("devices")).items()
        if _map(device).get("type") in {"ic", "module"} or alias in inventory}
    if actual_assignments != assignments:
        add("FIXED_INSTANCE_CONTEXT_COVERAGE", "Frozen context physical devices must exactly equal the approved assignments")

    authenticated = {}; overlay = {}; seen = Counter()
    for raw in _rows(manifest.get("instances")):
        instance = _map(raw); mid, alias, cid = instance.get("module"), instance.get("device"), instance.get("component")
        if not all(isinstance(value, str) and value for value in (mid, alias, cid)):
            add("FIXED_INSTANCE_MANIFEST", "Instance component, module and alias must be explicit strings")
            continue
        if alias not in by_alias:
            if instance.get("identity") is not None or _map(netlist.get(cid)).get("type") in {"ic", "module"}:
                add("FIXED_INSTANCE_UNLISTED", "A physical device instance is absent from the approved fixed inventory", component=cid)
            continue
        identity, numbers = by_alias[alias]
        seen[alias] += 1
        device = _map(_map(_map(context_modules.get(mid)).get("devices")).get(alias)); actual = _map(netlist.get(cid))
        if (not isinstance(cid, str) or cid in authenticated or (mid, alias) not in assignments
                or instance.get("identity") != identity or instance.get("pin_space") not in {"physical", "physical_with_catalog_aliases"}):
            add("FIXED_INSTANCE_MANIFEST", "Compilation instance must match one approved frozen identity and assignment", component=cid)
            continue
        is_non_ic = identity["inventory_kind"] != "ic"
        source_id = None if is_non_ic else identity["supplier_part"]
        expected_name = identity["catalog_model"] or identity["input_model"] or identity["supplier_part"]
        for origin, item in (("context", device), ("netlist", actual)):
            if (item.get("type") != identity["component_type"] or item.get("is_abstract") is not is_non_ic
                    or item.get("id") != source_id or item.get("name") != expected_name
                    or item.get("value") is not None or _pin_inventory(item.get("pins")) != numbers):
                add("FIXED_INSTANCE_REALIZATION", "Actual device identity and exact physical pins differ from the frozen catalog instance", component=cid, origin=origin)
        authenticated[cid] = deepcopy(dict(identity))
        if is_non_ic:
            overlay[cid] = deepcopy(dict(identity))
    if seen != Counter({alias: 1 for alias in inventory}):
        add("FIXED_INSTANCE_COVERAGE", "Compilation must contain every approved fixed instance exactly once")
    if {cid for cid, item in netlist.items() if _map(item).get("type") in {"ic", "module"} or cid in overlay} != set(authenticated):
        add("FIXED_INSTANCE_NETLIST_COVERAGE", "Every actual physical device must have an authenticated compilation instance")
    if _map(manifest.get("identity_overlay")) != overlay:
        add("FIXED_IDENTITY_OVERLAY", "Module identity overlay must exactly equal the authenticated functional instances")
    if diagnostics:
        result["status"] = "fail" if any(row["status"] == "fail" for row in diagnostics) else "unresolved"
        return result
    result.update(status="authenticated", components=authenticated,
        authenticated_identity_anchors={cid: identity["supplier_part"] for cid, identity in overlay.items()})
    return result


__all__ = ["IDENTITY_FIELDS", "PHYSICAL_DEVICE_TYPES", "FIXED_COLLECTIONS", "iter_fixed_devices", "authenticate_fixed_inventory"]
