"""Adapter for the EasyEDA PCB extraction JSON supplied with benchmark cases."""

from __future__ import annotations

import json
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


FORMAT = "easyeda_pcb_extract_v1"


def _read_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"PCB extraction root must be an object: {path}")
    return payload


def is_pcb_extract_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".json":
        return False
    try:
        payload = _read_payload(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    components = payload.get("components")
    if not isinstance(components, dict):
        return False
    # These fields distinguish a PCB extraction from generated/case netlists.
    if "boardOutline" not in payload or "footprints" not in payload:
        return False
    return all(
        isinstance(component, dict) and isinstance(component.get("nets"), dict)
        for component in components.values()
    )


def pcb_extract_files(path: Path) -> list[Path]:
    return [candidate for candidate in direct_files(path, "*.json") if is_pcb_extract_file(candidate)]


def parse_pcb_extract(path: str | Path) -> tuple[UnifiedNetlist, bool, tuple[str, ...]]:
    source = Path(path)
    payload = _read_payload(source)
    if not is_pcb_extract_file(source):
        raise ValueError(f"not an {FORMAT} document: {source}")

    components = payload["components"]
    schematic_info = payload.get("schematicInfo")
    schematic_info = schematic_info if isinstance(schematic_info, dict) else {}
    warnings: list[str] = []
    invalid_net_records = 0
    output: UnifiedNetlist = {}

    for component_id, component in components.items():
        designator = str(component_id)
        raw_nets = component.get("nets") or {}
        pins: dict[str, str] = {}
        for pin_token, net_name in raw_nets.items():
            if isinstance(net_name, (str, int, float)):
                normalized = str(net_name).strip()
                if normalized:
                    pins[str(pin_token)] = normalized
            elif net_name is not None:
                invalid_net_records += 1

        device_name = str(component.get("name") or "")
        value = str(component.get("value") or "")
        footprint = str(component.get("footprint") or "")
        props_for_type = {
            "designator": designator,
            "deviceName": device_name,
            "value": value,
            "footprint": footprint,
        }
        props: dict[str, Any] = {
            "designator": designator,
            "deviceName": device_name,
            "supplierPart": "",
            "value": value,
            "deviceType": canonical_device_type(props_for_type, pin_count=len(pins)),
            "deviceNotes": "",
            "footprint": footprint,
            "_orig_gge": designator,
            "_source_format": FORMAT,
        }
        schematic = schematic_info.get(component_id)
        if isinstance(schematic, dict):
            if schematic.get("page") is not None:
                props["_schematic_page"] = schematic["page"]
            if schematic.get("zone") is not None:
                props["_schematic_zone"] = schematic["zone"]
        output[f"pcb__{designator}"] = {"props": props, "pins": pins}

    has_schematic_context = bool(schematic_info)
    if not has_schematic_context:
        warnings.append("schematic_context_unavailable:schematicInfo is empty")
    if invalid_net_records:
        warnings.append(f"invalid_net_records_ignored:{invalid_net_records}")
    if components and not any(entry["pins"] for entry in output.values()):
        warnings.append("no_connected_pins:all component net mappings are empty")
    return output, has_schematic_context, tuple(warnings)


class EasyEdaPcbExtractAdapter:
    format_name = FORMAT
    aliases = ("easyeda_pcb", "pcb_extract")
    adapter_version = "1"

    def can_load(self, path: Path) -> bool:
        return bool(pcb_extract_files(path))

    def load(self, path: Path) -> ReferenceLoadResult:
        sources = pcb_extract_files(path)
        if not sources:
            raise ValueError(f"no {FORMAT} file found at {path}")
        if len(sources) > 1:
            names = ", ".join(source.name for source in sources)
            raise ValueError(
                f"multiple {FORMAT} files found ({names}); pass one asset path explicitly"
            )
        source = sources[0]
        netlist, schematic_context, warnings = parse_pcb_extract(source)
        return ReferenceLoadResult(
            netlist=netlist,
            capability=ReferenceCapability(
                components=True,
                values=True,
                connectivity=True,
                pin_identity="source_token",
                schematic_context=schematic_context,
            ),
            provenance=ReferenceProvenance(
                format=self.format_name,
                adapter=type(self).__name__,
                adapter_version=self.adapter_version,
                sources=(source_provenance(source),),
            ),
            warnings=warnings,
        )
