"""Authenticate fixed non-IC terminal facts from an offline source snapshot.

This new inventory has a stronger source gate than legacy IC/module snapshots:
each current catalog pin number and label needs its own authenticated field
record. A catalog row plus an unrelated newly acquired PDF is insufficient.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re

from datasheet_automation.knowledge.evidence import authenticate_source_quote, normalize_source_text

_CATALOG_TYPES = {"field_effect_transistor_mosfet": "mosfet", "bipolar_junction_transistor_bjt": "transistor",
                  "mems_microphone": "microphone", "antenna": "antenna"}


def _map(value):
    return value if isinstance(value, Mapping) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _mapping_evidence(record, evidence, part, number, field, value):
    from datasheet_automation.knowledge.pin_identity import PinIdentityProof, validate_pin_identity_proof
    mapping = _map(evidence.get("mapping")); sha = evidence.get("document_sha256")
    if (mapping.get("supplier_part") != part or mapping.get("catalog_pin_number") != number
            or mapping.get("catalog_" + field) != value or evidence.get("authorized_field") != field
            or evidence.get("authorized_value") != value or evidence.get("source_ref") != "pin_identity_mapping:" + str(sha)):
        return False
    registered = [row for row in _rows(record.get("pin_identity_mappings"))
                  if isinstance(row, Mapping) and row.get("mapping_sha256") == sha]
    if len(registered) != 1 or registered[0] != mapping:
        return False
    class SnapshotReader:
        def read_primary_source_evidence(self, supplier_part, **kwargs):
            if supplier_part != part:
                raise ValueError("identity source scope mismatch")
            return record
    try:
        proof = {key: mapping[key] for key in PinIdentityProof.__annotations__}
        proof["manufacturer_evidence"] = [{key: cite[key] for key in ("claim", "source_ref", "document_sha256", "quote")}
                                          for cite in proof["manufacturer_evidence"]]
        current = validate_pin_identity_proof(SnapshotReader(), proof)
        canonical = json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode()).hexdigest() == sha
    except (ValueError, TypeError, KeyError):
        return False


def _field_evidence(record, evidence, part, number, field, value):
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("source_kind") == "pin_identity_mapping":
        return _mapping_evidence(record, evidence, part, number, field, value)
    if evidence.get("source_kind") == "manufacturer_datasheet_revision":
        from datasheet_automation.knowledge.datasheet_revision import revision_pin_identity
        return (record.get("supplier_part") == part
                and evidence.get("source_selection_sha256") == _map(record.get("source_version_selection")).get("selection_sha256")
                and revision_pin_identity(record, evidence, number, field, value))
    quote = evidence.get("quote")
    if not isinstance(quote, str) or not isinstance(value, str) or not value.strip():
        return False
    # Require the exact claimed token in the supported quotation. This checks
    # source attribution/literal identity; it does not infer an electrical role.
    normalized = normalize_source_text(quote)
    if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(normalize_source_text(value)) + r"(?![A-Za-z0-9_])", normalized):
        return False
    for chunk in _rows(record.get("evidence")):
        if not isinstance(chunk, Mapping):
            continue
        if (chunk.get("source_ref") == evidence.get("source_ref")
                and chunk.get("document_sha256") == evidence.get("document_sha256")):
            checked = authenticate_source_quote(record, chunk, quote, fields=[field])
            if checked and checked.get("source_kind") == evidence.get("source_kind"):
                return True
    return False


def authenticate_physical_catalog(record: Mapping, *, supplier_part: str, component_type: str) -> dict:
    """Return explicit unresolved diagnostics; never consult SQL or a case file."""
    from .fixed_inventory import PHYSICAL_DEVICE_TYPES
    record = _map(record); catalog = _map(record.get("device")); diagnostics = []
    def add(rule, message, **detail):
        diagnostics.append({"rule": rule, "status": "unresolved", "message": message,
                            "supplier_part": supplier_part, **detail})
    if (component_type not in PHYSICAL_DEVICE_TYPES or catalog.get("supplier_part") != supplier_part
            or record.get("supplier_part") != supplier_part):
        add("PHYSICAL_CATALOG_IDENTITY", "An exact supported physical inventory identity is required")
    if _CATALOG_TYPES.get(catalog.get("category_level_2")) != component_type:
        add("PHYSICAL_CATALOG_TYPE", "The explicit input type must match a supported catalog category",
            component_type=component_type, catalog_category=catalog.get("category_level_2"))
    quality = _map(record.get("source_quality"))
    if quality.get("identity_qualification") not in {None, "legacy_metadata_conflict"}:
        add("PHYSICAL_SOURCE_IDENTITY", "The primary source has an unresolved device identity qualification")
    pins = record.get("pins", []); records = record.get("catalog_field_evidence", [])
    if not isinstance(pins, list) or not pins or not isinstance(records, list):
        add("PHYSICAL_PIN_SOURCE", "Physical pin inventory and current field evidence are required")
        return {"status": "unresolved", "diagnostics": diagnostics}
    for pin in pins:
        pin = _map(pin); number = pin.get("pin_number")
        for field in ("pin_number", "pin_name"):
            value = pin.get(field)
            supported = False
            for raw in records:
                row = _map(raw)
                asserted = _map(row.get("value")).get("new") if row.get("action") == "correct" else row.get("value")
                if (row.get("entity") != "pin" or row.get("pin_number") != number
                        or row.get("supplier_part") != supplier_part
                        or row.get("field_name") != field or row.get("action") not in {"insert", "fill", "retained", "correct"}
                        or row.get("current_value") != value or asserted != value):
                    continue
                if _field_evidence(record, row.get("evidence"), supplier_part, number, field, value):
                    supported = True
                    break
            if not supported:
                add("PHYSICAL_PIN_SOURCE", "The current physical pin field has no authenticated source-supported catalog record",
                    pin=number, field=field, value=value)
    return {"status": "authenticated" if not diagnostics else "unresolved", "diagnostics": diagnostics}


__all__ = ["authenticate_physical_catalog"]
