"""Deterministic, local expansion of abstract devices and circuit templates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping

from jsonschema.validators import validator_for

from .errors import CommonDeviceError
from .models import CommonDevice, CompilationResult
from .registry import TemplateRegistry
from .semantics import validate_semantics


MAX_DEPTH = 16
MAX_INSTANCES = 10_000
MAX_PINS = 100_000
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
_COMPONENT_ID = re.compile(r"gge[0-9]+")
_INSTANCE_FIELDS = {"instance_id", "template_ref", "parameters", "connections"}
_LEAF_OPTIONS = {"designator", "name", "value", "component_id", "pin_space", "pin_bindings", "extra_pins", "binding_source"}

# A network identity is independent of its eventual display label.
NetRef = tuple[str, ...] | None


def _fields(value: Any, required: set[str], optional: set[str], path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CommonDeviceError(f"{path}: expected an object")
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing or unknown:
        raise CommonDeviceError(f"{path}: missing fields {sorted(missing)}, unknown fields {sorted(unknown)}")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
        raise CommonDeviceError(f"{path}: expected a nonempty text string without control characters")
    return value


def _instance_id(value: Any, path: str) -> str:
    name = _text(value, path)
    if "/" in name:
        raise CommonDeviceError(f"{path}: '/' is reserved for instance scopes")
    return name


def _json_copy(value: Any, path: str = "plan", depth: int = 0) -> Any:
    if depth > 64:
        raise CommonDeviceError(f"{path}: JSON nesting exceeds 64")
    if value is None or type(value) in (bool, str, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise CommonDeviceError(f"{path}: non-finite numbers are not allowed")
        return value
    if isinstance(value, list):
        if len(value) > MAX_PINS:
            raise CommonDeviceError(f"{path}: array exceeds size limit")
        return [_json_copy(item, f"{path}[{index}]", depth + 1) for index, item in enumerate(value)]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise CommonDeviceError(f"{path}: JSON keys must be strings")
        return {key: _json_copy(item, f"{path}.{key}", depth + 1) for key, item in value.items()}
    raise CommonDeviceError(f"{path}: expected a JSON-compatible value")


def _digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _external(value: Any, path: str) -> NetRef:
    if value is None:
        return None
    label = _text(value, path)
    if label == "null":
        raise CommonDeviceError(f"{path}: use JSON null for a known unconnected pin")
    return ("external", label)


def _endpoints(template: Mapping[str, Any], parameters: Mapping[str, Any]) -> list[tuple[str, int, str]]:
    ports = template.get("ports")
    if not isinstance(ports, list) or not ports:
        raise CommonDeviceError("template.ports: expected a nonempty array")
    result: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    for port in ports:
        if not isinstance(port, dict):
            raise CommonDeviceError("template.ports: expected port objects")
        name = _text(port.get("id"), "template.port.id")
        if name in seen or "[" in name or "]" in name:
            raise CommonDeviceError(f"template.port.id: duplicate or ambiguous port {name!r}")
        seen.add(name)
        if ("width" in port) == ("width_parameter" in port):
            raise CommonDeviceError(f"template.port {name}: specify exactly one width form")
        width = port.get("width") if "width" in port else parameters.get(port["width_parameter"])
        if type(width) is not int or not 1 <= width <= MAX_PINS:
            raise CommonDeviceError(f"template.port {name}: width must be a positive bounded integer")
        if len(result) + width > MAX_PINS:
            raise CommonDeviceError("template: expanded endpoints exceed pin limit")
        vector = "width_parameter" in port or width != 1
        result.extend((name, bit, f"{name}[{bit}]" if vector else name) for bit in range(width))
    return result


@dataclass
class _Leaf:
    device: dict[str, Any]
    nets: list[NetRef]
    prefix: str
    component_id: str | None
    record: dict[str, Any]


class _Compiler:
    def __init__(self, registry: TemplateRegistry) -> None:
        self.registry = registry
        self.leaves: list[_Leaf] = []
        self.records: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.local_refs: set[tuple[str, ...]] = set()
        self.external_labels: set[str] = set()
        self.instance_count = 0
        self.pin_count = 0

    def remember(self, reference: NetRef) -> NetRef:
        if reference is not None:
            if reference[0] == "external":
                self.external_labels.add(reference[1])
            else:
                self.local_refs.add(reference)
        return reference

    def expand(
        self,
        instance: dict[str, Any],
        scope: tuple[str, ...],
        active: tuple[tuple[str, str], ...] = (),
        incoming: dict[str, NetRef] | None = None,
    ) -> None:
        self.instance_count += 1
        if len(scope) > MAX_DEPTH or self.instance_count > MAX_INSTANCES:
            raise CommonDeviceError("Circuit exceeds expansion depth or instance count limit")
        path = "/".join(scope)
        if "device" in instance:
            _fields(instance, {"instance_id", "device"}, {"component_id"}, path)
            if "component_id" in instance:
                _text(instance["component_id"], path + ".component_id")
            if incoming is not None:
                raise CommonDeviceError(f"{path}: embedded explicit devices are not template children")
            self.explicit_device(instance, scope)
            return

        _fields(instance, _INSTANCE_FIELDS, _LEAF_OPTIONS, path)
        for field in ("designator", "component_id"):
            if field in instance:
                _text(instance[field], path + "." + field)
        reference = _fields(instance["template_ref"], {"template_id", "version"}, set(), path + ".template_ref")
        template_id = _text(reference["template_id"], path + ".template_id")
        version = _text(reference["version"], path + ".version")
        if not _VERSION.fullmatch(version):
            raise CommonDeviceError(f"{path}: template version must be major.minor.patch")
        key = (template_id, version)
        if key in active:
            raise CommonDeviceError(f"{path}: cyclic template dependency {template_id}@{version}")
        template = self.registry.get(template_id, version)
        if template.get("template_id") != template_id or template.get("version") != version:
            raise CommonDeviceError(f"{path}: registry returned a different template identity")
        parameters = instance["parameters"]
        if not isinstance(parameters, dict):
            raise CommonDeviceError(f"{path}.parameters: expected an object")
        schema = template.get("parameters_schema")
        if not isinstance(schema, dict):
            raise CommonDeviceError(f"{path}: template has no parameter schema")
        try:
            validator = validator_for(schema)(schema)
            error = next(validator.iter_errors(parameters), None)
        except Exception as exc:
            raise CommonDeviceError(f"{path}: invalid parameter schema: {exc}") from exc
        if error is not None:
            raise CommonDeviceError(f"{path}.parameters: {error.message}")
        endpoints = _endpoints(template, parameters)
        expected = {endpoint[2] for endpoint in endpoints}
        raw_connections = instance["connections"] if incoming is None else incoming
        _fields(raw_connections, expected, set(), path + ".connections")
        connections = {
            name: self.remember(_external(raw_connections[name], path + ".connections." + name) if incoming is None else raw_connections[name])
            for _, _, name in endpoints
        }
        kind = template.get("kind")
        record = {
            "instance_id": path,
            "scope": list(scope),
            "template_ref": deepcopy(reference),
            "template_sha256": self.registry.definition_sha256(template_id, version),
            "template_content_sha256": _digest(template),
            "kind": kind,
            "parameters": deepcopy(parameters),
            "_connections": connections,
        }
        self.records.append(record)
        if kind == "composite":
            if instance.keys() & _LEAF_OPTIONS:
                raise CommonDeviceError(f"{path}: device identity and pin options apply only to leaf instances")
            # An open module port still connects its internal users together.
            # None on a primitive terminal remains an individually open pin.
            effective = {
                port: self.remember(("local_port", *scope, port)) if net is None else net
                for port, net in connections.items()
            }
            record["unconnected_external_ports"] = [port for port, net in connections.items() if net is None]
            record["_internal_port_connections"] = effective
            self.composite(template, parameters, effective, scope, active + (key,))
        elif kind == "primitive":
            for diagnostic in validate_semantics(template, parameters):
                self.diagnostics.append({"instance_id": path, **diagnostic})
            self.primitive(instance, template, endpoints, connections, record)
        else:
            raise CommonDeviceError(f"{path}: unsupported template kind {kind!r}")

    def composite(
        self,
        template: dict[str, Any],
        parameters: dict[str, Any],
        connections: dict[str, NetRef],
        scope: tuple[str, ...],
        active: tuple[tuple[str, str], ...],
    ) -> None:
        body = template.get("body", {})
        children = body.get("instances")
        if body.get("representation") != "composite" or not isinstance(children, list) or not children:
            raise CommonDeviceError("Composite body must contain a nonempty instances array")
        names: set[str] = set()
        used_ports: set[str] = set()
        for child in children:
            _fields(child, _INSTANCE_FIELDS, set(), "/".join(scope) + ".body.instance")
            name = _instance_id(child["instance_id"], "child.instance_id")
            if name in names:
                raise CommonDeviceError(f"Duplicate child instance id {name!r}")
            names.add(name)
            if not isinstance(child["parameters"], dict) or not isinstance(child["connections"], dict):
                raise CommonDeviceError("Child parameters and connections must be objects")
            values: dict[str, Any] = {}
            for parameter, binding in child["parameters"].items():
                if isinstance(binding, dict) and set(binding) == {"parameter"}:
                    source = _text(binding["parameter"], "parameter reference")
                    if source not in parameters:
                        raise CommonDeviceError(f"Missing parent parameter {source!r}")
                    values[parameter] = deepcopy(parameters[source])
                elif isinstance(binding, dict) and set(binding) == {"literal"}:
                    values[parameter] = deepcopy(binding["literal"])
                else:
                    raise CommonDeviceError("Child parameter binding needs exactly parameter or literal")
            wires: dict[str, NetRef] = {}
            for endpoint, binding in child["connections"].items():
                if isinstance(binding, dict) and set(binding) == {"port"}:
                    port = _text(binding["port"], "parent port reference")
                    if port not in connections:
                        raise CommonDeviceError(f"Unknown parent port {port!r}")
                    used_ports.add(port)
                    wires[endpoint] = connections[port]
                elif isinstance(binding, dict) and set(binding) == {"net"}:
                    net = _text(binding["net"], "local net")
                    wires[endpoint] = self.remember(("local", *scope, net))
                elif isinstance(binding, dict) and set(binding) == {"unconnected"} and binding["unconnected"] is True:
                    wires[endpoint] = None
                else:
                    raise CommonDeviceError("Child connection needs exactly port, net, or unconnected:true")
            expanded = {"instance_id": name, "template_ref": deepcopy(child["template_ref"]), "parameters": values, "connections": {}}
            self.expand(expanded, scope + (name,), active, wires)
        missing = connections.keys() - used_ports
        unused_connected = [port for port in missing if connections[port] is None or connections[port][0] != "local_port"]
        if unused_connected:
            raise CommonDeviceError(f"Composite has externally connected but unused ports: {sorted(unused_connected)}")
        for port in sorted(missing):
            self.diagnostics.append({"instance_id": "/".join(scope), "code": "composite:unused_open_port", "status": "unresolved", "message": f"Open port {port!r} has no internal users."})

    def primitive(
        self,
        instance: dict[str, Any],
        template: dict[str, Any],
        endpoints: list[tuple[str, int, str]],
        connections: dict[str, NetRef],
        record: dict[str, Any],
    ) -> None:
        path = record["instance_id"]
        pin_space = instance.get("pin_space", "logical")
        pins: list[dict[str, Any]] = []
        nets: list[NetRef] = []
        bindings: list[dict[str, Any]] = []
        if pin_space == "logical":
            if instance.keys() & {"pin_bindings", "extra_pins", "binding_source"}:
                raise CommonDeviceError(f"{path}: physical bindings require pin_space=physical")
            for port, bit, endpoint in endpoints:
                pins.append({"id": endpoint, "name": endpoint, "net": None})
                nets.append(connections[endpoint])
                bindings.append({"port": port, "bit": bit, "pin_id": endpoint, "name": endpoint})
        elif pin_space == "physical":
            _text(instance.get("binding_source"), path + ".binding_source")
            supplied = instance.get("pin_bindings")
            if not isinstance(supplied, list):
                raise CommonDeviceError(f"{path}.pin_bindings: expected an array")
            available = {(port, bit): endpoint for port, bit, endpoint in endpoints}
            covered: set[tuple[str, int]] = set()
            for binding in supplied:
                _fields(binding, {"port", "bit", "pin_id", "name"}, set(), path + ".pin_binding")
                port = _text(binding["port"], path + ".pin_binding.port")
                bit = binding["bit"]
                if type(bit) is not int or (port, bit) not in available or (port, bit) in covered:
                    raise CommonDeviceError(f"{path}: unknown or duplicate endpoint binding {(port, bit)!r}")
                covered.add((port, bit))
                pins.append({"id": binding["pin_id"], "name": binding["name"], "net": None})
                nets.append(connections[available[(port, bit)]])
                bindings.append(deepcopy(binding))
            if covered != available.keys():
                raise CommonDeviceError(f"{path}: every logical endpoint needs one physical pin binding")
            extra = instance.get("extra_pins", [])
            if not isinstance(extra, list):
                raise CommonDeviceError(f"{path}.extra_pins: expected an array")
            for pin in extra:
                _fields(pin, {"id", "name", "net"}, set(), path + ".extra_pin")
                nets.append(self.remember(_external(pin["net"], path + ".extra_pin.net")))
                pins.append({"id": pin["id"], "name": pin["name"], "net": None})
            record["binding_source"] = instance["binding_source"]
            record["extra_pin_ids"] = [pin["id"] for pin in extra]
        else:
            raise CommonDeviceError(f"{path}.pin_space: expected logical or physical")

        value = instance.get("value")
        if "value" not in instance:
            spec = template.get("value_spec")
            if spec is not None:
                _fields(spec, {"parameter", "unit"}, set(), "template.value_spec")
                if not isinstance(spec["unit"], str):
                    raise CommonDeviceError("template.value_spec.unit must be text")
                number = instance["parameters"].get(spec["parameter"])
                if number is not None:
                    if type(number) not in (int, float):
                        raise CommonDeviceError("Derived electrical values must use a numeric parameter")
                    value = str(number) + spec["unit"]
        device = {
            "name": instance.get("name"),
            "designator": instance.get("designator"),
            "type": template.get("device_type"),
            "is_abstract": True,
            "value": value,
            "description": template.get("abstract_description"),
            "id": None,
            "pins": pins,
        }
        prefix = _text(template.get("designator_prefix"), "template.designator_prefix")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", prefix):
            raise CommonDeviceError("template.designator_prefix must use letters and digits")
        record.update({"pin_space": pin_space, "pin_bindings": bindings})
        self.add_leaf(_Leaf(device, nets, prefix, instance.get("component_id"), record))

    def explicit_device(self, instance: dict[str, Any], scope: tuple[str, ...]) -> None:
        device = CommonDevice.from_dict(instance["device"]).to_dict()
        nets = [self.remember(_external(pin["net"], "device.pin.net")) for pin in device["pins"]]
        for pin in device["pins"]:
            pin["net"] = None
        record = {"instance_id": "/".join(scope), "scope": list(scope), "kind": "explicit_device", "pin_space": "explicit", "parameters": {}, "pin_bindings": []}
        self.records.append(record)
        self.diagnostics.append({"instance_id": record["instance_id"], "code": "explicit_device:no_template_validation", "status": "unresolved", "message": "Explicit source pins are preserved; no template electrical or physical validation was performed."})
        self.add_leaf(_Leaf(device, nets, "X", instance.get("component_id"), record))

    def add_leaf(self, leaf: _Leaf) -> None:
        self.pin_count += len(leaf.device["pins"])
        if self.pin_count > MAX_PINS:
            raise CommonDeviceError("Circuit exceeds total pin limit")
        if leaf.component_id is not None and (not isinstance(leaf.component_id, str) or not _COMPONENT_ID.fullmatch(leaf.component_id)):
            raise CommonDeviceError("component_id must be gge followed by digits")
        self.leaves.append(leaf)

    def finish(self, plan: dict[str, Any]) -> CompilationResult:
        used_names: set[str] = set()
        used_ids: set[str] = set()
        for leaf in self.leaves:
            name = leaf.device["designator"]
            if name is not None:
                _text(name, "designator")
                if name in used_names:
                    raise CommonDeviceError(f"Duplicate designator {name!r}")
                used_names.add(name)
            if leaf.component_id is not None:
                if leaf.component_id in used_ids:
                    raise CommonDeviceError(f"Duplicate component_id {leaf.component_id!r}")
                used_ids.add(leaf.component_id)

        labels = set(self.external_labels)
        local_names: dict[tuple[str, ...], str] = {}
        for reference in sorted(self.local_refs):
            suffix_parts = (*reference[1:-1], "__port__", reference[-1]) if reference[0] == "local_port" else reference[1:]
            base = "__local__/" + "/".join(suffix_parts)
            name, suffix = base, 2
            while name in labels:
                name, suffix = f"{base}#{suffix}", suffix + 1
            labels.add(name)
            local_names[reference] = name

        def label(reference: NetRef) -> str | None:
            return None if reference is None else reference[1] if reference[0] == "external" else local_names[reference]

        netlist: dict[str, dict[str, Any]] = {}
        next_id = 1
        counters: dict[str, int] = {}
        for leaf in self.leaves:
            if leaf.device["designator"] is None:
                index = counters.get(leaf.prefix, 1)
                while f"{leaf.prefix}{index}" in used_names:
                    index += 1
                leaf.device["designator"] = f"{leaf.prefix}{index}"
                counters[leaf.prefix] = index + 1
                used_names.add(leaf.device["designator"])
            component_id = leaf.component_id
            if component_id is None:
                while f"gge{next_id}" in used_ids:
                    next_id += 1
                component_id = f"gge{next_id}"
                used_ids.add(component_id)
                next_id += 1
            for pin, reference in zip(leaf.device["pins"], leaf.nets):
                pin["net"] = label(reference)
            netlist[component_id] = CommonDevice.from_dict(leaf.device).to_dict()
            leaf.record.update({"component_id": component_id, "designator": leaf.device["designator"]})

        for record in self.records:
            if "_connections" in record:
                record["connections"] = {endpoint: label(reference) for endpoint, reference in record.pop("_connections").items()}
            if "_internal_port_connections" in record:
                record["internal_port_connections"] = {endpoint: label(reference) for endpoint, reference in record.pop("_internal_port_connections").items()}
        manifest = {
            "schema_version": "1.0",
            "compiler_version": "0.1.0",
            "status": "logical_ready",
            "input_sha256": _digest(plan),
            "lookup_policy": "no_database",
            "validation": {"connectivity": "complete", "physical": "unresolved", "electrical": "unresolved"},
            "counts": {"expanded_instances": self.instance_count, "components": len(netlist), "pins": self.pin_count},
            "instances": self.records,
            "local_nets": [{"scope": list(reference[1:-1]), "kind": "unconnected_composite_port" if reference[0] == "local_port" else "local_net", "local_name": reference[-1], "net": name} for reference, name in local_names.items()],
            "diagnostics": self.diagnostics,
        }
        return CompilationResult(netlist=netlist, manifest=manifest)


def compile_circuit(plan: Mapping[str, Any], registry: TemplateRegistry | None = None) -> CompilationResult:
    """Compile a strict plan without querying parts, guessing pins, or merging local scopes."""
    copied = _json_copy(plan)
    _fields(copied, {"schema_version", "instances"}, set(), "plan")
    if copied["schema_version"] != "1.0":
        raise CommonDeviceError("plan.schema_version must be '1.0'")
    instances = copied["instances"]
    if not isinstance(instances, list) or len(instances) > MAX_INSTANCES:
        raise CommonDeviceError("plan.instances must be an array within the expansion limit")
    compiler = _Compiler(registry if registry is not None else TemplateRegistry.load_builtin())
    seen: set[str] = set()
    for instance in instances:
        if not isinstance(instance, dict):
            raise CommonDeviceError("plan.instances: every instance must be an object")
        name = _instance_id(instance.get("instance_id"), "instance_id")
        if name in seen:
            raise CommonDeviceError(f"Duplicate instance_id {name!r}")
        seen.add(name)
        compiler.expand(instance, (name,))
    return compiler.finish(copied)
