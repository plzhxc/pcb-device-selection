"""Closed, bounded application-circuit candidates and explicit target inputs."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, field_validator, model_validator


Identifier = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]
Text = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
Detail = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
SHA256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Number = StrictInt | Annotated[float, Field(strict=True, allow_inf_nan=False)]
Scalar = Text | StrictBool | Number
ConditionPath = Annotated[str, StringConstraints(strict=True, pattern=r"^(configuration|requirements)\.[A-Za-z][A-Za-z0-9_.-]{0,100}$")]
EvidenceIDs = Annotated[list[Identifier], Field(min_length=1, max_length=32)]
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,100}$")


def canonical_digest(value: Any) -> str:
    """SHA-256 of canonical finite JSON; an integrity check, not a signature."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        value = dict(value)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


digest = canonical_digest


def _unique(values: list[str], name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must be unique")


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, validate_assignment=True)

    @model_validator(mode="before")
    @classmethod
    def bounded_strings(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for item in value.values():
                if isinstance(item, str) and (item != item.strip() or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in item)):
                    raise ValueError("Strings must have no surrounding whitespace or control characters")
        return value


class Device(ClosedModel):
    manufacturer: Text
    model: Text
    package: Text


class Document(ClosedModel):
    id: Identifier
    url: Annotated[str, StringConstraints(strict=True, min_length=8, max_length=2048, pattern=r"^https?://[^\s]+$")]
    sha256: SHA256


class Evidence(ClosedModel):
    id: Identifier
    document_id: Identifier
    page: Annotated[StrictInt, Field(ge=1, le=100000)]
    locator: Text
    kind: Literal["pin_table", "application_diagram", "mode_table", "operating_conditions", "text"]
    note: Detail


class Pin(ClosedModel):
    logical: Identifier
    physical: Text
    label: Text
    evidence: EvidenceIDs

    @field_validator("evidence")
    @classmethod
    def unique_evidence(cls, values: list[str]) -> list[str]:
        _unique(values, "pin evidence")
        return values


class Predicate(ClosedModel):
    field: ConditionPath
    op: Literal["eq", "in", "gte", "lte"]
    value: Scalar | Annotated[list[Scalar], Field(min_length=1, max_length=32)]

    @model_validator(mode="after")
    def appropriate_value(self) -> Predicate:
        if self.op == "in":
            if not isinstance(self.value, list):
                raise ValueError("in requires a nonempty bounded scalar list")
            tokens = [json.dumps(item, ensure_ascii=False, allow_nan=False) for item in self.value]
            _unique(tokens, "in alternatives")
        elif isinstance(self.value, list):
            raise ValueError("Only in accepts a list")
        elif self.op in {"gte", "lte"} and type(self.value) not in {int, float}:
            raise ValueError("Numeric comparisons require finite numbers, excluding booleans")
        return self


class Conditions(ClosedModel):
    all: Annotated[list[Predicate], Field(max_length=32)]


class Connection(ClosedModel):
    id: Identifier
    endpoints: Annotated[list[Annotated[str, StringConstraints(strict=True, pattern=r"^(pin|port):[A-Za-z][A-Za-z0-9_.-]{0,63}$")]], Field(min_length=2, max_length=512)]
    evidence: EvidenceIDs
    kind: Literal["required", "example"]

    @field_validator("endpoints", "evidence")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        _unique(values, "connection endpoints/evidence")
        return values


class Fragment(ClosedModel):
    schema_version: Literal["pcbagent.application_circuit.v1"] = "pcbagent.application_circuit.v1"
    id: Identifier
    title: Text
    description: Detail
    device: Device
    documents: Annotated[list[Document], Field(min_length=1, max_length=32)]
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=512)]
    pins: Annotated[list[Pin], Field(min_length=1, max_length=512)]
    ports: Annotated[list[Identifier], Field(max_length=256)]
    when: Conditions
    condition_evidence: Annotated[dict[ConditionPath, EvidenceIDs], Field(max_length=32)]
    connections: Annotated[list[Connection], Field(min_length=1, max_length=512)]
    unresolved: Annotated[list[Detail], Field(max_length=128)]

    @model_validator(mode="after")
    def references_and_topology(self) -> Fragment:
        for label, values in (
            ("document IDs", [row.id for row in self.documents]),
            ("evidence IDs", [row.id for row in self.evidence]),
            ("logical pins", [row.logical for row in self.pins]),
            ("physical pins", [row.physical for row in self.pins]),
            ("ports", self.ports),
            ("connection IDs", [row.id for row in self.connections]),
        ):
            _unique(values, label)
        documents = {row.id for row in self.documents}
        evidence = {row.id: row for row in self.evidence}
        if any(row.document_id not in documents for row in self.evidence):
            raise ValueError("Evidence references an unknown document")

        def require_evidence(refs: list[str]) -> None:
            _unique(refs, "evidence references")
            if set(refs) - evidence.keys():
                raise ValueError("Unknown evidence reference")

        for pin in self.pins:
            require_evidence(pin.evidence)
            if not any(evidence[ref].kind in {"pin_table", "text"} for ref in pin.evidence):
                raise ValueError("Every physical pin mapping needs pin_table or text evidence")
        fields = {predicate.field for predicate in self.when.all}
        if set(self.condition_evidence) != fields:
            raise ValueError("condition_evidence must exactly cover condition fields")
        for refs in self.condition_evidence.values():
            require_evidence(refs)
        expected = {f"pin:{row.logical}" for row in self.pins} | {f"port:{port}" for port in self.ports}
        seen: set[str] = set()
        for connection in self.connections:
            require_evidence(connection.evidence)
            endpoints = set(connection.endpoints)
            if endpoints - expected:
                raise ValueError("Connection references an undeclared endpoint")
            if endpoints & seen:
                raise ValueError("An endpoint cannot belong to multiple independent net classes")
            seen.update(endpoints)
        if seen != expected:
            raise ValueError("Every declared pin and port must belong to a connection")
        return self


class Target(ClosedModel):
    schema_version: Literal["pcbagent.application_target.v1"] = "pcbagent.application_target.v1"
    id: Identifier
    device: Device
    pin_catalog: Annotated[dict[Text, Text], Field(min_length=1, max_length=2048)]
    configuration: Annotated[dict[str, Scalar], Field(max_length=256)]
    requirements: Annotated[dict[str, Scalar], Field(max_length=256)]

    @field_validator("configuration", "requirements")
    @classmethod
    def valid_selection_keys(cls, values: dict[str, Any]) -> dict[str, Any]:
        if any(not _KEY.fullmatch(key) for key in values):
            raise ValueError("Selection keys must be bounded configuration/requirement identifiers")
        return values

    @field_validator("pin_catalog")
    @classmethod
    def valid_pin_text(cls, values: dict[str, str]) -> dict[str, str]:
        for text in (*values.keys(), *values.values()):
            if text != text.strip() or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text):
                raise ValueError("Physical pin IDs and labels must not contain surrounding whitespace or controls")
        return values


__all__ = ["Fragment", "Target", "Device", "Document", "Evidence", "Pin", "Predicate", "Conditions", "Connection", "canonical_digest", "digest"]
