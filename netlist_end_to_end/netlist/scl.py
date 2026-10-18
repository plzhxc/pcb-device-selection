"""Compile connection-only SCL without executing Python or inventing devices.

The context owns every device, value, physical pin and declared open-pin state.
SCL can only declare voltage-domain metadata and explicit wire connections.
"""


from __future__ import annotations

import ast
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import math
from typing import Any, Mapping, Sequence

from validation_platform.netlist_graph import NetlistFormatError, parse_netlist_graph
from .scl_context import _CompilerContext
from .scl_model import (
    Endpoint, MAX_AST_NODES, MAX_PINS as MAX_PINS, MAX_PORT_BITS as MAX_PORT_BITS,
    MAX_SOURCE_BYTES, SCHEMA, SCLResult, SCLSource, _DEVICE_FIELDS as _DEVICE_FIELDS,
    _FUNCTIONS, _Device, _Invalid, _canonical, _endpoint, _fields as _fields,
    _hash, _text as _text,
)


class _Compiler(_CompilerContext):
    @staticmethod
    def literal(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant) and type(node.value) in {str, int, float, type(None)}:
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = _Compiler.literal(node.operand)
            if type(value) in {int, float}:
                return -value if isinstance(node.op, ast.USub) else value
        raise _Invalid("INVALID_LITERAL", "only literal strings, finite numbers and None are allowed")

    @staticmethod
    def signature(call: ast.Call, positional: int, required_keywords: set[str], optional_keywords: set[str] = frozenset()) -> dict[str, ast.AST]:
        if len(call.args) != positional or any(isinstance(arg, ast.Starred) for arg in call.args):
            raise _Invalid("INVALID_SIGNATURE", f"expected {positional} explicit positional arguments")
        names = [kw.arg for kw in call.keywords]
        if (None in names or len(names) != len(set(names)) or required_keywords - set(names)
                or set(names) - required_keywords - optional_keywords):
            raise _Invalid("INVALID_SIGNATURE", f"expected keywords {sorted(required_keywords)}; optional {sorted(optional_keywords)}")
        return {kw.arg: kw.value for kw in call.keywords}

    def expression(self, node: ast.AST, env: dict, source: SCLSource) -> Endpoint | _Device:
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise _Invalid("UNKNOWN_HANDLE", f"unknown handle {node.id!r}")
            return env[node.id]
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            raise _Invalid("FORBIDDEN_EXPRESSION", "only declared handles and pin/module_port endpoint calls are allowed")
        name = node.func.id
        if name == "voltage_domain_ref":
            self.signature(node, 1, set())
            if source.module is None:
                raise _Invalid("INVALID_DOMAIN_REFERENCE_SCOPE", "voltage_domain_ref is a local endpoint; top-level sources may connect only cross-module ports")
            domain_id = self.literal(node.args[0])
            if not isinstance(domain_id, str) or not domain_id.strip() or "\x00" in domain_id:
                raise _Invalid("INVALID_DOMAIN_REFERENCE", "voltage_domain_ref requires one nonempty literal domain ID without NUL")
            # Actual definitions can occur in a later fragment or statement.
            # This records a reference only: no definition, metadata or wire.
            self.domain_references.append((domain_id, source, node))
            self.operation("voltage_domain_ref", source, node, domain_id=domain_id)
            return ("domain", domain_id)
        if name == "pin":
            self.signature(node, 2, set())
            device = self.expression(node.args[0], env, source)
            number = self.literal(node.args[1])
            if not isinstance(device, _Device) or not isinstance(number, str):
                raise _Invalid("INVALID_PIN", "pin requires a local device handle and a string physical pin ID")
            endpoint = ("pin", device.module, device.alias, number)
            if endpoint not in self.pins:
                raise _Invalid("UNKNOWN_PIN", f"{device.module}.{device.alias} has no physical pin {number!r}")
            return endpoint
        if name == "module_port":
            kwargs = self.signature(node, 2, set(), {"bit"})
            module, port = (self.literal(arg) for arg in node.args)
            bit = self.literal(kwargs["bit"]) if "bit" in kwargs else 0
            if not isinstance(module, str) or not isinstance(port, str) or type(bit) is not int:
                raise _Invalid("INVALID_PORT", "module_port requires string module/port IDs and integer bit")
            if source.module is not None and module != source.module:
                raise _Invalid("CROSS_MODULE_ACCESS", "local source may reference only its own module ports")
            endpoint = ("port", module, port, str(bit))
            if endpoint not in self.ports:
                raise _Invalid("UNKNOWN_PORT", f"unknown module port {module}.{port}[{bit}]")
            return endpoint
        raise _Invalid("FORBIDDEN_EXPRESSION", f"unsupported endpoint function {name!r}")

    def domain(self, call: ast.Call, source: SCLSource, statement: ast.AST) -> Endpoint:
        kwargs = self.signature(call, 1, {"voltage_v", "reference"}, {"profile_ref"})
        domain_id = self.literal(call.args[0])
        reference = self.literal(kwargs["reference"])
        voltage = self.literal(kwargs["voltage_v"])
        if not isinstance(domain_id, str) or not domain_id.strip() or "\x00" in domain_id:
            raise _Invalid("INVALID_DOMAIN", "voltage_domain ID must be a nonempty string without NUL")
        if reference is not None and (not isinstance(reference, str) or not reference.strip() or "\x00" in reference):
            raise _Invalid("INVALID_DOMAIN", "reference must be a nonempty domain ID or None")
        try:
            finite = voltage is None or type(voltage) in {int, float} and math.isfinite(voltage)
        except OverflowError:
            finite = False
        if not finite:
            raise _Invalid("INVALID_DOMAIN", "voltage_v must be a finite number or None")
        if reference is None and voltage not in (0, None):
            raise _Invalid("INVALID_DOMAIN", "a nonzero voltage requires an explicit reference domain ID")
        record = {"id": domain_id, "voltage_v": voltage, "reference": reference}
        if "profile_ref" in kwargs:
            from validation_platform.static_voltages import voltage_domain_profile_valid
            record["profile_ref"] = self.literal(kwargs["profile_ref"])
            if not voltage_domain_profile_valid(record, self.static_environment):
                raise _Invalid("INVALID_DOMAIN", "profile_ref requires a frozen source, explicit reference and voltage_v=None")
        previous = self.domains.get(domain_id)
        if previous is not None and previous != record:
            raise _Invalid("DOMAIN_DEFINITION_CONFLICT", f"conflicting definitions of voltage domain {domain_id!r}")
        self.domains[domain_id] = record
        endpoint = ("domain", domain_id)
        self.root(endpoint)
        self.operation("voltage_domain", source, statement, domain=record)
        return endpoint

    def operation(self, kind: str, source: SCLSource, node: ast.AST, **data: Any) -> None:
        self.operations.append({"kind": kind, "source": source.name, "module": source.module,
                                "line": node.lineno, "column": node.col_offset + 1, **data})

    def connection(self, a: Endpoint | _Device, b: Endpoint | _Device,
                   source: SCLSource, node: ast.AST) -> None:
        if isinstance(a, _Device) or isinstance(b, _Device):
            raise _Invalid("MISSING_PIN", "connect requires two explicit pin, port or domain endpoints")
        if a == b:
            raise _Invalid("SELF_CONNECTION", "an endpoint cannot connect to itself")
        if source.module is None:
            if a[0] != "port" or b[0] != "port" or a[1] == b[1]:
                raise _Invalid("INVALID_TOP_CONNECTION", "top-level connections require ports from two different modules")
            self.top_edges.append((a, b))
        else:
            self.local_edges[source.module].append((a, b))
        self.join(a, b)
        self.operation("connect", source, node, endpoints=[_endpoint(e) for e in sorted((a, b))])

    def statement(self, node: ast.stmt, env: dict, source: SCLSource) -> None:
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                raise _Invalid("FORBIDDEN_ASSIGNMENT", "assignment must bind one new handle")
            name = node.targets[0].id
            if name in env or name in _FUNCTIONS or name.startswith("_"):
                raise _Invalid("DUPLICATE_HANDLE", f"handle {name!r} is already bound or reserved")
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "voltage_domain":
                value = self.domain(node.value, source, node)
            else:
                value = self.expression(node.value, env, source)
                if isinstance(value, _Device):
                    raise _Invalid("FORBIDDEN_ASSIGNMENT", "device aliases are fixed by the context")
            env[name] = value
            return
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            raise _Invalid("FORBIDDEN_STATEMENT", "only endpoint/domain bindings and connection calls are allowed")
        call = node.value
        if isinstance(call.func, ast.Name) and call.func.id == "connect":
            self.signature(call, 2, set())
            self.connection(*(self.expression(arg, env, source) for arg in call.args), source, node)
            return
        if isinstance(call.func, ast.Name) and call.func.id == "voltage_domain":
            self.domain(call, source, node)
            return
        if isinstance(call.func, ast.Attribute) and call.func.attr == "connect_device_pin":
            self.signature(call, 3, set())
            device = self.expression(call.func.value, env, source)
            target = self.expression(call.args[1], env, source)
            if not isinstance(device, _Device) or not isinstance(target, _Device):
                raise _Invalid("INVALID_DEVICE", "connect_device_pin requires two local device handles")
            ids = [self.literal(call.args[0]), self.literal(call.args[2])]
            if any(not isinstance(number, str) for number in ids):
                raise _Invalid("INVALID_PIN", "physical pin IDs must be strings")
            endpoints = [("pin", d.module, d.alias, number) for d, number in zip((device, target), ids)]
            if any(endpoint not in self.pins for endpoint in endpoints):
                raise _Invalid("UNKNOWN_PIN", "connect_device_pin references an unknown physical pin")
            self.connection(*endpoints, source, node)
            return
        raise _Invalid("FORBIDDEN_CALL", "call is not a permitted SCL connection operation")

    def source(self, source: SCLSource) -> None:
        if (not isinstance(source, SCLSource) or not isinstance(source.name, str) or not source.name.strip()
                or not isinstance(source.text, str) or source.module is not None and source.module not in self.modules):
            raise _Invalid("INVALID_SOURCE", "source requires a unique name, text and existing module or None")
        encoded = source.text.encode("utf-8")
        if len(encoded) > MAX_SOURCE_BYTES:
            raise _Invalid("RESOURCE_LIMIT", "SCL source exceeds the byte limit")
        self.source_records.append({"name": source.name, "module": source.module,
                                    "sha256": hashlib.sha256(encoded).hexdigest()})
        try:
            tree = ast.parse(source.text, filename=source.name, mode="exec")
        except (SyntaxError, RecursionError, ValueError) as exc:
            self.diagnostic("SYNTAX_ERROR", str(exc), source=source)
            return
        if sum(1 for _ in ast.walk(tree)) > MAX_AST_NODES:
            raise _Invalid("RESOURCE_LIMIT", "SCL source exceeds the AST node limit")
        env = {alias: _Device(module, alias) for module, alias in self.devices if module == source.module}
        for node in tree.body:
            try:
                self.statement(node, env, source)
            except (_Invalid, RecursionError) as exc:
                self.diagnostic(exc.code if isinstance(exc, _Invalid) else "RESOURCE_LIMIT", str(exc), source=source, node=node)

    def validate_connections(self) -> dict[Endpoint, list[Endpoint]]:
        for domain_id, source, node in self.domain_references:
            if domain_id not in self.domains:
                self.diagnostic("UNKNOWN_VOLTAGE_DOMAIN", f"voltage_domain_ref refers to undeclared domain {domain_id!r}",
                                source=source, node=node)
        for domain_id, definition in sorted(self.domains.items()):
            reference = definition["reference"]
            if reference is not None and reference not in self.domains:
                self.diagnostic("UNKNOWN_REFERENCE_DOMAIN", f"{domain_id!r} references undeclared domain {reference!r}")
        completed: set[str] = set()
        for domain_id in sorted(self.domains):
            trail: set[str] = set()
            current: str | None = domain_id
            while current in self.domains and current not in completed:
                if current in trail:
                    self.diagnostic("DOMAIN_REFERENCE_CYCLE", f"voltage reference cycle includes {current!r}")
                    break
                trail.add(current)
                current = self.domains[current]["reference"]
            completed.update(trail)
        groups: dict[Endpoint, list[Endpoint]] = defaultdict(list)
        for endpoint in sorted(self.parents):
            groups[self.root(endpoint)].append(endpoint)
        for members in groups.values():
            domains = [e[1] for e in members if e[0] == "domain"]
            if len(domains) > 1:
                self.diagnostic("POWER_DOMAIN_CONFLICT", f"distinct voltage domains are shorted: {domains}")
            pins = [e for e in members if e[0] == "pin"]
            external = any(self.ports.get(e, False) for e in members if e[0] == "port")
            satisfied = len(pins) >= 2 or bool(domains) or external
            if not satisfied:
                for endpoint in pins:
                    if not self.pins[endpoint]:
                        self.diagnostic("UNCONNECTED_REQUIRED_PIN", "required pin lacks another physical pin, domain or external port", endpoint=endpoint)
        top_ports = {p for edge in self.top_edges for p in edge}
        for module in sorted(self.modules):
            adjacency: dict[Endpoint, set[Endpoint]] = defaultdict(set)
            for a, b in self.local_edges[module]:
                adjacency[a].add(b)
                adjacency[b].add(a)
            visited: set[Endpoint] = set()
            local_bound: set[Endpoint] = set()
            for start in adjacency:
                if start in visited:
                    continue
                todo, connected = [start], set()
                while todo:
                    current = todo.pop()
                    if current in connected:
                        continue
                    connected.add(current)
                    todo.extend(adjacency[current] - connected)
                visited.update(connected)
                if any(p[0] == "pin" and p[1] == module for p in connected):
                    local_bound.update(p for p in connected if p[0] == "port")
            for endpoint, external in self.ports.items():
                if endpoint[1] != module:
                    continue
                if endpoint not in local_bound:
                    self.diagnostic("UNBOUND_MODULE_PORT", "declared port bit has no local physical pin binding", endpoint=endpoint)
                if not external and endpoint not in top_ports:
                    self.diagnostic("UNCONNECTED_INTERNAL_PORT", "internal port bit lacks an explicit top-level connection", endpoint=endpoint)
        return groups

    def finish(self) -> SCLResult:
        groups = self.validate_connections()
        wire_endpoints = {tuple(e) for edges in self.local_edges.values() for edge in edges for e in edge}
        wire_endpoints.update(e for edge in self.top_edges for e in edge)
        networks, pin_nets = [], {}
        for members in sorted(groups.values()):
            physical = [p for p in members if p[0] == "pin"]
            if not physical or not any(p in wire_endpoints for p in members):
                continue
            net = f"N{len(networks) + 1:06d}"
            networks.append({"id": net, "members": [_endpoint(e) for e in members]})
            pin_nets.update((p, net) for p in physical)
        counts = Counter(d["designator"] for d in self.devices.values())
        used: set[str] = set()
        netlist, instances = {}, []
        for (module, alias), device in sorted(self.devices.items()):
            gge = f"gge{len(netlist) + 1}"
            copied = deepcopy(device)
            wanted = device["designator"] if counts[device["designator"]] == 1 else f"{module}__{device['designator']}"
            designator, suffix = wanted, 1
            while designator in used:
                suffix += 1
                designator = f"{wanted}__{suffix}"
            used.add(designator)
            copied["designator"] = designator
            for pin in copied["pins"]:
                pin["net"] = pin_nets.get(("pin", module, alias, pin["id"]))
            netlist[gge] = copied
            instances.append({"module": module, "device": alias, "component": gge,
                              "input_designator": device["designator"], "designator": designator,
                              "pin_space": "physical"})
        self.operations.sort(key=lambda row: (row["source"], row["line"], row["column"], row["kind"]))
        self.diagnostics.sort(key=_canonical)
        manifest = {"schema_version": SCHEMA, "revision": self.revision,
                    "context_sha256": self.context_hash,
                    **({"static_environment_sha256": _hash(self.static_environment)} if self.static_environment is not None else {}),
                    "sources": sorted(self.source_records, key=lambda row: row["name"]),
                    "instances": instances, "networks": networks,
                    "voltage_domains": [self.domains[key] for key in sorted(self.domains)],
                    "operations": self.operations,
                    "unconnected_pins": [_endpoint(p) for p in sorted(self.pins) if p not in pin_nets],
                    "status": "invalid" if self.diagnostics else "compiled",
                    "checks": {"physical_pin_coverage": not any(d["code"] == "UNCONNECTED_REQUIRED_PIN" for d in self.diagnostics),
                               "voltage_metadata": "unresolved" if any(d["voltage_v"] is None for d in self.domains.values()) else "defined" if self.domains else "not_applicable",
                               "electrical_role_validation": "not_performed", "datasheet_constraints": "not_performed"}}
        if not self.diagnostics:
            try:
                parse_netlist_graph(netlist, source="scl_export")
            except NetlistFormatError as exc:
                self.diagnostic("INVALID_EXPORT", str(exc))
                manifest["status"] = "invalid"
        if not self.diagnostics:
            manifest["netlist_sha256"] = _hash(netlist)
        return SCLResult(None if self.diagnostics else netlist, manifest, self.diagnostics)


def compile_scl(sources: Sequence[SCLSource], context: Mapping[str, Any]) -> SCLResult:
    """Compile all local fragments and the explicit top-level wires together.

    ``unconnected=True`` lets the compiler retain an open physical terminal;
    it does not establish manufacturer electrical permission or prohibit a
    connection. Missing/false requires a connection for compilation. Independent
    source checks determine whether each actual open state is electrically valid.
    Source order does not affect connectivity or exported numbering. A failed
    compile retains diagnostic provenance but never returns a ready netlist.
    """
    compiler = _Compiler()
    try:
        compiler.load_context(context)
    except (_Invalid, TypeError, ValueError, OverflowError, RecursionError) as exc:
        compiler.diagnostic(exc.code if isinstance(exc, _Invalid) else "INVALID_CONTEXT", str(exc))
        return SCLResult(None, {"schema_version": SCHEMA, "status": "invalid", "revision": compiler.revision,
                               "context_sha256": compiler.context_hash, "sources": [], "instances": [],
                               "networks": [], "operations": [], "voltage_domains": [],
                               "checks": {"physical_pin_coverage": False, "electrical_role_validation": "not_performed",
                                          "datasheet_constraints": "not_performed"}}, compiler.diagnostics)
    try:
        if not isinstance(sources, (tuple, list)) or not all(isinstance(s, SCLSource) for s in sources):
            raise _Invalid("INVALID_SOURCE", "sources must be a list or tuple of SCLSource")
        names = [s.name for s in sources]
        if any(not isinstance(name, str) or not name.strip() for name in names) or len(names) != len(set(names)):
            raise _Invalid("INVALID_SOURCE", "source names must be unique nonempty strings")
        for source in sorted(sources, key=lambda s: s.name):
            compiler.source(source)
    except (_Invalid, TypeError, ValueError, OverflowError, RecursionError) as exc:
        compiler.diagnostic(exc.code if isinstance(exc, _Invalid) else "INVALID_CONTEXT", str(exc))
    return compiler.finish()


__all__ = ["SCLSource", "SCLResult", "compile_scl"]
