"""Content-addressed experiment records and versioned, additive pin facts."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any

from .inputs import digest

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}")
_DDL = (
    """CREATE TABLE IF NOT EXISTS device_pin_contract_versions (
       supplier_part VARCHAR(50) NOT NULL,
       contract_sha256 CHAR(64) NOT NULL,
       document_sha256 CHAR(64) NOT NULL,
       schema_version VARCHAR(80) NOT NULL,
       payload_json JSON NOT NULL,
       created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
       PRIMARY KEY (supplier_part,contract_sha256),
       KEY idx_contract_created (supplier_part,created_at)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS case_pin_bindings (
       run_id VARCHAR(100) NOT NULL,
       case_id VARCHAR(128) NOT NULL,
       board_id VARCHAR(100) NOT NULL,
       source_kind VARCHAR(20) NOT NULL,
       payload_sha256 CHAR(64) NOT NULL,
       payload_json JSON NOT NULL,
       created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
       PRIMARY KEY (run_id,case_id,board_id,source_kind)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


class RecordStore:
    """Write immutable artifacts inside an explicitly selected runtime directory.

    Hash-addressed events form an experiment ledger, not a second Agent trace
    service. Concurrent writers never overwrite completed artifacts.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        allowed = Path(__file__).resolve().parents[3] / "07_TEMP"
        if not self.root.is_relative_to(allowed) or self.root == allowed:
            raise ValueError("experiment intermediates must be inside Worklib/07_TEMP/<component>")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def put(self, category: str, value: Any) -> dict[str, str]:
        if not _NAME.fullmatch(category):
            raise ValueError("invalid artifact category")
        sha = digest(value)
        directory = self.root / category
        directory.mkdir(exist_ok=True, mode=0o700)
        path = directory / (sha + ".json")
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if json.loads(path.read_text(encoding="utf-8")) != value:
                raise ValueError("existing content-addressed record was modified")
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        return {"path": str(path), "sha256": sha}

    def event(self, kind: str, payload: Mapping[str, Any]) -> dict[str, str]:
        return self.put("events", {"schema": "pcbagent.experiment_event.v1", "kind": kind,
            "created_at": datetime.now(timezone.utc).isoformat(), "payload": dict(payload)})


class PinContractStore:
    """Keep facts separate from case-dependent modes and connections in MySQL.

    The caller supplies the existing catalog connection factory. No credentials
    or database files are persisted by this class. Versions are append-only.
    """

    def __init__(self, connect: Callable[[], Any]) -> None:
        self.connect = connect

    def ensure_schema(self) -> None:
        cn = self.connect()
        try:
            cur = cn.cursor()
            for statement in _DDL:
                cur.execute(statement)
            cn.commit()
        finally:
            cn.close()

    def save_device(self, payload: Mapping[str, Any]) -> str:
        data = dict(payload)
        part = data.get("supplier_part")
        document = data.get("document_sha256")
        if not isinstance(part, str) or not re.fullmatch(r"[CS][0-9]+", part):
            raise ValueError("C/S identity required")
        if not isinstance(document, str) or not re.fullmatch(r"[0-9a-f]{64}", document):
            raise ValueError("document digest required")
        if data.get("schema") != "pcbagent.device_pin_contracts.v1" or not isinstance(data.get("pins"), dict) or not data["pins"]:
            raise ValueError("versioned, nonempty pin contract required")
        selected = data.get("source_version_selection")
        if selected is not None:
            from datasheet_automation.knowledge.datasheet_revision import _sha, SELECTION_SCHEMA
            if (not isinstance(selected, Mapping) or selected.get("schema") != SELECTION_SCHEMA
                    or selected.get("supplier_part") != part
                    or selected.get("canonical_primary_document_sha256") != document
                    or _sha({k: v for k, v in selected.items() if k != "selection_sha256"}) != selected.get("selection_sha256")):
                raise ValueError("role contract must retain a valid explicit revision selection")
            for pin in data["pins"].values():
                for field in ("electrical_type", "modes", "required_connection", "allow_nc", "voltage_min_v", "voltage_max_v", "resources", "gpio_remap_safe"):
                    if pin.get(field) is not None:
                        raise ValueError("revision role contract cannot merge electrical fields")
                for refs in pin.get("field_evidence", {}).values():
                    if any(ref.get("document_sha256") != selected.get("selected_document_sha256")
                            or ref.get("source_kind") != "manufacturer_datasheet_revision"
                            or ref.get("source_selection_sha256") != selected.get("selection_sha256") for ref in refs):
                        raise ValueError("revision role contract mixes source versions")
        sha = digest(data)
        cn = self.connect()
        try:
            cur = cn.cursor()
            cur.execute("INSERT IGNORE INTO device_pin_contract_versions (supplier_part,contract_sha256,document_sha256,schema_version,payload_json) VALUES (%s,%s,%s,%s,%s)",
                        (part, sha, document, data["schema"], json.dumps(data, ensure_ascii=False)))
            cn.commit()
        except BaseException:
            cn.rollback()
            raise
        finally:
            cn.close()
        return sha

    def latest_device(self, supplier_part: str, *, document_sha256: str | None = None,
                      source_authentication_version: int | None = None,
                      source_selection_sha256: str | None = None) -> dict[str, Any] | None:
        cn = self.connect()
        try:
            cur = cn.cursor(dictionary=True)
            query = "SELECT payload_json FROM device_pin_contract_versions WHERE supplier_part=%s"
            args = [supplier_part]
            if source_selection_sha256 is None:
                query += " AND (JSON_EXTRACT(payload_json,'$.source_version_selection') IS NULL OR JSON_TYPE(JSON_EXTRACT(payload_json,'$.source_version_selection'))='NULL')"
            else:
                if not isinstance(source_selection_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", source_selection_sha256):
                    raise ValueError("exact source selection SHA required")
                query += " AND JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.source_version_selection.selection_sha256'))=%s"
                args.append(source_selection_sha256)
            if document_sha256:
                query += " AND document_sha256=%s"
                args.append(document_sha256)
            if source_authentication_version is not None:
                query += " AND JSON_EXTRACT(payload_json,'$.provenance.source_authentication_version')=%s"
                args.append(source_authentication_version)
            cur.execute(query + " ORDER BY created_at DESC,contract_sha256 DESC LIMIT 1", args)
            row = cur.fetchone()
            return json.loads(row["payload_json"]) if row and isinstance(row["payload_json"], str) else row["payload_json"] if row else None
        finally:
            cn.close()

    def save_bindings(self, *, run_id: str, case_id: str, board_id: str,
                      source_kind: str, payload: Mapping[str, Any]) -> str:
        if source_kind not in {"generated", "reference"}:
            raise ValueError("binding source must be generated or reference")
        data = dict(payload)
        sha = digest(data)
        cn = self.connect()
        try:
            cur = cn.cursor(dictionary=True)
            key = (run_id, case_id, board_id, source_kind)
            cur.execute("SELECT payload_sha256 FROM case_pin_bindings WHERE run_id=%s AND case_id=%s AND board_id=%s AND source_kind=%s FOR UPDATE", key)
            old = cur.fetchone()
            if old and old["payload_sha256"] != sha:
                raise ValueError("case run is immutable; use a new run_id")
            if not old:
                cur.execute("INSERT INTO case_pin_bindings (run_id,case_id,board_id,source_kind,payload_sha256,payload_json) VALUES (%s,%s,%s,%s,%s,%s)",
                            (*key, sha, json.dumps(data, ensure_ascii=False)))
            cn.commit()
        except BaseException:
            cn.rollback()
            raise
        finally:
            cn.close()
        return sha


__all__ = ["RecordStore", "PinContractStore"]
