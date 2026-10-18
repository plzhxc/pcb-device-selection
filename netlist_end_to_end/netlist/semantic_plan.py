"""Normalize and validate a semantic plan before freezing its connectivity context."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any, Mapping
import math

from jsonschema import Draft202012Validator
from validation_platform.common_terminal_contracts import (
    authorize_common_open_pins,
    check_common_port_coverage,
)
from validation_platform.operating_selections import bind_operating_conditions
from validation_platform.planning_obligations import CANDIDATE_MODE

from ..common_devices import CommonDevice
from .prompts import load_prompt
from .scl import compile_scl
from .semantic_contracts import PLAN_SCHEMA
from .semantic_domains import (
    external_source_port_valid,
    materialize_voltage_domains,
    port_domain_selection_valid,
)
from .semantic_inventory import _input_conditions, _pin_nc_policy, _template_facts
from .semantic_selection import (
    authenticated_selection_constraints,
    fixed_device_part,
    resolve_plan_operating_selections,
    validate_machine_selection,
)
from .semantic_shared import MAX_COMMON_DEVICES, MAX_MODULES, _diagnostic, _digest
from .semantic_values import normalize_common_values


def _normalize_contact_identity_maps(plan: dict) -> list[dict]:
    """Materialize identity maps only for complete canonical logical contacts.

    No physical contact numbering or pin permutation is inferred. An explicitly
    supplied map, including an incorrect one, remains subject to validation.
    """
    records = []
    for module in plan["modules"]:
        for alias, spec in module["common_devices"].items():
            if spec["device"]["type"] != "connector" or "terminal_mapping" in spec:
                continue
            count = spec.get("parameters", {}).get("contact_count")
            if type(count) is not int or not 1 <= count <= 1024:
                continue
            ids = [pin["id"] for pin in spec["device"]["pins"]]
            canonical = [f"contact_{i}" for i in range(1, count + 1)]
            if len(ids) != count or set(ids) != set(canonical):
                continue
            spec["terminal_mapping"] = {pin: pin for pin in canonical}
            records.append({"kind": "canonical_logical_contact_identity", "module": module["id"],
                            "device": alias, "before": None, "after": deepcopy(spec["terminal_mapping"]),
                            "pin_ids_changed": False, "physical_numbering_inferred": False})
    return records


def _normalize_connector_numbering(plan: dict, pool: Mapping) -> list[dict]:
    """Serialize abstract contact_i as logical pin str(i) before any SCL exists.

    The explicit complete bijection, not pin order/name or circuit wiring,
    determines this representation. Build all three replacements before
    committing an instance; incomplete maps or usage coverage stay untouched
    for the existing contract validators. The caller owns the raw model plan.
    Fixed catalog devices and their physical/CAD identities never participate.
    """
    records = []
    for module in plan["modules"]:
        for alias, spec in module["common_devices"].items():
            device = spec["device"]
            if (alias in pool or alias in module["core_instances"]
                    or device["type"] != "connector" or device["is_abstract"] is not True
                    or device["id"] is not None):
                continue
            count = spec.get("parameters", {}).get("contact_count")
            if type(count) is not int or not 1 <= count <= 1024:
                continue
            ids = [pin["id"] for pin in device["pins"]]
            mapping = spec.get("terminal_mapping")
            canonical = {f"contact_{i}": str(i) for i in range(1, count + 1)}
            if (len(ids) != count or len(set(ids)) != count
                    or not isinstance(mapping, Mapping) or set(mapping) != set(ids)
                    or any(not isinstance(value, str) for value in mapping.values())
                    or set(mapping.values()) != set(canonical)):
                continue
            usages = [usage for usage in module["pin_usage"] if usage["device"] == alias]
            usage_ids = [usage["pin"] for usage in usages]
            if len(usage_ids) != count or set(usage_ids) != set(ids):
                continue
            renaming = {pin: canonical[mapping[pin]] for pin in ids}
            if all(old == new for old, new in renaming.items()):
                continue
            # Simultaneous replacement handles swaps such as 1->2 and 2->1.
            # Contact-keyed parameters and port bindings already name canonical
            # contacts, so changing them would change the selected interface.
            new_pins = [{**pin, "id": renaming[pin["id"]]} for pin in device["pins"]]
            new_mapping = {str(i): f"contact_{i}" for i in range(1, count + 1)}
            new_usage = [{**usage, "pin": renaming[usage["pin"]]} if usage["device"] == alias else usage
                         for usage in module["pin_usage"]]
            records.append({"kind": "canonical_connector_numbering", "contract": "scl.connector_contacts.1.0",
                "module": module["id"], "device": alias, "pin_space": "logical",
                "before": {"pin_ids": ids, "terminal_mapping": deepcopy(mapping), "pin_usage_ids": usage_ids},
                "after": {"pin_ids": [pin["id"] for pin in new_pins], "terminal_mapping": deepcopy(new_mapping),
                          "pin_usage_ids": [renaming[pin] for pin in usage_ids]},
                "pin_id_mapping": dict(sorted(renaming.items())), "source_refs": deepcopy(spec["source_refs"]),
                "physical_numbering_inferred": False, "connectivity_generated": False,
                "electrical_validation_credit": False})
            device["pins"], spec["terminal_mapping"], module["pin_usage"] = new_pins, new_mapping, new_usage
    return records


def _normalize_passive_numbering(plan: dict, pool: Mapping) -> list[dict]:
    """Give explicitly mapped, nonpolarized abstract a/b primitives IDs 1/2.

    This is a simultaneous representation change before SCL generation. Neither
    names, pin order, values nor reference topology supply a missing mapping.
    Polarized/unknown-polarization capacitors and fixed catalog pins are excluded.
    """
    records = []
    for module in plan["modules"]:
        for alias, spec in module["common_devices"].items():
            device = spec["device"]; parameters = spec.get("parameters", {})
            if (alias in pool or alias in module["core_instances"]
                    or device["type"] not in {"resistor", "capacitor", "inductor"}
                    or device["is_abstract"] is not True or device["id"] is not None):
                continue
            if device["type"] == "capacitor" and (parameters.get("polarization") != "non_polarized"
                    or parameters.get("positive_port") is not None):
                continue
            ids = [pin["id"] for pin in device["pins"]]
            mapping = spec.get("terminal_mapping")
            if (len(ids) != 2 or len(set(ids)) != 2 or not isinstance(mapping, Mapping)
                    or set(mapping) != set(ids) or any(not isinstance(v, str) for v in mapping.values())
                    or set(mapping.values()) != {"a", "b"}):
                continue
            usages = [row for row in module["pin_usage"] if row["device"] == alias]
            usage_ids = [row["pin"] for row in usages]
            if len(usage_ids) != 2 or set(usage_ids) != set(ids):
                continue
            renaming = {pin: {"a": "1", "b": "2"}[mapping[pin]] for pin in ids}
            if all(old == new for old, new in renaming.items()):
                continue
            new_pins = [{**pin, "id": renaming[pin["id"]]} for pin in device["pins"]]
            new_usage = [{**row, "pin": renaming[row["pin"]]} if row["device"] == alias else row
                         for row in module["pin_usage"]]
            new_mapping = {"1": "a", "2": "b"}
            records.append({"kind": "canonical_passive_numbering", "contract": "scl.nonpolar_ab.1.0",
                "module": module["id"], "device": alias, "pin_space": "logical",
                "before": {"pin_ids": ids, "terminal_mapping": deepcopy(mapping), "pin_usage_ids": usage_ids},
                "after": {"pin_ids": [pin["id"] for pin in new_pins], "terminal_mapping": new_mapping,
                          "pin_usage_ids": [renaming[pin] for pin in usage_ids]},
                "pin_id_mapping": dict(sorted(renaming.items())), "source_refs": deepcopy(spec["source_refs"]),
                "physical_numbering_inferred": False, "connectivity_generated": False,
                "electrical_validation_credit": False})
            device["pins"], spec["terminal_mapping"], module["pin_usage"] = new_pins, new_mapping, new_usage
    return records


def _freeze(plan: Any, pool: dict, source_index: dict, board_id: str, *, input_derived_fields: list | None = None,
            normalizations: list | None = None, operating_authority: Mapping | None = None,
            static_environment: Mapping | None = None,
            planning_validation_mode: str = "strict") -> tuple[dict | None, list]:
    errors = [_diagnostic("PLAN_SCHEMA", e.message, path=".".join(map(str, e.absolute_path)))
              for e in Draft202012Validator(PLAN_SCHEMA).iter_errors(plan)]
    if errors:
        return None, errors
    primitive_templates = _template_facts()
    records, value_errors = normalize_common_values(plan, primitive_templates, source_index)
    records.extend(_normalize_contact_identity_maps(plan))
    records.extend(_normalize_connector_numbering(plan, pool))
    records.extend(_normalize_passive_numbering(plan, pool))
    errors.extend(value_errors)
    if normalizations is not None:
        normalizations.extend(records)
    if len(plan["modules"]) > MAX_MODULES:
        return None, [_diagnostic("RESOURCE_LIMIT", "too many modules")]
    operating = resolve_plan_operating_selections(plan, pool, operating_authority)
    errors.extend(_diagnostic(row["rule"], row["message"], **{k:v for k,v in row.items() if k not in {"rule", "message"}})
                  for row in operating["diagnostics"])
    if operating["diagnostics"]:
        return None, errors
    if operating["receipts"]:
        # A provisional module freeze must not mutate the shared instance pool.
        # Recompute permissions from original source facts, never from the old
        # allowance flags or the model's unconnected bool.
        pool = deepcopy(pool)
        assert operating_authority is not None
        frozen_input = operating_authority["board_input"]
        for alias, spec in pool.items():
            part = fixed_device_part(spec); raw = operating_authority["knowledge"][part]
            selected_binding = bind_operating_conditions(board_input=frozen_input, supplier_part=part,
                source_index=source_index, operating=operating["conditions_by_alias"].get(alias))
            binding = selected_binding["conditions"]
            _, input_selections = _input_conditions(frozen_input, frozen_input.get("requirements"), part, source_index)
            constraints = authenticated_selection_constraints(raw, part, binding)
            spec["selection_constraints"] = constraints["pins"]
            policies = {pin["id"]: _pin_nc_policy(_map_pin, binding, input_selections,
                {**raw, "supplier_part": part}, raw.get("evidence", []), part=part, pin=pin["id"])
                for pin in spec["device"]["pins"]
                for _map_pin in [(raw.get("contracts") or {}).get("pins", {}).get(pin["id"], {})]}
            spec["allow_nc"] = {pin: policy["allowed"] for pin, policy in policies.items()}
            spec["conditional_nc"] = {pin: policy for pin, policy in policies.items() if policy["allowed"] and policy["conditions"]}
    modules, assigned, common_count = {}, [], 0

    def refs(row: dict, where: str) -> None:
        if not row["source_refs"] or any(ref not in source_index for ref in row["source_refs"]):
            errors.append(_diagnostic("UNSUPPORTED_SOURCE", f"{where} requires existing source_index references"))

    for module in plan["modules"]:
        mid = module["id"]
        if mid == "__top__":
            errors.append(_diagnostic("RESERVED_MODULE_ID", "__top__ is reserved for the top-level source"))
            continue
        if mid in modules:
            errors.append(_diagnostic("DUPLICATE_MODULE", f"duplicate module {mid!r}"))
            continue
        devices = {}
        for alias in module["core_instances"]:
            assigned.append(alias)
            if alias not in pool:
                errors.append(_diagnostic("UNKNOWN_CORE_INSTANCE", f"module {mid} references unapproved core {alias}"))
            else:
                devices[alias] = deepcopy(pool[alias]["device"])
        for alias, spec in module["common_devices"].items():
            common_count += 1
            if alias in pool or alias in devices:
                errors.append(_diagnostic("CORE_ALIAS_REUSE", f"common alias {alias} conflicts with fixed core pool"))
                continue
            refs(spec, f"common {mid}.{alias}")
            if spec["device"].get("name") in {p["identity"].get("catalog_model") for p in pool.values()} - {None}:
                errors.append(_diagnostic("FIXED_MODEL_COMMON_SUBSTITUTION", "a supplied fixed model cannot be duplicated or substituted with a generated common device", module=mid, device=alias))
            item = deepcopy(spec["device"])
            definitions = [d for d in primitive_templates if d["device_type"] == item["type"]]
            if len(definitions) == 1:
                from ..common_devices.terminal_mapping import primitive_terminal_mapping
                _, mapping_errors = primitive_terminal_mapping(definitions[0],
                    [pin["id"] for pin in item["pins"]], spec.get("terminal_mapping"),
                    numeric_ab={"1": "a", "2": "b"}, parameters=spec.get("parameters"))
                errors.extend(_diagnostic(row["code"], row["message"], module=mid, device=alias,
                    **{k:v for k,v in row.items() if k not in {"code", "message"}}) for row in mapping_errors)
            if item["type"] == "module":
                errors.append(_diagnostic("FUNCTIONAL_MODULE_REQUIRES_FIXED_INVENTORY", "physical functional modules must come from the exact SQL-backed fixed pool; a generic common module cannot substitute for missing device knowledge", module=mid, device=alias))
            if item["type"] == "connector" and item["value"] is not None:
                errors.append(_diagnostic("CONNECTOR_NON_ELECTRICAL_VALUE", "connector value must be null; contact count belongs in parameters.contact_count", module=mid, device=alias))
            if item["type"] == "capacitor":
                rating = spec.get("parameters", {}).get("rated_voltage_v")
                if type(rating) not in {int, float} or type(rating) is float and not math.isfinite(rating) or rating <= 0:
                    errors.append(_diagnostic("CAPACITOR_RATING_REQUIRED", "capacitor parameters.rated_voltage_v must be an explicit finite positive JSON number in volts; select a supported rating before SCL generation", module=mid, device=alias, field="parameters.rated_voltage_v", actual=rating))
            if not item["pins"] and item["type"] not in {"mechanical", "fiducial"}:
                errors.append(_diagnostic("MISSING_COMMON_TERMINALS", f"{mid}.{alias} requires explicit logical terminals"))
            if item["type"] in {"resistor", "capacitor", "inductor", "ferrite_bead", "crystal", "oscillator"} and not item["value"]:
                errors.append(_diagnostic("MISSING_COMMON_VALUE", f"{mid}.{alias} requires an explicit electrical value"))
            try:
                CommonDevice.from_dict({**item, "pins": [{**p, "net": None} for p in item["pins"]]})
            except (TypeError, ValueError) as exc:
                errors.append(_diagnostic("INVALID_COMMON_DEVICE", str(exc), module=mid, device=alias))
                continue
            devices[alias] = item
        usages = {}
        for usage in module["pin_usage"]:
            key = (usage["device"], usage["pin"])
            if key in usages:
                errors.append(_diagnostic("DUPLICATE_PIN_USAGE", f"duplicate pin usage {mid}.{key}"))
            usages[key] = usage
            if operating_authority is not None and usage["device"] in pool:
                selected = bind_operating_conditions(board_input=operating_authority["board_input"],
                    supplier_part=fixed_device_part(pool[usage["device"]]), source_index=source_index,
                    operating=operating["conditions_by_alias"].get(usage["device"]), planned=usage)
                errors.extend(_diagnostic(row["rule"], row["message"], module=mid, device=key[0], pin=key[1],
                    **{k:v for k,v in row.items() if k not in {"rule", "message"}}) for row in selected["diagnostics"])
            authorization = pool.get(usage["device"], {}).get("conditional_nc", {}).get(usage["pin"], {})
            if usage["unconnected"]:
                for field, fact in authorization.get("conditions", {}).items():
                    category, name = field.split(".", 1)
                    existing = usage.get(category, {}).get(name)
                    if existing is not None and (type(existing["value"]) is not type(fact["value"]) or existing["value"] != fact["value"]):
                        errors.append(_diagnostic("INPUT_CONDITION_CONFLICT", "planned condition conflicts with the frozen selection used to authorize this open pin", module=mid, device=key[0], pin=key[1], field=field))
                        continue
                    usage.setdefault(category, {})[name] = {"value": fact["value"], "source_refs": sorted(set(fact["source_refs"]) | set((existing or {}).get("source_refs", [])))}
                    if input_derived_fields is not None:
                        input_derived_fields.append({"module": mid, "device": key[0], "pin": key[1], "field": field,
                            "value": fact["value"], "source_refs": fact["source_refs"], "rule_ids": authorization["rule_ids"], "method": "exact_frozen_input_selection"})
            refs(usage, f"pin usage {mid}.{key}")
            for category in ("configuration", "requirements"):
                for condition, fact in usage.get(category, {}).items():
                    refs(fact, f"pin usage {mid}.{key}.{category}.{condition}")
        required = {(alias, pin["id"]) for alias, item in devices.items() for pin in item["pins"]}
        if set(usages) != required:
            errors.append(_diagnostic("PIN_USAGE_COVERAGE", "pin_usage must cover exactly every fixed physical pin and common terminal",
                                      module=mid, missing=sorted(required-set(usages)), extra=sorted(set(usages)-required)))
        domain_map = {d["id"]: d for d in plan["voltage_domains"]}
        errors.extend(validate_machine_selection(module, pool, domain_map, planning_validation_mode=planning_validation_mode))
        common_authorizations = {}
        for alias, spec in module["common_devices"].items():
            if spec["device"]["type"] != "connector":
                continue
            authorization = authorize_common_open_pins(spec,
                [usage for key,usage in usages.items() if key[0] == alias], module["ports"], source_index)
            common_authorizations[alias] = authorization
            errors.extend(_diagnostic(d["rule"], d["message"], module=mid, device=alias,
                **{key: value for key,value in d.items() if key not in {"rule", "message", "module", "device"}})
                for d in authorization["diagnostics"])
        if common_authorizations:
            errors.extend(_diagnostic(d["rule"], d["message"], module=mid,
                **{key: value for key,value in d.items() if key not in {"rule", "message", "module"}})
                for d in check_common_port_coverage(list(common_authorizations.values()), module["ports"]))
        for alias, item in devices.items():
            for pin in item["pins"]:
                use = usages.get((alias, pin["id"]), {})
                allowed = use.get("unconnected", False)
                permission = (pool[alias]["allow_nc"].get(pin["id"], False) if alias in pool
                    else common_authorizations.get(alias, {}).get("allow_nc", {}).get(pin["id"], False))
                if allowed and not permission and planning_validation_mode != CANDIDATE_MODE:
                    errors.append(_diagnostic("UNSUPPORTED_OPEN_PIN", "planner cannot authorize an open pin without its confirmed device contract", module=mid, device=alias, pin=pin["id"]))
                pin["unconnected"] = allowed
        for port_id, port in module["ports"].items():
            refs(port, f"port {mid}.{port_id}")
            if not port_domain_selection_valid(port, domain_map):
                errors.append(_diagnostic("PORT_DOMAIN_SELECTION", "port domain/binding must both be null or name a declared domain with rail/logic semantics", module=mid, port=port_id, allowed_domains=sorted(domain_map)))
        supply_symbols = {}
        declared_domains = sorted(d["id"] for d in plan["voltage_domains"])
        for binding in module.get("supply_bindings", []):
            refs(binding, f"supply binding {mid}.{binding['device']}.{binding['symbol']}")
            key = (binding["device"], binding["symbol"])
            canonical = pool.get(binding["device"], {}).get("canonical_supply_symbols", {})
            if binding["symbol"] not in canonical or binding["pin"] not in canonical.get(binding["symbol"], {}).get("pin_numbers", []):
                errors.append(_diagnostic("SUPPLY_SYMBOL_CONTRACT", "use the exact authenticated manufacturer supply symbol and one of its allowed physical pins; a rail id or grounded TEST pin is not a new supply symbol",
                    module=mid, binding=binding, canonical_supply_symbols=deepcopy(canonical),
                    **({"repair_scope": "supply_bindings only; preserve pin_usage, declared domains, ports and physical SCL connection obligations",
                        "repair_hint": load_prompt('semantic_supply_symbol_repair.md'),
                        "qualification_limit": "Omitting an optional annotation does not establish relative voltage, logic supply/reference identity, NC permission or electrical readiness; unresolved source obligations remain."}
                       if not canonical else {})))
            if binding["device"] not in pool or binding["device"] not in devices:
                errors.append(_diagnostic("INVALID_SUPPLY_DEVICE", "supply symbol must belong to a core instance assigned to this module",
                    module=mid, binding=binding, allowed_devices=sorted(alias for alias in devices if alias in pool)))
            elif (binding["device"], binding["pin"]) not in required:
                errors.append(_diagnostic("INVALID_SUPPLY_PIN", "supply binding names a nonexistent physical pin of this core instance",
                    module=mid, binding=binding, allowed_pins=[p["id"] for p in devices[binding["device"]]["pins"]]))
            if binding["domain"] not in declared_domains:
                errors.append(_diagnostic("UNKNOWN_SUPPLY_DOMAIN", "supply binding must reference a declared voltage domain",
                    module=mid, binding=binding, allowed_domains=declared_domains))
            if key in supply_symbols:
                errors.append(_diagnostic("DUPLICATE_SUPPLY_SYMBOL", "one device supply symbol must have one canonical physical-pin mapping; keep the datasheet-supported supply pin. A TEST or mode pin connected to that rail belongs in SCL, not another supply-symbol definition",
                    module=mid, binding=binding, existing_binding=deepcopy(supply_symbols[key]),
                    repair_scope="supply_bindings only; preserve intended pin connectivity in SCL"))
            else:
                supply_symbols[key] = binding
        modules[mid] = {"devices": devices, "ports": {pid: {"width": p["width"], "external": p["external"]} for pid, p in module["ports"].items()}}
    counts = Counter(assigned)
    if set(counts) != set(pool) or any(n != 1 for n in counts.values()):
        errors.append(_diagnostic("CORE_INVENTORY_MISMATCH", "every fixed core instance must occur exactly once",
                                  missing=sorted(set(pool)-set(counts)), duplicate=sorted(k for k,n in counts.items() if n>1)))
    if common_count > MAX_COMMON_DEVICES:
        errors.append(_diagnostic("RESOURCE_LIMIT", "too many common devices"))
    domain_ids = set()
    for domain in plan["voltage_domains"]:
        refs(domain, f"domain {domain['id']}")
        if domain["id"] in domain_ids:
            errors.append(_diagnostic("DUPLICATE_DOMAIN", f"duplicate planned domain {domain['id']}"))
        domain_ids.add(domain["id"])
    context = {"schema_version": "scl.connectivity.v1", "revision": f"{board_id}:{_digest(plan)}", "modules": modules}
    if static_environment is not None:
        from validation_platform.static_voltages import validate_environment
        try:
            context["static_environment"] = validate_environment(static_environment, requirements=[
                row["value"] for key, row in source_index.items() if key.startswith("requirement:") and "value" in row])
        except (ValueError, TypeError) as exc:
            errors.append(_diagnostic("STATIC_ENVIRONMENT_INVALID", str(exc)))
    for module in plan["modules"]:
        for pid, port in module["ports"].items():
            if not external_source_port_valid(port, static_environment):
                errors.append(_diagnostic("STATIC_SOURCE_PORT", "external source terminal must be a scalar actual rail port of a frozen source", module=module["id"], port=pid))
    try:
        domain_source, _ = materialize_voltage_domains(plan, board_id)
    except (ValueError, TypeError) as exc:
        errors.append(_diagnostic("INVALID_FROZEN_VOLTAGE_DOMAINS", str(exc)))
        domain_source = None
    checked = compile_scl([domain_source] if domain_source else [], context)
    errors.extend(d for d in checked.diagnostics if d["code"] in {
        "INVALID_CONTEXT", "RESOURCE_LIMIT", "INVALID_EXPORT", "INVALID_DOMAIN",
        "DOMAIN_DEFINITION_CONFLICT", "UNKNOWN_REFERENCE_DOMAIN", "DOMAIN_REFERENCE_CYCLE"})
    if not any(m["devices"] for m in modules.values()):
        errors.append(_diagnostic("EMPTY_CIRCUIT", "plan contains no physical devices or common instances"))
    return (None if errors else context), errors

