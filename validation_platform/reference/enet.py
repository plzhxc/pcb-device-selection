"""Adapter for EasyEDA ``.enet`` schematic exports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from validation_platform.reference.common import direct_files, source_provenance
from validation_platform.reference.models import (
    ReferenceCapability,
    ReferenceLoadResult,
    ReferenceProvenance,
    UnifiedNetlist,
)
from ..taxonomy import canonical_device_type


FORMAT = "easyeda_enet_v2"


def classify_device_type(props: dict[str, Any], *, pin_count: int = 0) -> str:
    return canonical_device_type(props, pin_count=pin_count)


def is_enet_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".enet":
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and isinstance(payload.get("components"), dict)


def enet_files(path: Path) -> list[Path]:
    return [candidate for candidate in direct_files(path, "*.enet") if is_enet_file(candidate)]


def parse_enet_sheet(path: str | Path) -> UnifiedNetlist:
    """Parse one sheet using the historical flat-netlist key and prop schema."""

    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    components = payload.get("components") or {}
    if not isinstance(components, dict):
        raise ValueError(f"invalid EasyEDA .enet components object: {source}")

    sheet = source.name.removeprefix("Netlist_").removesuffix(".enet")
    sheet = re.sub(r"_\d{4}-\d{2}-\d{2}$", "", sheet)
    output: UnifiedNetlist = {}
    for component_id, component in components.items():
        if not isinstance(component, dict):
            continue
        source_props = component.get("props") or {}
        if not isinstance(source_props, dict):
            source_props = {}
        pins: dict[str, str] = {}
        pin_info = component.get("pinInfoMap") or {}
        if isinstance(pin_info, dict):
            for pin_id, pin in pin_info.items():
                if not isinstance(pin, dict):
                    continue
                net = pin.get("net")
                pin_number = pin.get("number") or pin_id
                if net:
                    pins[str(pin_number)] = str(net)
        key = f"{sheet}__{component_id}"
        physical_type = classify_device_type(source_props, pin_count=len(pins))
        output[key] = {
            "props": {
                "designator": source_props.get("Designator", ""),
                "deviceName": (
                    source_props.get("Manufacturer Part")
                    or source_props.get("DeviceName")
                    or ""
                ),
                "supplierPart": source_props.get("Supplier Part") or "",
                "value": source_props.get("Value") or "",
                "deviceType": physical_type,
                "_physical_device_type": physical_type,
                "deviceNotes": source_props.get("Description") or "",
                "_sheet": sheet,
                "_orig_gge": component_id,
            },
            "pins": pins,
        }
    return output


class EasyEdaEnetAdapter:
    format_name = FORMAT
    aliases = ("enet", "easyeda_enet")
    adapter_version = "1"

    def can_load(self, path: Path) -> bool:
        return bool(enet_files(path))

    def load(self, path: Path) -> ReferenceLoadResult:
        sources = enet_files(path)
        if not sources:
            raise ValueError(f"no valid EasyEDA .enet files found under {path}")
        combined: UnifiedNetlist = {}
        warnings: list[str] = []
        for source in sources:
            parsed = parse_enet_sheet(source)
            if not parsed:
                warnings.append(f"empty_components:{source.name}")
            combined.update(parsed)
        if not any(entry.get("pins") for entry in combined.values()):
            warnings.append("no_connected_pins:all .enet components have empty pin mappings")
        return ReferenceLoadResult(
            netlist=combined,
            capability=ReferenceCapability(
                components=True,
                values=True,
                connectivity=True,
                pin_identity="number",
                schematic_context=True,
            ),
            provenance=ReferenceProvenance(
                format=self.format_name,
                adapter=type(self).__name__,
                adapter_version=self.adapter_version,
                sources=tuple(source_provenance(source) for source in sources),
            ),
            warnings=tuple(warnings),
        )
