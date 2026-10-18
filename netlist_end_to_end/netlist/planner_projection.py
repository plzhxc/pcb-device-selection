"""Closed planner write sets and replayable host-owned field projection."""
from copy import deepcopy
from collections.abc import Mapping
from jsonschema import Draft202012Validator
from netlist_end_to_end.netlist.semantic_contracts import MODULE, DOMAIN, PORT, STRINGS, STRING, obj


INVENTORY_REPAIR_SCHEMA = obj({"assignments": {"type": "array", "maxItems": 128,
    "items": obj({"core_instance": STRING, "module_id": STRING})}, "unresolved": STRINGS})

LOCAL = ("common_devices", "pin_usage", "supply_bindings", "operating_selections")
BOUNDARY_REQUEST = obj({"reason": STRING, "source_refs": STRINGS,
    "ports": {"type": "object", "additionalProperties": PORT},
    "voltage_domains": {"type": "array", "items": DOMAIN}},
    ["reason", "source_refs"])
PERIPHERAL = obj({"schema": {"const": "pcbagent.peripheral_choices.v1"},
    **{key: deepcopy(MODULE["properties"][key]) for key in LOCAL},
    "assumptions": STRINGS, "unresolved": STRINGS,
    "boundary_request": BOUNDARY_REQUEST},
    ["schema", "common_devices", "pin_usage", "assumptions", "unresolved"])
def boundary_schema():
    from validation_platform.planning_obligations import OBLIGATIONS_SCHEMA
    return obj({"schema": {"const": "pcbagent.architecture_boundary_patch.v1"},
    "module_ports": {"type": "array", "items": obj({"module": STRING,
        "ports": {"type": "object", "additionalProperties": PORT}})},
    "voltage_domains": {"type": "array", "items": DOMAIN},
    "new_assumptions": STRINGS, "new_unresolved": STRINGS,
    "new_obligations": OBLIGATIONS_SCHEMA},
    ["schema", "module_ports", "new_assumptions", "new_unresolved", "new_obligations"])


def checked(schema, value):
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise ValueError("; ".join(error.message for error in errors))


def union(old, new):
    return list(dict.fromkeys([*old, *new]))


def peripheral(frozen, choices):
    """Return canonical proposal and an UNAPPLIED explicit boundary request."""
    checked(PERIPHERAL, choices)
    request = choices.get("boundary_request")
    if request is not None and (not request["source_refs"] or not ({"ports", "voltage_domains"} & set(request))):
        raise ValueError("boundary request needs an explicit field proposal and source references")
    module = deepcopy(frozen["module"])
    for field in LOCAL:
        if field in choices:
            module[field] = deepcopy(choices[field])
    canonical = {"modules": [module],
        "voltage_domains": deepcopy(frozen["voltage_domains"]),
        "assumptions": deepcopy(choices["assumptions"]),
        "unresolved": deepcopy(choices["unresolved"])}
    return canonical, deepcopy(choices.get("boundary_request"))


def boundary(original, prior_unresolved, patch):
    """Only the architecture owner proposes domain/port changes; validate later."""
    checked(boundary_schema(), patch)
    if not isinstance(prior_unresolved, Mapping):
        raise ValueError("prior peripheral statements must have explicit module ownership")
    out = deepcopy(original)
    modules = {module["id"]: module for module in out["modules"]}
    if set(prior_unresolved) != set(modules):
        raise ValueError("prior peripheral ownership incomplete")
    for values in prior_unresolved.values():
        checked(STRINGS, values)
    seen = set()
    for update in patch["module_ports"]:
        mid = update["module"]
        if mid not in modules or mid in seen:
            raise ValueError("unknown or duplicate module update")
        seen.add(mid)
        modules[mid]["ports"] = deepcopy(update["ports"])
    if "voltage_domains" in patch:
        out["voltage_domains"] = deepcopy(patch["voltage_domains"])
    ids = {row["id"] for row in out.get("obligations", [])}
    for row in patch["new_obligations"]:
        if row["id"] in ids:
            raise ValueError("existing or duplicate obligation cannot be replaced")
        ids.add(row["id"])
    out["obligations"] = [*deepcopy(out.get("obligations", [])), *deepcopy(patch["new_obligations"])]
    prior = [text for mid in sorted(prior_unresolved) for text in prior_unresolved[mid]]
    out["unresolved"] = union(out["unresolved"], [*prior, *patch["new_unresolved"]])
    out["assumptions"] = union(out["assumptions"], patch["new_assumptions"])
    return out


def frozen_boundary_observations(architecture, context, manifest, diagnostics):
    """Describe reproduced boundary failures; grant no connection or source fact.

    Callers supply an independently recompiled manifest, never a model manifest.
    A missing physical pin alone is not evidence that its boundary needs changing.
    """
    structural = {"UNCONNECTED_INTERNAL_PORT", "UNBOUND_MODULE_PORT",
                  "UNCONNECTED_REQUIRED_PIN", "POWER_DOMAIN_CONFLICT", "PORT_BINDING_KIND_CONFLICT"}
    if (not manifest.get("instances") or not diagnostics
            or any(row.get("code") not in structural for row in diagnostics)):
        return None
    modules = {row["id"]: row for row in architecture.get("modules", [])}
    actual = context.get("modules", {})
    if set(modules) != set(actual) or any(
            {key: {field: definition.get(field) for field in ("width", "external")}
             for key, definition in module.get("ports", {}).items()} != actual[mid].get("ports")
            for mid, module in modules.items()):
        return None
    from validation_platform.static_voltages import domain_definition
    try:
        domains = {row["id"]: domain_definition(row) for row in architecture.get("voltage_domains", [])}
    except (KeyError, TypeError, ValueError):
        return None
    if domains != {row["id"]: row for row in manifest.get("voltage_domains", [])}:
        return None

    def definition(endpoint):
        mid, pid, bit = endpoint.get("module"), endpoint.get("port"), endpoint.get("bit")
        port = modules.get(mid, {}).get("ports", {}).get(pid)
        return port if (endpoint.get("kind") == "port" and isinstance(port, Mapping)
            and type(bit) is int and 0 <= bit < port.get("width", 0)) else None

    observations = []
    from validation_platform.contract_binding import check_top_port_bindings
    binding_conflicts = check_top_port_bindings(architecture, manifest)
    for issue in diagnostics:
        code = issue["code"]
        if code == "PORT_BINDING_KIND_CONFLICT":
            if issue not in binding_conflicts:
                return None
            observations.append({"field": "ports", "boundaries": deepcopy(issue["boundaries"]),
                "operations": [deepcopy(issue["operation"])], "diagnostic": deepcopy(issue), "proposed": None})
        elif code in {"UNCONNECTED_INTERNAL_PORT", "UNBOUND_MODULE_PORT"}:
            endpoint = issue.get("endpoint", {})
            port = definition(endpoint)
            if port is None or code == "UNCONNECTED_INTERNAL_PORT" and port.get("external") is not False:
                return None
            observations.append({"module": endpoint["module"], "field": "ports", "port": endpoint["port"],
                "expected": deepcopy(port), "diagnostic": deepcopy(issue), "proposed": None})
        elif code == "POWER_DOMAIN_CONFLICT":
            for network in manifest.get("networks", []):
                members = network.get("members", [])
                ids = sorted(row["id"] for row in members if row.get("kind") == "voltage_domain")
                if (len(ids) < 2 or not set(ids) <= set(domains)
                        or issue.get("message") != f"distinct voltage domains are shorted: {ids}"):
                    continue
                ports = [{"endpoint": deepcopy(row), "expected": deepcopy(definition(row))}
                    for row in members if definition(row) is not None
                    and definition(row).get("domain_binding") == "rail"
                    and definition(row).get("domain") in ids]
                # Purely local shorts or undeclared associations stay with SCL.
                # Require real frozen rail boundaries covering each conflicting ID.
                if (len({row["endpoint"]["module"] for row in ports}) < 2
                        or {row["expected"]["domain"] for row in ports} != set(ids)):
                    continue
                edges = [deepcopy(row) for row in manifest.get("operations", [])
                    if row.get("kind") == "connect" and row.get("endpoints")
                    and all(endpoint in members for endpoint in row["endpoints"])]
                observations.append({"field": "voltage_domains", "expected": [deepcopy(domains[key]) for key in ids],
                    "boundaries": ports, "network": deepcopy(network), "operations": edges,
                    "diagnostic": deepcopy(issue), "proposed": None})
    for observation in observations:
        observation["observation_schema"] = "pcbagent.frozen_boundary_observation.v2"
    return observations or None


KINDS = ("pcbagent.peripheral_choices.v1", "pcbagent.architecture_boundary_patch.v1")


def _map(value):
    return value if isinstance(value, Mapping) else {}


def _request_packets(record):
    import json
    packets = []
    for message in record.get("messages", []):
        if not isinstance(message, Mapping) or message.get("type") != "UserMessage":
            continue
        try:
            value = json.loads(message.get("content", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            packets.append(value)
    return packets


def _projection_packets(record):
    return [value for value in _request_packets(record) if "planner_projection" in value
        or _map(_map(_map(value.get("output_schema")).get("properties")).get("schema")).get("const") in KINDS]


def uses_projection(records):
    return any(isinstance(record, Mapping) and (
        _map(_map(record.get("values")).get("result")).get("schema") in KINDS
        or _projection_packets(record)) for record in records or [])


def _packet(record):
    packets = _projection_packets(record)
    if len(packets) != 1:
        raise ValueError("projection requires its unique actual request")
    return packets[0]


def _inventory_assignment(architecture, fixed_aliases, raw, phase):
    checked(INVENTORY_REPAIR_SCHEMA, raw)
    if phase != "architecture" or not isinstance(architecture, Mapping):
        raise ValueError("inventory patch has no preceding architecture")
    modules = {row["id"]: row for row in architecture["modules"]}
    occupied = [alias for row in modules.values() for alias in row["core_instances"]]
    missing = fixed_aliases - set(occupied)
    assignments = raw["assignments"]
    targets = [row["core_instance"] for row in assignments]
    if (not missing or len(occupied) != len(set(occupied)) or not set(occupied) <= fixed_aliases
            or raw["unresolved"] or set(targets) != missing or len(targets) != len(missing)
            or any(row["module_id"] not in modules for row in assignments)):
        raise ValueError("inventory patch is not an exact missing-alias assignment")
    architecture = deepcopy(architecture)
    modules = {row["id"]: row for row in architecture["modules"]}
    for row in assignments:
        modules[row["module_id"]]["core_instances"].append(row["core_instance"])
    return architecture


def replay(records):
    """Rebuild projected answers from raw text and ordered prior owner answers.

    No caller-supplied assembled answer or projection hash grants authority.
    Normal full-answer histories remain legacy inputs; an already projected
    peripheral phase cannot fall back to a full-answer owner replacement.
    """
    from agent_framework.structured_output import extract_tolerant, loads_tolerant
    from netlist_end_to_end.schemas import RESULT_PATTERN
    from validation_platform.certificate_facts import digest
    selected, summaries, issues, rejected = {}, [], [], {}
    architecture, peripherals, owner_base, phase = None, {}, None, None
    projected_phase, fixed_aliases = False, set()
    projection_run = uses_projection(records)
    for index, record in enumerate(records or []):
        if not isinstance(record, Mapping) or record.get("status") != "success":
            continue
        stage, mid = record.get("stage"), record.get("module")
        if stage not in {"netlist_architecture", "netlist_peripheral", "netlist_architecture_inventory_repair"}:
            continue
        raw = _map(record.get("values")).get("result")
        if not isinstance(raw, Mapping):
            continue
        kind = raw.get("schema")
        projected = kind in KINDS
        try:
            if projected or projection_run:
                text, _ = extract_tolerant(RESULT_PATTERN, record.get("text"), expect_json=True)
                parsed, _ = loads_tolerant(text or "")
                if parsed != raw:
                    raise ValueError("planner values differ from actual model text")
            if projected:
                packet = _packet(record)
                if packet.get("planner_projection") != kind:
                    raise ValueError("projection request and answer kinds differ")
            else:
                packet = None
                if _projection_packets(record):
                    raise ValueError("projected request cannot return an unversioned full answer")
            if stage == "netlist_architecture_inventory_repair":
                if not projection_run:
                    continue
                try:
                    architecture = _inventory_assignment(architecture, fixed_aliases, raw, phase)
                except (KeyError, TypeError, ValueError) as exc:
                    rejected[("netlist_architecture", None)] = {"code": "PLANNING_PENDING_PROVENANCE", "severity": "error",
                        "message": "Unrepaired inventory patch: " + str(exc), "stage": stage, "module": mid}
                    continue
                rejected.pop(("netlist_architecture", None), None)
                # The architecture record stays raw; only its independently replayed
                # identity assembly changes. All old obligations/pending remain exact.
                selected[("netlist_architecture", None)] = deepcopy(architecture)
                continue
            if stage == "netlist_architecture":
                if kind == "pcbagent.architecture_boundary_patch.v1":
                    if phase != "reconciliation":
                        if not isinstance(architecture, Mapping) or not peripherals:
                            raise ValueError("owner patch has no actual prior architecture/peripheral phase")
                        owner_base = (deepcopy(architecture),
                            {key: deepcopy(value["unresolved"]) for key, value in peripherals.items()})
                    baseline, pending = owner_base
                    review = packet.get("architecture_reconciliation", {})
                    if (packet.get("projection_baseline") != baseline
                            or review.get("peripheral_unresolved") != pending):
                        raise ValueError("owner baseline/pending differs from actual ordered prior answers")
                    phase = "reconciliation"
                    try:
                        canonical = boundary(baseline, pending, raw)
                    except (KeyError, TypeError, ValueError) as exc:
                        rejected[(stage, mid)] = {"code": "PLANNING_PENDING_PROVENANCE", "severity": "error",
                            "message": "Unrepaired owner patch: " + str(exc), "stage": stage, "module": mid}
                        continue
                    rejected.pop((stage, mid), None)
                elif projected:
                    raise ValueError("wrong projection kind for architecture")
                else:
                    if projected_phase and phase in {"peripheral", "reconciliation"}:
                        raise ValueError("projected peripheral phase requires explicit owner patch provenance")
                    canonical = deepcopy(raw)
                    packets = [p for p in _request_packets(record) if isinstance(p.get("core_inventory"), Mapping)]
                    fixed_aliases = set(packets[0]["core_inventory"]) if len(packets) == 1 else set()
                    phase = "architecture"
                architecture = canonical
                rejected.pop((stage, mid), None)
            else:
                if phase in {"architecture", "reconciliation"}:
                    peripherals = {}
                    phase = "peripheral"
                if kind == "pcbagent.peripheral_choices.v1":
                    modules = {row["id"]: row for row in (architecture or {}).get("modules", [])}
                    frozen = packet.get("frozen_architecture", {})
                    if (mid not in modules or frozen.get("module") != modules[mid]
                            or frozen.get("voltage_domains") != architecture["voltage_domains"]):
                        raise ValueError("peripheral baseline differs from actual preceding architecture")
                    try:
                        canonical, _ = peripheral(frozen, raw)
                    except (KeyError, TypeError, ValueError) as exc:
                        rejected[(stage, mid)] = {"code": "PLANNING_PENDING_PROVENANCE", "severity": "error",
                            "message": "Unrepaired peripheral choice: " + str(exc), "stage": stage, "module": mid}
                        continue
                    rejected.pop((stage, mid), None)
                    projected_phase = True
                elif projected:
                    raise ValueError("wrong projection kind for peripheral")
                else:
                    if projection_run:
                        raise ValueError("a projected run cannot downgrade a peripheral to a full answer")
                    canonical = deepcopy(raw)
                peripherals[mid] = canonical
            if projected:
                selected[(stage, mid)] = canonical
                summaries.append({"record_index": index, "stage": stage, "module": mid,
                    "schema": kind, "raw_sha256": digest(raw), "assembled_sha256": digest(canonical),
                    "actual_request_sha256": digest(packet)})
            else:
                selected.pop((stage, mid), None)
        except (KeyError, TypeError, ValueError) as exc:
            issues.append({"code": "PLANNING_PENDING_PROVENANCE", "severity": "error",
                "message": str(exc), "stage": stage, "module": mid, "record_index": index})
    return selected, summaries, [*issues, *rejected.values()]


def owned_fields_match(proposal, assembled, stage):
    if proposal.get("voltage_domains") != assembled.get("voltage_domains"):
        return False
    before = {row["id"]: row for row in assembled.get("modules", [])}
    after = {row.get("id"): row for row in proposal.get("modules", [])}
    if set(before) != set(after):
        return False
    return all(before[mid].get(field) == after[mid].get(field)
        for mid in before for field in ("id", "label", "intent", "core_instances", "ports"))
