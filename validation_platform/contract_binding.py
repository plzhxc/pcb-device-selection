"""Bind frozen semantic generation results to independently sourced pin facts.

Only in-memory generation input, manifest and knowledge snapshots are consumed.
No reference case, database, network or model is read. Core roles come from the
knowledge ``contracts.pins`` mapping (a direct pin mapping is also accepted).
Common roles come only from exact logical ports of validated built-in templates;
numeric package pins are never guessed from a/b. Unknown evidence stays unknown.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
import json
from typing import Any

from .electrical_values import normalized_value
from .pin_contracts import SCHEMA, decimal_number, evidence_errors

# Project logical numbering only; does not claim a manufacturer's package order.
TWO_TERMINAL_LOGICAL_NUMBERING = {"version": "1.0", "mapping": {"1": "a", "2": "b"}}


def _map(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _rows(value: Any) -> list:
    return value if isinstance(value, list) else []


def supply_pin_numbers(supply: Any) -> list[str]:
    """Read the reviewed symbol map, preserving legacy single-pin contracts."""
    supply = _map(supply)
    pins = supply.get("pin_numbers", [supply.get("pin")])
    if (not isinstance(pins, list) or not pins or len(pins) > 1024
            or not all(isinstance(pin, str) and pin for pin in pins) or len(pins) != len(set(pins))
            or supply.get("pin") is not None and supply["pin"] not in pins):
        return []
    return list(pins)


def check_declared_port_domains(module_plan: Mapping, manifest: Mapping) -> list[dict]:
    """Check rail ports against actual networks; logic domains are only context.

    The plan and its input references must already be authenticated by the
    caller. This validates declared connectivity, never interprets port names
    and never concludes logic-level compatibility from nominal supply voltage.
    """
    mid = module_plan.get("id")
    domains = {d.get("id"): d for d in _rows(manifest.get("voltage_domains")) if isinstance(d, Mapping)}
    networks = [_map(network) for network in _rows(manifest.get("networks"))]
    diagnostics = []
    def add(rule: str, status: str, message: str, port: str, **details: Any) -> None:
        diagnostics.append({"rule": rule, "status": status, "message": message, "module": mid, "port": port, **details})
    for port_id, raw in _map(module_plan.get("ports")).items():
        port = _map(raw)
        # Older interfaces did not declare domain intent. They cannot acquire a
        # domain assertion merely from a supply-looking name or purpose string.
        if "domain" not in port and "domain_binding" not in port:
            continue
        did, binding = port.get("domain"), port.get("domain_binding")
        if binding not in {None, "rail", "logic"}:
            add("PORT_DOMAIN_BINDING", "fail", "Unsupported domain binding kind", port_id)
            continue
        if did is None or binding is None:
            add("PORT_DOMAIN_UNKNOWN", "unresolved", "Port voltage-domain intent is explicitly unknown", port_id)
            continue
        if not isinstance(did, str) or did not in domains:
            add("PORT_DOMAIN_UNKNOWN", "fail", "Port refers to an undeclared voltage domain", port_id, domain=did)
            continue
        if binding == "logic":
            add("PORT_LOGIC_DOMAIN_CONTEXT", "not_applicable", "A logic domain is nominal supply context, not a rail-wire equality or logic-level proof", port_id, domain=did)
            continue
        width = port.get("width")
        if type(width) is not int or not 1 <= width <= 1024:
            add("PORT_DOMAIN_WIDTH", "fail", "Rail port requires a bounded explicit width", port_id)
            continue
        rail_nets = {network.get("id") for network in networks if any(
            member.get("kind") == "voltage_domain" and member.get("id") == did
            for member in _rows(network.get("members")) if isinstance(member, Mapping))}
        for bit in range(width):
            port_nets = {network.get("id") for network in networks if any(
                member.get("kind") == "port" and member.get("module") == mid and member.get("port") == port_id
                and member.get("bit") == bit for member in _rows(network.get("members")) if isinstance(member, Mapping))}
            if len(rail_nets) != 1 or None in rail_nets or rail_nets != port_nets:
                add("PORT_RAIL_DOMAIN_NET", "fail", "External supply/reference endpoint must connect to its declared actual rail net", port_id,
                    bit=bit, domain=did, actual_port_nets=sorted(port_nets, key=str), actual_rail_nets=sorted(rail_nets, key=str))
    return diagnostics


def check_top_port_bindings(plan: Mapping, manifest: Mapping) -> list[dict]:
    """Reject direct top-level rail/logic boundary joins, not local pin bias.

    Callers authenticate the frozen plan and compiler manifest. Only explicit
    declarations and reproduced operations count; names and voltage values do
    not establish port intent. Unknown intent and indirect paths stay outside
    this narrow check. This is a frozen boundary-contract mismatch, not a claim
    that constant logic levels are electrically impossible. An owner may declare
    an intentional constant enable/strap feed as rail-bound; physical pin facts
    still require independent checking. The diagnostic is shared by construction
    and readback, never authority to choose or change a connection.
    """
    if manifest.get("schema_version") != "scl.connectivity.v1" or manifest.get("status") != "compiled":
        return []
    modules = {row["id"]: row for row in _rows(plan.get("modules"))
               if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
    domains = {row["id"] for row in _rows(manifest.get("voltage_domains"))
               if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
    sources = {row.get("name") for row in _rows(manifest.get("sources"))
               if isinstance(row, Mapping) and isinstance(row.get("name"), str) and row.get("module") is None}
    diagnostics = []
    for operation in _rows(manifest.get("operations")):
        operation = _map(operation)
        endpoints = _rows(operation.get("endpoints"))
        if (operation.get("kind") != "connect" or operation.get("module") is not None
                or not isinstance(operation.get("source"), str) or operation["source"] not in sources
                or type(operation.get("line")) is not int or operation["line"] < 1 or len(endpoints) != 2
                or not all(isinstance(endpoint, Mapping) and endpoint.get("kind") == "port"
                    and isinstance(endpoint.get("module"), str) and isinstance(endpoint.get("port"), str) for endpoint in endpoints)
                or endpoints[0].get("module") == endpoints[1].get("module")):
            continue
        ports = []
        for endpoint in endpoints:
            port = _map(_map(modules.get(endpoint.get("module"))).get("ports")).get(endpoint.get("port"))
            bit = endpoint.get("bit")
            if (not isinstance(port, Mapping) or type(bit) is not int
                    or type(port.get("width")) is not int or not 0 < port["width"] <= 1024 or not 0 <= bit < port["width"]
                    or not isinstance(port.get("domain_binding"), str) or port["domain_binding"] not in {"rail", "logic"}
                    or not isinstance(port.get("domain"), str) or port["domain"] not in domains):
                break
            ports.append({"endpoint": deepcopy(dict(endpoint)), "expected": deepcopy(dict(port))})
        if len(ports) != 2 or {row["expected"]["domain_binding"] for row in ports} != {"rail", "logic"}:
            continue
        diagnostics.append({"rule": "PORT_BINDING_KIND_CONFLICT", "code": "PORT_BINDING_KIND_CONFLICT",
            "severity": "error", "status": "fail",
            "message": "A direct top-level connection joins explicit rail and logic module-port contracts; the boundary owner must reconsider incompatible ports, not infer a replacement wire",
            "source": operation["source"], "line": operation.get("line"),
            "operation": deepcopy(dict(operation)), "boundaries": ports})
    return diagnostics


def _fact_rows(record: Mapping) -> dict:
    raw = record.get("contracts")
    if isinstance(raw, Mapping):
        raw = raw.get("pins", raw)
        return {str(k): deepcopy(dict(v)) for k, v in _map(raw).items() if isinstance(v, Mapping)}
    return {str(v["pin_number"]): deepcopy(dict(v)) for v in _rows(raw)
            if isinstance(v, Mapping) and isinstance(v.get("pin_number"), str)}


def _authenticated_references(record: Mapping, part: str, references: Any, fields: list[str]) -> bool:
    """Compare stored field quotes to this snapshot's actual primary sources."""
    from datasheet_automation.knowledge.evidence import (
        authenticate_source_quote, primary_document_sha256, primary_source_registry)
    if evidence_errors(references) or not all(isinstance(field, str) and field for field in fields):
        return False
    bundle = {**dict(record), "supplier_part": part}
    current = primary_source_registry(bundle)
    contract = _map(record.get("contracts")); stored = _map(contract.get("source_registry"))
    version = _map(contract.get("provenance")).get("source_authentication_version")
    # Existing v2 contracts predate the optional registry. They retain their
    # documented original-PDF metadata boundary; HTML/image never use it.
    if not current and not stored and version == 2:
        sha = primary_document_sha256(bundle)
        return bool(sha) and all(e.get("source_kind") == "datasheet" and e.get("source_authentication_version") == 2 and
            e.get("provenance") == "original_pdf_native" and e.get("document_sha256") == sha for e in references)
    for evidence in references:
        source_ref = evidence.get("source_ref")
        if source_ref not in current or stored and stored.get(source_ref) != current[source_ref]:
            return False
        if version == 3 and source_ref not in stored:
            return False
        candidates = [chunk for chunk in _rows(bundle.get("evidence")) if _map(chunk).get("source_ref") == source_ref]
        approved = [authenticate_source_quote(bundle, chunk, evidence.get("quote"), fields=fields) for chunk in candidates]
        if not any(proof and all(evidence.get(key) == proof.get(key) for key in
                ("source_kind", "source_ref", "document_sha256", "provenance", "source_authentication_version")) and
                (proof["source_kind"] != "manufacturer_datasheet_revision" or all(evidence.get(key) == proof.get(key)
                    for key in ("source_selection_sha256", "applicability_receipt_sha256",
                                "canonical_primary_document_sha256", "page", "locator"))) and
                (proof.get("provenance") not in {"reviewed_manufacturer_image_fields", "reviewed_original_pdf_transcription"} or all(
                    evidence.get(key) == value for key, value in proof.items() if key != "source_url")) and
                (proof["source_kind"] not in {"datasheet", "manufacturer_application_note"} or evidence.get("page") == proof.get("page")) and
                (proof["source_kind"] in {"datasheet", "manufacturer_application_note"} or evidence.get("locator") == proof.get("locator")) for proof in approved):
            return False
    return True


def bind_generation_contracts(generation: Mapping[str, Any], knowledge: Mapping[str, Any], *,
                              board_input: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a check_pin_contracts-compatible manifest with binding diagnostics.

``generation`` is generate_semantic_board's result, containing plan, context,
compilation (netlist/manifest), source_index. ``board_input`` contains the actual
request and requirements used for this board; its canonical SHA256 is recorded.
Plan pin_usage may provide usage/capability, selected_mode, domain and resources.
Absent choices are never inferred from net names, pin names or reference wiring.
"""
    generation, knowledge = _map(generation), _map(knowledge)
    compilation = _map(generation.get("compilation"))
    manifest, netlist = _map(compilation.get("manifest")), _map(compilation.get("netlist"))
    context, plan = _map(generation.get("context")), _map(generation.get("plan"))
    board_input = _map(board_input)
    source_index = _map(generation.get("source_index"))
    diagnostics: list[dict] = []

    def add(code: str, message: str, status: str = "unresolved", **detail: Any) -> None:
        diagnostics.append({"rule": code, "status": status, "message": message, **detail})

    result = {"schema": SCHEMA, "components": {}, "domains": {}, "constraints": [],
              "source_bundles": {},
              "input_sha256": _hash(board_input) if board_input else None,
              "authenticated_identity_anchors": {},
              "binding_diagnostics": diagnostics,
              "binding_scope": "physical core pins and explicitly mapped logical common terminals"}
    if compilation.get("ok") is not True or manifest.get("status") != "compiled" or not netlist:
        add("BINDING_COMPILATION", "Only a successful nonempty compilation can supply contracts")
        return result
    if manifest.get("context_sha256") != _hash(context) or manifest.get("netlist_sha256") != _hash(netlist):
        add("BINDING_DIGEST", "Context or netlist differs from its compilation digest", "fail")
        return result
    from .fixed_inventory import authenticate_fixed_inventory
    fixed_inventory = authenticate_fixed_inventory(generation, knowledge, board_input=board_input)
    diagnostics.extend(fixed_inventory["diagnostics"])
    result["authenticated_identity_anchors"] = deepcopy(fixed_inventory["authenticated_identity_anchors"])
    result["fixed_inventory_authentication"] = {key: deepcopy(fixed_inventory[key])
        for key in ("schema", "status", "fixed_inventory_sha256")}
    fixed_identities = fixed_inventory["components"]
    from .operating_selections import (resolve_operating_selections, bind_operating_conditions,
                                      make_bound_operating_context)
    operating = {"conditions_by_alias": {}}
    if any(_map(m).get("operating_selections") for m in _rows(plan.get("modules"))) or generation.get("operating_selections"):
        operating = resolve_operating_selections(plan, _map(generation.get("fixed_inventory")), knowledge,
            board_input=board_input, source_index=source_index)
        if (fixed_inventory["status"] != "authenticated" or operating["diagnostics"] or
                operating != generation.get("operating_selections") or
                manifest.get("operating_selections_sha256") != _hash(operating)):
            add("BINDING_OPERATING_SELECTION", "Internal selections must be rebuilt from the exact final plan, input, source and authenticated fixed inventory", "fail",
                reasons=operating.get("diagnostics", []))
            operating = {"conditions_by_alias": {}}
        else:
            result["operating_selection_context"] = make_bound_operating_context(generation, knowledge, board_input)
    modules = {m.get("id"): m for m in _rows(plan.get("modules")) if isinstance(m, Mapping)}
    context_modules = _map(context.get("modules"))
    seen_components, seen_instances = set(), set()
    instances = _rows(manifest.get("instances"))
    usages = {}
    for mid, module in modules.items():
        for raw in _rows(module.get("pin_usage")):
            use = _map(raw)
            key = (mid, use.get("device"), use.get("pin"))
            if key in usages:
                add("BINDING_USAGE_DUPLICATE", "Duplicate planned pin usage", "fail", endpoint=list(key))
            usages[key] = use

    def sources(refs: Any) -> list[dict]:
        out = []
        for ref in _rows(refs):
            if not isinstance(ref, str) or ref not in source_index:
                add("BINDING_SOURCE", "Unknown planning source reference", source_ref=ref)
                continue
            indexed = _map(source_index[ref])
            if isinstance(indexed.get("evidence"), list) and not evidence_errors(indexed["evidence"], allow_input=True):
                out.extend(deepcopy(indexed["evidence"]))
                continue
            value = None
            if ref == "request":
                value = board_input.get("request")
            elif ref == "requirements":
                value = board_input.get("requirements")
            elif ref.startswith("requirement:"):
                rid = ref.split(":", 1)[1]
                candidates = [r for i, r in enumerate(_rows(board_input.get("requirements")))
                              if str(r.get("id", i) if isinstance(r, Mapping) else i) == rid]
                if len(candidates) == 1:
                    value = candidates[0]
            if value not in (None, "", []):
                quote = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
                out.append({"source_kind": "input", "source_ref": str(board_input.get("board_id") or board_input.get("case_id") or "generation_input"),
                            "document_sha256": result["input_sha256"], "quote": quote, "locator": ref})
                continue
            if ref.startswith("evidence:"):
                chunk_id = ref.split(":", 1)[1]
                matches = [(part, row, record) for part, record in knowledge.items() if isinstance(record, Mapping)
                           for row in _rows(record.get("evidence")) if isinstance(row, Mapping) and str(row.get("id")) == chunk_id]
                if len(matches) == 1:
                    part, row, record = matches[0]
                    from datasheet_automation.knowledge.evidence import authenticate_source_quote
                    proof = authenticate_source_quote({**dict(record), "supplier_part": part}, row, row.get("content"), fields=[])
                    if proof and not evidence_errors([proof]):
                        out.append({**proof, "chunk_id": row.get("id")})
                        continue
            add("BINDING_SOURCE_LOCATOR", "Source identity alone lacks a claim-level evidence locator", source_ref=ref)
        return out

    network_domains = {}
    external_pins = set()
    external_logic_ports = {}
    external_rail_ports = []
    for raw in _rows(manifest.get("networks")):
        network = _map(raw)
        members = [_map(m) for m in _rows(network.get("members"))]
        for member in members:
            if member.get("kind") == "voltage_domain":
                key = member.get("id")
                if key in network_domains and network_domains[key] != network.get("id"):
                    add("BINDING_DOMAIN_DUPLICATE", "One domain maps to multiple compiled networks", "fail", domain=key)
                network_domains[key] = network.get("id")
        external = any(m.get("kind") == "port" and
                       _map(_map(context_modules.get(m.get("module"))).get("ports")).get(m.get("port"), {}).get("external") is True for m in members)
        if external:
            external_pins.update((m.get("module"), m.get("device"), m.get("pin")) for m in members if m.get("kind") == "pin")
        for member in members:
            if member.get("kind") != "port":
                continue
            declaration = _map(_map(_map(modules.get(member.get("module"))).get("ports")).get(member.get("port")))
            if declaration.get("external") is not True:
                continue
            endpoint = {"module": member.get("module"), "port": member.get("port"), "bit": member.get("bit"),
                        "domain": declaration.get("domain"), "net": network.get("id")}
            if "external_source" in declaration:
                endpoint["external_source"] = deepcopy(declaration["external_source"])
            if declaration.get("domain_binding") == "rail":
                external_rail_ports.append(endpoint)
            elif declaration.get("domain_binding") == "logic":
                for pin_member in members:
                    if pin_member.get("kind") == "pin":
                        key = (pin_member.get("module"), pin_member.get("device"), pin_member.get("pin"))
                        external_logic_ports.setdefault(key, []).append(endpoint)
    compiled_domains = {d.get("id"): d for d in _rows(manifest.get("voltage_domains")) if isinstance(d, Mapping)}
    for raw in _rows(plan.get("voltage_domains")):
        domain = _map(raw); did = domain.get("id")
        compiled = _map(compiled_domains.get(did))
        from .static_voltages import domain_definition
        try:
            definition = domain_definition(domain)
        except (TypeError, ValueError):
            definition = None
        if not did or definition is None or compiled != definition:
            add("BINDING_DOMAIN_PLAN", "Compiled voltage domain differs from the frozen plan", "fail", domain=did)
            continue
        result["domains"][did] = {"net": network_domains.get(did), "voltage_v": domain.get("voltage_v"),
                                  "reference": domain.get("reference"), "evidence": sources(domain.get("source_refs"))}
        if "profile_ref" in domain:
            result["domains"][did]["profile_ref"] = domain["profile_ref"]
    if set(compiled_domains) != set(result["domains"]):
        add("BINDING_DOMAIN_COVERAGE", "Planned and compiled domain inventories differ", "fail")

    from .static_voltages import bind_environment
    from .conditional_pin_rules import has_terminal_voltage_predicate
    if any(_map(_map(_map(r).get("contracts")).get("device_electrical")).get("schema") == "pcbagent.regulator_device_electrical.v1"
           or any(has_terminal_voltage_predicate(_map(pin).get("conditional_rules"))
                  for pin in _map(_map(_map(r).get("contracts")).get("pins")).values()) for r in knowledge.values()):
        # Receiver roots are supplied by this independent binder, never by a
        # transfer certificate. Retain the original selected plan and compiled
        # port/pin incidence so re-signing a derived view cannot change them.
        payload = {"context": deepcopy(dict(context)), "plan": deepcopy(dict(plan)),
                   "compilation": deepcopy(dict(compilation))}
        result["generation_binding_sha256"] = _hash(payload)
        result["generation_binding_snapshot"] = payload
        result["regulator_source_records"] = deepcopy(dict(knowledge))
    static_binding, static_errors = bind_environment(board_input, context, manifest, result["domains"], external_rail_ports)
    if board_input.get("static_environment") is not None or static_errors:
        result["static_voltage_binding"] = static_binding or {}
    for error in static_errors:
        add("BINDING_STATIC_ENVIRONMENT", error, "fail")

    registry = None
    extra_common = {}
    common_authorizations = {}
    def common_facts(device: Mapping, instance: Mapping, specification: Mapping) -> dict:
        nonlocal registry
        if instance.get("pin_space") != "logical":
            add("COMMON_PHYSICAL_BINDING", "Common physical pins require an explicit sourced package binding")
            return {}
        if registry is None:
            from netlist_end_to_end.common_devices.registry import TemplateRegistry
            registry = TemplateRegistry.load_builtin()
        definitions = [registry.get(t["template_id"], t["version"]) for t in registry.list_templates() if t["kind"] == "primitive"]
        matches = [d for d in definitions if d.get("device_type") == device.get("type")]
        if len(matches) != 1:
            from .common_terminal_contracts import build_common_terminal_contract
            contract = build_common_terminal_contract(device.get("type"),
                [p.get("id") for p in _rows(device.get("pins")) if isinstance(p, Mapping)],
                terminal_mapping=specification.get("terminal_mapping"), parameters=specification.get("parameters"),
                source_evidence=sources(specification.get("source_refs")))
            diagnostics.extend({**d, "component": instance.get("component")} for d in contract["diagnostics"])
            if device.get("type") == "connector":
                from .common_terminal_contracts import authorize_common_open_pins, check_common_port_nets
                mid, alias, cid = instance["module"], instance["device"], instance["component"]
                declared_module = _map(modules.get(mid))
                trusted_index = deepcopy(dict(source_index))
                selected_usages = [row for row in _rows(declared_module.get("pin_usage")) if _map(row).get("device") == alias]
                selected_rows = [specification, *selected_usages, *_map(declared_module.get("ports")).values()]
                selected_refs = {ref for row in selected_rows for ref in _rows(_map(row).get("source_refs")) if isinstance(ref, str)}
                for ref in selected_refs:
                    indexed = _map(source_index.get(ref))
                    actual_input = None
                    if ref == "request":
                        actual_input = board_input.get("request")
                    elif ref.startswith("requirement:"):
                        matching = [requirement for requirement in _rows(board_input.get("requirements"))
                                    if _map(requirement).get("id") == ref.split(":", 1)[1]]
                        if len(matching) == 1:
                            actual_input = matching[0]
                    else:
                        continue
                    if actual_input is None or _map(indexed).get("value", actual_input) != actual_input:
                        trusted_index.pop(ref, None)
                        add("COMMON_SOURCE_INPUT_MISMATCH", "Connector source index differs from the actual frozen input", "fail", component=cid, source_ref=ref)
                    else:
                        trusted_index[ref] = {**dict(_map(indexed)), "value": actual_input}
                authorization = authorize_common_open_pins(specification,
                    selected_usages,
                    _map(declared_module.get("ports")), trusted_index)
                diagnostics.extend({**d, "component": cid} for d in authorization["diagnostics"])
                diagnostics.extend(check_common_port_nets(authorization, module=mid, device=alias, component=cid,
                    netlist=netlist, networks=_rows(manifest.get("networks"))))
                common_authorizations.setdefault(mid, []).append(authorization)
                contract["open_pin_authorization"] = authorization
                for pid, fact in contract["pins"].items():
                    if fact.get("allow_nc") is True and not authorization["allow_nc"].get(pid):
                        fact["allow_nc"] = None
            extra_common[instance.get("component")] = contract
            return contract["pins"]
        definition = matches[0]
        ports = definition["ports"]
        vector = any("width_parameter" in p or p.get("width", 1) != 1 for p in ports)
        scalar_ab = not vector and {p["id"] for p in ports} == {"a", "b"}
        actual = {p.get("id") for p in _rows(device.get("pins")) if isinstance(p, Mapping)}
        if (actual == {"1", "2"} and scalar_ab
                and specification.get("terminal_mapping") is None and instance.get("logical_pin_mapping") is None):
            add("COMMON_NUMBERING_DECLARATION", "Numeric logical terminals lack the explicit versioned 1=a, 2=b declaration")
            return {}
        from netlist_end_to_end.common_devices.terminal_mapping import primitive_terminal_mapping
        logical_mapping, mapping_errors = primitive_terminal_mapping(definition,
            [p.get("id") for p in _rows(device.get("pins")) if isinstance(p, Mapping)],
            specification.get("terminal_mapping"), numeric_ab=instance.get("logical_pin_mapping"),
            parameters=specification.get("parameters"))
        if mapping_errors:
            for row in mapping_errors:
                add(row["code"], row["message"], "fail", component=instance.get("component"),
                    **{k:v for k,v in row.items() if k not in {"code", "message"}})
            return {}
        if actual == {"1", "2"} and scalar_ab:
            if (instance.get("logical_pin_mapping") != TWO_TERMINAL_LOGICAL_NUMBERING["mapping"]
                    or logical_mapping != instance.get("logical_pin_mapping")):
                add("COMMON_NUMBERING_DECLARATION", "Numeric logical terminals must match the explicit versioned 1=a, 2=b convention", "fail")
                return {}
        template_id, version = definition["template_id"], definition["version"]
        evidence = [{"source_kind": "template", "source_ref": f"{template_id}@{version}",
                     "document_sha256": registry.definition_sha256(template_id, version),
                     "quote": json.dumps(ports, ensure_ascii=False, sort_keys=True), "locator": "ports"}]
        if actual == {"1", "2"} and scalar_ab:
            evidence.append({"source_kind": "template", "source_ref": "pcbagent.common_two_terminal_logical_numbering@1.0",
                             "document_sha256": _hash(TWO_TERMINAL_LOGICAL_NUMBERING),
                             "quote": json.dumps(TWO_TERMINAL_LOGICAL_NUMBERING, sort_keys=True),
                             "locator": "validation_platform.contract_binding.TWO_TERMINAL_LOGICAL_NUMBERING"})
        required = set(definition["parameters_schema"].get("required", []))
        supplied_parameters = _map(specification.get("parameters"))
        required -= set(supplied_parameters)
        parameter_values = deepcopy(dict(supplied_parameters))
        value_spec = definition.get("value_spec", {})
        value_dimension, nominal = normalized_value(device.get("value"))
        if value_spec and value_dimension == normalized_value("1" + value_spec.get("unit", ""))[0] and value_dimension not in {"missing", "text"}:
            required.discard(value_spec.get("parameter"))
            parameter = value_spec.get("parameter")
            if parameter in parameter_values and decimal_number(parameter_values[parameter]) != nominal:
                add("COMMON_VALUE_PARAMETER_CONFLICT", "Scalar value conflicts with explicit template parameter", "fail", template=template_id)
            parameter_values.setdefault(parameter, nominal)
            param = definition["parameters_schema"].get("properties", {}).get(value_spec.get("parameter"), {})
            if ("minimum" in param and nominal < param["minimum"]) or ("exclusiveMinimum" in param and nominal <= param["exclusiveMinimum"]):
                add("COMMON_VALUE_RANGE", "Nominal value violates the template parameter range", "fail", template=template_id)
        if required:
            add("COMMON_PARAMETERS", "Required template parameters are not supplied by scalar value", template=template_id, parameters=sorted(required))
        else:
            from jsonschema.validators import validator_for
            for error in validator_for(definition["parameters_schema"])(definition["parameters_schema"]).iter_errors(parameter_values):
                add("COMMON_PARAMETER_SCHEMA", "Template parameter constraint failed: " + error.message, "fail", template=template_id)
        passive_ports = {p["id"] for p in ports if p.get("direction") == "passive"}
        if vector:
            from netlist_end_to_end.common_devices.compiler import _endpoints
            passive_ports = {terminal for port, _, terminal in _endpoints(definition, supplied_parameters)
                             if port in passive_ports}
        if device.get("type") == "capacitor":
            extra_common[instance.get("component")] = {
                "voltage_obligation": {
                    "kind": "capacitor_terminal_difference",
                    "parameters": deepcopy(dict(supplied_parameters)),
                    "terminal_mapping": deepcopy(logical_mapping),
                    "template_evidence": deepcopy(evidence),
                }}
        return {pin_id: {"semantic_role": "passive_terminal", "electrical_type": "passive",
                           "capabilities": ["passive"], "modes": ["passive"], "resources": [],
                           "required_connection": True, "allow_nc": False, "evidence": deepcopy(evidence),
                           **({"logical_terminal": port_id} if vector else {})}
                for pin_id, port_id in logical_mapping.items() if port_id in passive_ports}

    for raw in instances:
        instance = _map(raw)
        mid, alias, cid = instance.get("module"), instance.get("device"), instance.get("component")
        if not all(isinstance(k, str) and k for k in (mid, alias, cid)) or cid in seen_components or (mid, alias) in seen_instances:
            add("BINDING_INSTANCE", "Manifest instance must have unique module/device and component identities", "fail")
            continue
        seen_components.add(cid); seen_instances.add((mid, alias))
        device = _map(_map(_map(context_modules.get(mid)).get("devices")).get(alias))
        actual = _map(netlist.get(cid))
        if not device or not actual or any(device.get(k) != actual.get(k) for k in ("id", "type", "name", "value", "is_abstract")):
            add("BINDING_DEVICE", "Manifest identity differs from the frozen context or netlist", "fail", component=cid)
            continue
        is_functional = device.get("type") == "module"
        identity = _map(fixed_identities.get(cid))
        is_physical = identity.get("inventory_kind") == "physical_device"
        part = identity.get("supplier_part") if identity else device.get("id")
        is_fixed_source = device.get("type") == "ic" or bool(identity)
        record = {}
        facts = {}
        contract_provenance = None
        if is_fixed_source:
            from datasheet_automation.knowledge.evidence import primary_document_sha256
            record = _map(knowledge.get(part)); catalog = _map(record.get("device"))
            device_contract = _map(record.get("contracts"))
            contract_provenance = deepcopy(device_contract.get("provenance"))
            source_hash = primary_document_sha256(record)
            source_authenticated = (_map(contract_provenance).get("source_authentication_version") in {2, 3} and
                                    device_contract.get("document_sha256") == source_hash and bool(source_hash) and
                                    device_contract.get("supplier_part") == part and
                                    (_map(contract_provenance).get("source_authentication_version") != 3 or bool(device_contract.get("source_registry"))))
            source_pins = _rows(record.get("pins"))
            numbers = [p.get("pin_number") for p in source_pins if isinstance(p, Mapping)]
            context_numbers = [p.get("id") for p in _rows(device.get("pins")) if isinstance(p, Mapping)]
            if catalog.get("supplier_part") != part or type(catalog.get("pin_count")) is not int or catalog["pin_count"] != len(numbers) or len(set(numbers)) != len(numbers) or set(numbers) != set(context_numbers):
                add("CORE_PHYSICAL_IDENTITY", "Catalog identity and complete physical pin inventory are not confirmed", component=cid)
            elif not source_authenticated:
                add("CORE_SOURCE_AUTHENTICATION", "Core contracts require version 2 PDF or version 3 registered primary-source authentication", component=cid)
            else:
                from .certificate_facts import frozen_source_bundle
                result["source_bundles"].setdefault(part, frozen_source_bundle(record, part))
                facts = _fact_rows(record)
                for pid, fact in list(facts.items()):
                    if fact.get("supplier_part") not in (None, part):
                        add("CORE_FACT_IDENTITY", "Pin contract belongs to a different part", "fail", component=cid, pin=pid)
                        facts.pop(pid)
                        continue
                    evidence = fact.get("evidence")
                    factual_fields = [field for field in ("semantic_role", "electrical_type", "capabilities", "modes", "required_connection", "allow_nc", "voltage_min_v", "voltage_max_v", "resources", "gpio_remap_safe", "logic_levels", "application_interface_profile") if fact.get(field) is not None]
                    supported = _authenticated_references(record, part, evidence, []) and all(_authenticated_references(record, part,
                        _map(fact.get("field_evidence")).get(field, evidence), [field]) for field in factual_fields)
                    # A rule-only pin may have no unconditional fact or base
                    # evidence. Authenticate its rules separately below; never
                    # manufacture a role or unconditional connection policy.
                    if (factual_fields and not supported) or (
                            not factual_fields and not _rows(fact.get("conditional_rules"))):
                        add("CORE_PIN_SOURCE_AUTHENTICATION", "Pin field evidence does not match the registered original source and permitted claim scope", component=cid, pin=pid)
                        facts.pop(pid)
                        continue
                    # Conditional claims are copied only from the trusted device
                    # contract, never from the generated plan. Review hashes are
                    # checked by the evaluator against the full rule payload.
                    for rule in _rows(fact.get("conditional_rules")):
                        refs = _map(rule).get("evidence")
                        effect = _map(_map(rule).get("effect"))
                        rule_fields = ([k for k in ("allow_nc", "required_connection") if k in effect] if effect.get("kind") == "connection_policy" else
                                       ["logic_levels"] if effect.get("kind") == "logic_levels" else
                                       ["application_interface_profile"] if effect.get("kind") == "application_interface_profile" else
                                       ["voltage_min_v", "voltage_max_v"] if effect.get("kind") == "voltage_range" else
                                       ["electrical_type", "modes"] if effect.get("kind") == "operating_pin_mode" else
                                       ["capabilities", "semantic_role"] if effect.get("kind") == "pin_function_role" else
                                       ["capabilities", "modes"] if effect.get("kind") == "pin_selection_constraint" else
                                       ["required_connection"] if effect.get("kind") == "pin_relation" else
                                       [effect.get("check")] if effect.get("kind") == "not_applicable" else ["unsupported_effect"])
                        if has_terminal_voltage_predicate([rule]):
                            rule_fields.append("terminal_voltage_v")
                        from datasheet_automation.knowledge.evidence import primary_source_registry
                        has_original = (effect.get("kind") != "pin_function_role" or
                                        bool(primary_source_registry({**record, "supplier_part": part})))
                        if not has_original or not _authenticated_references(record, part, refs, rule_fields):
                            add("CORE_CONDITIONAL_SOURCE", "Conditional rule source differs from the authenticated device document", component=cid, pin=pid)
                            fact["conditional_rules"] = []
                            if not factual_fields:
                                facts.pop(pid)
                            break
        elif (is_functional or _map(instance.get("identity")).get("inventory_kind") == "physical_device"
              or alias in _rows(_map(modules.get(mid)).get("core_instances"))):
            add("BINDING_FUNCTIONAL_IDENTITY", "A physical module requires the complete authenticated fixed-inventory chain; generic common contracts cannot substitute", component=cid)
        else:
            specification = _map(_map(_map(modules.get(mid)).get("common_devices")).get(alias))
            facts = common_facts(device, instance, specification)
        bindings = {}
        for pin in _rows(device.get("pins")):
            pid = _map(pin).get("id"); use = usages.get((mid, alias, pid), {})
            if not use:
                add("BINDING_USAGE", "No frozen use for an exact pin", component=cid, pin=pid)
            use_evidence = sources(use.get("source_refs"))
            capability = use.get("capability")
            if capability is None and use.get("usage") in (_map(facts.get(pid)).get("capabilities") or []):
                # Migration of older exact machine tokens only. Descriptive usage
                # prose is never interpreted, compared or mapped by synonyms.
                capability = use.get("usage")
            binding = {"selected_role": use.get("selected_role"), "usage": capability,
                       "usage_explanation": use.get("usage"),
                       "mode": use.get("selected_mode"), "domain": use.get("domain"),
                       "resources": deepcopy(use.get("resources")), "evidence": use_evidence,
                       "unconnected": use.get("unconnected")}
            from .conditional_pin_rules import bind_frozen_input_conditions, rule_condition_fields
            fields = rule_condition_fields(_map(facts.get(pid)).get("conditional_rules"))
            if any(_map(_map(rule).get("effect")).get("kind") in {"logic_levels", "application_interface_profile"}
                   for rule in _rows(_map(facts.get(pid)).get("conditional_rules"))):
                from .logic_interfaces import HOST_FIELDS
                fields.extend("requirements." + field for field in HOST_FIELDS)
            selected = bind_operating_conditions(board_input=board_input, supplier_part=part,
                source_index=source_index, planned=use, fields=fields,
                operating=operating["conditions_by_alias"].get(alias))
            binding.update(selected["conditions"])
            if selected["planned_conditions"]:
                binding["planned_conditions"] = selected["planned_conditions"]
            diagnostics.extend({**row, "component": cid, "pin": pid} for row in selected["diagnostics"])
            if not use_evidence:
                add("BINDING_USAGE_EVIDENCE", "Planned use has no attributable source evidence", component=cid, pin=pid)
            if not is_fixed_source and not is_functional and pid in facts and instance.get("pin_space") == "logical":
                # Passive is a fixed template fact, independent of proposed wiring.
                extra = extra_common.get(cid, {})
                if pid in extra.get("bindings", {}):
                    fixed = extra["bindings"][pid]
                    role = facts[pid]["semantic_role"]
                    terminal = facts[pid].get("logical_terminal")
                    if binding["selected_role"] in {role, terminal} or (role == "passive_terminal" and binding["selected_role"] == "passive"):
                        binding.update(deepcopy(fixed))
                elif binding["selected_role"] in {None, "passive", "passive_terminal"}:
                    binding.update(selected_role="passive_terminal", usage="passive", mode="passive", resources=[])
            if (mid, alias, pid) in external_pins:
                binding["external_endpoint"] = True
            if (mid, alias, pid) in external_logic_ports:
                binding["external_logic_ports"] = deepcopy(external_logic_ports[(mid, alias, pid)])
                binding["external_rail_ports"] = deepcopy(external_rail_ports)
            bindings[pid] = binding
        supplies = {}
        for raw_supply in _rows(_map(modules.get(mid)).get("supply_bindings")):
            supply = _map(raw_supply)
            if supply.get("device") != alias:
                continue
            symbol = supply.get("symbol")
            if not isinstance(symbol, str) or not symbol or symbol in supplies:
                add("BINDING_SUPPLY_SYMBOL", "Supply symbol must be explicit and unique per component", "fail", component=cid)
                continue
            known_supply = _map(_map(_map(record.get("contracts")).get("supply_pins")).get(symbol)) if is_fixed_source else {}
            supplied_refs = sources(supply.get("source_refs"))
            known_refs = known_supply.get("evidence")
            if supply.get("pin") not in supply_pin_numbers(known_supply) or not _authenticated_references(record, part, known_refs, ["pin_number", "pin_name"]):
                add("BINDING_SUPPLY_FACT", "Supply symbol to physical-pin relation lacks matching authenticated device facts", component=cid, symbol=symbol)
                continue
            supplies[symbol] = {"pin": supply.get("pin"), "domain": supply.get("domain"),
                                "identity_evidence": deepcopy(known_refs), "binding_evidence": deepcopy(supplied_refs),
                                "evidence": [*deepcopy(known_refs), *supplied_refs] if supplied_refs else []}
        result["components"][cid] = {"supplier_part": part, "component_type": device.get("type"),
                                     "pin_space": instance.get("pin_space"), "pins": facts, "bindings": bindings, "supplies": supplies,
                                     "logical_pin_mapping": deepcopy(instance.get("logical_pin_mapping")),
                                     "source_provenance": contract_provenance,
                                     "source_registry": deepcopy(_map(record.get("contracts")).get("source_registry")) if is_fixed_source else None,
                                     "document_sha256": _map(record.get("contracts")).get("document_sha256") if is_fixed_source else None,
                                     "fixed_identity": deepcopy(dict(identity)) if identity else None,
                                     "logical_terminal_contract": {k: v for k, v in extra_common.get(cid, {}).items() if k not in {"pins", "bindings", "diagnostics"}},
                                     "module": mid, "device": alias}
        if alias in operating["conditions_by_alias"]:
            result["components"][cid]["operating_selection_alias"] = alias
        from .device_electrical import SUPPORTED_TYPES, bind_device_electrical
        from .analog_application import is_receiver_rc_profile
        regulator_profile = _map(_map(record.get("contracts")).get("device_electrical")).get("schema") == "pcbagent.regulator_device_electrical.v1"
        receiver_rc_profile = is_receiver_rc_profile(_map(record.get("contracts")).get("device_electrical"))
        if is_fixed_source and (device.get("type") in SUPPORTED_TYPES or regulator_profile or receiver_rc_profile):
            result["components"][cid]["device_electrical"] = bind_device_electrical(
                record, part, board_input=board_input, source_index=source_index)
            if alias in operating["conditions_by_alias"]:
                result["components"][cid]["device_electrical"]["operating_selection_alias"] = alias
            diagnostics.extend({"component": cid, **row} for row in
                result["components"][cid]["device_electrical"]["binding_diagnostics"])
            if regulator_profile:
                result["regulator_plan_snapshot"] = {"sha256": _hash(plan), "payload": deepcopy(dict(plan))}
        voltage_obligation = result["components"][cid]["logical_terminal_contract"].get("voltage_obligation")
        if isinstance(voltage_obligation, dict):
            # This is an abstract procurement constraint selected in the plan,
            # not a manufacturer rating inferred from a source quote.
            selection = deepcopy(dict(_map(_map(_map(modules.get(mid)).get("common_devices")).get(alias))))
            voltage_obligation["selection"] = {
                "kind": "frozen_abstract_design_selection", "plan_sha256": _hash(plan),
                "locator": f"modules[{mid}].common_devices[{alias}]",
                "specification": selection, "specification_sha256": _hash(selection),
            }
    if seen_components != set(netlist):
        add("BINDING_INSTANCE_COVERAGE", "Manifest does not cover every generated component", "fail")
    expected_instances = {(mid, alias) for mid, module in context_modules.items() for alias in _map(_map(module).get("devices"))}
    if seen_instances != expected_instances:
        add("BINDING_CONTEXT_COVERAGE", "Manifest does not cover every frozen context instance", "fail")
    from .common_terminal_contracts import check_common_port_coverage
    for mid, authorizations in common_authorizations.items():
        diagnostics.extend({**d, "module": mid} for d in check_common_port_coverage(authorizations, _map(_map(modules.get(mid)).get("ports"))))
    for module in modules.values():
        diagnostics.extend(check_declared_port_domains(module, manifest))
    diagnostics.extend(check_top_port_bindings(plan, manifest))
    from .planning_obligations import bind_obligations
    obligation_binding = bind_obligations(generation, knowledge, board_input, result)
    if obligation_binding["status"] != "absent":
        result["architecture_obligations"] = obligation_binding
    return result


__all__ = ["bind_generation_contracts", "supply_pin_numbers", "check_declared_port_domains", "check_top_port_bindings"]
