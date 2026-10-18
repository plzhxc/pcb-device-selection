"""Check module contact boundaries without inferring them from catalog counts."""
from __future__ import annotations

from collections.abc import Mapping
import re


def authenticate_module_catalog(record: Mapping, *, supplier_part: str) -> dict:
    """Reject aggregate rows and reauthenticate grouped-contact inventories.

    Existing numeric-only module inventories retain their explicitly labelled
    legacy status. That compatibility path is not a new completeness proof.
    Non-numeric contacts require the reviewed grouped source contract.
    """
    from datasheet_automation.knowledge.pin_extraction import _source_evidence
    from datasheet_automation.knowledge.terminal_inventory import authenticate_terminal_inventory
    record = record if isinstance(record, Mapping) else {}
    device = record.get("device") or {}; pins = record.get("pins") or []
    diagnostics = []
    def bad(rule, message):
        diagnostics.append({"rule": rule, "status": "unresolved", "message": message,
                            "supplier_part": supplier_part})
        return {"status": "unresolved", "diagnostics": diagnostics}
    if (not isinstance(supplier_part, str) or not re.fullmatch(r"[CS][0-9]+", supplier_part)
            or not isinstance(device, Mapping) or not isinstance(pins, list) or not pins
            or device.get("supplier_part") != supplier_part
            or (record.get("supplier_part") is not None and record.get("supplier_part") != supplier_part)
            or type(device.get("pin_count")) is not int or device["pin_count"] != len(pins)
            or any(not isinstance(pin, Mapping) or not isinstance(pin.get("pin_number"), str)
                   or not pin["pin_number"] for pin in pins)
            or len({pin["pin_number"] for pin in pins}) != len(pins)):
        return bad("MODULE_CONTACT_INVENTORY", "Module inventory requires unique actual contacts and exact source identity")
    aggregate = re.compile(r"\d\s*[-–—~]\s*(?:PIN\s*)?\d|\b(?:left|right)\s+edge\b", re.I)
    if any(aggregate.search(pin["pin_number"]) for pin in pins):
        return bad("MODULE_AGGREGATE_CONTACT", "A range or board-edge summary is not an individual physical contact")
    records = record.get("catalog_field_evidence") or []
    if not isinstance(records, list):
        return bad("MODULE_CONTACT_EVIDENCE", "Current catalog field evidence must be an array")
    count_records = [row for row in records if isinstance(row, Mapping)
        and row.get("supplier_part") == supplier_part and row.get("entity") == "device"
        and row.get("field_name") == "pin_count" and type(row.get("current_value")) is int
        and row["current_value"] == len(pins)
        and isinstance(row.get("evidence"), Mapping)
        and isinstance(row["evidence"].get("derivation"), Mapping)
        and row["evidence"]["derivation"].get("parser") == "reviewed_module_contact_groups_v4"]
    if not count_records:
        if all(re.fullmatch(r"[1-9][0-9]*", pin["pin_number"]) for pin in pins):
            return {"status": "legacy_numeric_inventory", "diagnostics": [],
                    "limitation": "Retained numeric module compatibility; no new source-completeness claim."}
        return bad("MODULE_GROUPED_CONTACT_SOURCE", "Named module contacts require a complete reviewed source inventory")
    if record.get("supplier_part") != supplier_part:
        return bad("MODULE_CONTACT_EVIDENCE", "Grouped source evidence must carry its exact supplier identity")
    candidates = []
    chunks_by_ref = {row.get("source_ref"): row for row in record.get("evidence", [])
                     if isinstance(row, Mapping)}
    for pin in pins:
        evidence = {}
        for field in ("pin_number", "pin_name"):
            found = []
            for row in records:
                if not isinstance(row, Mapping):
                    continue
                asserted = row.get("value")
                if row.get("action") == "correct" and isinstance(asserted, Mapping):
                    asserted = asserted.get("new")
                if (row.get("supplier_part") == supplier_part and row.get("entity") == "pin"
                        and row.get("pin_number") == pin["pin_number"] and row.get("field_name") == field
                        and row.get("action") in {"insert", "retained", "fill", "correct"}
                        and row.get("current_value") == pin.get(field) and asserted == pin.get(field)
                        and isinstance(row.get("evidence"), Mapping)):
                    cite = row["evidence"]
                    chunk = chunks_by_ref.get(cite.get("source_ref"))
                    checked = _source_evidence({"chunk_id": chunk.get("id"), "quote": cite.get("quote")}, record) if chunk else None
                    if checked and field in checked.get("reviewed_claim_scopes", []):
                        found.append(checked)
            if not found:
                return bad("MODULE_CONTACT_FIELD_SOURCE", "Every current contact identifier and label needs authenticated field evidence")
            evidence[field] = found[-1]
        candidates.append({"pin_number": pin["pin_number"], "pin_name": pin.get("pin_name"), "evidence": evidence})
    for row in count_records:
        cite = row["evidence"]
        actual_value = row.get("value")
        if row.get("action") == "correct" and isinstance(actual_value, Mapping):
            actual_value = actual_value.get("new")
        if (row.get("action") not in {"insert", "retained", "fill", "correct"}
                or type(actual_value) is not int or actual_value != len(pins)):
            continue
        package = None
        for chunk in record.get("evidence", []):
            if isinstance(chunk, Mapping) and chunk.get("source_ref") == cite.get("source_ref"):
                import json
                try:
                    package = json.loads(chunk["content"]).get("package")
                except (ValueError, TypeError, AttributeError):
                    pass
        facts = {"device_name": device.get("device_name"), "package": package, "pins": candidates}
        extraction = {"terminal_inventory_review": cite.get("source_ref"), "pin_count": len(pins),
                      "pins_complete": True, "pins": candidates}
        try:
            checked = authenticate_terminal_inventory(extraction, record, facts, source_evidence=_source_evidence)
        except (ValueError, KeyError, TypeError):
            continue
        if checked.get("derivation") == cite.get("derivation"):
            return {"status": "authenticated", "diagnostics": [], "inventory": checked["derivation"]}
    return bad("MODULE_CONTACT_COMPLETENESS_SOURCE", "Current grouped inventory does not match its original reviewed sources")


__all__ = ["authenticate_module_catalog"]
