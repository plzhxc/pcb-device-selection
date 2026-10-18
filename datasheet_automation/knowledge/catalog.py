"""Evidence-backed, conservative writes to the device_parameters catalog.

The caller supplies a MySQL Connector connection and reviewed source facts.
This module neither extracts facts from case wiring nor opens connections.  A
dry run executes SELECT/SHOW only; production writes are an explicit choice.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any


class CatalogError(ValueError):
    """Invalid evidence, ambiguous identity, or stale correction precondition."""


_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_]{1,64}$")
_PART = re.compile(r"^(C[0-9]+|S[0-9]+)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEVICE_FIELDS = frozenset({"device_name", "category_level_1", "category_level_2", "pin_count"})
_PIN_FIELDS = frozenset({
    "pin_number", "pin_name", "pin_type", "pin_description",
    "pin_connection_notes", "pin_impedance", "pin_frequency",
})
_EVIDENCE_KINDS = frozenset({"datasheet", "manufacturer_page", "manufacturer_image", "pin_identity_mapping", "manufacturer_ordering_notice", "manufacturer_datasheet_revision"})

EVIDENCE_DDL = """CREATE TABLE IF NOT EXISTS catalog_field_evidence (
 evidence_id CHAR(64) NOT NULL PRIMARY KEY,
 supplier_part VARCHAR(50) NOT NULL,
 entity VARCHAR(16) NOT NULL,
 entity_key VARCHAR(64) NOT NULL,
 field_name VARCHAR(64) NOT NULL,
 action VARCHAR(24) NOT NULL,
 value_json JSON NOT NULL,
 evidence_json JSON NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 KEY idx_catalog_evidence_part (supplier_part)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""


def _pin_query_name(part: str, number: Any) -> str:
    """Derive the catalog lookup key, preserving the exact source identifier."""
    if not isinstance(number, str) or not number.strip() or len(number) > 50:
        raise CatalogError("pin_number must be a nonempty string of at most 50 characters")
    value = f"{part}:{number}"
    if len(value) > 255:
        raise CatalogError("derived pin_query_name exceeds the catalog column length")
    return value


def _json(value: Any) -> str:
    def convert(item: Any) -> Any:
        if isinstance(item, Decimal):
            return str(item)
        raise TypeError(f"unsupported catalog value: {type(item).__name__}")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=convert)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CatalogError(f"invalid SQL identifier: {value!r}")
    return f"`{value}`"


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _scalar(value: Any, where: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal))):
        raise CatalogError(f"{where}: expected text, number, or null")
    if isinstance(value, (float, Decimal)) and not math.isfinite(value):
        raise CatalogError(f"{where}: non-finite number")


def _evidence(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogError(f"{where}: source evidence is required")
    result = dict(value)
    if result.get("source_kind") not in _EVIDENCE_KINDS:
        raise CatalogError(f"{where}: evidence must be a datasheet or manufacturer source")
    for field in ("source_ref", "quote"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise CatalogError(f"{where}: evidence.{field} is required")
    if not _SHA256.fullmatch(str(result.get("document_sha256", ""))):
        raise CatalogError(f"{where}: evidence.document_sha256 must identify the source bytes")
    page = result.get("page")
    if page is not None and (type(page) is not int or page < 1):
        raise CatalogError(f"{where}: evidence.page must be a one-based integer")
    if result["source_kind"] in {"datasheet", "manufacturer_ordering_notice", "manufacturer_datasheet_revision"} and page is None:
        raise CatalogError(f"{where}: datasheet evidence needs a page")
    if result["source_kind"] != "datasheet" and not str(result.get("locator") or "").strip():
        raise CatalogError(f"{where}: manufacturer page/image needs a locator")
    _json(result)
    return result


class CatalogStore:
    """One-device transactions, with fill-only defaults and guarded corrections.

    ``upsert`` defaults to dry_run=True.  It does not commit a transaction owned
    by its caller and refuses connections to any database except
    ``device_parameters``.  Call ``ensure_schema`` separately before a write.
    """

    def __init__(self, connection: Any, *, pin_identity_authenticator: Callable | None = None,
                 source_fact_authenticator: Callable | None = None) -> None:
        self.connection = connection
        self.pin_identity_authenticator = pin_identity_authenticator
        self.source_fact_authenticator = source_fact_authenticator

    def _authenticated_evidence(self, evidence: Any, *, part: str, entity: str,
                                field: str, value: Any, catalog_pin_number: str | None = None) -> dict:
        """A CAD alias proves identity only after the current source chain is checked."""
        if isinstance(evidence, Mapping) and evidence.get("source_kind") == "manufacturer_datasheet_revision":
            if (not callable(self.source_fact_authenticator)
                    or (entity, field) not in {("device", "device_name"), ("device", "pin_count"),
                        ("pin", "pin_number"), ("pin", "pin_name")}):
                raise CatalogError("revision writes require an exact source authenticator and identity-only field")
            try:
                checked = self.source_fact_authenticator(part, evidence, entity=entity, field=field,
                    value=value, catalog_pin_number=catalog_pin_number)
            except (ValueError, TypeError, KeyError) as exc:
                raise CatalogError(f"revision source authentication failed: {exc}") from exc
            proof = checked.get("evidence") if isinstance(checked, Mapping) else None
            if (not isinstance(checked, Mapping) or checked.get("status") != "verified" or not isinstance(proof, Mapping)
                    or proof.get("source_kind") != "manufacturer_datasheet_revision"
                    or proof.get("authorized_field") != field or proof.get("authorized_value") != value
                    or proof.get("catalog_pin_number") != catalog_pin_number
                    or proof.get("source_selection_sha256") != evidence.get("source_selection_sha256")):
                raise CatalogError("revision source result differs from the exact catalog target")
            evidence = proof
        if isinstance(evidence, Mapping) and evidence.get("source_kind") == "manufacturer_ordering_notice":
            if (entity != "device" or field != "device_name"
                    or evidence.get("document_role") != "manufacturer_ordering_notice"
                    or evidence.get("claim_scope") != "explicitly_reviewed_fields"
                    or evidence.get("reviewed_claim_scopes") != ["device_name"]
                    or evidence.get("source_authentication_version") != 3
                    or not isinstance(value, str) or value not in str(evidence.get("quote", ""))):
                raise CatalogError("manufacturer ordering notices authenticate only a literal exact device name")
        if isinstance(evidence, Mapping) and evidence.get("source_kind") == "pin_identity_mapping":
            if entity != "pin" or field not in {"pin_number", "pin_name"} or not callable(self.pin_identity_authenticator):
                raise CatalogError("pin identity mappings require a source authenticator and can prove only pin number/name")
            try:
                checked = self.pin_identity_authenticator(part, evidence.get("document_sha256"), field, value)
            except (ValueError, TypeError, KeyError) as exc:
                raise CatalogError(f"pin identity mapping authentication failed: {exc}") from exc
            if not isinstance(checked, Mapping) or checked.get("status") != "verified":
                raise CatalogError("pin identity mapping was not verified")
            proof = checked.get("evidence")
            if (not isinstance(proof, Mapping) or proof.get("source_kind") != "pin_identity_mapping"
                    or proof.get("document_sha256") != evidence.get("document_sha256")
                    or proof.get("authorized_field") != field or proof.get("authorized_value") != value
                    or not isinstance(proof.get("mapping"), Mapping)
                    or proof["mapping"].get("catalog_pin_number") != catalog_pin_number
                    or proof["mapping"].get("supplier_part") != part):
                raise CatalogError("pin identity mapping scope does not match the target catalog terminal")
            evidence = proof
        return _evidence(evidence, f"{part}.{entity}.{field}")

    def _idle(self) -> None:
        if getattr(self.connection, "in_transaction", False):
            raise CatalogError("catalog requires an idle connection; caller transaction is active")

    @staticmethod
    def _database(cursor: Any) -> None:
        cursor.execute("SELECT DATABASE() AS database_name")
        if cursor.fetchone().get("database_name") != "device_parameters":
            raise CatalogError("catalog writes are restricted to device_parameters")

    def ensure_schema(self) -> None:
        """Explicit DDL setup; never called implicitly by a dry run."""
        self._idle()
        cursor = self.connection.cursor(dictionary=True)
        try:
            self._database(cursor)
            cursor.execute(EVIDENCE_DDL)
            self.connection.commit()
        finally:
            cursor.close()

    def upsert(self, payload: Mapping[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        """Plan/apply reviewed facts; preserve existing nonempty values by default.

        Corrections are explicit dictionaries with entity (device/pin/parameter),
        field, value, expect_old, evidence and, for pins, pin_index.  Deleting a
        duplicate pin uses action=delete and expect_old containing its complete
        pin_index plus all seven public pin fields.  No bulk replacement exists.
        Unresolved observations contain field_path, message and evidence.
        Pin identity fills/corrections also synchronize their derived lookup
        keys. A guarded same-value identity correction repairs a stale key
        without changing the physical pin. Derived keys are never caller input.
        """
        self._idle()
        if type(dry_run) is not bool:
            raise CatalogError("dry_run must be boolean")
        if not isinstance(payload, Mapping):
            raise CatalogError("catalog payload must be an object")
        allowed = _DEVICE_FIELDS | {"supplier_part", "identity_scope", "pins", "parameters",
            "device_evidence", "parameter_evidence", "corrections", "observations", "pins_complete"}
        if set(payload) - allowed:
            raise CatalogError(f"unknown payload fields: {sorted(set(payload) - allowed)}")
        part = payload.get("supplier_part")
        if not isinstance(part, str) or not _PART.fullmatch(part):
            raise CatalogError("supplier_part must be C<number> or project S<number>")
        expected_scope = "lcsc" if part.startswith("C") else "project"
        if payload.get("identity_scope", "lcsc") != expected_scope:
            raise CatalogError(f"{part}: identity_scope must be {expected_scope}")
        if not isinstance(payload.get("device_name"), str) or not payload["device_name"].strip():
            raise CatalogError(f"{part}: device_name is required for identity checking")
        if type(payload.get("pins_complete", False)) is not bool:
            raise CatalogError("pins_complete must be boolean")
        cursor = self.connection.cursor(dictionary=True)
        try:
            self.connection.start_transaction(readonly=dry_run)
            self._database(cursor)
            report = self._plan(cursor, payload, dry_run)
            if dry_run:
                self.connection.rollback()
            else:
                self._apply(cursor, report)
                self.connection.commit()
            report.pop("_evidence", None)
            report["status"] = "planned" if dry_run else "committed"
            return report
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            cursor.close()

    def _plan(self, cursor: Any, payload: Mapping[str, Any], dry_run: bool) -> dict[str, Any]:
        part = payload["supplier_part"]
        lock = "" if dry_run else " FOR UPDATE"
        cursor.execute("SELECT * FROM devices WHERE supplier_part=%s" + lock, (part,))
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise CatalogError(f"{part}: ambiguous device identity")
        device = dict(rows[0]) if rows else None
        if device:
            for field in _DEVICE_FIELDS:
                if field in payload and payload[field] != device[field]:
                    raise CatalogError(f"{part}: identity conflict at {field}; use an explicit correction")
        else:
            missing = [field for field in _DEVICE_FIELDS if _empty(payload.get(field))]
            if missing:
                raise CatalogError(f"{part}: new device missing {sorted(missing)}")
            device = {field: payload[field] for field in _DEVICE_FIELDS}
            device.update(supplier_part=part, device_id=None)
            if type(device["pin_count"]) is not int or device["pin_count"] < 1:
                raise CatalogError(f"{part}: positive integer pin_count required")
            for field in ("device_name", "category_level_1", "category_level_2"):
                if not isinstance(device[field], str) or not device[field].strip():
                    raise CatalogError(f"{part}: nonempty {field} required")
        table = "params_" + device["category_level_2"]
        cursor.execute("SHOW COLUMNS FROM " + _identifier(table))
        columns = {row["Field"] for row in cursor.fetchall()} - {"device_id"}
        if not columns:
            raise CatalogError(f"{part}: existing category parameter table is required")
        report: dict[str, Any] = {
            "supplier_part": part, "identity_scope": "lcsc" if part.startswith("C") else "project",
            "device_id": device["device_id"], "parameter_table": table,
            "operations": [], "retained": [], "conflicts": [], "unresolved": [], "_evidence": [],
            "derived_fields": [],
        }
        operations = report["operations"]
        touched: set[tuple[str, int | None, str]] = set()

        def record(entity: str, key: int | None, field: str, value: Any, action: str, evidence: Any) -> None:
            actual_value = value["new"] if action == "correct" else value
            terminal = None
            if entity == "pin" and isinstance(evidence, Mapping) and evidence.get("source_kind") in {"pin_identity_mapping", "manufacturer_datasheet_revision"}:
                terminal = actual_value if field == "pin_number" else next(
                    (pin["pin_number"] for pin in payload.get("pins", []) if pin.get("pin_index") == key),
                    pins.get(key, {}).get("pin_number"))
            source = self._authenticated_evidence(evidence, part=part, entity=entity, field=field,
                                                 value=actual_value, catalog_pin_number=terminal)
            report["_evidence"].append({"entity": entity, "entity_key": "" if key is None else str(key),
                "field_name": field, "value": value, "action": action, "evidence": source})

        if not rows:
            for field in ("device_name", "pin_count"):
                record("device", None, field, device[field], "insert", payload.get("device_evidence", {}).get(field))
            operations.append({"action": "insert", "entity": "device", "values": {
                k: v for k, v in device.items() if k != "device_id"}})

        old_pins: list[dict[str, Any]] = []
        old_params: list[dict[str, Any]] = []
        if rows:
            cursor.execute("SELECT * FROM pins WHERE device_id=%s ORDER BY pin_index" + lock, (device["device_id"],))
            old_pins = cursor.fetchall()
            cursor.execute("SELECT * FROM " + _identifier(table) + " WHERE device_id=%s" + lock, (device["device_id"],))
            old_params = cursor.fetchall()
        if len(old_params) > 1:
            raise CatalogError(f"{part}: multiple parameter rows")
        pins = {p["pin_index"]: dict(p) for p in old_pins}
        params = dict(old_params[0]) if old_params else {}
        if not old_params:
            operations.append({"action": "insert", "entity": "parameter", "values": {}})

        def fill(entity: str, key: int | None, target: dict[str, Any], fields: Mapping[str, Any], evidences: Mapping[str, Any]) -> None:
            values = {}
            for field, value in fields.items():
                _scalar(value, field)
                if _empty(value):
                    continue
                old = target.get(field)
                action = "fill" if _empty(old) else "retained" if old == value else "conflict"
                record(entity, key, field, value, action, evidences.get(field))
                location = {"entity": entity, "pin_index": key, "field": field}
                if action == "fill":
                    values[field] = value
                    target[field] = value
                    touched.add((entity, key, field))
                elif action == "retained":
                    report["retained"].append(location)
                else:
                    report["conflicts"].append({**location, "existing": old, "proposed": value})
            if values:
                operations.append({"action": "update", "entity": entity, "pin_index": key, "values": values})

        incoming = payload.get("pins", [])
        if not isinstance(incoming, list):
            raise CatalogError("pins must be a list")
        seen = set()
        for pin in incoming:
            if not isinstance(pin, Mapping) or set(pin) - (_PIN_FIELDS | {"pin_index", "evidence"}):
                raise CatalogError(f"{part}: unexpected pin fields")
            index = pin.get("pin_index")
            if type(index) is not int or index < 1 or index in seen:
                raise CatalogError(f"{part}: pin_index must be unique positive integers")
            seen.add(index)
            for field in ("pin_number", "pin_name"):
                if not isinstance(pin.get(field), str) or not pin[field].strip():
                    raise CatalogError(f"{part}: pin {index} requires {field}")
            if index not in pins:
                if any(p["pin_number"] == pin["pin_number"] for p in pins.values()):
                    raise CatalogError(f"{part}: pin number {pin['pin_number']} already exists at a different index")
                pins[index] = {"pin_index": index, "pin_query_name": _pin_query_name(part, pin["pin_number"])}
                operations.append({"action": "insert", "entity": "pin", "pin_index": index,
                    "values": {"pin_index": index, "pin_number": pin["pin_number"], "pin_name": pin["pin_name"],
                               "pin_query_name": pins[index]["pin_query_name"]}})
            elif pins[index]["pin_number"] != pin["pin_number"]:
                raise CatalogError(f"{part}: pin {index} number identity conflict")
            fill("pin", index, pins[index], {k: v for k, v in pin.items() if k in _PIN_FIELDS}, pin.get("evidence", {}))
        proposed = payload.get("parameters", {})
        if not isinstance(proposed, Mapping) or set(proposed) - columns:
            raise CatalogError(f"{part}: parameters must use columns from {table}")
        fill("parameter", None, params, proposed, payload.get("parameter_evidence", {}))

        for correction in payload.get("corrections", []):
            entity, key = correction.get("entity"), correction.get("pin_index")
            field = correction.get("field")
            if entity == "device":
                target, fields = device, {"device_name", "pin_count"}
            elif entity == "parameter":
                target, fields = params, columns
            elif entity == "pin" and key in pins:
                target, fields = pins[key], _PIN_FIELDS
            else:
                raise CatalogError(f"{part}: invalid correction entity or pin_index")
            if correction.get("action", "update") == "delete":
                if entity != "pin":
                    raise CatalogError("only individual pins support deletion")
                expected = correction.get("expect_old")
                required = _PIN_FIELDS | {"pin_index"}
                if not isinstance(expected, Mapping) or set(expected) != required or any(target.get(k) != v for k, v in expected.items()):
                    raise CatalogError(f"{part}: stale/incomplete expect_old for deleted pin {key}")
                if any(item[0] == "pin" and item[1] == key for item in touched):
                    raise CatalogError(f"{part}: pin {key} is both filled and deleted")
                record(entity, key, "*", expected, "delete", correction.get("evidence"))
                operations.append({"action": "delete", "entity": "pin", "pin_index": key})
                del pins[key]
                continue
            if correction.get("action", "update") != "update" or field not in fields:
                raise CatalogError(f"{part}: field cannot be corrected: {entity}.{field}")
            token = (entity, key, field)
            if token in touched or "expect_old" not in correction or target.get(field) != correction["expect_old"]:
                raise CatalogError(f"{part}: stale/overlapping expect_old at {entity}.{key}.{field}")
            value = correction.get("value")
            _scalar(value, field)
            if entity == "pin" and field == "pin_name" and (not isinstance(value, str) or len(value) > 100):
                raise CatalogError(f"{part}: pin_name must be a string of at most 100 characters")
            if (entity == "device" or field in {"pin_name", "pin_number"}) and _empty(value):
                raise CatalogError(f"{part}: identity field {field} cannot become null")
            if entity == "device" and field == "pin_count" and (type(value) is not int or value < 1):
                raise CatalogError(f"{part}: positive integer pin_count required")
            record(entity, key, field, {"old": target.get(field), "new": value}, "correct", correction.get("evidence"))
            if target.get(field) != value:
                operations.append({"action": "update", "entity": entity, "pin_index": key, "values": {field: value}})
            target[field] = value
            touched.add(token)

        numbers = [p.get("pin_number") for p in pins.values()]
        if len(set(numbers)) != len(numbers):
            report["unresolved"].append({"field_path": "pins", "message": "Existing duplicate physical pin numbers require evidence-backed correction"})
            if incoming or any(c.get("entity") == "pin" for c in payload.get("corrections", [])):
                raise CatalogError(f"{part}: proposed pin writes leave duplicate physical pin numbers")
        if len(pins) != device["pin_count"]:
            report["unresolved"].append({"field_path": "pins", "message": f"{len(pins)} pin rows for declared pin_count={device['pin_count']}"})
            if payload.get("pins_complete"):
                raise CatalogError(f"{part}: pins_complete does not match declared pin_count")
        if payload.get("pins_complete") and seen != set(pins):
            raise CatalogError(f"{part}: pins_complete requires every resulting pin in payload.pins")
        identity_targets = seen | {c.get("pin_index") for c in payload.get("corrections", [])
            if c.get("entity") == "pin" and c.get("field") in {"pin_number", "pin_name"}
            and c.get("action", "update") == "update"}
        self._derive_pin_queries(cursor, report, pins, identity_targets, lock)
        for observation in payload.get("observations", []):
            field_path, message = observation.get("field_path"), observation.get("message")
            if not isinstance(field_path, str) or not field_path.strip() or not isinstance(message, str) or not message.strip():
                raise CatalogError("observations require field_path and message")
            report["unresolved"].append({"field_path": field_path, "message": message})
            record("observation", None, "unresolved", {"field_path": field_path, "message": message}, "unresolved", observation.get("evidence"))
        return report

    def _derive_pin_queries(self, cursor: Any, report: dict[str, Any], pins: Mapping,
                            targets: set[int], lock: str) -> None:
        """Synchronize targeted keys under the same locks as source corrections.

        The audit action is deliberately ``derive``: readers of manufacturer
        field evidence must not mistake this code-derived key for a source fact.
        Key swaps between retained rows are rejected; no temporary identities
        or implicit renumbering are manufactured to evade the unique index.
        """
        part, device_id = report["supplier_part"], report["device_id"]
        expected = {index: _pin_query_name(part, pins[index]["pin_number"])
                    for index in sorted(targets) if index in pins}
        if not expected:
            return
        deleted = {op["pin_index"] for op in report["operations"]
                   if op["entity"] == "pin" and op["action"] == "delete"}
        for index, key in expected.items():
            # An equality query uses MySQL's actual collation, including its
            # case/accent equivalences. Python string comparison cannot do so.
            cursor.execute("SELECT device_id,pin_index,pin_query_name FROM pins WHERE pin_query_name=%s" + lock, (key,))
            for owner in cursor.fetchall():
                if ((owner["device_id"], owner["pin_index"]) != (device_id, index)
                        and not (owner["device_id"] == device_id and owner["pin_index"] in deleted)):
                    raise CatalogError(f"{part}: derived pin_query_name for pin {index} conflicts with an existing row")
        for index, key in expected.items():
            old = pins[index].get("pin_query_name")
            if old == key:
                continue
            inputs = {"supplier_part": part, "pin_number": pins[index]["pin_number"]}
            metadata = {"schema": "pcbagent.catalog_derived_field.v1", "source_kind": "catalog_derivation",
                "algorithm": "supplier_part + ':' + pin_number", "algorithm_version": 1,
                "inputs": inputs, "manufacturer_claim": False}
            change = {"entity": "pin", "pin_index": index, "field": "pin_query_name",
                      "old": old, "new": key, "derivation": metadata}
            report["derived_fields"].append(change)
            report["_evidence"].append({"entity": "pin", "entity_key": str(index),
                "field_name": "pin_query_name", "value": {"old": old, "new": key},
                "action": "derive", "evidence": metadata})
            report["operations"].append({"action": "update", "entity": "pin", "pin_index": index,
                                         "values": {"pin_query_name": key}})
            pins[index]["pin_query_name"] = key

    def _apply(self, cursor: Any, report: dict[str, Any]) -> None:
        device_id = report["device_id"]
        for operation in report["operations"]:
            entity = operation["entity"]
            table = {"device": "devices", "pin": "pins", "parameter": report["parameter_table"]}[entity]
            values = dict(operation.get("values", {}))
            action = operation["action"]
            if action == "insert":
                if entity != "device":
                    values["device_id"] = device_id
                cursor.execute("INSERT INTO " + _identifier(table) + " (" + ",".join(_identifier(k) for k in values)
                    + ") VALUES (" + ",".join("%s" for _ in values) + ")", tuple(values.values()))
                if entity == "device":
                    device_id = cursor.lastrowid
                    report["device_id"] = device_id
            else:
                where = " WHERE device_id=%s"
                keys = (device_id,)
                if entity == "pin":
                    where += " AND pin_index=%s"
                    keys += (operation["pin_index"],)
                if action == "delete":
                    cursor.execute("DELETE FROM " + _identifier(table) + where, keys)
                else:
                    cursor.execute("UPDATE " + _identifier(table) + " SET " + ",".join(_identifier(k) + "=%s" for k in values)
                        + where, tuple(values.values()) + keys)
        for item in report["_evidence"]:
            canonical = _json({"supplier_part": report["supplier_part"], **item})
            evidence_id = hashlib.sha256(canonical.encode()).hexdigest()
            cursor.execute("INSERT INTO catalog_field_evidence "
                "(evidence_id,supplier_part,entity,entity_key,field_name,action,value_json,evidence_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE evidence_id=evidence_id",
                (evidence_id, report["supplier_part"], item["entity"], item["entity_key"], item["field_name"],
                 item["action"], _json(item["value"]), _json(item["evidence"])))


__all__ = ["CatalogError", "CatalogStore", "EVIDENCE_DDL"]
