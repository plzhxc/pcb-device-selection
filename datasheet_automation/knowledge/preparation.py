"""Case-by-case knowledge preparation, without exposing reference connections.

The cleaned netlists supply an identity inventory only. Catalog facts and
datasheet evidence returned to generation always come from the knowledge
services. This module never queries refer_netlist or writes case inputs.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4
from typing import Any

from .store import KnowledgeStore, KnowledgeStoreConfig

WORKLIB = Path(__file__).resolve().parents[3]
CASE_ROOT = WORKLIB / "02_CASE" / "02_dataset"
RUNTIME_ROOT = WORKLIB / "07_TEMP" / "case-semantic-validation" / "knowledge"
IDENTITY = re.compile(r"[CS][0-9]{1,20}")
IDENTITY_DDL = """CREATE TABLE IF NOT EXISTS case_device_identity (
 mapping_key CHAR(64) NOT NULL PRIMARY KEY,
 case_name VARCHAR(128) NOT NULL,
 source_file VARCHAR(255) NOT NULL,
 component_key VARCHAR(128) NOT NULL,
 reference_sha256 CHAR(64) NOT NULL,
 supplier_part VARCHAR(50) NOT NULL,
 model_name VARCHAR(255) DEFAULT NULL,
 identity_status VARCHAR(40) NOT NULL,
 source_json JSON NOT NULL,
 version INT NOT NULL DEFAULT 1,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE KEY unique_case_identity (case_name,source_file,component_key,reference_sha256),
 KEY idx_identity_supplier (supplier_part)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
MODULE_IDENTITY_DDL = IDENTITY_DDL.replace("case_device_identity (", "case_functional_module_identity (")
PHYSICAL_IDENTITY_DDL = IDENTITY_DDL.replace("case_device_identity (", "case_physical_device_identity (")
_IDENTITY_TABLES = frozenset({"case_device_identity", "case_functional_module_identity", "case_physical_device_identity"})


def configure_local_services(*, container: str = "pcb-mysql") -> dict[str, Any]:
    """Load an existing managed local credential into this process, never disk.

    This is an explicit deployment helper, not a network discovery fallback.
    Only the approved MySQL container is read. Returned metadata has no secret.
    """
    if container != "pcb-mysql":
        raise ValueError("only the managed pcb-mysql container is supported")
    origin = "environment"
    if not os.environ.get("PCBAGENT_MYSQL_PASSWORD"):
        if os.environ.get("PCBAGENT_MYSQL_HOST","127.0.0.1") not in {"127.0.0.1","localhost"}:
            raise ValueError("managed local credentials cannot be used for a remote MySQL host")
        if os.environ.get("PCBAGENT_MYSQL_PORT","3307")!="3307":
            raise ValueError("managed local credentials require the managed MySQL port")
        result = subprocess.run(["docker", "inspect", container], capture_output=True,
                                text=True, check=True, timeout=10)
        records = json.loads(result.stdout)
        values = dict(item.split("=", 1) for item in records[0]["Config"].get("Env", []) if "=" in item)
        password = values.get("MYSQL_ROOT_PASSWORD")
        if not password:
            raise RuntimeError("managed MySQL password unavailable")
        os.environ["PCBAGENT_MYSQL_PASSWORD"] = password
        origin = "managed_container"
    os.environ.setdefault("PCBAGENT_MYSQL_HOST", "127.0.0.1")
    os.environ.setdefault("PCBAGENT_MYSQL_PORT", "3307")
    os.environ.setdefault("PCBAGENT_DATASHEET_OUTPUT", str(RUNTIME_ROOT))
    return {"credential_source": origin, "mysql_host": os.environ["PCBAGENT_MYSQL_HOST"],
            "mysql_port": int(os.environ["PCBAGENT_MYSQL_PORT"])}


def _case_netlists(directory: Path) -> list[Path]:
    path = directory / f"{directory.name}-cleaned.json"
    return [path] if path.is_file() else []


def _inventory_netlists(root: Path):
    for directory in sorted(root.iterdir()):
        if directory.is_dir() and directory.name != "__analysis__":
            yield from _case_netlists(directory)


def inventory_cases(case_root: str | Path = CASE_ROOT) -> dict[str, Any]:
    """Enumerate each active reference once; omit all reference net assignments."""
    root = Path(case_root).resolve()
    cases, instances = [], []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name == "__analysis__":
            continue
        files = _case_netlists(directory)
        cases.append({"case": directory.name, "files": [path.name for path in files]})
        for path in files:
            data = path.read_bytes()
            document_hash = hashlib.sha256(data).hexdigest()
            for key, device in json.loads(data).items():
                if device.get("type") != "ic" or device.get("is_abstract") is not False:
                    continue
                part = device.get("id")
                if part is not None and (not isinstance(part, str) or not IDENTITY.fullmatch(part)):
                    raise ValueError(f"invalid identity in {path.name}/{key}")
                instances.append({"case": directory.name, "file": path.name, "component": key,
                    "designator": device.get("designator"), "name": device.get("name"), "id": part,
                    "reference_sha256": document_hash,
                    "pin_numbers": [str(pin["id"]) for pin in device["pins"]]})
    return {"cases": cases, "instances": instances}


def inventory_named_physical_devices(case_root: str | Path = CASE_ROOT, *,
                                     component_types: Sequence[str]) -> dict[str, Any]:
    """Enumerate selected named device classes without returning pins or values.

    Types are an explicit audit selection, not automatic generation requirements.
    Manufacturer and supplier identities still need the original BOM and source
    authentication; a cleaned abstract flag neither proves nor rejects identity.
    """
    if (not isinstance(component_types, (list, tuple)) or not component_types
            or any(not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z_]*", kind)
                   or kind in {"ic", "module"} for kind in component_types)
            or len(component_types) != len(set(component_types))):
        raise ValueError("a unique explicit selection of non-IC/module component types is required")
    root = Path(case_root).resolve()
    instances = []
    for path in _inventory_netlists(root):
        raw = path.read_bytes(); document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError(f"component mapping required in {path.name}")
        sha = hashlib.sha256(raw).hexdigest()
        for key, device in document.items():
            if (not isinstance(device, Mapping) or device.get("type") not in component_types
                    or not isinstance(device.get("name"), str) or not device["name"].strip()):
                continue
            part = device.get("id")
            if part is not None and (not isinstance(part, str) or not IDENTITY.fullmatch(part)):
                raise ValueError(f"invalid named physical identity in {path.name}/{key}")
            instances.append({"case": path.parent.name, "file": path.name, "component": key,
                "designator": device.get("designator"), "name": device["name"], "id": part,
                "source_type": device["type"], "source_is_abstract": device.get("is_abstract"),
                "reference_sha256": sha})
    return {"scope": "named_physical_identity_metadata_only", "component_types": sorted(component_types),
            "instances": instances, "instance_count": len(instances),
            "reference_connections_included": False, "reference_pins_included": False,
            "reference_values_included": False, "generation_requirements_inferred": False}


def original_identity_metadata(inventory: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Read only whitelisted device properties from original case ENET files.

    Cleaned component keys can change; require a unique original designator.
    The merged keyboard uses its core inventory's explicit original ENET link.
    Pin maps, connections, layout and neighboring components are never returned.
    """
    inventory=inventory or inventory_cases();cache={};core_cache={};result=[]
    allowed={"DeviceName","Name","Manufacturer","Manufacturer Part","Supplier Part",
             "Supplier Footprint","FootprintName","Datasheet","Device","Symbol","Footprint"}
    root=(WORKLIB/"02_CASE"/"02_dataset").resolve()
    for instance in inventory["instances"]:
        source_component = None
        if instance["case"] == "case1-keyboard" and instance["file"] == "case1-keyboard-cleaned.json":
            case_root = root / instance["case"]
            if instance["case"] not in core_cache:
                core_cache[instance["case"]] = json.loads((case_root / "core_devices.json").read_text(encoding="utf-8"))
            links = [(device, link) for device in core_cache[instance["case"]]
                     for link in device.get("instances", [])
                     if link.get("netlist") == instance["file"] and link.get("component") == instance["component"]]
            if len(links) != 1:
                raise ValueError("merged keyboard IC needs one original source link")
            device, link = links[0]
            if (any(device.get(field) != instance.get(field) for field in ("id", "name"))
                    or link.get("designator") != instance["designator"]):
                raise ValueError("merged keyboard core inventory does not match its reference")
            source_name, source_component = link.get("source_netlist"), link.get("source_component")
            if not isinstance(source_name, str) or not isinstance(source_component, str) or not source_component:
                raise ValueError("merged keyboard IC is missing original ENET provenance")
            source = (case_root / source_name).resolve()
            if source.parent != (case_root / "netlist").resolve() or source.suffix != ".enet":
                raise ValueError("merged keyboard original source escaped its netlist directory")
            if not source.is_file():
                raise ValueError("merged keyboard original ENET source is missing")
        else:
            case_root = root / instance["case"]
            if instance["file"] == f"{instance['case']}-cleaned.json":
                sources = sorted((case_root / "netlist").glob("*.enet"))
                if not sources:
                    continue
                if len(sources) != 1:
                    raise ValueError("cleaned reference needs one original ENET or explicit source provenance")
                source = sources[0].resolve()
            else:
                source = (case_root / "netlist" / Path(instance["file"]).with_suffix(".enet")).resolve()
        if not source.is_relative_to(root):raise ValueError("original source escaped case root")
        if not source.is_file():continue
        if source not in cache:
            raw=source.read_bytes();document=json.loads(raw)
            cache[source]=(hashlib.sha256(raw).hexdigest(),[(key,dict(component.get("props",{}))) for key,component in document["components"].items()])
        sha,components=cache[source]
        matches=[(key,props) for key,props in components if props.get("Designator")==instance["designator"]
                 and (source_component is None or key == source_component)]
        if source_component is not None and len(matches) != 1:
            raise ValueError("merged keyboard original ENET component does not match its designator")
        if len(matches)!=1:continue
        key,props=matches[0]
        result.append({"case":instance["case"],"file":instance["file"],"component":instance["component"],
            "original_component":key,"designator":instance["designator"],"id":instance["id"],
            "source_file":str(source),"source_sha256":sha,"source_scope":"identity_metadata_only",
            "props":{field:props[field] for field in sorted(allowed) if props.get(field)}})
    return result


def inventory_functional_modules(case_root: str | Path = CASE_ROOT) -> dict[str, Any]:
    """Inventory named module instances independently from the IC audit.

    The cleaned format marks physical modules as abstract too. Keep that source
    flag and the original component identity; do not substitute the module's
    internal IC or infer any circuit connection from its reference pins.
    """
    root = Path(case_root).resolve()
    instances = []
    for path in _inventory_netlists(root):
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        for key, device in json.loads(raw).items():
            if device.get("type") != "module" or not isinstance(device.get("name"), str) or not device["name"].strip():
                continue
            part = device.get("id")
            if part is not None and (not isinstance(part, str) or not IDENTITY.fullmatch(part)):
                raise ValueError(f"invalid module identity in {path.name}/{key}")
            instances.append({"case": path.parent.name, "file": path.name, "component": key,
                "designator": device.get("designator"), "name": device["name"], "id": part,
                "reference_sha256": sha, "source_type": "module",
                "source_is_abstract": device.get("is_abstract"),
                "pin_numbers": [str(pin["id"]) for pin in device.get("pins", [])]})
    case_files: dict[str, set[str]] = {}
    for row in instances:
        case_files.setdefault(row["case"], set()).add(row["file"])
    return {"scope": "functional_modules_separate_from_ic", "instances": instances,
            "cases": [{"case": case, "files": sorted(files)} for case, files in sorted(case_files.items())],
            "instance_count": len(instances), "unique_names": len({row["name"] for row in instances}),
            "reference_connections_included": False}


class PreparationService:
    """Read-back audit and resumable, additive preparation of datasheet assets."""

    def __init__(self, config: KnowledgeStoreConfig | None = None) -> None:
        self.config = config or KnowledgeStoreConfig.from_env()

    def _mysql(self, database: str = "device_parameters") -> Any:
        import mysql.connector
        cfg = self.config
        return mysql.connector.connect(host=cfg.mysql_host, port=cfg.mysql_port,
            user=cfg.mysql_user, password=cfg.mysql_password, database=database,
            connection_timeout=10, autocommit=False)

    def open_catalog_connection(self) -> Any:
        """Open an idle catalog connection for evidence-backed writer APIs."""
        return self._mysql("device_parameters")

    @contextmanager
    def preparation_lock(self, supplier_part: str, *, timeout_s: int = 30):
        """Serialize additive document preparation for an identity across processes."""
        if not IDENTITY.fullmatch(supplier_part):raise ValueError("invalid identity")
        cn=self._mysql("datasheet_assets");acquired=False
        try:
            cur=cn.cursor();name="pcbagent_datasheet_"+supplier_part
            cur.execute("SELECT GET_LOCK(%s,%s)",(name,timeout_s));acquired=cur.fetchone()[0]==1
            if not acquired:raise RuntimeError("device preparation already active; retry later")
            yield
        finally:
            if acquired:
                cur.execute("SELECT RELEASE_LOCK(%s)",(name,));cur.fetchone()
            cn.close()

    def read_device_knowledge(self, supplier_part: str, *, evidence_limit: int = 30) -> dict[str, Any]:
        return self.knowledge(supplier_part, evidence_limit=evidence_limit)

    def read_source_documents(self, supplier_part: str) -> list[dict[str, Any]]:
        from .source_evidence import read_source_documents
        return read_source_documents(self, supplier_part)

    def register_secondary_pdf(self, supplier_part: str, **source: Any) -> dict[str, Any]:
        from .secondary_pdf import register_secondary_pdf
        return register_secondary_pdf(self, supplier_part, **source)

    def reconcile_wrong_primary_pdf(self, supplier_part: str, **source: Any) -> dict[str, Any]:
        """Explicitly replace a misclassified primary after archival and guards."""
        from .native_pdf import reconcile_wrong_primary_pdf
        return reconcile_wrong_primary_pdf(self, supplier_part, **source)

    def register_datasheet_revision(self, supplier_part: str, **source: Any) -> dict[str, Any]:
        from .datasheet_revision import register_datasheet_revision
        return register_datasheet_revision(self, supplier_part, **source)

    def select_datasheet_revision(self, supplier_part: str, **selection: Any) -> dict[str, Any]:
        from .datasheet_revision import select_datasheet_revision
        return select_datasheet_revision(self, supplier_part, **selection)

    def read_selected_source_evidence(self, supplier_part: str, **selection: Any) -> dict[str, Any]:
        from .datasheet_revision import read_selected_source_evidence
        return read_selected_source_evidence(self, supplier_part, **selection)

    def read_secondary_pdf_evidence(self, supplier_part: str, **selection: Any) -> list[dict[str, Any]]:
        from .secondary_pdf import read_secondary_pdf_evidence
        return read_secondary_pdf_evidence(self, supplier_part, **selection)

    def render_pdf_page(self, supplier_part: str, page: int, *, source_pdf_sha256: str | None = None,
                        source_selection: Any = None,
                        revision_identity_origin_sha256: str | None = None) -> dict[str, Any]:
        from .pdf_review import render_pdf_page
        return render_pdf_page(self, supplier_part, page, source_pdf_sha256=source_pdf_sha256,
            source_selection=source_selection, revision_identity_origin_sha256=revision_identity_origin_sha256)

    def register_pdf_page_review(self, supplier_part: str, page: int, **review: Any) -> dict[str, Any]:
        from .pdf_review import register_pdf_page_review
        return register_pdf_page_review(self, supplier_part, page, **review)

    def register_public_cad_sources(self, supplier_part: str, **sources: Any) -> dict[str, Any]:
        from .pin_positions import register_public_cad_sources
        return register_public_cad_sources(self, supplier_part, **sources)

    def index_pdf_page_review(self, supplier_part: str, receipt_sha256: str) -> dict[str, Any]:
        from .pdf_review import index_pdf_page_review
        return index_pdf_page_review(self, supplier_part, receipt_sha256)

    def register_pin_identity_mapping(self, proof: Any) -> dict[str, Any]:
        from .pin_identity import register_pin_identity_mapping
        return register_pin_identity_mapping(self, proof)

    def read_pin_identity_mappings(self, supplier_part: str, *, mapping_sha256: str | None = None) -> list[dict[str, Any]]:
        from .pin_identity import read_pin_identity_mappings
        return read_pin_identity_mappings(self, supplier_part, mapping_sha256=mapping_sha256)

    def authenticate_pin_identity_mapping(self, supplier_part: str, mapping_sha256: str, field: str, value: str) -> dict[str, Any]:
        from .pin_identity import authenticate_pin_identity_mapping
        return authenticate_pin_identity_mapping(self, supplier_part, mapping_sha256, field, value)

    def read_catalog_field_evidence(self, supplier_part: str) -> list[dict[str, Any]]:
        """Read evidence whose asserted value still equals the current SQL field.

        Correction records retain their original old/new payload; only ``new``
        is compared. Deleted rows, stale values and missing fields are excluded.
        This returns source claims for independent authentication, not a claim
        that every historical catalog source has already been authenticated.
        """
        if not isinstance(supplier_part, str) or not IDENTITY.fullmatch(supplier_part):
            raise ValueError("valid identity required")
        cn = self.open_catalog_connection()
        try:
            cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='device_parameters' AND table_name='catalog_field_evidence'")
            if not cur.fetchone()["n"]:
                return []
            cur.execute("SELECT * FROM devices WHERE supplier_part=%s", (supplier_part,))
            devices = cur.fetchall()
            if not devices:
                return []
            if len(devices) != 1:
                raise ValueError("catalog identity is ambiguous")
            device = devices[0]
            cur.execute("SELECT * FROM pins WHERE device_id=%s ORDER BY pin_index", (device["device_id"],))
            pin_rows = cur.fetchall()
            pins = {str(row["pin_index"]): row for row in pin_rows}
            if len(pins) != len(pin_rows):
                raise ValueError("catalog pin indexes are ambiguous")
            cur.execute("SELECT evidence_id,entity,entity_key,field_name,action,value_json,evidence_json,created_at FROM catalog_field_evidence WHERE supplier_part=%s ORDER BY created_at DESC,evidence_id", (supplier_part,))
            records = cur.fetchall(); params = {}
            if any(row["entity"] == "parameter" for row in records):
                category = device.get("category_level_2")
                if not isinstance(category, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,57}", category):
                    raise ValueError("invalid catalog parameter category")
                cur.execute("SELECT * FROM `params_" + category + "` WHERE device_id=%s", (device["device_id"],))
                rows = cur.fetchall()
                if len(rows) > 1:
                    raise ValueError("catalog parameter rows are ambiguous")
                params = rows[0] if rows else {}
            result = []
            for row in records:
                action = row["action"]
                if action not in {"insert", "fill", "correct", "retained"}:
                    continue
                value = json.loads(row["value_json"]) if isinstance(row["value_json"], str) else row["value_json"]
                if action == "correct":
                    if not isinstance(value, dict) or set(value) != {"old", "new"}:
                        continue
                    asserted = value["new"]
                else:
                    asserted = value
                current = {"device": device, "pin": pins.get(row["entity_key"], {}), "parameter": params}.get(row["entity"], {})
                field = row["field_name"]
                if field not in current or json.dumps(asserted, sort_keys=True, default=str) != json.dumps(current[field], sort_keys=True, default=str):
                    continue
                evidence = json.loads(row["evidence_json"]) if isinstance(row["evidence_json"], str) else row["evidence_json"]
                result.append({"evidence_id": row["evidence_id"], "supplier_part": supplier_part,
                    "entity": row["entity"], "entity_key": row["entity_key"], "field_name": field,
                    "pin_number": current.get("pin_number") if row["entity"] == "pin" else None,
                    "value": value, "current_value": current[field], "action": action,
                    "evidence": evidence, "created_at": str(row["created_at"]),
                    "source_authentication_required": True})
            return result
        finally:
            cn.rollback(); cn.close()

    def read_primary_source_evidence(self, supplier_part: str, *, pages: list[int] | None = None,
                                     query: str | None = None, limit: int = 20,
                                     include_symbol_mappings: bool = True) -> dict[str, Any]:
        from .source_evidence import read_primary_source_evidence
        return read_primary_source_evidence(self, supplier_part, pages=pages, query=query,
            limit=limit, include_symbol_mappings=include_symbol_mappings)

    def read_symbol_mappings(self, supplier_part: str) -> list[dict[str, Any]]:
        """Read filtered CAD mappings; never read reference assignments here."""
        if not IDENTITY.fullmatch(supplier_part):raise ValueError("invalid identity")
        cn=self._mysql("datasheet_assets");result=[]
        try:
            cn.start_transaction(readonly=True);cur=cn.cursor(dictionary=True)
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_source_documents'")
            if not cur.fetchone()["n"]:return []
            cur.execute("SELECT source_sha256,source_url,source_bytes,quality_json FROM datasheet_source_documents WHERE lichuang_id=%s AND source_mime='application/json'",(supplier_part,))
            for row in cur.fetchall():
                data=bytes(row["source_bytes"])
                if hashlib.sha256(data).hexdigest()!=row["source_sha256"]:raise ValueError("mapping source hash mismatch")
                payload=json.loads(data)
                quality=row["quality_json"]
                quality=json.loads(quality) if isinstance(quality,str) else quality
                if quality.get("qualification")=="public_lcsc_std_component_original_json":
                    from .std_symbol import parse_std_symbol
                    if quality.get("supplier_part")!=supplier_part or row["source_url"]!=f"https://lceda.cn/api/products/{supplier_part}/components":
                        raise ValueError("public symbol source identity differs")
                    result.append(parse_std_symbol(data,supplier_part,expected_model=quality.get("model")))
                    continue
                if payload.get("schema")!="pcbagent.local_symbol_mapping.v1" or payload.get("supplier_part")!=supplier_part:
                    continue
                result.append({"source_kind":"local_symbol","source_ref":row["source_url"],
                    "document_sha256":row["source_sha256"],"mapping":payload})
        finally:cn.rollback();cn.close()
        return result

    def register_public_std_symbol(self, supplier_part: str, **kwargs: Any) -> dict[str, Any]:
        """Register original public LCSC Std bytes for identity-only mappings."""
        from .std_symbol import register_std_symbol
        return register_std_symbol(self, supplier_part, **kwargs)

    def read_evidence(self, supplier_part: str, query: str, *, limit: int = 20) -> dict[str, Any]:
        """Retrieve identity-filtered BM25 evidence with page and document hash."""
        if not IDENTITY.fullmatch(supplier_part) or not str(query).strip():
            raise ValueError("valid identity and a nonempty query are required")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        metadata = self.knowledge(supplier_part,evidence_limit=1)
        mc=self._milvus()
        try:
            hits=mc.search(collection_name=self.config.milvus_collection,data=[query],
                anns_field="content_sparse_vector",filter="lichuang_id == "+json.dumps(supplier_part),
                limit=limit,search_params={"metric_type":"BM25"},
                output_fields=["id","lichuang_id","title","content","content_type","page_idx","section_path","image_id"])
            metadata["evidence"]=[{"score":hit["distance"],**hit["entity"],
                "document_sha256":(metadata["document"] or {}).get("pdf_sha256") or (metadata["source_quality"] or {}).get("document_sha256")} for hit in (hits[0] if hits else [])]
        finally:
            mc.close()
        from .web_source import decorate_retained_web_chunks
        metadata["evidence"] = decorate_retained_web_chunks(metadata, metadata["evidence"])
        from .pdf_review import decorate_review_chunks
        metadata["evidence"] = decorate_review_chunks(self, supplier_part, metadata["evidence"])
        from .secondary_pdf import decorate_secondary_chunks
        metadata["evidence"] = decorate_secondary_chunks(self, supplier_part, metadata["evidence"])
        return metadata

    def read_chunks(self, supplier_part: str, ids: list[int]) -> dict[str, Any]:
        """Read cited chunk IDs, always also constraining their device identity."""
        if not IDENTITY.fullmatch(supplier_part) or len(ids)>1000:
            raise ValueError("valid identity and at most 1000 chunk IDs required")
        if any(type(value) is not int or value<0 for value in ids):
            raise ValueError("chunk IDs must be nonnegative integers")
        metadata=self.knowledge(supplier_part,evidence_limit=1)
        metadata["evidence"]=[]
        if not ids:
            return metadata
        mc=self._milvus()
        try:
            chunks=mc.query(collection_name=self.config.milvus_collection,
                filter="lichuang_id == "+json.dumps(supplier_part)+" and id in "+json.dumps(sorted(set(ids))),
                limit=len(set(ids)),consistency_level="Strong",
                output_fields=["id","lichuang_id","title","content","content_type","page_idx","section_path","image_id"])
            metadata["evidence"]=[{**row,"document_sha256":(metadata["document"] or {}).get("pdf_sha256") or (metadata["source_quality"] or {}).get("document_sha256")} for row in chunks]
        finally:
            mc.close()
        from .web_source import decorate_retained_web_chunks
        metadata["evidence"] = decorate_retained_web_chunks(metadata, metadata["evidence"])
        from .pdf_review import decorate_review_chunks
        metadata["evidence"] = decorate_review_chunks(self, supplier_part, metadata["evidence"])
        from .secondary_pdf import decorate_secondary_chunks
        metadata["evidence"] = decorate_secondary_chunks(self, supplier_part, metadata["evidence"])
        return metadata

    def read_pdf_page_evidence(self, supplier_part: str, *, pages: list[int] | None = None,
                               query: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Read exact one-based PDF pages from SQL, with verified document hash.

        These source transcriptions use pdf:<sha256>:page:<N> citations and do
        not mutate or relabel older Milvus chunks with unknown page indices.
        Coverage metadata distinguishes pages actually scanned from the query
        matches returned after limiting. Scanned pages may have no native text;
        this metadata does not claim visual/table interpretation completeness.
        """
        if not IDENTITY.fullmatch(supplier_part) or type(limit) is not int or not 1<=limit<=200:
            raise ValueError("valid identity and limit 1..200 required")
        if pages is not None and (len(pages)>200 or any(type(p) is not int or p<1 for p in pages)):
            raise ValueError("pages must be at most 200 positive one-based integers")
        metadata=self.knowledge(supplier_part,evidence_limit=1)
        metadata.update(native_pdf_page_count=None, native_pdf_pages_scanned=[],
                        native_pdf_evidence_total_before_limit=0,
                        native_pdf_evidence_truncated=False)
        cn=self._mysql("datasheet_assets")
        try:
            cn.start_transaction(readonly=True);cur=cn.cursor(dictionary=True)
            cur.execute("SELECT pdf_bytes,pdf_sha256 FROM datasheet_documents WHERE lichuang_id=%s",(supplier_part,))
            row=cur.fetchone()
        finally:
            cn.rollback();cn.close()
        if not row or not row["pdf_bytes"]:
            metadata["evidence"]=[];metadata["pdf_status"]="original_pdf_unavailable";return metadata
        data=bytes(row["pdf_bytes"]);sha=hashlib.sha256(data).hexdigest()
        if sha!=row["pdf_sha256"]:
            raise ValueError("stored PDF hash mismatch")
        import pypdfium2 as pdfium
        from .native_pdf import _PDFIUM_LOCK
        evidence=[];terms=re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+",query.casefold()) if query else []
        source_kind = "related_document" if str((metadata.get("source_quality") or {}).get("original_source_kind", "")).startswith("related_") else "datasheet"
        with _PDFIUM_LOCK:
            document=pdfium.PdfDocument(data)
            try:
                metadata["native_pdf_page_count"] = len(document)
                if pages and max(pages)>len(document):raise ValueError("requested page exceeds source PDF")
                for index in range(len(document)):
                    if pages is not None and index+1 not in pages:continue
                    page=document[index];textpage=page.get_textpage()
                    try:text=textpage.get_text_range().strip()
                    finally:textpage.close();page.close()
                    metadata["native_pdf_pages_scanned"].append(index + 1)
                    score=sum(text.casefold().count(term) for term in terms)
                    if terms and not score:continue
                    evidence.append({"id":f"pdf:{sha}:page:{index+1}","source_ref":f"pdf:{sha}:page:{index+1}",
                        "lichuang_id":supplier_part,"title":f"Original PDF page {index+1}",
                        "content":text,"content_type":"text","page_idx":index,"image_id":"",
                        "document_sha256":sha,"score":score,"source_quality":"native_pdf_text_only",
                        "source_kind":source_kind,"provenance":"original_pdf_native"})
            finally:document.close()
        if terms:evidence.sort(key=lambda r:(-r["score"],r["page_idx"]))
        metadata["native_pdf_evidence_total_before_limit"] = len(evidence)
        metadata["native_pdf_evidence_truncated"] = len(evidence) > limit
        metadata["evidence"]=evidence[:limit];metadata["pdf_status"]="verified_original_pdf"
        metadata["stored_source_quality"]=metadata["source_quality"]
        inherited={key:value for key,value in (metadata["source_quality"] or {}).items()
                   if key in {"identity_qualification","text_encoding_review_required","text_encoding_note","acquisition","original_source_kind"}}
        metadata["source_quality"]={**inherited,"method":"original_pdf_native","qualification":"text_only",
            "pdf_sha256":sha,"visual_interpretation":"unverified","table_interpretation":"unverified"}
        return metadata

    def ensure_identity_schema(self) -> None:
        cn = self._mysql()
        try:
            cur = cn.cursor()
            cur.execute(IDENTITY_DDL)
            cur.execute(MODULE_IDENTITY_DDL)
            cur.execute(PHYSICAL_IDENTITY_DDL)
            cn.commit()
        finally:
            cn.close()

    def bind_identity(self, instance: Mapping[str, Any], *, supplier_part: str | None,
                      source: Mapping[str, Any], status: str = "confirmed") -> dict[str, Any]:
        """Persist one immutable source-instance binding; allocate S if requested.

        S allocation is serialized against this registry and existing catalogs.
        It identifies a project-scoped device, never asserts a guessed C match.
        Existing C/S identities cannot be overwritten. Evidence must state the
        performed search and uncertainty for project-scoped identities.
        """
        return self._bind_identity("case_device_identity", instance,
            supplier_part=supplier_part, source=source, status=status)

    def bind_module_identity(self, instance: Mapping[str, Any], *, supplier_part: str | None,
                             source: Mapping[str, Any], status: str = "confirmed") -> dict[str, Any]:
        """Bind a physical module outside the immutable-format IC resolution map."""
        if instance.get("source_type") != "module" or source.get("scope") != "functional_module":
            raise ValueError("explicit functional module identity scope required")
        return self._bind_identity("case_functional_module_identity", instance,
            supplier_part=supplier_part, source=source, status=status)

    def _bind_identity(self, table: str, instance: Mapping[str, Any], *, supplier_part: str | None,
                       source: Mapping[str, Any], status: str) -> dict[str, Any]:
        if table not in _IDENTITY_TABLES:
            raise ValueError("unsupported identity registry")
        if not source.get("source_ref") or not source.get("reason"):
            raise ValueError("identity source_ref and reason are required")
        if status not in {"confirmed", "project_scoped", "unresolved_model"}:
            raise ValueError("invalid identity status")
        if supplier_part is not None and not IDENTITY.fullmatch(supplier_part):
            raise ValueError("invalid C/S identity")
        original = instance.get("id")
        if original and supplier_part != original:
            raise ValueError("cannot override the input's confirmed identity")
        fields = [str(instance[k]) for k in ("case", "file", "component", "reference_sha256")]
        if not re.fullmatch(r"[0-9a-f]{64}", fields[-1]):
            raise ValueError("reference SHA256 required")
        key = hashlib.sha256(json.dumps(fields).encode()).hexdigest()
        cn = self._mysql()
        locked = False
        try:
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT GET_LOCK('pcbagent_project_identity',30) AS acquired")
            locked = cur.fetchone()["acquired"] == 1
            if not locked:
                raise RuntimeError("identity allocation lock unavailable")
            cur.execute(f"SELECT * FROM {table} WHERE mapping_key=%s", (key,))
            old = cur.fetchone()
            if old:
                if supplier_part and old["supplier_part"] != supplier_part:
                    raise ValueError("existing identity binding conflict; explicit migration required")
                return {"supplier_part": old["supplier_part"], "status": old["identity_status"], "existing": True}
            if supplier_part is None:
                cur.execute("SELECT supplier_part FROM devices WHERE supplier_part LIKE 'S%' UNION SELECT supplier_part FROM case_device_identity WHERE supplier_part LIKE 'S%' UNION SELECT lichuang_id FROM datasheet_assets.datasheet_documents WHERE lichuang_id LIKE 'S%'")
                used = {row["supplier_part"] for row in cur.fetchall()}
                for registry in sorted(_IDENTITY_TABLES - {"case_device_identity"}):
                    cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='device_parameters' AND table_name=%s", (registry,))
                    if cur.fetchone()["n"]:
                        cur.execute(f"SELECT supplier_part FROM {registry} WHERE supplier_part LIKE 'S%'")
                        used.update(row["supplier_part"] for row in cur.fetchall())
                highest = max([1] + [int(part[1:]) for part in used if IDENTITY.fullmatch(part)])
                supplier_part = f"S{highest + 1:04d}"
            cur.execute(f"INSERT INTO {table} (mapping_key,case_name,source_file,component_key,reference_sha256,supplier_part,model_name,identity_status,source_json) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (key,*fields,supplier_part,instance.get("name"),status,json.dumps(dict(source),ensure_ascii=False)))
            cn.commit()
            return {"supplier_part": supplier_part, "status": status, "existing": False}
        except BaseException:
            cn.rollback()
            raise
        finally:
            if locked:
                cur.execute("SELECT RELEASE_LOCK('pcbagent_project_identity')")
                cur.fetchone()
            cn.close()

    def identity_map(self) -> dict[str, dict[str, Any]]:
        return self._identity_map("case_device_identity")

    def module_identity_map(self) -> dict[str, dict[str, Any]]:
        """Module overlay only; never insert these identities into format 1.3 IDs."""
        return self._identity_map("case_functional_module_identity")

    def bind_physical_identity(self, instance: Mapping[str, Any], *, supplier_part: str | None,
                               source: Mapping[str, Any], status: str = "confirmed") -> dict[str, Any]:
        """Persist named non-IC identities without changing reference netlist IDs."""
        kind = instance.get("source_type")
        if (not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z_]*", kind)
                or kind in {"ic", "module"} or source.get("scope") != "physical_device"
                or source.get("component_type") != kind):
            raise ValueError("exact named physical component type and identity scope required")
        return self._bind_identity("case_physical_device_identity", instance,
            supplier_part=supplier_part, source=source, status=status)

    def physical_identity_map(self) -> dict[str, dict[str, Any]]:
        """Return only the physical overlay, including its explicit source type."""
        rows = self._identity_map("case_physical_device_identity")
        for row in rows.values():
            source = row["source"]
            kind = source.get("component_type")
            if source.get("scope") != "physical_device" or not isinstance(kind, str) or kind in {"ic", "module"}:
                raise ValueError("stored physical identity has an invalid scope or type")
            row["component_type"] = kind
        return rows

    def _identity_map(self, table: str) -> dict[str, dict[str, Any]]:
        if table not in _IDENTITY_TABLES:
            raise ValueError("unsupported identity registry")
        cn = self._mysql()
        try:
            cn.start_transaction(readonly=True)
            cur = cn.cursor(dictionary=True)
            cur.execute(f"SELECT case_name,source_file,component_key,reference_sha256,supplier_part,model_name,identity_status,source_json,version FROM {table} ORDER BY created_at")
            result = {}
            for row in cur.fetchall():
                source = json.loads(row["source_json"]) if isinstance(row["source_json"],str) else row["source_json"]
                key = "/".join(row[k] for k in ("case_name","source_file","component_key"))
                result[key] = {"supplier_part":row["supplier_part"],"reference_sha256":row["reference_sha256"],
                    "source_ref":source["source_ref"],"source":source,"status":row["identity_status"],"version":row["version"],"model_name":row["model_name"]}
            return result
        finally:
            cn.rollback();cn.close()

    def add_identity_evidence(self, supplier_part: str, source: Mapping[str, Any], *,
                              model_name: str | None = None, status: str | None = None) -> int:
        """Append better provenance while preserving previous evidence versions."""
        return self._add_identity_evidence("case_device_identity", supplier_part, source,
            model_name=model_name, status=status)

    def add_module_identity_evidence(self, supplier_part: str, source: Mapping[str, Any], *,
                                     model_name: str | None = None, status: str | None = None) -> int:
        if source.get("scope") != "functional_module":
            raise ValueError("explicit functional module identity scope required")
        return self._add_identity_evidence("case_functional_module_identity", supplier_part, source,
            model_name=model_name, status=status)

    def _add_identity_evidence(self, table: str, supplier_part: str, source: Mapping[str, Any], *,
                               model_name: str | None, status: str | None) -> int:
        if table not in _IDENTITY_TABLES:
            raise ValueError("unsupported identity registry")
        if not IDENTITY.fullmatch(supplier_part) or not source.get("source_ref") or not source.get("reason"):
            raise ValueError("valid identity and source evidence required")
        if status is not None and status not in {"confirmed","project_scoped","unresolved_model"}:
            raise ValueError("invalid identity status")
        cn=self._mysql()
        try:
            cur=cn.cursor(dictionary=True)
            cur.execute(f"SELECT mapping_key,version,source_json,model_name,identity_status FROM {table} WHERE supplier_part=%s FOR UPDATE",(supplier_part,))
            rows=cur.fetchall()
            for row in rows:
                old=json.loads(row["source_json"]) if isinstance(row["source_json"],str) else row["source_json"]
                history=list(old.get("history",[]))
                history.append({"version":row["version"],"source":{k:v for k,v in old.items() if k!="history"},"model_name":row["model_name"],"status":row["identity_status"]})
                updated={**dict(source),"history":history}
                cur.execute(f"UPDATE {table} SET source_json=%s,model_name=COALESCE(%s,model_name),identity_status=COALESCE(%s,identity_status),version=version+1 WHERE mapping_key=%s AND version=%s",(json.dumps(updated,ensure_ascii=False),model_name,status,row["mapping_key"],row["version"]))
                if cur.rowcount!=1:
                    raise RuntimeError("identity source changed concurrently")
            cn.commit();return len(rows)
        except BaseException:
            cn.rollback();raise
        finally:
            cn.close()

    def _milvus(self) -> Any:
        from pymilvus import MilvusClient
        cfg = self.config
        return MilvusClient(uri=cfg.milvus_url, token=cfg.milvus_token or "",
                            db_name=cfg.milvus_database, timeout=20,
                            alias="preparation_" + uuid4().hex)

    def knowledge(self, supplier_part: str, *, evidence_limit: int = 30) -> dict[str, Any]:
        """Generation-safe device facts: SQL pins plus identified datasheet chunks."""
        if not IDENTITY.fullmatch(supplier_part):
            raise ValueError("supplier_part must be a C/S identity")
        if type(evidence_limit) is not int or not 1 <= evidence_limit <= 200:
            raise ValueError("evidence_limit must be between 1 and 200")
        cn = self._mysql()
        try:
            cn.start_transaction(readonly=True)
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT device_id, supplier_part, device_name, category_level_1, category_level_2, pin_count FROM devices WHERE supplier_part=%s", (supplier_part,))
            device = cur.fetchone()
            pins = []
            if device:
                cur.execute("SELECT pin_index,pin_number,pin_name,pin_type,pin_description,pin_connection_notes,pin_impedance,pin_frequency FROM pins WHERE device_id=%s ORDER BY pin_index", (device["device_id"],))
                pins = cur.fetchall()
            identity_constraints=[]
            for identity_table in sorted(_IDENTITY_TABLES):
                cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='device_parameters' AND table_name=%s", (identity_table,))
                if not cur.fetchone()["n"]:
                    continue
                cur.execute(f"SELECT model_name,identity_status,source_json,version FROM {identity_table} WHERE supplier_part=%s",(supplier_part,))
                for row in cur.fetchall():
                    source=row.pop("source_json")
                    source=json.loads(source) if isinstance(source,str) else source
                    row["source"]={key:value for key,value in source.items() if key!="history"}
                    if identity_table == "case_functional_module_identity":
                        row["registry_scope"] = "functional_module"
                    elif identity_table == "case_physical_device_identity":
                        row["registry_scope"] = "physical_device"
                    identity_constraints.append(row)
            cur.execute("SELECT lichuang_id,product_name,pdf_sha256,pdf_size_bytes,pdf_url,chunk_count,image_count FROM datasheet_assets.datasheet_documents WHERE lichuang_id=%s", (supplier_part,))
            document = cur.fetchone()
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_ingestion_provenance'")
            quality = None
            if cur.fetchone()["n"]:
                cur.execute("SELECT details_json FROM datasheet_assets.datasheet_ingestion_provenance WHERE lichuang_id=%s",(supplier_part,))
                quality_row=cur.fetchone()
                if quality_row:
                    quality=json.loads(quality_row["details_json"]) if isinstance(quality_row["details_json"],str) else quality_row["details_json"]
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_source_documents'")
            source_assets=[]
            if cur.fetchone()["n"]:
                cur.execute("SELECT source_sha256,source_url,source_mime,is_primary,quality_json FROM datasheet_assets.datasheet_source_documents WHERE lichuang_id=%s",(supplier_part,))
                for asset in cur.fetchall():
                    asset_quality=asset.pop("quality_json")
                    asset["quality"]=json.loads(asset_quality) if isinstance(asset_quality,str) else asset_quality
                    if asset["is_primary"] and quality is None:
                        quality=asset["quality"]
                    source_assets.append(asset)
        finally:
            cn.rollback()
            cn.close()
        mc = self._milvus()
        try:
            chunks = mc.query(collection_name=self.config.milvus_collection,
                filter="lichuang_id == " + json.dumps(supplier_part), limit=evidence_limit,
                output_fields=["id", "lichuang_id", "title", "content", "content_type", "page_idx", "section_path", "image_id"],
                consistency_level="Strong")
        finally:
            mc.close()
        if quality is None and document:
            quality={"method":"legacy_optimized","qualification":"legacy_unverified",
                     "warning":"Legacy interpreted chunks require verification against original PDF or image; quote presence alone is insufficient.",
                     "pdf_sha256":document.get("pdf_sha256")}
        document_hash=(document or {}).get("pdf_sha256") or (quality or {}).get("document_sha256")
        chunks=[{**chunk,"document_sha256":document_hash} for chunk in chunks]
        from .web_source import decorate_retained_web_chunks
        chunks = decorate_retained_web_chunks({"source_quality": quality, "source_assets": source_assets}, chunks)
        from .pdf_review import decorate_review_chunks
        chunks = decorate_review_chunks(self, supplier_part, chunks)
        from .secondary_pdf import decorate_secondary_chunks
        chunks = decorate_secondary_chunks(self, supplier_part, chunks)
        return {"supplier_part": supplier_part, "device": device, "pins": pins,
                "document": document, "evidence": chunks,"source_quality":quality,"source_assets":source_assets,
                "identity_constraints":identity_constraints,
                "source_scope": "device_parameters_and_datasheet_only"}

    def audit(self, inventory: Mapping[str, Any] | None = None,
              *, identity_map: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Audit every identity against SQL metadata, actual pins, and Milvus counts.

        identity_map is keyed by case/file/component. Each entry must include
        supplier_part, a source reference and the unchanged reference SHA256.
        """
        inventory = inventory or inventory_cases()
        resolved = []
        for original in inventory["instances"]:
            row = dict(original)
            key = "/".join(row[k] for k in ("case", "file", "component"))
            resolution = (identity_map or {}).get(key)
            if resolution:
                part = resolution.get("supplier_part", "")
                if not IDENTITY.fullmatch(part) or not resolution.get("source_ref"):
                    raise ValueError(f"invalid identity resolution for {key}")
                if resolution.get("reference_sha256") != row["reference_sha256"]:
                    raise ValueError(f"stale reference identity mapping for {key}")
                if row["id"] and row["id"] != part:
                    raise ValueError(f"identity mapping would override confirmed {row['id']}")
                row["resolved_id"] = part
                row["resolution"] = dict(resolution)
            else:
                row["resolved_id"] = row["id"]
            resolved.append(row)
        ids = sorted({row["resolved_id"] for row in resolved if row["resolved_id"]})
        cn = self._mysql()
        try:
            cn.start_transaction(readonly=True)
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT d.device_id,d.supplier_part,d.device_name,d.category_level_1,d.category_level_2,d.pin_count,COUNT(p.pin_id) AS actual_pins FROM devices d LEFT JOIN pins p ON p.device_id=d.device_id GROUP BY d.device_id")
            catalog = {r["supplier_part"]: r for r in cur.fetchall()}
            cur.execute("SELECT lichuang_id,product_name,pdf_sha256,pdf_size_bytes,chunk_count,image_count,ingested_at FROM datasheet_assets.datasheet_documents")
            documents = {r["lichuang_id"]: r for r in cur.fetchall()}
            cur.execute("SELECT d.supplier_part,p.pin_number FROM devices d JOIN pins p ON d.device_id=p.device_id")
            pin_numbers: dict[str, list[str]] = {}
            for row in cur.fetchall():
                pin_numbers.setdefault(row["supplier_part"], []).append(row["pin_number"])
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_ingestion_provenance'")
            provenance={}
            if cur.fetchone()["n"]:
                cur.execute("SELECT lichuang_id,details_json FROM datasheet_assets.datasheet_ingestion_provenance")
                provenance={r["lichuang_id"]:json.loads(r["details_json"]) if isinstance(r["details_json"],str) else r["details_json"] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='datasheet_assets' AND table_name='datasheet_source_documents'")
            if cur.fetchone()["n"]:
                cur.execute("SELECT lichuang_id,quality_json FROM datasheet_assets.datasheet_source_documents WHERE is_primary=TRUE")
                for row in cur.fetchall():
                    provenance.setdefault(row["lichuang_id"],json.loads(row["quality_json"]) if isinstance(row["quality_json"],str) else row["quality_json"])
        finally:
            cn.rollback()
            cn.close()
        coverage = []
        mc = self._milvus()
        try:
            for part in ids:
                count = mc.query(collection_name=self.config.milvus_collection,
                    filter="lichuang_id == " + json.dumps(part), output_fields=["count(*)"],
                    consistency_level="Strong")[0]["count(*)"]
                device, document = catalog.get(part), documents.get(part)
                if document and part not in provenance:
                    provenance[part]={"method":"legacy_optimized","qualification":"legacy_unverified",
                        "warning":"Legacy interpreted chunks require original-source verification."}
                issues = []
                notices = []
                if not device:
                    issues.append("catalog_missing")
                elif device["pin_count"] != device["actual_pins"]:
                    issues.append("catalog_pin_count_mismatch")
                if len(set(pin_numbers.get(part, []))) != len(pin_numbers.get(part, [])):
                    issues.append("duplicate_catalog_pin_number")
                mismatches = []
                for instance in resolved:
                    if instance["resolved_id"] != part:
                        continue
                    expected, actual = set(instance["pin_numbers"]), set(pin_numbers.get(part, []))
                    if expected != actual:
                        mismatches.append({"case": instance["case"], "file": instance["file"],
                            "component": instance["component"], "catalog_missing_numbers": sorted(expected-actual),
                            "reference_missing_numbers": sorted(actual-expected)})
                if any(row["catalog_missing_numbers"] for row in mismatches):
                    issues.append("catalog_missing_reference_pin_numbers")
                if any(row["reference_missing_numbers"] and not row["catalog_missing_numbers"] for row in mismatches):
                    notices.append("reference_has_subset_of_catalog_pins")
                if any(row["reference_missing_numbers"] and row["catalog_missing_numbers"] for row in mismatches):
                    notices.append("catalog_and_reference_pin_number_sets_differ")
                if not document:
                    issues.append("document_missing")
                elif not document["pdf_sha256"] or not document["pdf_size_bytes"]:
                    issues.append("source_pdf_missing")
                if count < 1:
                    issues.append("milvus_missing")
                if document and document["chunk_count"] != count:
                    issues.append("cross_store_chunk_count_mismatch")
                coverage.append({"id": part, "catalog": device, "document": document,
                    "milvus_chunks": count, "issues": issues, "pin_mismatches": mismatches,
                    "notices":notices,"source_quality":provenance.get(part),
                    "assets_ready": bool(document and count and document["chunk_count"] == count
                                         and document["pdf_sha256"] and document["pdf_size_bytes"]),
                    "ready": not issues,"fully_interpreted":False if provenance.get(part) else None})
        finally:
            mc.close()
        return {"schema": "pcbagent.case_knowledge_audit.v1", "cases": inventory["cases"],
            "instances": resolved, "coverage": coverage, "summary": {
                "case_count": len(inventory["cases"]), "file_count": sum(len(r["files"]) for r in inventory["cases"]),
                "core_instances": len(resolved), "unique_identified": len(ids),
                "missing_id_instances": sum(not r["resolved_id"] for r in resolved),
                "catalog_devices": sum(bool(r["catalog"]) for r in coverage),
                "documents": sum(bool(r["document"]) for r in coverage),
                "milvus_devices": sum(r["milvus_chunks"] > 0 for r in coverage),
                "ready_devices": sum(r["ready"] for r in coverage),
                "issue_counts": dict(Counter(issue for r in coverage for issue in r["issues"]))}}

    def audit_functional_modules(self, case_root: str | Path = CASE_ROOT) -> dict[str, Any]:
        """Audit the separate module overlay, retaining its own denominator."""
        report = self.audit(inventory_functional_modules(case_root),
                            identity_map=self.module_identity_map())
        report["schema"] = "pcbagent.functional_module_knowledge_audit.v1"
        report["scope"] = "functional_modules_separate_from_ic"
        report["summary"]["module_instances"] = report["summary"].pop("core_instances")
        report["summary"]["knowledge_ready_is_not_electrical_qualification"] = True
        return report

    def prepare_device(self, supplier_part: str, *, product_name: str,
                       run_dir: str | Path | None = None,
                       pipeline: Callable[[Any], dict[str, Any]] | None = None) -> dict[str, Any]:
        """Acquire and ingest a missing device, preserving existing database rows.

        Existing partial records need explicit reconciliation, rather than
        replacement. Custom S identities require an explicitly supplied run.
        A completed run is reusable only with its PDF and optimized content.
        """
        if not IDENTITY.fullmatch(supplier_part):
            raise ValueError("supplier_part must be a C/S identity")
        cn = self._mysql("datasheet_assets")
        try:
            cn.start_transaction(readonly=True)
            cur = cn.cursor(dictionary=True)
            cur.execute("SELECT chunk_count,pdf_sha256,pdf_size_bytes FROM datasheet_documents WHERE lichuang_id=%s", (supplier_part,))
            existing = cur.fetchone()
        finally:
            cn.rollback(); cn.close()
        mc = self._milvus()
        try:
            count = mc.query(collection_name=self.config.milvus_collection,
                filter="lichuang_id == " + json.dumps(supplier_part), output_fields=["count(*)"],
                consistency_level="Strong")[0]["count(*)"]
        finally:
            mc.close()
        if existing or count:
            complete = bool(existing and count and existing["chunk_count"] == count
                            and existing["pdf_sha256"] and existing["pdf_size_bytes"])
            return {"id": supplier_part, "status": "existing" if complete else "reconciliation_required",
                    "milvus_chunks": count}
        if run_dir is None:
            from datasheet_automation.core.io import DATASHEET_RUNTIME_ROOT
            for candidate in (DATASHEET_RUNTIME_ROOT / supplier_part,
                              DATASHEET_RUNTIME_ROOT / "runs" / supplier_part,
                              RUNTIME_ROOT / "runs" / supplier_part):
                if (candidate / "optimized" / f"opti_{supplier_part}_content_list.json").is_file() and list(candidate.glob("*.pdf")):
                    run_dir = candidate
                    break
        if run_dir is None:
            if supplier_part.startswith("S"):
                return {"id": supplier_part, "status": "source_required"}
            from datasheet_automation.core.application import run
            from datasheet_automation.core.config import default_config
            request = default_config("full", query=supplier_part)
            request["options"]["output_root"] = str(RUNTIME_ROOT / "runs")
            request["options"]["reuse"] = True
            response = (pipeline or run)(request)
            if response.get("status") not in {"ok", "degraded"}:
                return {"id": supplier_part, "status": "pipeline_failed", "error_type": response.get("status")}
            data = response.get("data") or {}
            selected = (data.get("collection") or {}).get("selected_product") or {}
            if selected.get("product_code") != supplier_part:
                raise ValueError("pipeline selected a different device identity")
            run_dir = (data.get("artifacts") or {}).get("run_dir")
        directory = Path(str(run_dir)).resolve()
        pdfs = list(directory.glob("*.pdf"))
        if len(pdfs) != 1 or not pdfs[0].read_bytes().startswith(b"%PDF"):
            raise ValueError("prepared run requires one verified source PDF")
        provenance_file=directory/"native-provenance.json"
        if provenance_file.is_file():
            provenance=json.loads(provenance_file.read_text())
            if provenance.get("pdf_sha256")!=hashlib.sha256(pdfs[0].read_bytes()).hexdigest():
                raise ValueError("prepared run PDF differs from its extraction provenance")
        store = KnowledgeStore(self.config)
        try:
            result = store.ingest_run(directory, supplier_part, product_name=product_name,
                                      pdf_path=pdfs[0], replace=False)
        finally:
            if store._milvus is not None:store._milvus.close()
        return {"id": supplier_part, "status": "ingested", "ingest": result}


def save_snapshot(payload: Mapping[str, Any], filename: str = "coverage.json") -> Path:
    if Path(filename).name != filename or not filename.endswith(".json"):
        raise ValueError("snapshot requires a simple JSON filename")
    RUNTIME_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = RUNTIME_ROOT / filename
    temporary=RUNTIME_ROOT/("."+filename+"."+uuid4().hex)
    try:
        descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(descriptor,"w",encoding="utf-8") as stream:
            json.dump(payload,stream,ensure_ascii=False,indent=2,default=str)
        os.replace(temporary,target)
    finally:
        if temporary.exists():temporary.unlink()
    return target


__all__ = ["PreparationService", "configure_local_services", "inventory_cases", "inventory_functional_modules", "original_identity_metadata", "save_snapshot"]
