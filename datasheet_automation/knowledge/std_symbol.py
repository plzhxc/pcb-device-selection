"""Exact public LCSC Standard-library identities, separate from manufacturer facts."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

QUALIFICATION = "public_lcsc_std_component_original_json"
SCHEMA = "pcbagent.public_lcsc_std_symbol.v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _coordinate(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("finite library coordinate required")
    return result


def _symbol_pins(data: Mapping) -> list[dict]:
    pins = []
    if not isinstance(data, Mapping) or not isinstance(data.get("shape"), list):
        raise ValueError("original symbol shapes required")
    for shape in data["shape"]:
        if not isinstance(shape, str):
            raise ValueError("original symbol shape must be text")
        kind = shape.split("~", 1)[0]
        if kind in {"W", "N", "F", "LIB", "VIA", "NETFLAG", "NETPORT"}:
            raise ValueError("project connections cannot enter a library source")
        if kind == "P":
            try:
                fields = [segment.split("~") for segment in shape.split("^^")]
                h, name, number = fields[0], fields[3], fields[4]
                if h[3] != number[4] or not h[3].strip() or not name[4].strip():
                    raise ValueError("symbol identity annotations differ")
                pins.append({"pin_number": h[3], "pin_name": name[4], "element_id": h[7]})
            except IndexError as exc:
                raise ValueError("complete original symbol pin annotation required") from exc
    return pins


def _multipart_pins(result: Mapping, main_pins: list[dict], footprint: Mapping,
                    supplier_part: str, expected_model: str) -> tuple[list[dict], dict | None]:
    units = result.get("subparts", [])
    if not isinstance(units, list):
        raise ValueError("symbol subparts must be an explicit flat list")
    if not units:
        return main_pins, None
    if not 1 <= len(units) <= 128:
        raise ValueError("bounded symbol subpart count required")
    def identities(pins):
        if (not pins or len({p["pin_number"] for p in pins}) != len(pins)
                or len({p["element_id"] for p in pins}) != len(pins)
                or any(not p["element_id"].strip() for p in pins)):
            raise ValueError("unique physical pins and local elements required in each symbol unit")
        return frozenset((p["pin_number"], p["pin_name"]) for p in pins)
    main = identities(main_pins)
    combined, uuids, matches = [], [], []
    manufacturer = result["dataStr"]["head"]["c_para"].get("Manufacturer")
    for unit in units:
        if not isinstance(unit, Mapping) or unit.get("subparts"):
            raise ValueError("only flat original symbol units are supported")
        data = unit.get("dataStr")
        head = data.get("head") if isinstance(data, Mapping) else None
        attrs = head.get("c_para") if isinstance(head, Mapping) else None
        uuid = unit.get("uuid")
        if (not isinstance(attrs, Mapping) or head.get("docType") != "6"
                or not re.fullmatch(r"[0-9a-f]{32}", str(uuid or ""))
                or uuid in uuids or uuid in {result["uuid"], footprint["uuid"]}
                or head.get("uuid") != uuid or head.get("puuid") != footprint["uuid"]
                or attrs.get("Supplier") != "LCSC" or attrs.get("Supplier Part") != supplier_part
                or attrs.get("Manufacturer Part") != expected_model or attrs.get("name") != expected_model
                or attrs.get("Manufacturer") != manufacturer
                or attrs.get("package") != footprint["title"]
                or ("docType" in unit and unit["docType"] != 6)
                or ("verify" in unit and unit["verify"] is not True)
                or ("writable" in unit and unit["writable"] is not False)
                or ("owner" in unit and (not isinstance(unit["owner"], Mapping)
                    or unit["owner"].get("username", "").casefold() != "lcsc"))):
            raise ValueError("symbol unit exact UUID/model/C/package provenance differs")
        pins = _symbol_pins(data)
        if identities(pins) == main:
            matches.append(uuid)
        uuids.append(uuid)
        # Element IDs are local to each original unit; physical pin numbers are
        # global and must remain unique in the existing final pad-bijection check.
        combined.extend({**pin, "source_element_id": pin["element_id"], "symbol_unit_uuid": uuid,
                         "element_id": uuid + ":" + pin["element_id"]} for pin in pins)
    if len(matches) != 1:
        raise ValueError("main symbol must match exactly one complete subpart by physical number and label")
    return combined, {"unit_uuids": uuids, "main_matching_unit_uuid": matches[0],
        "main_match_basis": "exact_physical_pin_number_and_library_label_set", "subpart_no_used": False}


def parse_std_symbol(raw: bytes, supplier_part: str, *, expected_model: str) -> dict:
    """Parse only a successful exact-C, LCSC-owned symbol/footprint response."""
    if not re.fullmatch(r"C[0-9]+", supplier_part) or not expected_model:
        raise ValueError("exact C and model required")
    obj = json.loads(raw)
    if obj.get("success") is not True or obj.get("code") != 0:
        raise ValueError("successful original public component response required")
    r = obj["result"]; footprint = r["packageDetail"]
    symbol_data, footprint_data = r["dataStr"], footprint["dataStr"]
    head, fh = symbol_data["head"], footprint_data["head"]
    attrs = head["c_para"]
    if (r.get("docType") != 2 or footprint.get("docType") != 4
            or head.get("docType") != "2" or fh.get("docType") != "4"
            or r.get("verify") is not True or r.get("writable") is not False
            or footprint.get("writable") is not False
            or r.get("owner", {}).get("username", "").casefold() != "lcsc"
            or footprint.get("owner", {}).get("username", "").casefold() != "lcsc"
            or attrs.get("Supplier") != "LCSC" or attrs.get("Supplier Part") != supplier_part
            or attrs.get("Manufacturer Part") != expected_model
            or r.get("szlcsc", {}).get("number") != supplier_part
            or r.get("lcsc", {}).get("number") != supplier_part):
        raise ValueError("public LCSC component identity differs")
    if (not re.fullmatch(r"[0-9a-f]{32}", str(r.get("uuid", "")))
            or not re.fullmatch(r"[0-9a-f]{32}", str(footprint.get("uuid", "")))
            or head.get("uuid") != r["uuid"] or fh.get("uuid") != footprint["uuid"]
            or head.get("puuid") != footprint["uuid"]
            or attrs.get("package") != footprint.get("title")
            or fh["c_para"].get("package") != footprint["title"]):
        raise ValueError("symbol/footprint exact UUID and package chain differs")
    pins, multipart = _multipart_pins(r, _symbol_pins(symbol_data), footprint, supplier_part, expected_model)
    pads = []
    for shape in footprint_data["shape"]:
        a = shape.split("~"); kind = a[0]
        if kind in {"LIB", "VIA"}:
            raise ValueError("project objects cannot enter a library source")
        if kind in {"TRACK", "SOLIDREGION"} and a[3 if kind == "TRACK" else 2]:
            raise ValueError("library geometry carries a net assignment")
        if kind == "PAD":
            if a[7] or a[6] != "1" or not a[8].strip():
                raise ValueError("only numbered top-layer library pads without nets are supported")
            pads.append({"pin_number": a[8], "x": _coordinate(a[2]), "y": _coordinate(a[3]),
                         "width": _coordinate(a[4]), "height": _coordinate(a[5]), "element_id": a[12]})
    if (not 2 <= len(pins) <= 128 or len(pads) != len(pins)
            or len({p["pin_number"] for p in pins}) != len(pins)
            or len({p["pin_number"] for p in pads}) != len(pads)
            or len({p["element_id"] for p in pins}) != len(pins)
            or len({p["element_id"] for p in pads}) != len(pads)
            or {p["pin_number"] for p in pins} != {p["pin_number"] for p in pads}
            or len({(p["x"], p["y"]) for p in pads}) != len(pads)
            or any(p["width"] <= 0 or p["height"] <= 0 for p in pads)):
        raise ValueError("complete unique symbol/pad inventory required")
    sha = hashlib.sha256(raw).hexdigest()
    payload = {"schema": SCHEMA, "supplier_part": supplier_part, "model": expected_model,
        "original_source_sha256": sha, "source_url": f"https://lceda.cn/api/products/{supplier_part}/components",
        "symbol_uuid": r["uuid"], "footprint_uuid": footprint["uuid"],
        "footprint_name": footprint["title"], "pins": pins, "pads": pads,
        "coordinate_unit": "10mil", "scope": "pin_number_to_public_library_label_only",
        "electrical_role_evidence": False, "reference_connections_included": False}
    if multipart is not None:
        payload["multipart"] = multipart
    return {"source_kind": "public_std_symbol", "source_ref": "public_std_symbol:" + sha,
            "document_sha256": sha, "mapping": payload}


def validate_std_symbol(bundle: Mapping, cad: Mapping) -> dict:
    """Reparse original frozen bytes; SQL access is unnecessary for validation."""
    sha = cad.get("document_sha256"); part = bundle.get("supplier_part")
    assets = [row for row in bundle.get("source_assets", []) if row.get("source_sha256") == sha]
    if len(assets) != 1:
        raise ValueError("one original public library asset required")
    asset = assets[0]; quality = asset.get("quality") or {}; raw = asset.get("original_json_text")
    if (asset.get("source_verified") is not True or asset.get("source_mime") != "application/json"
            or quality.get("qualification") != QUALIFICATION or quality.get("supplier_part") != part
            or asset.get("source_url") != f"https://lceda.cn/api/products/{part}/components"
            or not isinstance(raw, str) or hashlib.sha256(raw.encode()).hexdigest() != sha):
        raise ValueError("public library provenance/hash differs")
    current = parse_std_symbol(raw.encode(), part, expected_model=quality.get("model"))
    if current != cad:
        raise ValueError("filtered public library identity differs from original response")
    return current["mapping"]


def validate_exposed_pad_position(mapping: Mapping, number: str) -> None:
    """Require one extra EP-labelled central land and complete perimeter leads."""
    package = re.match(r"^(QFN|DFN)[-_ ]?([0-9]+)(?=_|$)", mapping["footprint_name"].upper())
    if package is None:
        package = re.match(r"^(ESOP)[-_ ]?([0-9]+)L?(?=_|$)", mapping["footprint_name"].upper())
    if package is None:
        raise ValueError("explicit QFN/DFN/ESOP lead count required")
    lead_count = int(package[2]); pads = mapping["pads"]
    ep = [p for p in mapping["pins"] if p["pin_name"].upper() in {"EP", "PAD", "EXPOSED PAD"}]
    if (len(ep) != 1 or ep[0]["pin_number"] != number or len(pads) != lead_count + 1
            or {p["pin_number"] for p in pads if p["pin_number"] != number}
                != {str(i) for i in range(1, lead_count + 1)}):
        raise ValueError("one additional exposed-pad alias plus complete numbered perimeter required")
    centre = next(p for p in pads if p["pin_number"] == number)
    others = [p for p in pads if p["pin_number"] != number]
    xs, ys = [p["x"] for p in others], [p["y"] for p in others]
    if (not min(xs) < centre["x"] < max(xs) or not min(ys) < centre["y"] < max(ys)
            or not math.isclose(centre["x"], (min(xs)+max(xs))/2, abs_tol=.01)
            or not math.isclose(centre["y"], (min(ys)+max(ys))/2, abs_tol=.01)
            or any(min(xs) < p["x"] < max(xs) and min(ys) < p["y"] < max(ys) for p in others)):
        raise ValueError("exposed-pad alias is not the unique central contact")
    if package[1] == "ESOP":
        # A source-labelled central land alone does not establish ESOP geometry.
        # Require two equally populated, aligned perimeter rows and a central
        # pad rectangle that does not overlap any numbered perimeter land.
        aligned_rows = False
        for across, along in (("x", "y"), ("y", "x")):
            rows = sorted({p[across] for p in others})
            if len(rows) != 2 or lead_count < 4 or lead_count % 2:
                continue
            positions = [sorted(p[along] for p in others if p[across] == row) for row in rows]
            if (len(positions[0]) == lead_count // 2 == len(positions[1])
                    and len(set(positions[0])) == lead_count // 2
                    and all(math.isclose(a, b, abs_tol=.01) for a, b in zip(*positions))):
                aligned_rows = True
        if not aligned_rows or any(
                abs(p["x"] - centre["x"]) < (p["width"] + centre["width"]) / 2
                and abs(p["y"] - centre["y"]) < (p["height"] + centre["height"]) / 2
                for p in others):
            raise ValueError("ESOP requires complete aligned perimeter rows and an isolated central land")


def register_std_symbol(service: Any, supplier_part: str, *, source_file: str | Path,
                        expected_sha256: str, expected_model: str) -> dict:
    """Store exact original public bytes additively; no catalog/role writes."""
    from .preparation import RUNTIME_ROOT
    from .web_source import SOURCE_DDL
    path = Path(source_file).resolve()
    if not path.is_relative_to(RUNTIME_ROOT.resolve()) or path.suffix != ".json":
        raise ValueError("public source must be an explicit knowledge runtime JSON")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("original response hash changed")
    parsed = parse_std_symbol(raw, supplier_part, expected_model=expected_model)
    metadata = service.read_device_knowledge(supplier_part, evidence_limit=1)
    if (metadata.get("document") or {}).get("product_name") != expected_model:
        raise ValueError("public model differs from exact manufacturer source identity")
    quality = {"qualification": QUALIFICATION, "method": "public_lcsc_std_api_capture",
        "supplier_part": supplier_part, "model": expected_model,
        "scope": "pin_number_and_label_only", "electrical_role_evidence": False}
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute(SOURCE_DDL)
        cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,'application/json',%s,FALSE,%s)",
            (supplier_part, expected_sha256, parsed["mapping"]["source_url"], raw, json.dumps(quality)))
        cn.commit()
    finally:
        cn.close()
    return parsed
