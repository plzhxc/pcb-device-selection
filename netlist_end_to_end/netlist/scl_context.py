"""Frozen device/port context and connection state for the SCL compiler."""

from __future__ import annotations

import ast
from collections import defaultdict
from copy import deepcopy
import keyword
from typing import Any, Mapping

from validation_platform.netlist_graph import NetlistFormatError, parse_netlist_graph
from .scl_model import (
    Endpoint, MAX_PINS, MAX_PORT_BITS, SCHEMA, SCLSource, _DEVICE_FIELDS,
    _FUNCTIONS, _Invalid, _endpoint, _fields, _hash, _text,
)


class _CompilerContext:
    def __init__(self) -> None:
        self.devices: dict[tuple[str, str], dict] = {}
        self.pins: dict[Endpoint, bool] = {}
        self.ports: dict[Endpoint, bool] = {}
        self.modules: set[str] = set()
        self.domains: dict[str, dict] = {}
        self.domain_references: list[tuple[str, SCLSource, ast.AST]] = []
        self.parents: dict[Endpoint, Endpoint] = {}
        self.operations: list[dict] = []
        self.local_edges: dict[str, list[tuple[Endpoint, Endpoint]]] = defaultdict(list)
        self.top_edges: list[tuple[Endpoint, Endpoint]] = []
        self.diagnostics: list[dict] = []
        self.source_records: list[dict] = []
        self.context_hash: str | None = None
        self.revision: str | None = None
        self.static_environment = None

    def diagnostic(self, code: str, message: str, *, source: SCLSource | None = None,
                   node: ast.AST | None = None, endpoint: Endpoint | None = None) -> None:
        item: dict[str, Any] = {"severity": "error", "code": code, "message": message}
        if source is not None:
            item.update(source=source.name, module=source.module,
                        line=getattr(node, "lineno", 1), column=getattr(node, "col_offset", 0) + 1)
        if endpoint is not None:
            item["endpoint"] = _endpoint(endpoint)
        self.diagnostics.append(item)

    def root(self, endpoint: Endpoint) -> Endpoint:
        self.parents.setdefault(endpoint, endpoint)
        current = endpoint
        while self.parents[current] != current:
            current = self.parents[current]
        while self.parents[endpoint] != endpoint:
            parent = self.parents[endpoint]
            self.parents[endpoint] = current
            endpoint = parent
        return current

    def join(self, a: Endpoint, b: Endpoint) -> None:
        a, b = self.root(a), self.root(b)
        if a != b:
            self.parents[max(a, b)] = min(a, b)

    def load_context(self, context: Mapping[str, Any]) -> None:
        _fields(context, {"schema_version", "revision", "modules"}, {"static_environment"}, "context")
        if "static_environment" in context:
            from validation_platform.static_voltages import validate_environment
            try:
                self.static_environment = validate_environment(context["static_environment"])
            except (ValueError, TypeError) as exc:
                raise _Invalid("INVALID_CONTEXT", str(exc)) from exc
        if context["schema_version"] != SCHEMA:
            raise _Invalid("INVALID_CONTEXT", f"schema_version must be {SCHEMA!r}")
        self.context_hash = _hash(context)
        self.revision = _text(context["revision"], "revision")
        modules = context["modules"]
        if not isinstance(modules, Mapping) or not modules:
            raise _Invalid("INVALID_CONTEXT", "modules must be a nonempty object")
        for module, raw in modules.items():
            _text(module, "module id")
            self.modules.add(module)
            _fields(raw, {"devices", "ports"}, set(), f"modules.{module}")
            if not isinstance(raw["devices"], Mapping) or not isinstance(raw["ports"], Mapping):
                raise _Invalid("INVALID_CONTEXT", f"{module}: devices and ports must be objects")
            check: dict[str, dict] = {}
            for alias, device in raw["devices"].items():
                if (not isinstance(alias, str) or not alias.isidentifier() or keyword.iskeyword(alias)
                        or alias.startswith("_") or alias in _FUNCTIONS):
                    raise _Invalid("INVALID_CONTEXT", f"{module}: invalid or reserved device alias {alias!r}")
                _fields(device, _DEVICE_FIELDS, set(), f"{module}.{alias}")
                if not isinstance(device["pins"], list):
                    raise _Invalid("INVALID_CONTEXT", f"{module}.{alias}.pins must be an array")
                copied = deepcopy(dict(device))
                copied["pins"] = []
                for pin in device["pins"]:
                    _fields(pin, {"id", "name"}, {"unconnected"}, f"{module}.{alias}.pins")
                    _text(pin["id"], "pin id")
                    allowed = pin.get("unconnected", False)
                    if not isinstance(allowed, bool):
                        raise _Invalid("INVALID_CONTEXT", "pin.unconnected must be a boolean")
                    endpoint = ("pin", module, alias, pin["id"])
                    if endpoint in self.pins:
                        raise _Invalid("INVALID_CONTEXT", f"duplicate physical pin {endpoint!r}")
                    self.pins[endpoint] = allowed
                    self.root(endpoint)
                    copied["pins"].append({"id": pin["id"], "name": pin["name"], "net": None})
                    if len(self.pins) > MAX_PINS:
                        raise _Invalid("RESOURCE_LIMIT", "too many context pins")
                copied["pins"].sort(key=lambda p: p["id"])
                check[f"gge{len(check) + 1}"] = copied
                self.devices[(module, alias)] = copied
            try:
                parse_netlist_graph(check, source=module)
            except NetlistFormatError as exc:
                raise _Invalid("INVALID_CONTEXT", str(exc)) from exc
            for port, definition in raw["ports"].items():
                _text(port, "port id")
                _fields(definition, {"width", "external"}, set(), f"{module}.ports.{port}")
                width = definition["width"]
                if type(width) is not int or not 1 <= width <= MAX_PORT_BITS or type(definition["external"]) is not bool:
                    raise _Invalid("INVALID_CONTEXT", "port width must be a positive bounded integer; external must be boolean")
                if len(self.ports) + width > MAX_PORT_BITS:
                    raise _Invalid("RESOURCE_LIMIT", "too many context port bits")
                for bit in range(width):
                    endpoint = ("port", module, port, str(bit))
                    self.ports[endpoint] = definition["external"]
                    self.root(endpoint)
