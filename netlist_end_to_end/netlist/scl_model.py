"""SCL source/result types, limits and strict value helpers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping

SCHEMA = "scl.connectivity.v1"
MAX_SOURCE_BYTES = 2_000_000
MAX_AST_NODES = 200_000
MAX_PINS = 100_000
MAX_PORT_BITS = 100_000
_FUNCTIONS = {"voltage_domain", "voltage_domain_ref", "pin", "module_port", "connect"}
_DEVICE_FIELDS = {"name", "designator", "type", "is_abstract", "value", "description", "id", "pins"}
Endpoint = tuple[str, ...]


@dataclass(frozen=True)
class SCLSource:
    name: str
    module: str | None
    text: str


@dataclass(frozen=True)
class SCLResult:
    netlist: dict[str, dict[str, Any]] | None
    manifest: dict[str, Any]
    diagnostics: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return self.netlist is not None and not any(d["severity"] == "error" for d in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return deepcopy({"ok": self.ok, "netlist": self.netlist,
                         "manifest": self.manifest, "diagnostics": self.diagnostics})


class _Invalid(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise _Invalid("INVALID_CONTEXT", f"{path}: expected a nonempty string without NUL")
    return value


def _fields(value: Any, required: set[str], optional: set[str], path: str) -> Mapping:
    if not isinstance(value, Mapping) or set(value) - required - optional or required - set(value):
        raise _Invalid("INVALID_CONTEXT", f"{path}: expected fields {sorted(required)}; optional {sorted(optional)}")
    return value


def _endpoint(value: Endpoint) -> dict[str, Any]:
    if value[0] == "pin":
        return {"kind": "pin", "module": value[1], "device": value[2], "pin": value[3]}
    if value[0] == "port":
        return {"kind": "port", "module": value[1], "port": value[2], "bit": int(value[3])}
    return {"kind": "voltage_domain", "id": value[1]}


@dataclass(frozen=True)
class _Device:
    module: str
    alias: str
