"""Authenticate complete, explicitly grouped module contact inventories.

Printed contact labels remain strings. A board contact, an internal component
designator and a built-in connector's mating contact are different entities.
This identity/count proof grants no electrical role or unused-pin permission.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
import re

GROUPED_INVENTORY_SCHEMA = "pcbagent.manufacturer_terminal_inventory.v4"
GROUP_TRANSCRIPTION_SCHEMA = "pcbagent.manufacturer_contact_group.v1"
_ID = re.compile(r"(?:[1-9][0-9]{0,3}|[A-Za-z]{1,8}[1-9][0-9]{0,3})$")
_GROUP = re.compile(r"[a-z][a-z0-9_]{0,31}$")
_KINDS = frozenset({"main", "debug", "test"})


def authenticate_grouped_inventory(inventory, extraction, knowledge, facts, proof,
                                   chunks, source_evidence):
    """Recheck the complete union and each separately reviewed contact table."""
    from .terminal_inventory import _authenticate_reviewed_receipt
    if (set(inventory) != {"schema", "supplier_part", "document_sha256", "device_name",
            "package", "complete_figure", "inventory_scope", "groups", "excluded_interfaces"}
            or inventory.get("schema") != GROUPED_INVENTORY_SCHEMA
            or inventory.get("supplier_part") != knowledge.get("supplier_part")
            or inventory.get("document_sha256") != proof["document_sha256"]
            or inventory.get("device_name") != facts.get("device_name")
            or inventory.get("package") != facts.get("package")
            or inventory.get("complete_figure") is not True
            or inventory.get("inventory_scope") != "module_board_contacts"):
        raise ValueError("grouped inventory requires the exact complete module board-contact scope")
    groups, excluded = inventory.get("groups"), inventory.get("excluded_interfaces")
    if (not isinstance(groups, list) or not 1 <= len(groups) <= 16
            or not isinstance(excluded, list) or len(excluded) > 16
            or any(not isinstance(row, Mapping) or set(row) != {"id", "reason"}
                   or not isinstance(row["id"], str) or not _GROUP.fullmatch(row["id"])
                   or not isinstance(row["reason"], str) or not row["reason"].strip() for row in excluded)
            or len({row["id"] for row in excluded}) != len(excluded)):
        raise ValueError("bounded groups and explicit excluded interface boundaries are required")
    parsed, pin_group_refs, group_ids, refs, group_proofs = {}, {}, set(), set(), []
    for group in groups:
        if (not isinstance(group, Mapping) or set(group) != {"id", "kind", "source_ref", "locator", "pin_ids"}
                or not isinstance(group["id"], str) or not _GROUP.fullmatch(group["id"])
                or group["id"] in group_ids or group["id"] in {row["id"] for row in excluded}
                or group["kind"] not in _KINDS
                or not isinstance(group["locator"], str) or not group["locator"].strip()
                or not isinstance(group["pin_ids"], list) or not 1 <= len(group["pin_ids"]) <= 256
                or any(not isinstance(pin, str) or not _ID.fullmatch(pin) for pin in group["pin_ids"])
                or len(set(group["pin_ids"])) != len(group["pin_ids"])):
            raise ValueError("each contact group requires unique actual manufacturer identifiers")
        group_ids.add(group["id"])
        chunk = chunks.get(str(group["source_ref"]))
        if (not chunk or chunk.get("source_ref") == proof.get("source_ref")
                or chunk.get("source_ref") in refs):
            raise ValueError("each group needs its own transcription distinct from completeness review")
        refs.add(chunk.get("source_ref"))
        checked = source_evidence({"chunk_id": chunk["id"], "quote": chunk["content"]}, knowledge)
        if not checked or checked.get("document_sha256") != proof["document_sha256"]:
            raise ValueError("group transcription must belong to the same original manufacturer PDF")
        _authenticate_reviewed_receipt(chunk, checked, knowledge,
            required_scopes={"pin_number", "pin_name", "module_contact_group"})
        try:
            table = json.loads(chunk["content"])
        except (ValueError, TypeError) as exc:
            raise ValueError("contact group requires a structured reviewed transcription") from exc
        if (not isinstance(table, Mapping) or set(table) != {"schema", "supplier_part", "document_sha256",
                "device_name", "group_id", "kind", "locator", "complete_group", "terminals"}
                or table.get("schema") != GROUP_TRANSCRIPTION_SCHEMA
                or table.get("supplier_part") != knowledge.get("supplier_part")
                or table.get("document_sha256") != proof["document_sha256"]
                or table.get("device_name") != facts.get("device_name")
                or table.get("group_id") != group["id"] or table.get("kind") != group["kind"]
                or table.get("locator") != group["locator"] or table.get("complete_group") is not True):
            raise ValueError("group transcription identity, physical scope and boundaries must agree")
        terminals = table.get("terminals")
        if not isinstance(terminals, list) or not 1 <= len(terminals) <= 256:
            raise ValueError("a bounded nonempty terminal table is required")
        group_numbers = []
        for row in terminals:
            if (not isinstance(row, Mapping) or set(row) != {"pin_id", "pin_name", "identifier_kind", "location"}
                    or not isinstance(row["pin_id"], str) or not _ID.fullmatch(row["pin_id"])
                    or row["pin_id"] in parsed or not isinstance(row["pin_name"], str)
                    or not row["pin_name"].strip() or len(row["pin_name"]) > 100
                    or not isinstance(row["location"], str) or not row["location"].strip()
                    or row["identifier_kind"] != ("manufacturer_pin_number" if row["pin_id"].isdigit()
                                                   else "manufacturer_contact_label")):
                raise ValueError("each row must be a unique printed contact, never a range or component callout")
            parsed[row["pin_id"]] = row["pin_name"]
            pin_group_refs[row["pin_id"]] = chunk["source_ref"]
            group_numbers.append(row["pin_id"])
        if set(group_numbers) != set(group["pin_ids"]) or len(group_numbers) != len(group["pin_ids"]):
            raise ValueError("completeness review and independently transcribed group disagree")
        group_proofs.append({"id": group["id"], "kind": group["kind"], "pin_ids": group_numbers,
            "transcription": checked, "locator": group["locator"]})
    actual = facts.get("pins", [])
    if (not 1 <= len(parsed) <= 256 or len(actual) != len(parsed)
            or len(extraction.get("pins", [])) != len(parsed)
            or {p.get("pin_number"): p.get("pin_name") for p in actual} != parsed
            or type(extraction.get("pin_count")) is not int or extraction["pin_count"] != len(parsed)
            or extraction.get("pins_complete") is not True):
        raise ValueError("candidate must equal every independently reviewed contact and label")
    for pin in actual:
        for field in ("pin_number", "pin_name"):
            citation = pin.get("evidence", {}).get(field, {})
            if (citation.get("source_kind") != "datasheet"
                    or citation.get("document_sha256") != proof["document_sha256"]
                    or citation.get("source_ref") != pin_group_refs[pin["pin_number"]]):
                raise ValueError("current pin identities must cite their own authenticated contact group")
    return {**deepcopy(dict(proof)), "derivation": {
        "method": "complete_manufacturer_terminal_inventory", "parser": "reviewed_module_contact_groups_v4",
        "inventory_scope": "module_board_contacts", "computed_terminal_count": len(parsed),
        "manufacturer_printed_pin_count": None, "manufacturer_contact_ids": sorted(parsed),
        "groups": group_proofs, "excluded_interfaces": deepcopy(excluded),
        "limitation": "Count is the scoped union of reviewed board contacts. Printed labels are not new numeric pins; built-in mating interfaces, internal component pins, electrical roles and NC permissions are not inferred."}}


__all__ = ["GROUPED_INVENTORY_SCHEMA", "GROUP_TRANSCRIPTION_SCHEMA", "authenticate_grouped_inventory"]
