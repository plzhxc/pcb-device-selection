"""Local CAD pin-number mappings, explicitly excluding reference connections."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .web_source import SOURCE_DDL


def store_local_symbol_mapping(service: Any, supplier_part: str, *, source_file: str | Path,
                               component_key: str, expected_sha256: str) -> dict[str, Any]:
    """Bind local symbol labels to an exact existing supplier C identity.

    A pin label can identify a CAD exposed pad. It cannot establish electrical
    role, recommended connections or the voltage domain of that pad.
    """
    from .preparation import IDENTITY,WORKLIB
    if not IDENTITY.fullmatch(supplier_part) or not supplier_part.startswith("C"):
        raise ValueError("a confirmed C identity is required for this mapping")
    path=Path(source_file).resolve();root=(WORKLIB/"02_CASE"/"02_dataset").resolve()
    if not path.is_relative_to(root) or path.suffix!=".enet" or path.parent.name!="netlist":
        raise ValueError("local symbol source must be an original case ENET")
    raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    if sha!=expected_sha256:raise ValueError("original source hash changed")
    component=json.loads(raw)["components"][component_key];props=component.get("props",{})
    if props.get("Supplier Part")!=supplier_part:raise ValueError("symbol supplier identity does not match")
    pins=[{"pin_number":str(pin.get("number","")),"pin_name":str(pin.get("name",""))}
          for pin in component.get("pinInfoMap",{}).values()]
    if not pins or any(not row["pin_number"] for row in pins):raise ValueError("symbol has missing pin numbers")
    if len({row["pin_number"] for row in pins})!=len(pins):raise ValueError("symbol pin numbers are ambiguous")
    payload={"schema":"pcbagent.local_symbol_mapping.v1","supplier_part":supplier_part,
        "source_file":str(path),"original_source_sha256":sha,"component_key":component_key,
        "model":props.get("Manufacturer Part") or props.get("DeviceName"),
        "symbol_uuid":props.get("Symbol"),"footprint_uuid":props.get("Footprint"),
        "footprint_name":props.get("FootprintName"),"pins":pins,
        "scope":"pin_number_to_local_symbol_label_only","electrical_role_evidence":False,
        "reference_connections_included":False}
    data=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()
    source_sha=hashlib.sha256(data).hexdigest();source_ref=f"local_symbol:{source_sha}"
    quality={"method":"local_symbol_mapping","qualification":"local_symbol_mapping",
             "scope":payload["scope"],"electrical_role_evidence":False,"original_source_sha256":sha}
    cn=service._mysql("datasheet_assets")
    try:
        cur=cn.cursor();cur.execute(SOURCE_DDL)
        cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,%s,%s,FALSE,%s)",
            (supplier_part,source_sha,source_ref,"application/json",data,json.dumps(quality)))
        cn.commit()
    finally:cn.close()
    return {"source_ref":source_ref,"document_sha256":source_sha,"source_kind":"local_symbol","mapping":payload}


__all__=["store_local_symbol_mapping"]
