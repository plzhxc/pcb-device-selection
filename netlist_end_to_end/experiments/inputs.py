"""Whitelist reference metadata into bounded, provenance-bearing case inputs."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from validation_platform.netlist_graph import parse_netlist_graph, read_netlist_json
from validation_platform.fixed_inventory import PHYSICAL_DEVICE_TYPES, iter_fixed_devices


SCHEMA = "pcbagent.limited_case_input.v1"
MAX_REQUEST_CHARS = 12_000
MAX_REQUIREMENTS = 100
_PART = re.compile(r"[CS][0-9]+")
_KINDS = {"functional", "interface", "operating", "component", "pin_policy"}
_SOURCES = {"original_request", "core_inventory", "datasheet", "manufacturer", "reference_component_analysis", "user", "design_decision"}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validate_connector_permissions(data: Mapping[str, Any]) -> None:
    """Authorize only explicit, finite private-interface co-design decisions."""
    entries = data["permissions"].get("connector_co_design", [])
    if not isinstance(entries, list) or len(entries) > 32:
        raise ValueError("connector_co_design must be a bounded array")
    requirements = {row["id"]: row for row in data["requirements"]}
    boards = {row["board_id"]: row for row in data["boards"]}
    seen_ids, seen_interfaces = set(), set()
    fields = {"id", "requirement_id", "board_id", "interface_id", "interface_quantity", "selector",
              "assignment_policy", "compatibility_scope", "contact_class", "fixed_contacts", "required_translation"}
    for entry in entries:
        if not isinstance(entry, dict) or not fields <= set(entry) or set(entry) - fields - {"spare_contact_policy"}:
            raise ValueError("connector co-design permission requires its complete explicit scope")
        for name in ("id", "requirement_id", "board_id", "interface_id"):
            if not isinstance(entry[name], str) or not 1 <= len(entry[name]) <= 128:
                raise ValueError("connector permission identifiers must be bounded nonempty text")
        identity = (entry["board_id"], entry["id"])
        interface = (entry["board_id"], entry["interface_id"])
        if identity in seen_ids or interface in seen_interfaces:
            raise ValueError("connector permission and interface must be unique on their board")
        seen_ids.add(identity); seen_interfaces.add(interface)
        board = boards.get(entry["board_id"])
        requirement = requirements.get(entry["requirement_id"])
        if board is None or requirement is None or requirement["source"] != "design_decision" or requirement["kind"] != "interface":
            raise ValueError("connector permission requires an existing board and explicit interface design decision")
        if (type(entry["interface_quantity"]) is not int or entry["interface_quantity"] != 1
                or entry["assignment_policy"] != "new_harness_co_design"
                or entry["compatibility_scope"] != "new_design_only"
                or entry["contact_class"] != "uniform_passive_contacts"
                or entry["fixed_contacts"] != [] or entry["required_translation"] is not True):
            raise ValueError("only one uniform private interface with a new translated harness is supported")
        selector = entry["selector"]
        if (not isinstance(selector, dict) or set(selector) != {"kind", "contact_count", "core_device_ids"}
                or selector["kind"] != "unique_private_abstract_connector"
                or type(selector["contact_count"]) is not int or not 2 <= selector["contact_count"] <= 32):
            raise ValueError("connector selector must name a bounded private abstract interface")
        parts = selector["core_device_ids"]
        if (not isinstance(parts, list) or not parts or len(parts) > 32
                or any(not isinstance(part, str) or not _PART.fullmatch(part) for part in parts)
                or len(parts) != len(set(parts))):
            raise ValueError("connector selector requires unique C/S core identities")
        counts = Counter()
        for _, device in iter_fixed_devices(board):
            counts[device["device_id"]] += device["quantity"]
        if any(counts[part] != 1 for part in parts):
            raise ValueError("each selected connector anchor must identify exactly one fixed instance")
        selection = requirement.get("selection")
        if selection and (selection["board_id"] != entry["board_id"] or selection["device_id"] not in parts):
            raise ValueError("connector permission contradicts its requirement selection scope")
        if "spare_contact_policy" in entry:
            policy = entry["spare_contact_policy"]
            if (not isinstance(policy, dict) or set(policy) != {"mode", "requirement_id", "max_contacts", "ground_scope"}
                    or policy["mode"] != "additional_ground_returns"
                    or policy["ground_scope"] != "unique_source_authenticated_interface_ground"
                    or not isinstance(policy["requirement_id"], str) or not policy["requirement_id"]
                    or type(policy["max_contacts"]) is not int or not 1 <= policy["max_contacts"] < selector["contact_count"]):
                raise ValueError("spare contact policy must explicitly bound additional returns to one source-authenticated interface ground")
            condition = requirements.get(policy["requirement_id"])
            chosen = condition.get("selection") if isinstance(condition, dict) else None
            if (policy["requirement_id"] == entry["requirement_id"] or not condition
                    or condition["source"] != "design_decision" or condition["kind"] != "interface"
                    or not isinstance(chosen, dict)
                    or chosen.get("field") != "requirements.allow_additional_connector_ground_returns"
                    or chosen.get("value") is not True or chosen.get("board_id") != entry["board_id"]
                    or chosen.get("device_id") not in parts or chosen.get("applies_to") != "all_instances"):
                raise ValueError("additional ground returns require a separate explicit interface design decision scoped to this board and fixed device")
            if sum(_entry["spare_contact_policy"].get("requirement_id") == policy["requirement_id"]
                   for _entry in entries if isinstance(_entry, dict) and isinstance(_entry.get("spare_contact_policy"), dict)) != 1:
                raise ValueError("a spare contact decision must authorize exactly one private interface")


def validate_input(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the generation-side object; it has no reference topology fields."""
    data = deepcopy(dict(value))
    required = {"schema", "case_id", "revision", "request", "boards", "requirements", "permissions", "provenance"}
    if set(data) != required or data["schema"] != SCHEMA:
        raise ValueError("invalid limited-input schema or unknown top-level fields")
    if not isinstance(data["case_id"], str) or not data["case_id"].strip():
        raise ValueError("case_id must be nonempty")
    if type(data["revision"]) is not int or data["revision"] < 0:
        raise ValueError("revision must be a nonnegative integer")
    if not isinstance(data["request"], str) or not 1 <= len(data["request"].strip()) <= MAX_REQUEST_CHARS:
        raise ValueError("request must be nonempty and at most 12000 characters")
    if not isinstance(data["boards"], list) or not data["boards"]:
        raise ValueError("at least one board is required")
    seen = set()
    for board in data["boards"]:
        if (not isinstance(board, dict) or not {"board_id", "label", "core_devices"} <= set(board)
                or set(board) - {"board_id", "label", "core_devices", "functional_devices", "physical_devices", "static_environment"}):
            raise ValueError("board permits only board_id, label, core_devices and optional functional_devices/physical_devices")
        if not re.fullmatch(r"board_[0-9]+", str(board["board_id"])) or board["board_id"] in seen:
            raise ValueError("board_id must be unique")
        seen.add(board["board_id"])
        if not isinstance(board["label"], str) or not isinstance(board["core_devices"], list):
            raise ValueError("invalid board label or core inventory")
        for device in board["core_devices"]:
            if not isinstance(device, dict) or set(device) != {"device_id", "model", "quantity"}:
                raise ValueError("core inventory permits only identity, model and quantity")
            if device["device_id"] is not None and not _PART.fullmatch(str(device["device_id"])):
                raise ValueError("core identity must be C/S digits or unresolved null")
            if device["model"] is not None and not isinstance(device["model"], str):
                raise ValueError("model must be text or null")
            if type(device["quantity"]) is not int or device["quantity"] < 1:
                raise ValueError("core quantity must be positive")
        physical = board.get("physical_devices", [])
        if not isinstance(physical, list):
            raise ValueError("physical_devices must be an array")
        for device in physical:
            if (not isinstance(device, dict) or set(device) != {"device_id", "model", "quantity", "component_type"}
                    or device["component_type"] not in PHYSICAL_DEVICE_TYPES
                    or device["device_id"] is not None and not _PART.fullmatch(str(device["device_id"]))
                    or not isinstance(device["model"], str) or not device["model"].strip()
                    or type(device["quantity"]) is not int or not 1 <= device["quantity"] <= 1000):
                raise ValueError("physical inventory requires an exact supported type, model, C/S identity or null, and bounded quantity")
        fixed_ids = [row["device_id"] for _, row in iter_fixed_devices(board) if row.get("device_id")]
        if len(fixed_ids) != len(set(fixed_ids)):
            raise ValueError("aggregate each fixed identity once across all inventory categories")
        functional = board.get("functional_devices", [])
        if not isinstance(functional, list):
            raise ValueError("functional_devices must be an array")
        for device in functional:
            if not isinstance(device, dict) or set(device) != {"device_id", "model", "quantity", "component_type"}:
                raise ValueError("functional inventory permits only identity, model, quantity and original component_type")
            if device["component_type"] != "module":
                raise ValueError("only original module components belong in functional_devices")
            if device["device_id"] is not None and not _PART.fullmatch(str(device["device_id"])):
                raise ValueError("functional identity must be C/S digits or unresolved null")
            if not isinstance(device["model"], str) or not device["model"].strip():
                raise ValueError("functional model must be nonempty text")
            if type(device["quantity"]) is not int or device["quantity"] < 1:
                raise ValueError("functional quantity must be positive")
    if not isinstance(data["requirements"], list) or len(data["requirements"]) > MAX_REQUIREMENTS:
        raise ValueError("requirements exceed the frozen input budget")
    ids = set()
    selections = {}
    for requirement in data["requirements"]:
        fields = {"id", "kind", "description", "source", "design_space_forced", "reason", "evidence_refs"}
        if not isinstance(requirement, dict) or not fields <= set(requirement) or set(requirement) - fields - {"selection"}:
            raise ValueError("each input supplement needs the complete provenance contract")
        for key in ("id", "description", "reason"):
            if not isinstance(requirement[key], str) or not requirement[key].strip():
                raise ValueError(f"requirement.{key} must be nonempty text")
        if requirement["id"] in ids or requirement["kind"] not in _KINDS or requirement["source"] not in _SOURCES:
            raise ValueError("duplicate requirement or unsupported kind/source")
        ids.add(requirement["id"])
        if len(requirement["description"]) > 2000 or type(requirement["design_space_forced"]) is not bool:
            raise ValueError("invalid requirement size or design-space flag")
        if not isinstance(requirement["evidence_refs"], list) or not requirement["evidence_refs"] or any(
            not isinstance(item, str) or not item.strip() for item in requirement["evidence_refs"]
        ):
            raise ValueError("requirement evidence_refs must be explicit nonempty references")
        if "selection" in requirement:
            selection = requirement["selection"]
            if not isinstance(selection, dict) or set(selection) != {"field", "value", "board_id", "device_id", "applies_to"}:
                raise ValueError("selection requires a scoped operating condition and its value")
            if not isinstance(selection["field"], str) or not re.fullmatch(r"(?:configuration|requirements)\.[A-Za-z][A-Za-z0-9_]{0,79}", selection["field"]):
                raise ValueError("selection field must name a configuration or requirements scalar")
            selected_value = selection["value"]
            if type(selected_value) not in {bool, str, int, float} or (type(selected_value) is float and not math.isfinite(selected_value)):
                raise ValueError("selection value must be a finite, non-null scalar")
            if isinstance(selected_value, str) and not 1 <= len(selected_value) <= 200:
                raise ValueError("selection text must be nonempty and bounded")
            selected_board = next((b for b in data["boards"] if b["board_id"] == selection["board_id"]), None)
            if selected_board is None or selection["device_id"] not in {d["device_id"] for _, d in iter_fixed_devices(selected_board) if d["device_id"]}:
                raise ValueError("selection must identify a fixed device present on its board")
            if selection["applies_to"] != "all_instances":
                raise ValueError("selection must explicitly apply to all instances of this device on the board")
            key = (selection["board_id"], selection["device_id"], selection["field"])
            if key in selections and (type(selections[key]) is not type(selected_value) or selections[key] != selected_value):
                raise ValueError("contradictory selected operating conditions")
            selections[key] = selected_value
    if (not isinstance(data["permissions"], dict) or "gpio_remapping" not in data["permissions"]
            or set(data["permissions"]) - {"gpio_remapping", "connector_co_design"}
            or type(data["permissions"]["gpio_remapping"]) is not bool):
        raise ValueError("permissions must explicitly specify gpio_remapping")
    _validate_connector_permissions(data)
    provenance = data["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"kind", "request_sha256", "parent_input_sha256", "source_manifest_sha256"}:
        raise ValueError("invalid input provenance")
    if provenance["kind"] != "reference_derived_limited":
        raise ValueError("case inputs must disclose their reference-derived origin")
    if provenance["request_sha256"] != hashlib.sha256(data["request"].encode()).hexdigest():
        raise ValueError("request digest mismatch")
    from validation_platform.static_voltages import validate_environment
    for board in data["boards"]:
        if "static_environment" in board:
            validate_environment(board["static_environment"], requirements=data["requirements"])

    return data


def reference_parse_options(reference: Mapping[str, Any]) -> dict[str, bool]:
    """Validate evaluator-only parsing options; legacy references remain strict."""
    options = reference.get("parse_options", {})
    if (not isinstance(options, Mapping) or set(options) - {"allow_duplicate_designators"}
            or any(type(value) is not bool for value in options.values())):
        raise ValueError("unsupported reference parse options")
    return {"allow_duplicate_designators": options.get("allow_duplicate_designators", False)}


def build_case_input(case_dir: str | Path, request: str, *,
                     identity_map: Mapping[str, Mapping[str, Any]] | None = None,
                     module_identity_map: Mapping[str, Mapping[str, Any]] | None = None,
                     functional_inventory: Mapping[str, Any] | None = None,
                     physical_identity_map: Mapping[str, Mapping[str, Any]] | None = None,
                     reference_selection: list[Mapping[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return separate generation and evaluator packages without modifying sources.

    The default selects the case's single <case>-cleaned.json reference. The
    merged keyboard retains its explicit duplicate-designator parsing option.
    Explicit selection freezes each chosen filename and its parsing options in
    the evaluator manifest. Only inventory metadata reaches generation input.
    """
    directory = Path(case_dir).resolve()
    if reference_selection is None and directory.name == "case1-keyboard":
        reference_selection = [{"file": "case1-keyboard-cleaned.json",
                                "parse_options": {"allow_duplicate_designators": True}}]
    options_by_file = {}
    if reference_selection is None:
        path = directory / f"{directory.name}-cleaned.json"
        if not path.is_file():
            raise ValueError(f"no cleaned reference {path.name} in {directory}")
        files = [path]
    else:
        if not isinstance(reference_selection, list) or not 1 <= len(reference_selection) <= 100:
            raise ValueError("explicit reference selection must be a bounded nonempty array")
        files = []
        for row in reference_selection:
            if not isinstance(row, Mapping) or set(row) != {"file", "parse_options"}:
                raise ValueError("each explicit reference requires a filename and parse options")
            name = row["file"]
            if (not isinstance(name, str) or Path(name).name != name
                    or not name.endswith(".json") or name in options_by_file):
                raise ValueError("reference filenames must be unique case-local JSON basenames")
            path = (directory / name).resolve()
            if path.parent != directory or path.name != name or path in files or not path.is_file():
                raise ValueError("explicit reference must exist inside its case directory")
            options_by_file[name] = reference_parse_options(row)
            files.append(path)
    boards, references = [], []
    identity_map = identity_map or {}
    module_identity_map = module_identity_map or {}
    physical_identity_map = physical_identity_map or {}
    for index, path in enumerate(files, 1):
        raw = read_netlist_json(path)
        graph = parse_netlist_graph(raw, source=str(path), **options_by_file.get(path.name, {}))
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        counts: Counter[tuple[str | None, str | None]] = Counter()
        resolutions = {}
        for component in graph.components.values():
            if not component.is_core:
                continue
            item = raw[component.key[1]]
            part = item["id"]
            key = f"{directory.name}/{path.name}/{component.key[1]}"
            resolved = identity_map.get(key)
            if resolved:
                if resolved.get("reference_sha256") != sha or not resolved.get("source_ref"):
                    raise ValueError(f"stale or unsupported identity resolution for {key}")
                candidate = resolved.get("supplier_part")
                if not isinstance(candidate, str) or not _PART.fullmatch(candidate) or (part and part != candidate):
                    raise ValueError(f"invalid identity override for {key}")
                part = candidate
                resolutions[component.key[1]] = dict(resolved)
            counts[(part, item["name"])] += 1
        board_id = f"board_{index:02d}"
        label = path.stem.removeprefix("Netlist_")
        if path.name == f"{directory.name}-cleaned.json":
            # A storage rename must not rename the frozen board label. Original
            # ENET names remain immutable; merged case1 keeps its existing label.
            sources = sorted((directory / "netlist").glob("*.enet"))
            label = ("case1-keyboard-fine" if directory.name == "case1-keyboard"
                     else sources[0].stem.removeprefix("Netlist_") if len(sources) == 1
                     else directory.name)
        boards.append({"board_id": board_id, "label": label, "core_devices": [
            {"device_id": part, "model": model, "quantity": quantity}
            for (part, model), quantity in sorted(counts.items(), key=lambda row: str(row[0]))]})
        functional_resolutions = {}
        module_items = {key: item for key, item in raw.items()
                        if item["type"] == "module" and isinstance(item["name"], str) and item["name"].strip()}
        if module_items:
            if functional_inventory is None:
                from datasheet_automation.knowledge.preparation import inventory_functional_modules
                functional_inventory = inventory_functional_modules(directory.parent)
            if (functional_inventory.get("scope") != "functional_modules_separate_from_ic"
                    or functional_inventory.get("reference_connections_included") is not False):
                raise ValueError("functional inventory must be the separate connection-free metadata inventory")
            inventory_rows = {row["component"]: row for row in functional_inventory["instances"]
                              if row["case"] == directory.name and row["file"] == path.name}
            if set(inventory_rows) != set(module_items):
                raise ValueError("functional inventory must cover every named module in this exact source file")
            functional_counts = Counter()
            for component, item in module_items.items():
                locator = f"{directory.name}/{path.name}/{component}"
                inventory = inventory_rows[component]
                if (inventory.get("reference_sha256") != sha or inventory.get("source_type") != "module"
                        or inventory.get("name") != item["name"]):
                    raise ValueError(f"stale functional inventory for {locator}")
                resolved = module_identity_map.get(locator)
                part = None
                if resolved:
                    if (resolved.get("reference_sha256") != sha or not resolved.get("source_ref")
                            or resolved.get("source", {}).get("scope") != "functional_module"):
                        raise ValueError(f"stale or unsupported functional identity resolution for {locator}")
                    candidate = resolved.get("supplier_part")
                    if not isinstance(candidate, str) or not _PART.fullmatch(candidate):
                        raise ValueError(f"invalid functional identity override for {locator}")
                    if resolved.get("status") in {"confirmed", "project_scoped"}:
                        part = candidate
                    functional_resolutions[component] = deepcopy(dict(resolved))
                functional_counts[(part, item["name"])] += 1
            boards[-1]["functional_devices"] = [
                {"device_id": part, "model": model, "quantity": quantity, "component_type": "module"}
                for (part, model), quantity in sorted(functional_counts.items(), key=lambda row: str(row[0]))]
        physical_items = {key: item for key, item in raw.items()
                          if item["type"] in PHYSICAL_DEVICE_TYPES and isinstance(item["name"], str) and item["name"].strip()}
        physical_resolutions = {}
        if physical_items:
            physical_counts = Counter()
            for component, item in physical_items.items():
                locator = f"{directory.name}/{path.name}/{component}"
                resolved = physical_identity_map.get(locator)
                part = None
                if resolved:
                    if (resolved.get("reference_sha256") != sha or not resolved.get("source_ref")
                            or resolved.get("source", {}).get("scope") != "physical_device"
                            or resolved.get("component_type") != item["type"]
                            or resolved.get("model_name") != item["name"]):
                        raise ValueError(f"stale or mismatched physical identity resolution for {locator}")
                    candidate = resolved.get("supplier_part")
                    if not isinstance(candidate, str) or not _PART.fullmatch(candidate):
                        raise ValueError(f"invalid physical identity override for {locator}")
                    if resolved.get("status") in {"confirmed", "project_scoped"}:
                        part = candidate
                    physical_resolutions[component] = deepcopy(dict(resolved))
                physical_counts[(part, item["name"], item["type"])] += 1
            boards[-1]["physical_devices"] = [
                {"device_id": part, "model": model, "quantity": quantity, "component_type": kind}
                for (part, model, kind), quantity in sorted(physical_counts.items(), key=lambda row: str(row[0]))]
        references.append({"board_id": board_id, "path": str(path), "sha256": sha,
                           "components": len(graph.components), "pins": len(graph.pins),
                           "identity_resolutions": resolutions})
        if reference_selection is not None:
            references[-1]["parse_options"] = options_by_file[path.name]
        if module_items:
            references[-1]["functional_identity_resolutions"] = functional_resolutions
        if physical_items:
            references[-1]["physical_identity_resolutions"] = physical_resolutions
    evaluator = {"schema": "pcbagent.case_reference_manifest.v1", "case_id": directory.name,
                 "references": references,
                 "excluded_derived_files": sorted(p.name for p in directory.glob("*.json")
                                                  if p not in files and p.name != "core_devices.json")}
    if reference_selection is not None:
        evaluator["reference_selection"] = {"policy": "explicit_files", "files": [p.name for p in files]}
    payload = {"schema": SCHEMA, "case_id": directory.name, "revision": 0, "request": request.strip(),
               "boards": boards, "requirements": [], "permissions": {"gpio_remapping": True},
               "provenance": {"kind": "reference_derived_limited",
                              "request_sha256": hashlib.sha256(request.strip().encode()).hexdigest(),
                              "parent_input_sha256": None, "source_manifest_sha256": digest(evaluator)}}
    return validate_input(payload), evaluator


def revise_input(previous: Mapping[str, Any], supplements: list[dict[str, Any]], *, hypothesis: str,
                 observation: str, core_model_corrections: list[dict[str, Any]] | None = None,
                 connector_co_design: list[dict[str, Any]] | None = None,
                 connector_spare_ground: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Append disclosed requirements without modifying the original experiment."""
    old = validate_input(previous)
    if not hypothesis.strip() or not observation.strip() or not (supplements or core_model_corrections or connector_co_design or connector_spare_ground):
        raise ValueError("input revision needs a hypothesis, observation and actual supplements")
    new = deepcopy(old)
    new["revision"] += 1
    new["requirements"].extend(deepcopy(supplements))
    spare_ids = set()
    for addition in connector_spare_ground or []:
        if (not isinstance(addition, dict) or set(addition) != {"board_id", "permission_id", "spare_contact_policy"}
                or any(not isinstance(addition[key], str) for key in ("board_id", "permission_id"))):
            raise ValueError("spare-ground revision requires a board, existing permission and explicit policy")
        key = (addition["board_id"], addition["permission_id"])
        matches = [entry for entry in new["permissions"].get("connector_co_design", [])
                   if (entry["board_id"], entry["id"]) == key]
        policy = addition["spare_contact_policy"]
        if (key in spare_ids or len(matches) != 1 or "spare_contact_policy" in matches[0]
                or not isinstance(policy, dict) or not isinstance(policy.get("requirement_id"), str)
                or policy.get("requirement_id") not in {row["id"] for row in supplements}):
            raise ValueError("new spare-ground authority needs one unchanged existing permission and a newly appended explicit decision")
        matches[0]["spare_contact_policy"] = deepcopy(policy)
        spare_ids.add(key)
    if connector_co_design is not None:
        if not isinstance(connector_co_design, list):
            raise ValueError("connector co-design additions must be an array")
        new["permissions"].setdefault("connector_co_design", []).extend(deepcopy(connector_co_design))
    corrected = set()
    for correction in core_model_corrections or []:
        if not isinstance(correction, dict) or set(correction) != {"board_id", "device_id", "before", "after", "evidence_refs", "reason"}:
            raise ValueError("model correction requires a complete scoped identity provenance record")
        if not isinstance(correction["after"], str) or not correction["after"].strip() or correction["after"] == correction["before"]:
            raise ValueError("a model correction must specify a changed, nonempty exact model")
        if not isinstance(correction["reason"], str) or not correction["reason"].strip() or not isinstance(correction["evidence_refs"], list) or not correction["evidence_refs"] or any(not isinstance(ref, str) or not ref.strip() for ref in correction["evidence_refs"]):
            raise ValueError("model correction requires explicit source references and rationale")
        key = (correction["board_id"], correction["device_id"])
        targets = [core for board in new["boards"] if board["board_id"] == correction["board_id"]
                   for core in board["core_devices"] if core["device_id"] == correction["device_id"]]
        if key in corrected or len(targets) != 1 or targets[0]["model"] != correction["before"]:
            raise ValueError("model correction must identify exactly one unchanged original C/S inventory row")
        targets[0]["model"] = correction["after"]
        corrected.add(key)
    new["provenance"]["parent_input_sha256"] = digest(old)
    new = validate_input(new)
    record = {"schema": "pcbagent.input_revision.v1", "case_id": old["case_id"],
              "before_sha256": digest(old), "after_sha256": digest(new),
              "revision": new["revision"], "hypothesis": hypothesis, "observation": observation,
              "supplements": deepcopy(supplements),
              "core_model_corrections": deepcopy(core_model_corrections or []),
              "connector_co_design_additions": deepcopy(connector_co_design or []),
              "connector_spare_ground_additions": deepcopy(connector_spare_ground or []),
              "design_space_forced": any(row["design_space_forced"] for row in supplements),
              "reference_informed": any(row["source"] == "reference_component_analysis" for row in supplements)}
    return new, record
