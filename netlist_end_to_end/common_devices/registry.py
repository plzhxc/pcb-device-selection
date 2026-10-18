"""Versioned, offline template resources; no database or document-tree access."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from importlib.resources import files
import json
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any

from jsonschema import Draft7Validator
from jsonschema.validators import validator_for

from .errors import CommonDeviceError


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CommonDeviceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise CommonDeviceError("non-finite JSON number")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"} and (not isinstance(item, str) or not item.startswith("#/")):
                raise CommonDeviceError("only local JSON Schema references are supported")
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def _read(root, relative: str) -> tuple[dict, bytes]:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or not path.parts:
        raise CommonDeviceError(f"unsafe resource path: {relative}")
    target = root.joinpath(*path.parts)
    if isinstance(root, Path) and not target.resolve().is_relative_to(root.resolve()):
        raise CommonDeviceError(f"resource escapes registry: {relative}")
    try:
        raw = target.read_bytes()
        if len(raw) > 2_000_000:
            raise CommonDeviceError(f"resource too large: {relative}")
        value = json.loads(raw, object_pairs_hook=_pairs)
    except (OSError, ValueError, RecursionError) as exc:
        raise CommonDeviceError(f"cannot read {relative}: {exc}") from exc
    _finite(value)
    if not isinstance(value, dict):
        raise CommonDeviceError(f"resource must be an object: {relative}")
    return value, raw


class TemplateRegistry:
    """Load an explicit catalog and return defensive copies of definitions."""

    def __init__(self, root):
        self._root = root
        catalog, _ = _read(root, "catalog.json")
        schema, _ = _read(root, "template.schema.json")
        Draft7Validator.check_schema(schema)
        validator = Draft7Validator(schema)
        self._templates: dict[tuple[str, str], dict[str, Any]] = {}
        self._hashes: dict[tuple[str, str], str] = {}
        entries = catalog.get("templates")
        if catalog.get("schema_version") != "1.0" or not isinstance(entries, list) or len(entries) > 1000:
            raise CommonDeviceError("invalid template catalog")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("definition"), str):
                raise CommonDeviceError("invalid catalog entry")
            definition, raw = _read(root, entry["definition"])
            errors = sorted(validator.iter_errors(definition), key=lambda error: str(error.path))
            if errors:
                raise CommonDeviceError(f"{entry['definition']}: {errors[0].message}")
            key = (definition["template_id"], definition["version"])
            digest = sha256(raw).hexdigest()
            if key != (entry.get("template_id"), entry.get("version")) or digest != entry.get("definition_sha256"):
                raise CommonDeviceError(f"catalog identity/hash mismatch: {entry['definition']}")
            if key in self._templates:
                raise CommonDeviceError(f"duplicate template: {key}")
            parameter_schema = definition["parameters_schema"]
            try:
                validator_for(parameter_schema).check_schema(parameter_schema)
            except Exception as exc:
                raise CommonDeviceError(f"invalid parameters_schema: {key}: {exc}") from exc
            for field in ("ports", "rules"):
                ids = [row["id"] for row in definition[field]]
                if len(ids) != len(set(ids)):
                    raise CommonDeviceError(f"duplicate {field} id: {key}")
            for port in definition["ports"]:
                if "width_parameter" in port and port["width_parameter"] not in parameter_schema.get("properties", {}):
                    raise CommonDeviceError(f"unknown width parameter: {key}")
            if definition["kind"] == "primitive":
                spec = definition.get("value_spec")
                if spec and spec["parameter"] not in parameter_schema.get("properties", {}):
                    raise CommonDeviceError(f"unknown value parameter: {key}")
            self._templates[key] = definition
            self._hashes[key] = digest
        self._check_dependencies()

    @classmethod
    def load_builtin(cls) -> TemplateRegistry:
        return cls(files(__package__).joinpath("assets"))

    @classmethod
    def from_directory(cls, path: str | Path) -> TemplateRegistry:
        return cls(Path(path).resolve())

    def _check_dependencies(self) -> None:
        complete = set()

        def visit(key, stack):
            if key in stack:
                raise CommonDeviceError(f"recursive template dependency: {key}")
            if len(stack) >= 16:
                raise CommonDeviceError("template dependency depth exceeds 16")
            if key in complete:
                return
            definition = self._templates.get(key)
            if definition is None:
                raise CommonDeviceError(f"unknown template dependency: {key}")
            if definition["kind"] == "composite":
                children = definition["body"]["instances"]
                ids = [child["instance_id"] for child in children]
                if len(ids) != len(set(ids)):
                    raise CommonDeviceError(f"duplicate child instance: {key}")
                for child in children:
                    ref = child["template_ref"]
                    visit((ref["template_id"], ref["version"]), (*stack, key))
            complete.add(key)

        for key in self._templates:
            visit(key, ())

    def get(self, template_id: str, version: str) -> dict[str, Any]:
        if not isinstance(template_id, str) or not isinstance(version, str):
            raise CommonDeviceError("template_id and version must be strings")
        try:
            return deepcopy(self._templates[(template_id, version)])
        except KeyError as exc:
            raise CommonDeviceError(f"unknown template/version: {template_id}@{version}") from exc

    def definition_sha256(self, template_id: str, version: str) -> str:
        self.get(template_id, version)
        return self._hashes[(template_id, version)]

    def list_templates(self) -> list[dict[str, Any]]:
        return [{key: definition[key] for key in ("template_id", "version", "kind", "description")}
                for _, definition in sorted(self._templates.items())]

    def load_example(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
            raise CommonDeviceError("invalid example name")
        return _read(self._root, f"examples/{name}.json")[0]
