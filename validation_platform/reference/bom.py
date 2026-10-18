"""Component-only adapter for UTF-16 tab-delimited EasyEDA BOM exports."""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from validation_platform.reference.common import source_provenance
from validation_platform.reference.models import (
    ReferenceCapability,
    ReferenceLoadResult,
    ReferenceProvenance,
    UnifiedNetlist,
)
from ..taxonomy import canonical_device_type


FORMAT = "utf16_bom_v1"
_REQUIRED_COLUMNS = {"Quantity", "Designator"}


def _candidate_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in {".csv", ".tsv"} else []
    if not path.is_dir():
        return []
    return sorted(
        candidate
        for candidate in path.iterdir()
        if candidate.is_file() and candidate.suffix.lower() in {".csv", ".tsv"}
    )


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    text = path.read_text(encoding="utf-16")
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    fieldnames = [str(name or "") for name in (reader.fieldnames or [])]
    rows = [
        {str(key or ""): str(value or "") for key, value in row.items()}
        for row in reader
        if row
    ]
    return fieldnames, rows


def is_utf16_bom_file(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in {".csv", ".tsv"}:
        return False
    try:
        columns, _ = _read_rows(path)
    except (OSError, UnicodeError, csv.Error):
        return False
    return _REQUIRED_COLUMNS.issubset(columns) and bool(
        {"Comment", "Value", "Manufacturer Part", "Supplier Part"}.intersection(columns)
    )


def bom_files(path: Path) -> list[Path]:
    return [candidate for candidate in _candidate_files(path) if is_utf16_bom_file(candidate)]


def _quantity(raw: str) -> int | None:
    text = str(raw or "").strip()
    if not re.fullmatch(r"\d+", text):
        return None
    value = int(text)
    return value if value > 0 else None


def parse_utf16_bom(path: str | Path) -> tuple[UnifiedNetlist, tuple[str, ...]]:
    source = Path(path)
    if not is_utf16_bom_file(source):
        raise ValueError(f"not a {FORMAT} document: {source}")
    _, rows = _read_rows(source)
    warnings: list[str] = [
        "connectivity_unavailable:utf16_bom_v1 contains components and values but no pin-to-net data"
    ]
    output: UnifiedNetlist = {}
    for row_number, row in enumerate(rows, start=2):
        quantity = _quantity(row.get("Quantity", ""))
        if quantity is None:
            raise ValueError(f"invalid positive integer Quantity at BOM row {row_number}")
        designators = [
            item.strip()
            for item in re.split(r"[,;\n]+", row.get("Designator", ""))
            if item.strip()
        ]
        if len(designators) != quantity:
            raise ValueError(
                "BOM Designator/Quantity mismatch at "
                f"row {row_number}: quantity={quantity}, designators={len(designators)}"
            )
        manufacturer_part = row.get("Manufacturer Part", "").strip()
        comment = row.get("Comment", "").strip()
        value = row.get("Value", "").strip() or comment
        device_name = manufacturer_part or comment or value
        supplier_part = row.get("Supplier Part", "").strip()
        footprint = row.get("Footprint", "").strip()

        for instance_index in range(quantity):
            designator = designators[instance_index]
            props_for_type: dict[str, Any] = {
                "designator": designator,
                "deviceName": device_name,
                "supplierPart": supplier_part,
                "value": value,
                "footprint": footprint,
            }
            output[f"bom__r{row_number}_{instance_index + 1}"] = {
                "props": {
                    **props_for_type,
                    "deviceType": canonical_device_type(props_for_type, pin_count=0),
                    "deviceNotes": comment,
                    "_source_bom_row": row_number,
                    "_source_format": FORMAT,
                },
                "pins": {},
            }
    return output, tuple(warnings)


class Utf16BomAdapter:
    format_name = FORMAT
    aliases = ("bom", "utf16_bom")
    adapter_version = "1"

    def can_load(self, path: Path) -> bool:
        return bool(bom_files(path))

    def load(self, path: Path) -> ReferenceLoadResult:
        sources = bom_files(path)
        if not sources:
            raise ValueError(f"no {FORMAT} file found at {path}")
        if len(sources) > 1:
            names = ", ".join(source.name for source in sources)
            raise ValueError(
                f"multiple {FORMAT} files found ({names}); pass one asset path explicitly"
            )
        source = sources[0]
        netlist, warnings = parse_utf16_bom(source)
        return ReferenceLoadResult(
            netlist=netlist,
            capability=ReferenceCapability(
                components=True,
                values=True,
                connectivity=False,
                pin_identity="unavailable",
                schematic_context=False,
            ),
            provenance=ReferenceProvenance(
                format=self.format_name,
                adapter=type(self).__name__,
                adapter_version=self.adapter_version,
                sources=(source_provenance(source),),
            ),
            warnings=warnings,
        )
