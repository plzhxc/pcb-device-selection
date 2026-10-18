"""Versioned CAD contact aliases backed by original manufacturer terminals."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal, TypedDict


class PinIdentityProof(TypedDict):
    supplier_part: str
    model: str
    package: str
    catalog_pin_number: str
    catalog_pin_name: str
    manufacturer_terminal: str
    manufacturer_pin_number: str | None
    terminal_kind: Literal["metal_tab", "exposed_thermal_pad", "exposed_pad", "unnumbered_labeled_lead", "physical_position"]
    cad_source_ref: str
    manufacturer_evidence: list[dict[str, Any]]
    reviewer: str
    rationale: str


MAPPING_DDL = """CREATE TABLE IF NOT EXISTS device_pin_identity_mappings (
 mapping_sha256 CHAR(64) NOT NULL PRIMARY KEY,
 supplier_part VARCHAR(50) NOT NULL,
 catalog_pin_number VARCHAR(64) NOT NULL,
 package_name VARCHAR(128) NOT NULL,
 manufacturer_terminal VARCHAR(128) NOT NULL,
 manufacturer_pin_number VARCHAR(64) DEFAULT NULL,
 version INT NOT NULL,
 status VARCHAR(32) NOT NULL,
 proof_json JSON NOT NULL,
 created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE KEY unique_pin_identity_version (supplier_part,catalog_pin_number,version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
_FIELDS = frozenset(PinIdentityProof.__annotations__)
_CLAIMS = frozenset({"pin_number", "pin_name", "physical_terminal_identity"})
_LEAD_INVENTORY_SCHEMA = "pcbagent.manufacturer_labeled_terminal_inventory.v1"


def _labeled_lead_inventory(bundle: dict[str, Any], row: dict[str, Any],
                            proof: PinIdentityProof, cad: dict[str, Any]) -> None:
    """Authenticate complete, unique manufacturer labels against CAD labels.

    A visual review identifies physical labels only. CAD supplies the numeric
    aliases; the manufacturer number remains null for every reviewed lead.
    """
    from .evidence import authenticate_source_quote
    if row.get("provenance") != "reviewed_original_pdf_transcription":
        raise ValueError("unnumbered leads require a reviewed complete label inventory")
    authenticated = authenticate_source_quote(bundle, row, row["content"], fields=["physical_terminal_identity"])
    if authenticated is None:
        raise ValueError("complete lead inventory source failed authentication")
    assets = {a.get("source_sha256"): a for a in bundle.get("source_assets", []) if a.get("source_verified") is True}
    quality = row.get("source_quality") or {}
    receipt_sha = quality.get("review_receipt_sha256")
    receipt = assets.get(receipt_sha, {}).get("record")
    if (not isinstance(receipt, dict)
            or hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != receipt_sha
            or receipt.get("supplier_part") != proof["supplier_part"]
            or receipt.get("pdf_sha256") != row["document_sha256"]
            or receipt.get("page") != row["page_idx"] + 1
            or receipt.get("transcript") != row["content"]
            or receipt.get("rendered_image_sha256") != quality.get("rendered_image_sha256")
            or receipt.get("transcript_sha256") != hashlib.sha256(row["content"].encode()).hexdigest()
            or "physical_terminal_identity" not in receipt.get("reviewed_claim_scopes", [])):
        raise ValueError("complete lead inventory receipt does not authenticate")
    try:
        inventory = json.loads(row["content"])
    except (ValueError, TypeError) as exc:
        raise ValueError("lead review must contain a structured label inventory") from exc
    if (not isinstance(inventory, dict) or inventory.get("schema") != _LEAD_INVENTORY_SCHEMA
            or inventory.get("supplier_part") != proof["supplier_part"]
            or inventory.get("model") != proof["model"]
            or inventory.get("document_sha256") != row["document_sha256"]
            or inventory.get("package") != proof["package"]
            or inventory.get("complete_package_view") is not True
            or inventory.get("manufacturer_numbers_present") is not False):
        raise ValueError("lead review identity, package or completeness is unresolved")
    terminals = inventory.get("terminals")
    if (not isinstance(terminals, list) or not 1 <= len(terminals) <= 2000
            or any(not isinstance(t, dict) or set(t) != {"label", "manufacturer_pin_number"}
                or not isinstance(t["label"], str) or not t["label"].strip()
                or t["manufacturer_pin_number"] is not None for t in terminals)):
        raise ValueError("each unnumbered lead requires exactly one original label")
    labels = [t["label"].casefold() for t in terminals]
    cad_labels = [pin.get("pin_name", "").casefold() for pin in cad["pins"]]
    cad_numbers = [pin.get("pin_number") for pin in cad["pins"]]
    if (len(labels) != len(set(labels)) or len(cad_labels) != len(set(cad_labels))
            or len(cad_numbers) != len(set(cad_numbers)) or any(not n for n in cad_numbers)
            or set(labels) != set(cad_labels)
            or proof["manufacturer_terminal"].casefold() not in labels):
        raise ValueError("complete unique CAD and manufacturer terminal inventories differ")


def _local_exposed_pad_inventory(bundle: dict[str, Any], row: dict[str, Any],
                                 proof: PinIdentityProof, cad: dict[str, Any]) -> None:
    """Join a complete reviewed physical view to a filtered CAD number alias.

    This is an identity-only join. A central exposed pad is not assigned a
    manufacturer number, a ground role, or a permission to leave it open.
    """
    from .evidence import authenticate_source_quote
    from .terminal_inventory import _authenticate_reviewed_receipt
    quality = row.get("source_quality") or {}
    scopes = {"package", "physical_terminal_identity", "pin_count"}
    if (row.get("document_sha256") != (bundle.get("document") or {}).get("pdf_sha256")
            or row.get("provenance") != "reviewed_original_pdf_transcription"
            or authenticate_source_quote(bundle, row, row.get("content", ""), fields=sorted(scopes)) is None):
        raise ValueError("local exposed-pad identity requires a complete reviewed original package view")
    _authenticate_reviewed_receipt(row, {
        "document_sha256": row["document_sha256"], "page": row["page_idx"] + 1,
        "source_ref": row["source_ref"], "review_receipt_sha256": quality.get("review_receipt_sha256"),
        "rendered_image_sha256": quality.get("rendered_image_sha256"),
        "reviewed_claim_scopes": quality.get("reviewed_claim_scopes")}, bundle, required_scopes=scopes)
    try:
        inventory = json.loads(row["content"])
    except (ValueError, TypeError) as exc:
        raise ValueError("complete exposed-pad review must be a structured inventory") from exc
    expected = {"schema", "supplier_part", "model", "package", "document_sha256",
                "complete_package_view", "numbered_perimeter_terminals", "unnumbered_exposed_pads"}
    if (not isinstance(inventory, dict) or set(inventory) != expected
            or inventory.get("schema") != "pcbagent.manufacturer_exposed_pad_inventory.v1"
            or inventory.get("supplier_part") != proof["supplier_part"]
            or inventory.get("model") != proof["model"]
            or inventory.get("package") != proof["package"]
            or inventory.get("document_sha256") != row["document_sha256"]
            or inventory.get("complete_package_view") is not True):
        raise ValueError("reviewed exposed-pad inventory identity or completeness differs")
    package = _package(proof["package"])
    match = re.fullmatch(r"(?:HVQFN|QFN|DFN)([0-9]+)", package)
    numbers = inventory.get("numbered_perimeter_terminals")
    if (match is None or not 2 <= int(match[1]) <= 256
            or not isinstance(numbers, list) or len(numbers) != int(match[1])
            or any(not isinstance(n, str) for n in numbers)
            or set(numbers) != {str(n) for n in range(1, int(match[1]) + 1)}):
        raise ValueError("complete numbered perimeter inventory must match the exact package count")
    if inventory.get("unnumbered_exposed_pads") != [{
            "physical_identity": "exposed_pad", "position": "center", "manufacturer_pin_number": None}]:
        raise ValueError("exactly one unnumbered central exposed pad must be independently reviewed")
    mapping_sha = hashlib.sha256(_canonical(cad).encode()).hexdigest()
    source_rows = [r for r in bundle.get("symbol_mappings", []) if r.get("source_ref") == proof["cad_source_ref"]]
    if (len(source_rows) != 1 or source_rows[0].get("source_kind") != "local_symbol"
            or source_rows[0].get("document_sha256") != mapping_sha
            or proof["cad_source_ref"] != "local_symbol:" + mapping_sha
            or cad.get("schema") != "pcbagent.local_symbol_mapping.v1"
            or cad.get("model") != proof["model"]
            or not re.fullmatch(r"[0-9a-f]{64}", str(cad.get("original_source_sha256", "")))
            or not isinstance(cad.get("symbol_uuid"), str) or not cad["symbol_uuid"]
            or not isinstance(cad.get("footprint_uuid"), str) or not cad["footprint_uuid"]):
        raise ValueError("complete filtered CAD identity/hash is unavailable or changed")
    pins = cad.get("pins")
    if (not isinstance(pins, list) or len(pins) != len(numbers) + 1
            or any(not isinstance(p, dict) or set(p) != {"pin_number", "pin_name"}
                   or not isinstance(p["pin_number"], str) or not p["pin_number"]
                   or not isinstance(p["pin_name"], str) or not p["pin_name"] for p in pins)
            or len({p["pin_number"] for p in pins}) != len(pins)
            or {p["pin_number"] for p in pins} != set(numbers) | {proof["catalog_pin_number"]}
            or proof["catalog_pin_number"] in numbers
            or proof["catalog_pin_name"].upper() != "EP"
            or proof["manufacturer_terminal"].upper() != "EP"
            or [p["pin_number"] for p in pins if p["pin_name"].upper() == "EP"] != [proof["catalog_pin_number"]]):
        raise ValueError("CAD must contain the complete perimeter plus exactly one distinct EP alias")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _package(value: str) -> str:
    # ESOP is an explicitly exposed-pad family. Keep a literal L suffix and
    # never infer this family from an ordinary SOP or a free-form EP suffix.
    esop = re.match(r"^ESOP[-_ ]?([0-9]+)(L)?(?=$|[-_ ][A-Z])", value.upper())
    if esop:
        return "ESOP" + esop[1] + (esop[2] or "")
    hvqfn = re.match(r"^HVQFN[-_ ]?([0-9]+)(?=$|[-_ ]+[A-Z]|[ ]*\()", value.upper())
    if hvqfn:
        return "HVQFN" + hvqfn[1]
    match = re.match(r"^(QFN|DFN|SOT|TO)[-_ ]?([0-9]+)", value.upper())
    if not match:
        raise ValueError("unsupported or unspecified physical package")
    family = "".join(match.groups())
    lead = re.match(r"[-_ ]([0-9]+)(?:L|[_-]|$)", value.upper()[match.end():])
    if lead and not (family == "SOT223" and lead[1] == "3"):
        return family + "-" + lead[1] + "L"
    return family


def _quoted_package_matches(package: str, quote: str) -> bool:
    """Compare complete source tokens without inventing printed suffixes.

    The internal SOT23-3L key may describe the literal token SOT23-3. It must
    not require an L absent from the manufacturer page, drop a printed lead
    count, or match the substring SOT23 inside SOT323/SOT523.
    """
    expected = _package(package)
    if expected.startswith("HVQFN"):
        pattern = r"(?<![A-Z0-9])HVQFN[-_ ]?[0-9]+(?![A-Z0-9]|[-_ ]+[0-9])"
        return any(_package(match.group()) == expected for match in re.finditer(pattern, quote.upper()))
    if expected.startswith("ESOP"):
        pattern = r"(?<![A-Z0-9])ESOP[-_ ]?[0-9]+L?(?![A-Z0-9]|[-_ ]+[0-9])"
        return any(_package(match.group()) == expected for match in re.finditer(pattern, quote.upper()))
    pattern = r"(?<![A-Z0-9])(?:QFN|DFN|SOT|TO)[-_ ]?[0-9]+(?:[-_ ][0-9]+L?)?(?![A-Z0-9]|[-_ ]+[0-9])"
    if any(_package(match.group()) == expected for match in re.finditer(pattern, quote.upper())):
        return True
    # Some mechanical drawings print "24 Lead QFN", preserving the same
    # explicit family/count without inventing a source token such as QFN-24.
    reverse = r"(?<![A-Z0-9])([0-9]+)[ -]+(?:LEAD|PIN)[ -]+(QFN|DFN)(?![A-Z0-9]|[-_ ]+[0-9])"
    return any(match[2] + match[1] == expected for match in re.finditer(reverse, quote.upper()))


def validate_pin_identity_proof(service: Any, proof: PinIdentityProof) -> dict[str, Any]:
    """Re-read both sources and validate a narrowly scoped alias proof.

    These aliases retain CAD numbering. They never assert that the manufacturer
    assigned that number or grant electrical role, voltage or connection rules.
    """
    from .preparation import IDENTITY
    if set(proof) != _FIELDS:
        raise ValueError("pin identity proof has missing or unknown fields")
    if proof["terminal_kind"] == "physical_position":
        from .pin_positions import validate_position_proof
        return validate_position_proof(service.read_primary_source_evidence(proof["supplier_part"], limit=200), proof)
    part = proof["supplier_part"]
    if not IDENTITY.fullmatch(part) or not part.startswith("C"):
        raise ValueError("pin identity mapping requires an exact C anchor")
    for field in _FIELDS - {"manufacturer_pin_number", "manufacturer_evidence"}:
        if not isinstance(proof[field], str) or not proof[field].strip():
            raise ValueError(f"pin identity proof requires {field}")
    if proof["manufacturer_pin_number"] is not None:
        raise ValueError("this mapping API only supports unnumbered manufacturer terminals")
    if proof["terminal_kind"] not in {"metal_tab", "exposed_thermal_pad", "exposed_pad", "unnumbered_labeled_lead"}:
        raise ValueError("unsupported terminal kind")
    if proof["terminal_kind"] in {"exposed_thermal_pad", "exposed_pad"} and re.sub(r"[^A-Z]", "", proof["manufacturer_terminal"].upper()) not in {"EP", "PAD", "EXPOSEDPAD", "THERMALPAD"}:
        raise ValueError("thermal pad identity cannot assert an electrical node label")
    if proof["catalog_pin_name"].casefold() != proof["manufacturer_terminal"].casefold():
        raise ValueError("CAD label and manufacturer terminal alias differ")
    bundle = service.read_primary_source_evidence(part, limit=200)
    quality = bundle.get("source_quality") or {}
    if quality.get("identity_qualification") not in {None, "legacy_metadata_conflict"}:
        raise ValueError("device identity remains unresolved")
    known_models = [(bundle.get("document") or {}).get("product_name"),
                    (bundle.get("device") or {}).get("device_name")]
    known_models.extend(row.get("model_name") for row in bundle.get("identity_constraints", []))
    if proof["model"] not in known_models:
        raise ValueError("mapping model does not match the C catalog/source identity")
    candidates = [row for row in bundle.get("symbol_mappings", []) if row["source_ref"] == proof["cad_source_ref"]]
    if len(candidates) != 1:
        raise ValueError("exact CAD mapping source unavailable")
    cad = candidates[0]; mapping = cad["mapping"]
    if cad.get("source_kind") == "public_std_symbol":
        from .std_symbol import validate_std_symbol, validate_exposed_pad_position
        mapping = validate_std_symbol(bundle, cad)
        if proof["terminal_kind"] not in {"exposed_pad", "exposed_thermal_pad"} or mapping["model"] != proof["model"]:
            raise ValueError("public Std mapping is limited to exact-model exposed-pad identities")
        validate_exposed_pad_position(mapping, proof["catalog_pin_number"])
    elif proof["terminal_kind"] == "exposed_pad" and cad.get("source_kind") != "local_symbol":
        raise ValueError("unnumbered exposed-pad identity requires authenticated public Std geometry")
    if mapping.get("supplier_part") != part or mapping.get("reference_connections_included") is not False or mapping.get("electrical_role_evidence") is not False:
        raise ValueError("CAD evidence is not a filtered exact-ID numbering source")
    cad_package, manufacturer_package = _package(mapping["footprint_name"]), _package(proof["package"])
    complete_lead_package = (proof["terminal_kind"] == "unnumbered_labeled_lead"
        and cad_package == manufacturer_package + "-" + str(len(mapping["pins"])) + "L")
    if cad_package != manufacturer_package and not complete_lead_package:
        raise ValueError("CAD and manufacturer package do not match")
    if proof["terminal_kind"] == "unnumbered_labeled_lead":
        cad_sha = hashlib.sha256(_canonical(mapping).encode()).hexdigest()
        if cad_sha != cad.get("document_sha256") or cad.get("source_ref") != "local_symbol:" + cad_sha:
            raise ValueError("filtered CAD inventory hash changed")
    pins = [row for row in mapping["pins"] if row["pin_number"] == proof["catalog_pin_number"]]
    if len(pins) != 1 or pins[0]["pin_name"].casefold() != proof["catalog_pin_name"].casefold():
        raise ValueError("CAD contact number/name not supported by source")
    supplied = proof["manufacturer_evidence"]
    if not isinstance(supplied, list) or not supplied:
        raise ValueError("manufacturer terminal evidence required")
    verified = []; claims = set()
    for cite in supplied:
        if set(cite) != {"claim", "source_ref", "document_sha256", "quote"}:
            raise ValueError("manufacturer evidence requires claim/ref/hash/quote only")
        claim = cite["claim"]
        if claim not in {"package", "terminal_exists", "terminal_function"}:
            raise ValueError("unsupported manufacturer mapping claim")
        matches = [row for row in bundle["evidence"] if row.get("source_ref") == cite["source_ref"]
                   and row.get("document_sha256") == cite["document_sha256"]]
        if len(matches) != 1:
            raise ValueError("manufacturer evidence reference is missing or ambiguous")
        row = matches[0]
        if row.get("source_kind") != "datasheet" or row.get("provenance") not in {"original_pdf_native", "reviewed_original_pdf_transcription"}:
            raise ValueError("manufacturer claim must use original PDF or an explicit visual review")
        if not isinstance(cite["quote"], str) or len(cite["quote"].strip()) < 3 or _text(cite["quote"]) not in _text(row["content"]):
            raise ValueError("manufacturer quote does not occur in verified source")
        if proof["terminal_kind"] in {"unnumbered_labeled_lead", "exposed_pad"}:
            from .evidence import authenticate_source_quote
            scope = "package" if claim == "package" else "physical_terminal_identity"
            if authenticate_source_quote(bundle, row, cite["quote"], fields=[scope]) is None:
                raise ValueError("manufacturer lead mapping source failed authentication")
        if row["provenance"] == "reviewed_original_pdf_transcription":
            scope = "package" if claim == "package" else "physical_terminal_identity"
            if scope not in row["source_quality"]["reviewed_claim_scopes"]:
                raise ValueError("visual review did not authorize this mapping claim")
        if claim == "package" and not _quoted_package_matches(proof["package"], cite["quote"]):
            raise ValueError("manufacturer package quote does not match requested package")
        if claim != "package":
            quote = _text(cite["quote"])
            if proof["terminal_kind"] == "metal_tab":
                if "tab" not in quote or proof["manufacturer_terminal"].casefold() not in quote:
                    raise ValueError("tab evidence does not identify its function/label")
            elif proof["terminal_kind"] == "unnumbered_labeled_lead":
                _labeled_lead_inventory(bundle, row, proof, mapping)
            elif proof["terminal_kind"] == "exposed_pad" and cad.get("source_kind") == "local_symbol":
                if cite["quote"] != row.get("content"):
                    raise ValueError("local exposed-pad identity must consume the entire reviewed inventory")
                _local_exposed_pad_inventory(bundle, row, proof, mapping)
            elif not all(word in quote for word in (("exposed", "pad") if proof["terminal_kind"] == "exposed_pad" else ("exposed", "thermal", "pad"))):
                raise ValueError("exposed thermal pad evidence is incomplete")
        claims.add(claim)
        verified.append({**cite, "source_kind": "datasheet", "page": row["page_idx"] + 1,
            "provenance": row["provenance"], "source_quality": row.get("source_quality")})
    required_claims = {"package", "terminal_exists"} if proof["terminal_kind"] == "exposed_pad" else {"package", "terminal_exists", "terminal_function"}
    if claims != required_claims:
        raise ValueError("package, physical terminal and function must all be evidenced")
    return {"schema": "pcbagent.pin_identity_mapping.v1", **dict(proof),
        "cad_evidence": {"source_ref": cad["source_ref"], "document_sha256": cad["document_sha256"],
            "source_kind": cad.get("source_kind", "local_symbol"), "pin_number": pins[0]["pin_number"], "pin_name": pins[0]["pin_name"],
            "package": mapping["footprint_name"]}, "manufacturer_evidence": verified,
        "allowed_claims": sorted(_CLAIMS), "electrical_role_inheritance": False,
        "manufacturer_number_is_asserted": False, "reference_connections_included": False,
        "status": "verified"}


def register_pin_identity_mapping(service: Any, proof: PinIdentityProof) -> dict[str, Any]:
    validated = validate_pin_identity_proof(service, proof)
    sha = hashlib.sha256(_canonical(validated).encode()).hexdigest()
    cn = service.open_catalog_connection()
    try:
        cur = cn.cursor(dictionary=True); cur.execute(MAPPING_DDL); cn.commit()
        cur.execute("SELECT GET_LOCK(%s,30) AS acquired", ("pcbagent_pin_identity_" + proof["supplier_part"],))
        if cur.fetchone()["acquired"] != 1:
            raise RuntimeError("pin identity version lock unavailable")
        try:
            cur.execute("SELECT version FROM device_pin_identity_mappings WHERE mapping_sha256=%s", (sha,))
            old = cur.fetchone()
            if old:
                version = old["version"]
            else:
                cur.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM device_pin_identity_mappings WHERE supplier_part=%s AND catalog_pin_number=%s", (proof["supplier_part"], proof["catalog_pin_number"]))
                version = cur.fetchone()["v"]
                cur.execute("INSERT INTO device_pin_identity_mappings (mapping_sha256,supplier_part,catalog_pin_number,package_name,manufacturer_terminal,manufacturer_pin_number,version,status,proof_json) VALUES (%s,%s,%s,%s,%s,%s,%s,'verified',%s)",
                    (sha, proof["supplier_part"], proof["catalog_pin_number"], proof["package"], proof["manufacturer_terminal"], proof["manufacturer_pin_number"], version, _canonical(validated)))
                cn.commit()
        finally:
            cur.execute("SELECT RELEASE_LOCK(%s)", ("pcbagent_pin_identity_" + proof["supplier_part"],)); cur.fetchone()
    except BaseException:
        cn.rollback(); raise
    finally: cn.close()
    return {**validated, "mapping_sha256": sha, "version": version,
            "source_ref": "pin_identity_mapping:" + sha, "source_kind": "pin_identity_mapping"}


def read_pin_identity_mappings(service: Any, supplier_part: str, *, mapping_sha256: str | None = None) -> list[dict[str, Any]]:
    from .preparation import IDENTITY
    if not IDENTITY.fullmatch(supplier_part):
        raise ValueError("valid identity required")
    if mapping_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", mapping_sha256):
        raise ValueError("valid mapping SHA256 required")
    cn = service.open_catalog_connection()
    try:
        cn.start_transaction(readonly=True); cur = cn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema='device_parameters' AND table_name='device_pin_identity_mappings'")
        if not cur.fetchone()["n"]:
            return []
        cur.execute("SELECT mapping_sha256,catalog_pin_number,version,status,proof_json FROM device_pin_identity_mappings WHERE supplier_part=%s ORDER BY version DESC", (supplier_part,))
        rows = cur.fetchall()
    finally: cn.rollback(); cn.close()
    result = []; seen = set()
    for row in rows:
        if mapping_sha256 and row["mapping_sha256"] != mapping_sha256:
            continue
        if not mapping_sha256 and row["catalog_pin_number"] in seen:
            continue
        seen.add(row["catalog_pin_number"])
        if row["status"] != "verified":
            raise ValueError("pin identity mapping is not verified")
        proof = row["proof_json"]
        proof = json.loads(proof) if isinstance(proof, str) else proof
        if hashlib.sha256(_canonical(proof).encode()).hexdigest() != row["mapping_sha256"]:
            raise ValueError("stored pin identity mapping hash mismatch")
        raw = {key: proof[key] for key in _FIELDS}
        raw["manufacturer_evidence"] = [{k: cite[k] for k in ("claim", "source_ref", "document_sha256", "quote")} for cite in raw["manufacturer_evidence"]]
        current = validate_pin_identity_proof(service, raw)
        if current != proof:
            raise ValueError("pin identity source chain changed; mapping requires a new version")
        result.append({**proof, "version": row["version"], "mapping_sha256": row["mapping_sha256"],
            "source_ref": "pin_identity_mapping:" + row["mapping_sha256"], "source_kind": "pin_identity_mapping"})
    return result


def authenticate_pin_identity_mapping(service: Any, supplier_part: str, mapping_sha256: str,
                                       field: str, value: str) -> dict[str, Any]:
    if field not in {"pin_number", "pin_name"}:
        raise ValueError("mapping does not authorize this catalog field")
    rows = read_pin_identity_mappings(service, supplier_part, mapping_sha256=mapping_sha256)
    if len(rows) != 1 or field not in {"pin_number", "pin_name"}:
        raise ValueError("mapping is unavailable or does not authorize this catalog field")
    mapping = rows[0]
    if mapping["catalog_" + field] != value:
        raise ValueError("catalog value differs from authenticated pin identity mapping")
    number_statement = ("manufacturer pin number remains null" if mapping["manufacturer_pin_number"] is None
                        else "manufacturer explicitly printed number " + mapping["manufacturer_pin_number"])
    terminal_statement = ("manufacturer physical position" if mapping["terminal_kind"] == "physical_position"
                          else "unnumbered manufacturer " + mapping["terminal_kind"])
    evidence = {"source_kind": "pin_identity_mapping", "source_ref": mapping["source_ref"],
        "document_sha256": mapping_sha256, "locator": "catalog_pin:" + mapping["catalog_pin_number"],
        "quote": f"CAD contact {mapping['catalog_pin_number']} ({mapping['catalog_pin_name']}) aliases the {terminal_statement} {mapping['manufacturer_terminal']} in {mapping['package']}; {number_statement}.",
        "mapping": mapping, "authorized_field": field, "authorized_value": value}
    return {"status": "verified", "evidence": evidence}


__all__ = ["PinIdentityProof", "MAPPING_DDL", "register_pin_identity_mapping", "read_pin_identity_mappings", "authenticate_pin_identity_mapping", "validate_pin_identity_proof"]
