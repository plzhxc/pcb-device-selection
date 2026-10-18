"""Lossless common-device values and the compiler's public result."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any, Mapping

from .errors import CommonDeviceError


def text(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise CommonDeviceError(f"{field}: expected a nonempty string" + (" or null" if nullable else ""))
    return value


@dataclass(frozen=True)
class Pin:
    id: str
    name: str | None
    net: str | None

    def __post_init__(self) -> None:
        text(self.id, "pin.id")
        text(self.name, "pin.name", nullable=True)
        text(self.net, "pin.net", nullable=True)
        if self.net == "null":
            raise CommonDeviceError('pin.net: use JSON null, not the string "null"')

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Pin:
        if not isinstance(value, Mapping) or set(value) != {"id", "name", "net"}:
            raise CommonDeviceError("pin: expected exactly id/name/net")
        return cls(value["id"], value["name"], value["net"])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "net": self.net}


@dataclass(frozen=True)
class CommonDevice:
    name: str | None
    designator: str
    type: str
    is_abstract: bool
    value: str | None
    description: str | None
    id: str | None
    pins: tuple[Pin, ...]

    def __post_init__(self) -> None:
        for key in ("name", "value", "description", "id"):
            text(getattr(self, key), f"device.{key}", nullable=True)
        text(self.designator, "device.designator")
        if not isinstance(self.type, str) or re.fullmatch(r"[a-z][a-z0-9_]*", self.type) is None or self.type in {"ic", "common_device"}:
            raise CommonDeviceError("device.type: expected a concrete non-IC category")
        if self.is_abstract is not True or self.id is not None:
            raise CommonDeviceError("abstract devices require is_abstract=true and id=null")
        if not isinstance(self.pins, (tuple, list)) or any(not isinstance(pin, Pin) for pin in self.pins):
            raise CommonDeviceError("device.pins: expected Pin instances")
        object.__setattr__(self, "pins", tuple(self.pins))
        ids = [pin.id for pin in self.pins]
        if len(ids) != len(set(ids)):
            raise CommonDeviceError("device.pins: duplicate pin id")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CommonDevice:
        fields = {"name", "designator", "type", "is_abstract", "value", "description", "id", "pins"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise CommonDeviceError("device: expected the eight cleaned-netlist fields")
        if not isinstance(value["pins"], list):
            raise CommonDeviceError("device.pins: expected an array")
        return cls(value["name"], value["designator"], value["type"], value["is_abstract"], value["value"], value["description"], value["id"],
                   tuple(Pin.from_dict(pin) for pin in value["pins"]))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "designator": self.designator, "type": self.type, "is_abstract": self.is_abstract,
                "value": self.value, "description": self.description, "id": self.id,
                "pins": [pin.to_dict() for pin in self.pins]}


@dataclass(frozen=True)
class CompilationResult:
    netlist: dict[str, dict[str, Any]]
    manifest: dict[str, Any]

    @property
    def core_devices(self) -> list[dict[str, Any]]:
        """This compiler never selects or constructs an IC."""
        return []

    def to_dict(self) -> dict[str, Any]:
        return deepcopy({"netlist": self.netlist, "manifest": self.manifest, "core_devices": []})
