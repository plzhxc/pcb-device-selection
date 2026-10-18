"""Evaluation-only source facts for an immutable reference graph.

No generated plan is accepted. Supported firmware modes are candidates, not
historical selections. A future proposed-configuration witness may select and
independently validate them without rewriting these reference facts.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any

from .certificate_facts import digest, frozen_source_bundle, EFFECT_FIELDS
from .contract_binding import _authenticated_references, _fact_rows, supply_pin_numbers
from .conditional_pin_rules import bind_frozen_input_conditions, evaluate_pin_rules
from .pin_contracts import SCHEMA, GROUND_ROLES
from .netlist_graph import PHYSICAL_DEVICE_TYPES


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def graph_binding_digest(graph: Any) -> str:
    return digest({key[1]: {"part": c.anchor_id, "type": c.type, "is_abstract": c.is_abstract,
        "pins": {pid: pin.net[1] if pin.net else None for pid, pin in graph.pin_map(key).items()}}
        for key, c in graph.components.items()})


def authenticate_physical_pin_inventory(graph: Any, key: Any, record: Mapping) -> dict:
    """Check actual numbered terminals against independently sourced identities.

    This establishes physical identity only, not a selected electrical mode.
    Pin-label-only images do not grant numeric mappings without their separately
    approved identity evidence. No generated-side fact is copied to a reference.
    """
    from datasheet_automation.knowledge.evidence import primary_document_sha256
    component = graph.components[key]; part = component.anchor_id
    from .physical_catalog import authenticate_physical_catalog
    device = _map(record.get("device"))
    rows = record.get("pins", []); rows = rows if isinstance(rows, list) else []
    numbers = [_map(row).get("pin_number") for row in rows]
    errors = []
    if (not part or device.get("supplier_part") != part or not primary_document_sha256(record) or
            not numbers or not all(isinstance(number, str) and number for number in numbers) or
            type(device.get("pin_count")) is not int or device["pin_count"] != len(numbers) or
            len(set(numbers)) != len(numbers) or set(numbers) != set(graph.pin_map(key))):
        errors.append("actual physical pin set or primary device identity differs from the complete frozen source catalog")
    checked = authenticate_physical_catalog(record, supplier_part=part, component_type=component.type)
    for issue in checked["diagnostics"]:
        errors.append(str(issue.get("pin", "device")) + ": " + issue["message"] +
                      (" (" + issue["field"] + ")" if issue.get("field") else ""))
    return {"authenticated": not errors, "supplier_part": part,
            "physical_pin_ids": sorted(graph.pin_map(key)), "diagnostics": errors}


def bind_reference_contracts(reference_graph: Any, knowledge: Mapping, *, board_input: Mapping,
                              reference_annotations: Mapping | None = None) -> dict:
    """Bind source roles and finite input conditions; leave mux choices unknown.

    Fixed physical supply/ground/passive directions may bind a mode. An
    unselected GPIO mode never becomes known merely because a generated pin
    uses it, or because one supported mode was extracted. Free-form annotations
    are reported, not accepted as an independent configuration proof.
    """
    from datasheet_automation.knowledge.evidence import primary_source_registry, primary_document_sha256
    result = {"schema": SCHEMA, "components": {}, "domains": {}, "constraints": [], "source_bundles": {},
              "input_sha256": digest(board_input), "reference_graph_sha256": graph_binding_digest(reference_graph),
              "binding_scope": "reference source facts and explicitly selected input conditions; no historical firmware inference",
              "binding_diagnostics": []}
    def add(rule: str, message: str, *, component: str | None = None, pin: str | None = None, status: str = "unresolved") -> None:
        result["binding_diagnostics"].append({"rule": rule, "status": status, "message": message, "component": component, "pin": pin})
    if reference_annotations:
        add("REFERENCE_CONFIGURATION_EVIDENCE", "Unscoped reference annotations cannot select firmware modes; provide a separately validated configuration witness")
    from .fixed_inventory import iter_fixed_devices
    inventory = list(iter_fixed_devices(board_input))
    actual = Counter((c.type, c.anchor_id) for c in reference_graph.components.values() if c.is_core)
    expected = Counter()
    for kind, device in inventory:
        expected[("ic" if kind == "ic" else device.get("component_type"), device.get("device_id"))] += device.get("quantity", 0)
    if expected != actual:
        add("REFERENCE_CORE_INVENTORY", "Reference C/S instance quantities differ from the frozen board inventory", status="fail")
    source_index = {"request": {"kind": "user_request", "value": board_input.get("request")}}
    source_index.update({"requirement:" + row["id"]: {"kind": "requirement", "value": row}
                         for row in board_input.get("requirements", []) if isinstance(row, Mapping) and isinstance(row.get("id"), str)})
    registry = None
    for key, component in reference_graph.components.items():
        cid, part = key[1], component.anchor_id
        pins = reference_graph.pin_map(key); facts = {}; supplies = {}
        cc = {"supplier_part": part, "component_type": component.type, "pin_space": "physical" if component.is_core else "logical",
              "pins": facts, "bindings": {}, "supplies": supplies, "logical_terminal_contract": {}}
        result["components"][cid] = cc
        if component.is_core:
            record = _map(knowledge.get(part)); device = _map(record.get("device")); source = _map(record.get("contracts"))
            from .device_electrical import SUPPORTED_TYPES, bind_device_electrical
            if component.type in SUPPORTED_TYPES:
                cc["device_electrical"] = bind_device_electrical(record, part,
                    board_input=board_input, source_index=source_index)
                result["binding_diagnostics"].extend({"component": cid, **row} for row in cc["device_electrical"]["binding_diagnostics"])
            if component.type in PHYSICAL_DEVICE_TYPES:
                identity_check = authenticate_physical_pin_inventory(reference_graph, key, record)
                cc["source_identity_authenticated"] = identity_check["authenticated"]
                cc["source_pin_inventory"] = identity_check
                selected_models = [row.get("model") for kind, row in inventory if kind == "physical_device" and
                                   row.get("component_type") == component.type and row.get("device_id") == part]
                from datasheet_automation.knowledge.model_identity import authenticate_model_display
                model_checks = [authenticate_model_display(record, supplier_part=part, component_type=component.type,
                    display_name=name, canonical_model=device.get("device_name"))
                    for name in (selected_models[0] if len(selected_models) == 1 else None, component.name)]
                if len(selected_models) != 1 or any(check["status"] not in {"exact", "authenticated_alias"} for check in model_checks):
                    cc["source_identity_authenticated"] = False
                    add("REFERENCE_PHYSICAL_MODEL", "Reference and input display names must each match the canonical source model or an independently authenticated identity alias", component=cid)
                if any(check["status"] == "authenticated_alias" for check in model_checks):
                    cc["model_display_identity"] = deepcopy(model_checks)
                for issue in identity_check["diagnostics"]:
                    add("REFERENCE_PHYSICAL_PIN_EVIDENCE", issue, component=cid)
            if component.type == "module":
                from .module_catalog import authenticate_module_catalog
                module_inventory = authenticate_module_catalog(record, supplier_part=part)
                cc["source_module_inventory"] = module_inventory
                if module_inventory["status"] == "unresolved":
                    for issue in module_inventory["diagnostics"]:
                        add(issue["rule"], issue["message"], component=cid)
                    continue
            source_pins = [row.get("pin_number") for row in record.get("pins", []) if isinstance(row, Mapping)]
            if (not part or device.get("supplier_part") != part or source.get("supplier_part") != part or
                    source.get("document_sha256") != primary_document_sha256(record) or
                    _map(source.get("provenance")).get("source_authentication_version") not in {2, 3} or
                    device.get("pin_count") != len(source_pins) or len(set(source_pins)) != len(source_pins) or set(source_pins) != set(pins)):
                add("REFERENCE_PHYSICAL_IDENTITY", "Source device identity and complete physical pin set do not match the reference", component=cid)
                continue
            cc.update(source_provenance=deepcopy(source.get("provenance")), source_registry=deepcopy(source.get("source_registry")),
                      document_sha256=source.get("document_sha256"), pins_complete=True)
            source_bundle = frozen_source_bundle(record, part)
            result["source_bundles"][part] = source_bundle
            current_registry = primary_source_registry(source_bundle["bundle"])
            if current_registry and not cc["source_registry"]:
                cc["source_registry"] = current_registry
            for pid, fact in _fact_rows(record).items():
                fields = [field for field in ("semantic_role", "electrical_type", "capabilities", "modes", "required_connection", "allow_nc", "resources", "gpio_remap_safe", "voltage_min_v", "voltage_max_v") if fact.get(field) is not None]
                if pid not in pins or not fields or not all(_authenticated_references(record, part,
                        _map(fact.get("field_evidence")).get(field, fact.get("evidence")), [field]) for field in fields):
                    add("REFERENCE_SOURCE_FACT", "Exact source field evidence is incomplete or unauthenticated", component=cid, pin=pid)
                    continue
                accepted_rules = []
                for rule in fact.get("conditional_rules", []):
                    effect = _map(_map(rule).get("effect"))
                    rule_fields = [effect.get("check")] if effect.get("kind") == "not_applicable" else EFFECT_FIELDS.get(effect.get("kind"), ["unsupported_effect"])
                    if _authenticated_references(record, part, _map(rule).get("evidence"), rule_fields):
                        accepted_rules.append(rule)
                    else:
                        add("REFERENCE_CONDITIONAL_SOURCE", "Conditional source is unauthenticated; the rule cannot grant a fact", component=cid, pin=pid)
                if "conditional_rules" in fact:
                    fact["conditional_rules"] = accepted_rules
                facts[pid] = fact
            for symbol, supply in _map(source.get("supply_pins")).items():
                numbers = supply_pin_numbers(supply)
                if numbers and set(numbers) <= set(pins) and _authenticated_references(record, part, _map(supply).get("evidence"), ["pin_number", "pin_name"]):
                    supplies[symbol] = {"pin_numbers": numbers, "pin": numbers[0] if len(numbers) == 1 else None,
                                        "domain": None, "evidence": deepcopy(supply["evidence"]),
                                        "identity_evidence": deepcopy(supply["evidence"]), "binding_evidence": []}
        else:
            if registry is None:
                from netlist_end_to_end.common_devices.registry import TemplateRegistry
                registry = TemplateRegistry.load_builtin()
            definitions = [registry.get(row["template_id"], row["version"]) for row in registry.list_templates() if row["kind"] == "primitive"]
            definitions = [row for row in definitions if row.get("device_type") == component.type]
            evidence = None
            if len(definitions) == 1:
                definition = definitions[0]; ports = definition["ports"]
                if {p["id"] for p in ports} == {"a", "b"} and set(pins) in ({"1", "2"}, {"a", "b"}) and all(p.get("direction") == "passive" for p in ports):
                    evidence = [{"source_kind": "template", "source_ref": definition["template_id"] + "@" + definition["version"],
                        "document_sha256": registry.definition_sha256(definition["template_id"], definition["version"]),
                        "quote": json.dumps(ports, sort_keys=True), "locator": "ports"}]
                    cc["logical_pin_mapping"] = {"1": "a", "2": "b"} if "1" in pins else {"a": "a", "b": "b"}
            elif component.type == "connector" and set(pins) in ({str(i) for i in range(1, len(pins) + 1)}, {f"contact_{i}" for i in range(1, len(pins) + 1)}):
                from .common_terminal_contracts import DEFINITIONS, VERSION
                definition = DEFINITIONS["connector"]
                evidence = [{"source_kind": "template", "source_ref": "pcbagent.common.connector@" + VERSION,
                    "document_sha256": digest({"version": VERSION, "definition": definition}),
                    "quote": json.dumps(definition, sort_keys=True), "locator": "DEFINITIONS.connector"}]
            if evidence:
                for pid in pins:
                    facts[pid] = {"semantic_role": "passive_terminal", "electrical_type": "passive", "capabilities": ["passive"],
                        "modes": ["passive"], "resources": [], "required_connection": component.type != "connector",
                        "allow_nc": component.type == "connector", "evidence": deepcopy(evidence)}
            else:
                add("REFERENCE_COMMON_TERMINALS", "Common subtype or complete logical terminal evidence is unavailable", component=cid)
        for pid, pin in pins.items():
            fact = _map(facts.get(pid))
            fields = [predicate.get("field") for rule in fact.get("conditional_rules", [])
                      for predicate in _map(_map(rule).get("when")).get("all", []) if isinstance(predicate, Mapping)]
            selected = bind_frozen_input_conditions(board_input=board_input, supplier_part=part,
                source_index=source_index, fields=fields)
            result["binding_diagnostics"].extend({"component": cid, "pin": pid, **row} for row in selected["diagnostics"])
            # A named singleton net has no wire peer, just like an absent net.
            # This records the observed topology; NC permission still requires
            # the independent source contract, and no net or pin is rewritten.
            binding = {"usage": None, "mode": None, "domain": None, "resources": None,
                       "historical_selected_mode": None,
                       "unconnected": not bool(reference_graph.connected_pins(pin.key)), **selected["conditions"]}
            effective = evaluate_pin_rules(fact, binding, domains=result["domains"], supplies=supplies,
                component_bindings=cc["bindings"], supplier_part=part, pin_number=pid,
                pin_nets={n: p.net[1] if p.net else None for n, p in pins.items()})["fact"]
            etype = effective.get("electrical_type")
            if etype in {"power_in", "power_out", "ground", "passive", "no_connect", "analog_input", "analog_output"} and effective.get("modes") == [etype]:
                binding.update(mode=etype, mode_selection_basis="documented_fixed_electrical_direction")
                if isinstance(fact.get("capabilities"), list) and len(fact["capabilities"]) == 1:
                    binding["usage"] = fact["capabilities"][0]
                if fact.get("resources") == []:
                    binding["resources"] = []
            cc["bindings"][pid] = binding
            if fact.get("semantic_role") in GROUND_ROLES and etype == "ground" and pin.net:
                did = "reference_zero:" + pin.net[1]
                result["domains"][did] = {"net": pin.net[1], "voltage_v": 0, "reference": None,
                    "evidence": deepcopy(fact.get("evidence")), "basis": "source ground role defines the reference datum"}
                binding["domain"] = did
        # No voltage is derived from a net label or borrowed generated plan.
        for supply in supplies.values():
            if supply.get("pin"):
                supply["domain"] = cc["bindings"][supply["pin"]].get("domain")
    return result


__all__ = ["bind_reference_contracts", "graph_binding_digest", "authenticate_physical_pin_inventory"]
