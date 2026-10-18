"""Authenticate complete manufacturer/CAD position correspondence, without nets."""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

CAD_SCHEMA = "pcbagent.cad_terminal_position_source.v1"
INVENTORY_SCHEMA = "pcbagent.manufacturer_terminal_position_inventory.v1"
REVIEW_SCHEMA = "pcbagent.manufacturer_terminal_position_review.v1"
_KINDS = {"two_end_marked", "two_rows_partial_number", "two_rows_center_pad"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _number(value: Any) -> float:
    if type(value) not in {float, int} or not math.isfinite(value):
        raise ValueError("finite CAD coordinate required")
    return float(value)


def _source(bundle: Mapping, sha: str, *, raw: bool = False) -> Mapping:
    rows = [a for a in bundle.get("source_assets", [])
            if a.get("source_sha256") == sha and a.get("source_verified") is True]
    if len(rows) != 1 or rows[0].get("source_mime") != "application/json":
        raise ValueError("unique verified CAD JSON asset required in frozen snapshot")
    asset = rows[0]
    if asset.get("quality", {}).get("supplier_part") != bundle.get("supplier_part"):
        raise ValueError("CAD asset is bound to a different identity")
    text = asset.get("original_json_text") if raw else _canonical(asset.get("record"))
    if not isinstance(text, str) or _sha(text) != sha:
        raise ValueError("frozen CAD bytes or canonical position binding changed")
    record = json.loads(text)
    if raw and (asset.get("quality", {}).get("qualification") != "public_cad_library_original_json"
                or record.get("success") is not True or record.get("code") != 0):
        raise ValueError("public CAD response is not a successful original component")
    return record["result"] if raw else record


def _lines(record: Mapping, kind: str) -> list:
    if record.get("docType") != {"SYMBOL": 2, "FOOTPRINT": 4}[kind] or record.get("public") is not True:
        raise ValueError("public single-library component required")
    rows = [json.loads(line) for line in record.get("dataStr", "").splitlines() if line.strip()]
    if not rows or rows[0][:2] != ["DOCTYPE", kind]:
        raise ValueError("CAD data is not the asserted library document")
    if any(not isinstance(row, list) for row in rows):
        raise ValueError("malformed CAD primitive")
    # Older library exports contain explicit empty/no-op array records.
    rows = [row for row in rows if row]
    if any(row[0] in {"WIRE", "NET", "COMPONENT", "SCHEMATIC", "TRACK", "VIA"} for row in rows):
        raise ValueError("board/schematic assignments cannot enter a library source")
    return rows


def _geometry(symbol: Mapping, footprint: Mapping) -> dict:
    sr, fr = _lines(symbol, "SYMBOL"), _lines(footprint, "FOOTPRINT")
    if sum(row[0] == "PART" for row in sr) != 1:
        raise ValueError("multi-part symbol position proof is unsupported")
    pins = {}
    for row in sr:
        if row[0] == "PIN":
            if row[1] in pins:
                raise ValueError("duplicate symbol pin primitive")
            pins[row[1]] = {"element": row[1], "x": _number(row[4]), "y": _number(row[5])}
    for row in sr:
        if row[0] == "ATTR" and row[2] in pins and row[3] in {"NUMBER", "NAME"}:
            field = {"NUMBER": "number", "NAME": "name"}[row[3]]
            if field in pins[row[2]] or not isinstance(row[4], str) or not row[4].strip():
                raise ValueError("ambiguous symbol pin name or number")
            pins[row[2]][field] = row[4]
    if not 2 <= len(pins) <= 64 or any(set(p) != {"element", "x", "y", "number", "name"} for p in pins.values()):
        raise ValueError("complete bounded symbol pin inventory required")
    pads, marks, body = [], [], []
    for row in fr:
        if row[0] == "PAD":
            if row[4] != 1 or not isinstance(row[5], str) or not row[5].strip():
                raise ValueError("only explicit top-layer physical numbered pads are supported")
            pads.append({"element": row[1], "number": row[5], "x": _number(row[6]), "y": _number(row[7])})
        if row[0] == "FILL" and row[4] == 49:
            if len(row[7]) != 1 or len(row[7][0]) != 4 or row[7][0][0] != "CIRCLE":
                raise ValueError("only one explicit component marking circle is supported")
            marks.append({"element": row[1], "x": _number(row[7][0][1]), "y": _number(row[7][0][2])})
        if row[0] == "POLY" and row[4] == 48:
            body.append(row[6])
    by_number = {p["number"]: p for p in pins.values()}
    if (len(by_number) != len(pins) or len(pads) != len(pins)
            or len({p["number"] for p in pads}) != len(pads)
            or {p["number"] for p in pads} != set(by_number)
            or len({(p["x"], p["y"]) for p in pads}) != len(pads) or len(marks) != 1):
        raise ValueError("complete unique pad/symbol correspondence and one orientation mark required")
    if not any(row[:4] == ["LAYER", 49, "COMPONENT_MARKING", "Component Marking Layer"] for row in fr):
        raise ValueError("mark must come from the component marking layer")
    canvas = [row for row in fr if row[0] == "CANVAS"]
    if len(canvas) != 1 or canvas[0][3] not in {"mil", "mm", "inch"}:
        raise ValueError("explicit recognized CAD display-unit metadata required")
    # PCB/footprint coordinates use mil; CANVAS unit changes display only.
    # https://prodocs.lceda.cn/cn/api/reference/pro-api.sys_unit.html
    # https://prodocs.lceda.cn/cn/format/pcb/common/
    return {"symbol_pins": by_number, "pads": pads, "mark": marks[0], "body": body}


def _distance(a: Mapping, b: Mapping) -> float:
    return (a["x"] - b["x"]) ** 2 + (a["y"] - b["y"]) ** 2


def _soic_ep_count(package: str) -> int:
    match = re.fullmatch(r"SOIC[- ]?EP([0-9]+)", str(package).upper())
    if not match or not 4 <= int(match[1]) <= 62 or int(match[1]) % 2:
        raise ValueError("explicit bounded even-lead SOIC-EP package required")
    return int(match[1])


def _pad_label(value: str) -> bool:
    return re.sub(r"[ _-]", "", value.upper()) in {"EP", "EPAD", "PAD", "THERMALPAD", "EXPOSEDPAD"}


def _positions(geometry: Mapping, inventory: Mapping) -> dict[str, Mapping]:
    pads, mark = geometry["pads"], geometry["mark"]
    closest = sorted(pads, key=lambda p: _distance(p, mark))
    if math.isclose(_distance(closest[0], mark), _distance(closest[1], mark), rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError("physical orientation mark does not uniquely identify an end/corner")
    anchor = closest[0]
    if inventory["topology"] == "two_end_marked":
        if len(pads) != 2:
            raise ValueError("marked-end topology requires exactly two complete end contacts")
        dimensions = inventory.get("body_dimensions_mm")
        if (not isinstance(dimensions, list) or len(dimensions) != 2
                or any(type(v) not in {int, float} or not 0 < v < 100 for v in dimensions)):
            raise ValueError("two-end identity requires manufacturer body dimensions")
        if len(geometry["body"]) != 1:
            raise ValueError("one explicit component body rectangle required")
        path = geometry["body"][0]
        if len(path) != 11 or path[2] != "L":
            raise ValueError("component body is not a closed rectangle")
        numbers = path[:2] + path[3:]
        xy = [(_number(numbers[i]), _number(numbers[i+1])) for i in range(0, len(numbers), 2)]
        if xy[0] != xy[-1] or len(set(xy[:-1])) != 4:
            raise ValueError("component body rectangle is not closed")
        xs, ys = {x for x, _ in xy}, {y for _, y in xy}
        if len(xs) != 2 or len(ys) != 2:
            raise ValueError("rotated/irregular component body requires independent geometry support")
        actual = sorted([(max(xs)-min(xs))*.0254, (max(ys)-min(ys))*.0254])
        if any(abs(a-b) > .005 for a, b in zip(actual, sorted(dimensions))):
            raise ValueError("manufacturer and exact CAD physical body dimensions differ")
        return {"marked_end": anchor, "opposite_end": closest[1]}
    center = None
    if inventory["topology"] == "two_rows_center_pad":
        count = _soic_ep_count(inventory["package"])
        if len(pads) != count + 1:
            raise ValueError("complete perimeter plus one center pad required")
        candidates = []
        for axis in ("x", "y"):
            values = sorted({pad[axis] for pad in pads})
            if len(values) == 3:
                middle = [pad for pad in pads if pad[axis] == values[1]]
                if len(middle) == 1:
                    candidates.append(middle[0])
        if len(candidates) != 1 or candidates[0] is anchor:
            raise ValueError("one center pad and a distinct perimeter orientation anchor required")
        center = candidates[0]
        pads = [pad for pad in pads if pad is not center]
        if any(not math.isclose(center[axis], (min(p[axis] for p in pads) + max(p[axis] for p in pads)) / 2,
                                rel_tol=0, abs_tol=1e-6) for axis in ("x", "y")):
            raise ValueError("exposed pad must occupy the package center")
    size = len(pads) // 2
    match = re.fullmatch(r"(?:SOP|SOIC|SSOP|TSSOP)[-_ ]?([0-9]+)", inventory["package"].upper())
    if (center is None and (not match or int(match[1]) != len(pads))) or size < 2 or len(pads) % 2:
        raise ValueError("exact printed dual-row package contact count required")
    axes = [axis for axis in ("x", "y") if len({p[axis] for p in pads}) == 2]
    if len(axes) != 1:
        raise ValueError("physical pads must form exactly two unambiguous parallel rows")
    across = axes[0]; along = "y" if across == "x" else "x"
    same = [p for p in pads if p[across] == anchor[across]]
    other = [p for p in pads if p[across] != anchor[across]]
    if (len(same) != size or len(other) != size or len({p[along] for p in same}) != size
            or {p[along] for p in same} != {p[along] for p in other}
            or anchor[along] not in {min(p[along] for p in same), max(p[along] for p in same)}):
        raise ValueError("complete equally ranked rows and a corner anchor required")
    reverse = anchor[along] == max(p[along] for p in same)
    return {**{f"{name}:{index}": pin for name, group in [("pin1_side", same), ("opposite_side", other)]
            for index, pin in enumerate(sorted(group, key=lambda p: p[along], reverse=reverse))},
            **({"center_pad": center} if center is not None else {})}


def _review(bundle: Mapping, cite: Mapping, scope: str) -> tuple[dict, Mapping]:
    from .pin_extraction import _source_evidence
    from .terminal_inventory import _authenticate_reviewed_receipt
    rows = [r for r in bundle.get("evidence", []) if r.get("source_ref") == cite.get("source_ref")
            and r.get("document_sha256") == cite.get("document_sha256")]
    if len(rows) != 1 or rows[0].get("provenance") != "reviewed_original_pdf_transcription":
        raise ValueError("unique reviewed original manufacturer image evidence required")
    row = rows[0]
    if cite.get("quote") != row["content"]:
        raise ValueError("position evidence must contain the complete literal reviewed inventory")
    checked = _source_evidence({"chunk_id": row["id"], "quote": row["content"]}, bundle)
    if not checked or scope not in checked.get("reviewed_claim_scopes", []):
        raise ValueError("manufacturer position review lacks required scope")
    _authenticate_reviewed_receipt(row, checked, bundle, required_scopes={scope})
    return json.loads(row["content"]), row


def _std_three_contact_positions(mapping: Mapping) -> dict[str, Mapping]:
    """Orient a top-layer three-pad package by its single side, never its symbol."""
    if not re.match(r"^SOT[-_ ]?23[-_ ]3(?=_|$)", mapping["footprint_name"].upper()):
        raise ValueError("exact three-contact SOT23 public footprint required")
    pads = mapping["pads"]
    if len(pads) != 3 or len(mapping["pins"]) != 3:
        raise ValueError("complete three-contact symbol/pad inventory required")
    axes = [axis for axis in ("x", "y") if len({p[axis] for p in pads}) == 2]
    if len(axes) != 1:
        raise ValueError("one single-contact side and one two-contact side required")
    across = axes[0]; along = "y" if across == "x" else "x"
    groups = [[p for p in pads if p[across] == value] for value in {p[across] for p in pads}]
    single = next(group[0] for group in groups if len(group) == 1)
    pair = next(group for group in groups if len(group) == 2)
    if not math.isclose(single[along], sum(p[along] for p in pair) / 2, abs_tol=1e-6, rel_tol=0):
        raise ValueError("single contact must align with the opposite pair midpoint")
    if any(abs(a["x"]-b["x"]) < (a["width"]+b["width"])/2
           and abs(a["y"]-b["y"]) < (a["height"]+b["height"])/2
           for i, a in enumerate(pads) for b in pads[i+1:]):
        raise ValueError("distinct three-contact lands must not overlap")
    mid = {axis: sum(p[axis] for p in pair)/2 for axis in ("x", "y")}
    # Standard-library screen coordinates have y increasing downwards. This
    # signed turn preserves top-view handedness under rotation, not reflection.
    def turn(p):
        return ((single["x"]-mid["x"])*(p["y"]-mid["y"])
                - (single["y"]-mid["y"])*(p["x"]-mid["x"]))
    if sorted(turn(p) > 0 for p in pair) != [False, True] or any(turn(p) == 0 for p in pair):
        raise ValueError("three-contact top-view handedness is ambiguous")
    return {"single_side": single,
            "pair_clockwise_from_single": next(p for p in pair if turn(p) > 0),
            "pair_counterclockwise_from_single": next(p for p in pair if turn(p) < 0)}


def _validate_std_three_contact(bundle: Mapping, proof: Mapping) -> dict:
    """Join exact public physical pads to independently reviewed original labels.

    Library label spelling is retained as library data. It is not a synonym
    dictionary, manufacturer numbering, or electrical authorization.
    """
    from .std_symbol import validate_std_symbol
    part, ref = proof["supplier_part"], proof["cad_source_ref"]
    rows = [row for row in bundle.get("symbol_mappings", []) if row.get("source_ref") == ref]
    if not re.fullmatch(r"C[0-9]+", part) or len(rows) != 1:
        raise ValueError("one exact C-anchored public Standard library source required")
    mapping = validate_std_symbol(bundle, rows[0])
    if (mapping["model"] != proof["model"] or mapping["supplier_part"] != part
            or not re.fullmatch(r"SOT[-_ ]?23(?:[-_ ]3)?", proof["package"].upper())
            or proof["manufacturer_pin_number"] is not None):
        raise ValueError("exact SOT23 identity with unnumbered manufacturer contacts required")
    cites = proof["manufacturer_evidence"]
    if (not isinstance(cites, list) or len(cites) != 2
            or any(not isinstance(c, dict) or set(c) != {"claim", "source_ref", "document_sha256", "quote"} for c in cites)
            or {c["claim"] for c in cites} != {"position_inventory", "position_completeness"}):
        raise ValueError("two independent complete original-image reviews required")
    inventory, first = _review(bundle, next(c for c in cites if c["claim"] == "position_inventory"), "physical_terminal_identity")
    review, second = _review(bundle, next(c for c in cites if c["claim"] == "position_completeness"), "physical_position_completeness")
    if (inventory.get("schema") != INVENTORY_SCHEMA or inventory.get("supplier_part") != part
            or inventory.get("model") != proof["model"] or inventory.get("package") != proof["package"]
            or inventory.get("document_sha256") != (bundle.get("document") or {}).get("pdf_sha256")
            or inventory.get("document_sha256") != first["document_sha256"]
            or inventory.get("page") != first["page_idx"]+1
            or inventory.get("complete_physical_view") is not True
            or inventory.get("topology") != "sot23_three_contact"
            or inventory.get("view") != "component_top_perspective"
            or inventory.get("anchor") != {"position_key": "single_side", "kind": "single_contact_side_and_handed_top_view"}):
        raise ValueError("exact complete original top-perspective three-contact package required")
    if (review.get("schema") != REVIEW_SCHEMA or review.get("supplier_part") != part
            or review.get("inventory_source_ref") != first["source_ref"]
            or review.get("inventory_transcript_sha256") != _sha(first["content"])
            or review.get("cad_source_ref") != ref
            or review.get("agreement") != "complete_positions_labels_anchor_and_printed_numbers"
            or review.get("cad_label_policy") != "raw_library_labels_are_not_manufacturer_synonyms"
            or first["source_ref"] == second["source_ref"]
            or first["document_sha256"] != second["document_sha256"] or first["page_idx"] != second["page_idx"]
            or first["source_quality"]["rendered_image_sha256"] != second["source_quality"]["rendered_image_sha256"]
            or first["source_quality"].get("reviewer") == second["source_quality"].get("reviewer")):
        raise ValueError("independent review must bind the exact original image inventory and public CAD bytes")
    positions = _std_three_contact_positions(mapping)
    items = inventory.get("positions")
    if (not isinstance(items, list) or len(items) != 3
            or any(not isinstance(item, dict) or set(item) != {"position_key", "manufacturer_label", "manufacturer_printed_pin_number"}
                   or not isinstance(item["manufacturer_label"], str) or not item["manufacturer_label"].strip()
                   or item["manufacturer_printed_pin_number"] is not None for item in items)
            or {item["position_key"] for item in items} != set(positions)
            or len({item["manufacturer_label"] for item in items}) != 3):
        raise ValueError("three unique original position labels without printed numbers required")
    by_position = {item["position_key"]: item for item in items}
    numbers = {p["pin_number"]: p for p in mapping["pins"]}
    matches = [(key, pad) for key, pad in positions.items() if pad["pin_number"] == proof["catalog_pin_number"]]
    if len(matches) != 1:
        raise ValueError("requested contact is not in the complete source inventory")
    key, pad = matches[0]; item = by_position[key]
    if proof["catalog_pin_name"] != item["manufacturer_label"] or proof["manufacturer_terminal"] != item["manufacturer_label"]:
        raise ValueError("catalog label differs from the original physical position")
    return {"schema": "pcbagent.pin_identity_mapping.v1", **dict(proof),
        "position_derivation": {"method": "complete_manufacturer_public_cad_position_bijection",
            "topology": inventory["topology"], "position": {"position_key": key, "manufacturer": item,
                "cad_pad": pad, "cad_symbol_pin": numbers[pad["pin_number"]]},
            "cad_source_sha256": rows[0]["document_sha256"],
            "cad_original_sources": {"public_std_symbol": {"sha256": rows[0]["document_sha256"],
                "symbol_uuid": mapping["symbol_uuid"], "footprint_uuid": mapping["footprint_uuid"]}},
            "manufacturer_inventory_source_ref": first["source_ref"], "completeness_review_source_ref": second["source_ref"],
            "physical_terminal_count": 3,
            "positions": [{"position_key": k, "catalog_pin_number": p["pin_number"], **by_position[k]} for k, p in positions.items()],
            "standard_numbering_assumed": False, "cad_label_is_manufacturer_label": False,
            "cad_label_policy": "raw_library_labels_are_not_manufacturer_synonyms"},
        "allowed_claims": ["pin_name", "pin_number", "physical_terminal_identity"],
        "electrical_role_inheritance": False, "manufacturer_number_is_asserted": False,
        "reference_connections_included": False, "status": "verified"}


def validate_position_proof(bundle: Mapping, proof: Mapping) -> dict:
    """Return a derived identity-only proof using only a frozen source bundle."""
    from .preparation import IDENTITY
    part = proof["supplier_part"]
    if not isinstance(part, str) or not IDENTITY.fullmatch(part) or bundle.get("supplier_part") != part:
        raise ValueError("exact C or registered project S identity required")
    if any(not isinstance(value, str) or not value.strip() for field, value in proof.items()
           if field not in {"manufacturer_pin_number", "manufacturer_evidence"}):
        raise ValueError("position proof requires explicit nonempty identity and reviewer fields")
    quality = bundle.get("stored_source_quality") or bundle.get("source_quality") or {}
    if not isinstance(quality, Mapping):
        raise ValueError("manufacturer source quality must be structured metadata")
    ordering_qualified = quality.get("identity_qualification") == "manufacturer_ordering_template_scope"
    if quality.get("identity_qualification") not in {None, "legacy_metadata_conflict", "manufacturer_ordering_template_scope"} or str(quality.get("original_source_kind", "")).startswith("related_"):
        raise ValueError("manufacturer source identity remains unresolved")
    if (bundle.get("document") or {}).get("product_name") != proof["model"]:
        raise ValueError("position model must match exact original-source identity")
    ref = proof["cad_source_ref"]
    if isinstance(ref, str) and re.fullmatch(r"public_std_symbol:[0-9a-f]{64}", ref):
        return _validate_std_three_contact(bundle, proof)
    if not isinstance(ref, str) or not re.fullmatch(r"public_cad:[0-9a-f]{64}", ref):
        raise ValueError("hashed public CAD identity binding required")
    binding = _source(bundle, ref.split(":")[1])
    if (binding.get("schema") != CAD_SCHEMA or binding.get("supplier_part") != part
            or binding.get("model") != proof["model"]
            or binding.get("reference_connections_included") is not False
            or binding.get("electrical_role_evidence") is not False):
        raise ValueError("CAD binding does not identify this exact device")
    raw = {kind: _source(bundle, binding["sources"][kind]["sha256"], raw=True)
           for kind in ("device", "symbol", "footprint")}
    device, symbol, footprint = raw["device"], raw["symbol"], raw["footprint"]
    if any(raw[kind].get("uuid") != binding["sources"][kind].get("uuid") for kind in raw):
        raise ValueError("public CAD UUID does not match the bound original asset")
    attr = device.get("attributes") or {}
    if attr.get("Symbol") != symbol.get("uuid") or attr.get("Footprint") != footprint.get("uuid"):
        raise ValueError("exact device/symbol/footprint UUID chain differs")
    model = attr.get("Manufacturer Part") or device.get("display_title")
    family_projection = model != proof["model"]
    if family_projection and not part.startswith("S"):
        raise ValueError("CAD manufacturer model differs from the source model")
    if part.startswith("C") and attr.get("Supplier Part") != part:
        raise ValueError("CAD supplier C anchor differs")
    if part.startswith("S"):
        if attr.get("Supplier Part"):
            raise ValueError("project S identity must not conceal an asserted supplier C identity")
        registered = [r for r in bundle.get("identity_constraints", [])
                      if r.get("identity_status") == "project_scoped" and r.get("model_name") == proof["model"]
                      and (((r.get("source") or {}).get("original_metadata") or {}).get("props") or {}).get("Device") == device.get("uuid")]
        if not registered:
            raise ValueError("project S identity lacks an independently registered exact CAD device")
    cites = proof["manufacturer_evidence"]
    if not isinstance(cites, list) or len(cites) != 2 or {c.get("claim") for c in cites} != {"position_inventory", "position_completeness"}:
        raise ValueError("two distinct complete original image reviews required")
    if any(set(cite) != {"claim", "source_ref", "document_sha256", "quote"} for cite in cites):
        raise ValueError("manufacturer evidence requires only claim/ref/hash/quote")
    inventory, first = _review(bundle, next(c for c in cites if c["claim"] == "position_inventory"), "physical_terminal_identity")
    review, second = _review(bundle, next(c for c in cites if c["claim"] == "position_completeness"), "physical_position_completeness")
    if (inventory.get("schema") != INVENTORY_SCHEMA or inventory.get("supplier_part") != part
            or inventory.get("model") != proof["model"] or inventory.get("package") != proof["package"]
            or inventory.get("document_sha256") != first["document_sha256"]
            or inventory.get("page") != first["page_idx"] + 1
            or inventory.get("complete_physical_view") is not True or inventory.get("topology") not in _KINDS
            or inventory.get("view") != "component_top_perspective"):
        raise ValueError("review must identify the exact complete oriented physical package")
    center_topology = inventory["topology"] == "two_rows_center_pad"
    project_model_evidence = None
    if ordering_qualified and not center_topology:
        raise ValueError("ordering-template qualification requires its explicit supported identity closure")
    if family_projection and not center_topology:
        raise ValueError("CAD manufacturer model differs from the source model")
    if center_topology and part.startswith("S"):
        # A public family title supplies physical library identity only. The
        # exact project model and all UUIDs must already be independently stored.
        original = [r["source"].get("original_metadata") for r in registered]
        exact = [row for row in original if isinstance(row, Mapping)
            and row.get("source_scope") == "identity_metadata_only"
            and re.fullmatch(r"[0-9a-f]{64}", str(row.get("source_sha256", "")))
            and isinstance(row.get("props"), Mapping)
            and row["props"].get("Name") == proof["model"]
            and row["props"].get("DeviceName") == device.get("display_title")
            and row["props"].get("FootprintName") == footprint.get("display_title")
            and all(row["props"].get(key) == raw[kind]["uuid"] for key, kind in
                    (("Device", "device"), ("Symbol", "symbol"), ("Footprint", "footprint")))]
        if len(exact) != 1 or attr.get("Manufacturer Part") not in {None, "", proof["model"]}:
            raise ValueError("project pad identity requires exact registered original instance and all public UUIDs")
    if center_topology and (part.startswith("S") or ordering_qualified):
        from .ordering_identity import authenticate_ordering_identity
        project_model_evidence = authenticate_ordering_identity({"device_name": proof["model"],
            "ordering_identity_review": inventory.get("ordering_identity_review")}, bundle)
    if (review.get("schema") != REVIEW_SCHEMA or review.get("supplier_part") != part
            or review.get("inventory_source_ref") != first["source_ref"]
            or review.get("inventory_transcript_sha256") != _sha(first["content"])
            or review.get("agreement") != "complete_positions_labels_anchor_and_printed_numbers"
            or first["source_ref"] == second["source_ref"]
            or first["document_sha256"] != second["document_sha256"] or first["page_idx"] != second["page_idx"]
            or first["source_quality"]["rendered_image_sha256"] != second["source_quality"]["rendered_image_sha256"]
            or first["source_quality"].get("reviewer") == second["source_quality"].get("reviewer")):
        raise ValueError("independent second review does not confirm the same exact original image inventory")
    geometry = _geometry(symbol, footprint); positions = _positions(geometry, inventory)
    if inventory["topology"] == "two_rows_partial_number":
        cad_package = re.match(r"^(?:SOP|SOIC|SSOP|TSSOP)[-_ ]?[0-9]+(?=_|$)", footprint.get("display_title", "").upper())
        if not cad_package or re.sub(r"[-_ ]", "", cad_package[0]) != re.sub(r"[-_ ]", "", inventory["package"].upper()):
            raise ValueError("exact CAD and manufacturer package family/count differ")
    if center_topology:
        count = _soic_ep_count(inventory["package"])
        cad_package = footprint.get("display_title", "").upper()
        if (not re.match(rf"^SOIC[-_ ]?{count}(?=_|$)", cad_package)
                or not re.search(r"[-_]EP$", cad_package)):
            raise ValueError("CAD package must explicitly identify the same SOIC lead count and exposed pad")
        center_rows = [row for row in _lines(footprint, "FOOTPRINT") if row[0] == "PAD"
            and row[5] == positions["center_pad"]["number"]]
        if (len(center_rows) != 1 or not isinstance(center_rows[0][10], list)
                or len(center_rows[0][10]) < 3 or center_rows[0][10][0] != "RECT"
                or any(_number(size) <= 0 for size in center_rows[0][10][1:3])):
            raise ValueError("one explicit rectangular central thermal pad required")
    items = inventory.get("positions")
    if not isinstance(items, list) or len(items) != len(positions):
        raise ValueError("manufacturer physical position inventory is incomplete")
    by_position = {}
    for item in items:
        if (not isinstance(item, dict) or set(item) != {"position_key", "manufacturer_label", "manufacturer_printed_pin_number"}
                or item["position_key"] in by_position or item["position_key"] not in positions
                or not isinstance(item["manufacturer_label"], str) or not item["manufacturer_label"].strip()):
            raise ValueError("every physical position needs one unique source label")
        by_position[item["position_key"]] = item
    selected = None
    for key, pad in positions.items():
        item = by_position[key]; named = geometry["symbol_pins"][pad["number"]]
        printed = item["manufacturer_printed_pin_number"]
        if printed is not None and (not isinstance(printed, str) or printed != pad["number"]):
            raise ValueError("manufacturer printed terminal number conflicts with CAD physical pad")
        if center_topology and key == "center_pad":
            if printed is not None or not _pad_label(item["manufacturer_label"]) or not _pad_label(named["name"]):
                raise ValueError("center-pad identity requires unnumbered physical pad labels, never a ground label")
        elif inventory["topology"] in {"two_rows_partial_number", "two_rows_center_pad"}:
            if named["name"].casefold() != item["manufacturer_label"].casefold():
                raise ValueError("manufacturer label at physical position conflicts with CAD symbol label")
            if center_topology and printed is None:
                raise ValueError("this topology requires every perimeter number explicitly printed")
        elif named["name"] != pad["number"] and named["name"].casefold() != item["manufacturer_label"].casefold():
            raise ValueError("CAD semantic label contradicts the marked-end manufacturer label")
        if pad["number"] == proof["catalog_pin_number"]:
            selected = {"position_key": key, "manufacturer": item, "cad_pad": pad, "cad_symbol_pin": named}
    anchor_key = "marked_end" if inventory["topology"] == "two_end_marked" else "pin1_side:0"
    anchor = inventory.get("anchor") or {}
    if anchor.get("position_key") != anchor_key or anchor.get("kind") != ("identification_mark" if inventory["topology"] == "two_end_marked" else "printed_pin1_and_top_mark"):
        raise ValueError("explicit original-view anchor required")
    if inventory["topology"] in {"two_rows_partial_number", "two_rows_center_pad"} and by_position[anchor_key]["manufacturer_printed_pin_number"] != "1":
        raise ValueError("partial-number topology requires actual printed pin1")
    if (not selected or selected["manufacturer"]["manufacturer_label"] != proof["manufacturer_terminal"]
            or proof["catalog_pin_name"] != proof["manufacturer_terminal"]
            or proof["manufacturer_pin_number"] != selected["manufacturer"]["manufacturer_printed_pin_number"]):
        raise ValueError("requested catalog identity differs from the source-authenticated physical position")
    return {"schema": "pcbagent.pin_identity_mapping.v1", **dict(proof),
            "position_derivation": {"method": "complete_manufacturer_public_cad_position_bijection",
                "topology": inventory["topology"], "position": selected, "cad_source_sha256": ref.split(":")[1],
                "cad_original_sources": binding["sources"], "manufacturer_inventory_source_ref": first["source_ref"],
                "completeness_review_source_ref": second["source_ref"], "physical_terminal_count": len(positions),
                "positions": [{"position_key": key, "catalog_pin_number": pad["number"], **by_position[key]} for key, pad in positions.items()],
                "standard_numbering_assumed": False, "cad_label_is_manufacturer_label": inventory["topology"] == "two_rows_partial_number",
                **({"project_model_evidence": project_model_evidence,
                    "public_family_title_is_exact_SKU": False,
                    "unnumbered_pad_label_join": "Physical exposed/thermal pad labels only; no electrical meaning."} if center_topology else {})},
            "allowed_claims": ["pin_name", "pin_number", "physical_terminal_identity"], "electrical_role_inheritance": False,
            "manufacturer_number_is_asserted": proof["manufacturer_pin_number"] is not None,
            "reference_connections_included": False, "status": "verified"}


def register_public_cad_sources(service: Any, supplier_part: str, *, model: str,
                                sources: Mapping[str, Mapping], reviewer: str) -> dict:
    """Preserve exact public library responses; source-only, no terminal approval."""
    from .preparation import IDENTITY, RUNTIME_ROOT
    from .web_source import SOURCE_DDL
    if not IDENTITY.fullmatch(supplier_part) or not model or not reviewer or set(sources) != {"device", "symbol", "footprint"}:
        raise ValueError("exact identity and three explicit public source files required")
    assets, bound = [], {}
    for kind, item in sources.items():
        path = Path(item["path"]).resolve(strict=True)
        if not path.is_relative_to(RUNTIME_ROOT.resolve()):
            raise ValueError("CAD response must be staged under dedicated knowledge runtime")
        raw = path.read_bytes(); record = json.loads(raw); result = record.get("result", {})
        uuid = result.get("uuid")
        expected = "https://pro.lceda.cn/api/" + ("devices/" if kind == "device" else "components/") + str(uuid)
        if record.get("success") is not True or record.get("code") != 0 or item.get("url") != expected or not re.fullmatch(r"[0-9a-f]{32}", str(uuid)):
            raise ValueError("exact successful public library endpoint required")
        sha = hashlib.sha256(raw).hexdigest()
        assets.append((sha, item["url"], raw, {"qualification": "public_cad_library_original_json", "supplier_part": supplier_part,
            "kind": kind, "uuid": uuid, "reviewer": reviewer, "electrical_role_evidence": False}))
        bound[kind] = {"sha256": sha, "uuid": uuid, "url": item["url"]}
    binding = {"schema": CAD_SCHEMA, "supplier_part": supplier_part, "model": model, "sources": bound,
               "reviewer": reviewer, "reference_connections_included": False, "electrical_role_evidence": False}
    raw = _canonical(binding).encode(); sha = hashlib.sha256(raw).hexdigest()
    assets.append((sha, "public_cad:" + sha, raw, {"qualification": "public_cad_position_identity_binding", "supplier_part": supplier_part}))
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute(SOURCE_DDL); cn.commit()
        for digest, url, raw, quality in assets:
            cur.execute("SELECT source_bytes FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s", (supplier_part, digest))
            prior = cur.fetchone()
            if prior and bytes(prior[0]) != raw:
                raise ValueError("existing source bytes changed")
            if not prior:
                cur.execute("INSERT INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,'application/json',%s,0,%s)",
                            (supplier_part, digest, url, raw, json.dumps(quality, ensure_ascii=False)))
        cn.commit()
    except BaseException:
        cn.rollback(); raise
    finally:
        cn.close()
    return {"source_ref": "public_cad:" + sha, "document_sha256": sha, "binding": binding, "assets": len(assets), "terminal_identity_verified": False}


__all__ = ["validate_position_proof", "register_public_cad_sources", "CAD_SCHEMA", "INVENTORY_SCHEMA", "REVIEW_SCHEMA"]
