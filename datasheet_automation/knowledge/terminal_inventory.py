"""Derive catalog counts from reviewed, complete manufacturer pin inventories.

Uses the existing original-PDF review receipts and source reader. A largest pin
number or a diagram caption is never interpreted as a terminal count.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import hashlib
import json
import re
import unicodedata
from typing import Any

INVENTORY_SCHEMA = "pcbagent.manufacturer_terminal_inventory.v1"
VISUAL_INVENTORY_SCHEMA = "pcbagent.manufacturer_terminal_inventory.v2"
DIAGRAM_INVENTORY_SCHEMA = "pcbagent.manufacturer_terminal_inventory.v3"
DIAGRAM_TRANSCRIPTION_SCHEMA = "pcbagent.manufacturer_numeric_terminal_diagram.v1"
TYPED_DIAGRAM_TRANSCRIPTION_SCHEMA = "pcbagent.manufacturer_numeric_terminal_diagram.v2"
_ROW = re.compile(r"^(\d+(?:\s*(?:、|,|，|[-~～–—])\s*\d+)*)(?:\(([^)]+)\))?\s+([A-Za-z][A-Za-z0-9_+./-]*)\b")
_PACKAGE = re.compile(r"(?:QFN|DFN|LQFP|TQFP|VQFN|WQFN|SOP|SOIC|SSOP|TSSOP|DIP|PDIP)(\d+)", re.I)


def _authenticate_reviewed_receipt(chunk: Mapping, proof: Mapping, knowledge: Mapping,
                                  *, required_scopes: set[str]) -> None:
    """Authenticate one complete original-PDF review, including its actual assets.

    The source reader verifies asset bytes. This consumer additionally binds the
    immutable receipt record to the current transcript, fields, supplier and
    original page; a self-consistent chunk checksum is insufficient.
    """
    quality = chunk.get("source_quality") or {}
    if not isinstance(quality, Mapping):
        raise ValueError("PDF review quality must be an object")
    sha, page, receipt_sha = proof.get("document_sha256"), proof.get("page"), proof.get("review_receipt_sha256")
    scopes = quality.get("reviewed_claim_scopes")
    if (chunk.get("provenance") != "reviewed_original_pdf_transcription"
            or chunk.get("lichuang_id") != knowledge.get("supplier_part")
            or not isinstance(chunk.get("content"), str)
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            or type(page) is not int or page < 1 or type(chunk.get("page_idx")) is not int
            or chunk["page_idx"] + 1 != page or chunk.get("document_sha256") != sha
            or not isinstance(receipt_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", receipt_sha)
            or chunk.get("source_ref") != f"pdf:{sha}:page:{page}:review:{receipt_sha}"
            or proof.get("source_ref") != chunk.get("source_ref")
            or quality.get("review_receipt_sha256") != receipt_sha
            or not isinstance(scopes, list) or not scopes
            or not all(isinstance(field, str) and field for field in scopes)
            or len(scopes) != len(set(scopes)) or not required_scopes <= set(scopes)
            or proof.get("reviewed_claim_scopes") != scopes
            or not isinstance(quality.get("reviewer"), str) or not quality["reviewer"].strip()
            or quality.get("review_method") != "explicit_visual_inspection_of_original_pdf_render"):
        raise ValueError("PDF review transcript, source page and authorized fields must agree")

    def asset(asset_sha: Any, mime: str) -> Mapping:
        rows = [row for row in knowledge.get("source_assets", []) if isinstance(row, Mapping)
                and row.get("source_sha256") == asset_sha]
        if (not isinstance(asset_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", asset_sha)
                or len(rows) != 1 or rows[0].get("source_verified") is not True
                or rows[0].get("source_mime") != mime):
            raise ValueError("PDF review requires unique verified receipt and original rendering assets")
        return rows[0]

    receipt_asset = asset(receipt_sha, "application/json")
    image_sha = quality.get("rendered_image_sha256")
    image_asset = asset(image_sha, "image/png")
    receipt = receipt_asset.get("record")
    try:
        actual_sha = hashlib.sha256(json.dumps(receipt, ensure_ascii=False, sort_keys=True,
                                               allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ValueError("PDF review receipt must be canonical finite JSON") from exc
    if (not isinstance(receipt, Mapping) or actual_sha != receipt_sha
            or receipt.get("schema") != "pcbagent.pdf_page_review.v1"
            or receipt.get("transcript") != chunk["content"]
            or receipt.get("supplier_part") != knowledge.get("supplier_part")
            or receipt.get("pdf_sha256") != sha or type(receipt.get("page")) is not int or receipt["page"] != page
            or receipt.get("transcript_sha256") != hashlib.sha256(chunk["content"].encode()).hexdigest()
            or receipt.get("transcript_sha256") != quality.get("transcript_sha256")
            or receipt.get("reviewed_claim_scopes") != scopes
            or receipt.get("reviewer") != quality.get("reviewer")
            or receipt.get("review_method") != quality.get("review_method")
            or receipt.get("rendered_image_sha256") != image_sha
            or proof.get("rendered_image_sha256") != image_sha):
        raise ValueError("PDF review record hash, transcript, identity, rendering and scopes must agree")
    for current in (receipt_asset, image_asset):
        metadata = current.get("quality") or {}
        if (not isinstance(metadata, Mapping) or metadata.get("pdf_sha256") != sha
                or type(metadata.get("page")) is not int or metadata["page"] != page):
            raise ValueError("PDF review assets must identify the same original PDF page")


def _normal(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _numbers(value: str) -> list[str]:
    result = []
    for group in re.split(r"\s*[、,，]\s*", value):
        bounds = re.split(r"\s*[-~～–—]\s*", group)
        if len(bounds) == 1:
            result.append(bounds[0].strip())
        elif len(bounds) == 2 and int(bounds[0]) <= int(bounds[1]) and int(bounds[1]) - int(bounds[0]) < 2000:
            result.extend(str(n) for n in range(int(bounds[0]), int(bounds[1]) + 1))
        else:
            raise ValueError("invalid or unbounded manufacturer terminal range")
    return result


def _visual_table_inventory(inventory: Mapping, extraction: Mapping, knowledge: Mapping,
                            facts: Mapping, proof: Mapping, chunks: Mapping,
                            source_evidence: Callable, *, diagram: bool = False) -> dict:
    """Count a separately reviewed single-package table or numeric diagram.

    A literal transcription and a completeness review are distinct receipts.
    This branch does not infer counts from package names or largest pin numbers.
    Unnumbered leads and unselected multi-package columns remain unsupported.
    A v2 table may separately identify a manufacturer-numbered EP/EPAD row.
    A v3 diagram may close one reviewed unnumbered center pad using a literal
    numbered EP/EPAD row on another same-PDF page and an independent union review;
    other unnumbered pads remain unsupported.
    A typed v2 diagram transcription instead covers up to 256 explicitly
    numbered contacts and one annotated center pad on the same original figure.
    """
    if (inventory.get("supplier_part") != knowledge.get("supplier_part")
            or inventory.get("document_sha256") != proof["document_sha256"]
            or inventory.get("device_name") != facts.get("device_name")
            or inventory.get("package") != facts.get("package")
            or inventory.get("complete_figure") is not True
            or inventory.get("diagram_kind" if diagram else "table_kind") != (
                "closed_single_package_numeric_pin_diagram" if diagram else "closed_single_package_numeric_pin_table")):
        raise ValueError("visual inventory requires an exact, complete single-package table")
    table = inventory.get("diagram" if diagram else "table")
    if not isinstance(table, Mapping) or set(table) != {"source_ref", "locator"} or not isinstance(table["locator"], str) or not table["locator"].strip():
        raise ValueError("visual table requires a precise independent transcription locator")
    chunk = chunks.get(str(table["source_ref"]))
    if (not chunk or chunk.get("provenance") != "reviewed_original_pdf_transcription"
            or chunk.get("source_ref") == proof.get("source_ref")):
        raise ValueError("table transcription must be a separately registered original-page review")
    checked = source_evidence({"chunk_id": chunk["id"], "quote": chunk["content"]}, knowledge)
    if (not checked or checked.get("document_sha256") != proof["document_sha256"]
            or checked.get("page") != proof["page"]
            or checked.get("review_receipt_sha256") == proof.get("review_receipt_sha256")
            or not {"pin_number", "pin_name"} <= set(checked.get("reviewed_claim_scopes", []))):
        raise ValueError("complete table and numeric/name transcription need distinct scoped reviews of the same page")
    _authenticate_reviewed_receipt(chunk, checked, knowledge, required_scopes=(
        {"pin_number", "pin_name", "numeric_terminal_diagram"} if diagram else {"pin_number", "pin_name"}))
    parsed, row_evidence, numbered_pad_annotations = {}, [], {}
    typed_diagram, numeric_limit, typed_rows = False, 64, {}
    if diagram:
        try:
            transcript = json.loads(chunk["content"])
        except (TypeError, ValueError) as exc:
            raise ValueError("numeric diagram requires a structured original-figure transcription") from exc
        if (not isinstance(transcript, Mapping)
                or transcript.get("schema") not in {DIAGRAM_TRANSCRIPTION_SCHEMA, TYPED_DIAGRAM_TRANSCRIPTION_SCHEMA}
                or transcript.get("supplier_part") != knowledge.get("supplier_part")
                or transcript.get("document_sha256") != proof["document_sha256"]
                or transcript.get("device_name") != facts.get("device_name")
                or transcript.get("package") != facts.get("package")
                or transcript.get("numbering_basis") != "manufacturer_printed_terminal_numbers"
                or transcript.get("locator") != table["locator"]):
            raise ValueError("diagram transcription must bind actual printed terminal numbers and the exact package")
        terminals = transcript.get("terminals")
        typed_diagram = transcript["schema"] == TYPED_DIAGRAM_TRANSCRIPTION_SCHEMA
        if typed_diagram:
            numeric_limit = 256
            complete_review = chunks.get(proof["source_ref"], {})
            if (proof.get("source_kind") != "datasheet"
                    or proof["document_sha256"] != (knowledge.get("document") or {}).get("pdf_sha256")
                    or set(transcript) != {"schema", "supplier_part", "document_sha256", "device_name",
                        "package", "numbering_basis", "locator", "terminals", "unindexed_features"}
                    or transcript["unindexed_features"] != []
                    or set(inventory) != {"schema", "supplier_part", "document_sha256", "device_name",
                        "package", "complete_figure", "diagram_kind", "diagram", "figure_terminals", "package_source"}
                    or checked.get("rendered_image_sha256") != proof.get("rendered_image_sha256")
                    or complete_review.get("source_quality", {}).get("reviewer", "").strip().casefold()
                       == chunk.get("source_quality", {}).get("reviewer", "").strip().casefold()):
                raise ValueError("typed diagram requires two independent reviews of one original primary figure")
            _authenticate_reviewed_receipt(chunk, checked, knowledge,
                required_scopes={"pin_number", "pin_name", "numeric_terminal_diagram", "physical_terminal_identity"})
            _authenticate_reviewed_receipt(complete_review, proof, knowledge,
                required_scopes={"terminal_inventory_completeness", "physical_terminal_identity"})
        row_fields = {"pin_number", "pin_name", "number_role", "label_role", "location"}
        if typed_diagram:
            row_fields |= {"terminal_kind", "physical_annotation"}
        label_pattern = (r"[A-Za-z](?:[A-Za-z0-9_+./-]|\[[0-9]+\]|\([A-Za-z0-9_+./-]+\))*"
                         if typed_diagram else r"[A-Za-z][A-Za-z0-9_+./-]*(?:\([A-Za-z0-9_+./-]+\)[A-Za-z0-9_+./-]*)*")
        if (not isinstance(terminals, list) or not 1 <= len(terminals) <= numeric_limit
                or any(not isinstance(row, Mapping)
                    or set(row) != row_fields
                    or not isinstance(row["pin_number"], str)
                    or not re.fullmatch(r"[1-9][0-9]{0,2}" if typed_diagram else r"[1-9][0-9]?", row["pin_number"])
                    or int(row["pin_number"]) > numeric_limit
                    or not isinstance(row["pin_name"], str)
                    or not re.fullmatch(label_pattern, row["pin_name"])
                    or row["number_role"] != "physical_pin_number"
                    or row["label_role"] != "terminal_label"
                    or not isinstance(row["location"], str) or not row["location"].strip()
                    for row in terminals)):
            raise ValueError("diagram rows must identify physical numbered leads; callouts, markings and unnumbered pads are not terminals")
        if typed_diagram:
            for row in terminals:
                kind, annotation = row["terminal_kind"], row["physical_annotation"]
                if kind == "exposed_pad":
                    if (row["location"] != "package_center" or not isinstance(annotation, str)
                            or not re.fullmatch(r"(?:exposed[ _-]+pad|thermal[ _-]+pad|ep|epad)", annotation, re.I)):
                        raise ValueError("numbered center pad needs its literal reviewed physical annotation")
                    numbered_pad_annotations[row["pin_number"]] = row["pin_name"]
                elif kind != "peripheral" or annotation is not None or row["location"] == "package_center":
                    raise ValueError("typed peripheral rows cannot conceal a center-pad annotation")
                typed_rows[row["pin_number"]] = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if len(numbered_pad_annotations) > 1:
                raise ValueError("typed diagram supports at most one explicitly numbered center pad")
        lines = [(row["pin_number"], row["pin_name"], None, json.dumps(row, ensure_ascii=False, sort_keys=True)) for row in terminals]
    else:
        lines = []
        for line in chunk["content"].splitlines():
            match = re.fullmatch(r"\s*([0-9]+)\.?(?:\s*\((EP|EPAD)\))?\s+([A-Za-z][A-Za-z0-9_+./-]*)(?:\s+.*)?", line)
            if match:
                number, annotation, name = match.groups()
                lines.append((number, name, annotation, line))
    for number, name, annotation, line in lines:
        if number in parsed:
            raise ValueError("visual table repeats a manufacturer terminal")
        row_proof = source_evidence({"chunk_id": chunk["id"], "quote": line}, knowledge)
        if not row_proof:
            raise ValueError("transcribed terminal row is not authenticated")
        if annotation is not None:
            if diagram or name != annotation:
                raise ValueError("numbered exposed pad must retain its explicitly printed EP/EPAD label")
            numbered_pad_annotations[number] = annotation
        parsed[number] = name
        row_evidence.append({"pin_numbers": [number], "pin_name": name, "evidence": row_proof})
    if not 1 <= len(parsed) <= numeric_limit:
        raise ValueError("visual table must contain a bounded numeric terminal inventory")
    pad_addendum_proof = None
    if diagram and transcript.get("unindexed_features") and "numbered_pad_addendum" not in inventory:
        raise ValueError("a depicted unnumbered feature requires an explicit supported identity closure")
    if "numbered_pad_addendum" in inventory:
        if not diagram or proof.get("source_kind") != "datasheet":
            raise ValueError("numbered pad addendum requires a primary numeric diagram")
        complete_review = chunks.get(proof["source_ref"], {})
        _authenticate_reviewed_receipt(complete_review, proof, knowledge,
            required_scopes={"terminal_inventory_completeness", "cross_page_numbered_pad_identity"})
        _authenticate_reviewed_receipt(chunk, checked, knowledge,
            required_scopes={"pin_number", "pin_name", "numeric_terminal_diagram", "physical_terminal_identity"})
        if complete_review.get("source_quality", {}).get("reviewer") == chunk.get("source_quality", {}).get("reviewer"):
            raise ValueError("cross-page union requires an independent completeness reviewer")
        if transcript.get("unindexed_features") != [{"kind": "exposed_pad", "location": "package_center",
                "manufacturer_pin_number": None, "pin_name": None}]:
            raise ValueError("exactly one original unnumbered center exposed pad must be reviewed")
        addendum = inventory["numbered_pad_addendum"]
        if (not isinstance(addendum, Mapping)
                or set(addendum) != {"source_ref", "quote", "pin_number", "pin_name", "kind"}
                or not isinstance(addendum["source_ref"], str)
                or addendum["source_ref"] in {proof["source_ref"], chunk["source_ref"]}
                or not isinstance(addendum["quote"], str) or not addendum["quote"].strip()
                or not isinstance(addendum["pin_number"], str)
                or not re.fullmatch(r"[1-9][0-9]?", addendum["pin_number"])
                or int(addendum["pin_number"]) > 64 or addendum["pin_number"] in parsed
                or addendum["pin_name"] not in {"EP", "EPAD"} or addendum["kind"] != "exposed_pad"):
            raise ValueError("one distinct explicitly numbered EP/EPAD table row is required")
        pad_chunk = chunks.get(addendum["source_ref"])
        pad_addendum_proof = (source_evidence({"chunk_id": pad_chunk["id"], "quote": addendum["quote"],
            "fields": ["pin_number", "pin_name"]}, knowledge) if pad_chunk else None)
        row_prefix = re.escape(addendum["pin_number"]) + r"\s+" + re.escape(addendum["pin_name"]) + r"(?:\s|$)"
        if (not pad_addendum_proof or pad_chunk.get("provenance") != "original_pdf_native"
                or pad_addendum_proof.get("source_kind") != "datasheet"
                or pad_addendum_proof.get("document_sha256") != proof["document_sha256"]
                or pad_addendum_proof.get("page") == proof["page"]
                or not re.match(row_prefix, _normal(addendum["quote"]))
                or _normal(pad_chunk["content"]).count(_normal(addendum["quote"])) != 1):
            raise ValueError("pad identity requires one literal original row on another page of the same PDF")
        number, name = addendum["pin_number"], addendum["pin_name"]
        parsed[number] = name
        numbered_pad_annotations[number] = name
        row_evidence.append({"pin_numbers": [number], "pin_name": name, "evidence": pad_addendum_proof})
        if len(parsed) > 64:
            raise ValueError("combined manufacturer inventory exceeds the bounded numeric parser")
    figure = inventory.get("figure_terminals")
    if (not isinstance(figure, list) or len(figure) != len(parsed)
            or any(not isinstance(row, Mapping) or set(row) != {"pin_number", "pin_name", "kind"}
                   or not isinstance(row["pin_number"], str)
                   or row["kind"] != ("exposed_pad" if row["pin_number"] in numbered_pad_annotations else "peripheral")
                   or not isinstance(row["pin_name"], str) for row in figure)
            or len({row["pin_number"] for row in figure}) != len(figure)
            or {row["pin_number"]: row["pin_name"] for row in figure} != parsed):
        raise ValueError("complete reviewed figure must equal every transcribed numbered terminal")
    package_claim = inventory.get("package_source")
    if not isinstance(package_claim, Mapping):
        raise ValueError("visual inventory requires separately authenticated package text")
    package_chunk = chunks.get(str(package_claim.get("source_ref")))
    package_proof = (source_evidence({"chunk_id": package_chunk["id"], "quote": package_claim.get("quote")}, knowledge)
                     if package_chunk else None)
    if (not package_proof or package_proof.get("document_sha256") != proof["document_sha256"]
            or ("reviewed_claim_scopes" in package_proof and "package" not in package_proof["reviewed_claim_scopes"])
            or _normal(facts["package"]).casefold() not in _normal(package_proof["quote"]).casefold()):
        raise ValueError("selected package must appear in authenticated original-source text")
    if package_chunk.get("provenance") == "reviewed_original_pdf_transcription":
        _authenticate_reviewed_receipt(package_chunk, package_proof, knowledge, required_scopes={"package"})
    elif package_chunk.get("provenance") != "original_pdf_native" and not (
            package_proof.get("source_kind") == "manufacturer_datasheet_revision"
            and package_chunk.get("provenance") == "original_secondary_pdf_native"
            and package_proof.get("source_selection_sha256") == (knowledge.get("source_version_selection") or {}).get("selection_sha256")):
        raise ValueError("package identity requires original text or an authenticated visual review")
    actual = facts.get("pins", [])
    if (len(actual) != len(parsed) or len(actual) != len(extraction.get("pins", []))
            or {p["pin_number"]: p["pin_name"] for p in actual} != parsed
            or type(extraction.get("pin_count")) is not int or extraction["pin_count"] != len(parsed)
            or extraction.get("pins_complete") is not True
            or any(p["evidence"][field].get("source_kind") != proof.get("source_kind")
                   or p["evidence"][field].get("document_sha256") != proof["document_sha256"]
                   for p in actual for field in ("pin_number", "pin_name"))):
        raise ValueError("candidate must equal the complete source-authenticated visual terminal table")
    if pad_addendum_proof is not None:
        pad = next(pin for pin in actual if pin["pin_number"] == inventory["numbered_pad_addendum"]["pin_number"])
        if any(pad["evidence"][field].get("source_ref") != pad_addendum_proof["source_ref"]
                or pad["evidence"][field].get("quote") != pad_addendum_proof["quote"]
                for field in ("pin_number", "pin_name")):
            raise ValueError("pad leaf identities must cite their actual numbered source row")
    if typed_diagram and any(
            pin["evidence"][field].get("source_ref") != checked["source_ref"]
            or _normal(pin["evidence"][field].get("quote", "")) != _normal(typed_rows[pin["pin_number"]])
            for pin in actual for field in ("pin_number", "pin_name")):
        raise ValueError("typed diagram leaf identities must cite their exact original reviewed row")
    if proof.get("source_kind") == "manufacturer_datasheet_revision":
        from .datasheet_revision import authenticate_revision_selection
        checked_selection = authenticate_revision_selection(knowledge, purpose="catalog_identity")
        complete_review = chunks.get(proof["source_ref"], {})
        if (chunk.get("source_quality", {}).get("reviewer") == complete_review.get("source_quality", {}).get("reviewer")):
            raise ValueError("revision diagram completeness requires an independent reviewer")
        if any(p["evidence"][field].get("source_selection_sha256") != checked_selection["selection"]["selection_sha256"]
                for p in actual for field in ("pin_number", "pin_name")):
            raise ValueError("visual terminal identity mixes revision selections")
    elif proof.get("source_kind") != "datasheet":
        raise ValueError("visual inventory requires a datasheet or explicitly selected revision")
    return {**deepcopy(dict(proof)), "derivation": {
        "method": "complete_manufacturer_terminal_inventory", "parser": (
            "reviewed_numeric_single_package_diagram_v3" if diagram else "reviewed_numeric_single_package_table_v2"),
        "manufacturer_printed_pin_count": None, "computed_terminal_count": len(parsed),
        "manufacturer_pin_numbers": sorted(parsed, key=lambda number: (int(number), number)),
        "peripheral_terminal_count": len(parsed) - len(numbered_pad_annotations),
        "numbered_exposed_pad_count": len(numbered_pad_annotations),
        ("diagram_transcription" if diagram else "table_transcription"): checked,
        ("diagram_locator" if diagram else "table_locator"): table["locator"],
        "package_evidence": package_proof, "source_rows": row_evidence,
        **({"numbered_pad_addendum": pad_addendum_proof} if pad_addendum_proof is not None else {}),
        **({"diagram_transcription_schema": TYPED_DIAGRAM_TRANSCRIPTION_SCHEMA} if typed_diagram else {}),
        "limitation": ("Every numbered peripheral and optional annotated center pad comes from two independent reviews of one original diagram. Literal indexed labels are preserved. The unique inventory determines count, not the largest number; no electrical permission is granted." if typed_diagram else "Peripheral identity comes from the original numeric diagram; one separately numbered EP/EPAD comes from its literal native row on another same-PDF page under independent union review. No source conflict is rewritten and no electrical permission is granted." if pad_addendum_proof is not None else "Count is derived from two distinct reviewed original numeric pin-diagram records; no manufacturer printed table or total is asserted. Callouts, device markings, dimensions and CAD aliases are excluded." if diagram else
            "Count is derived from a separately transcribed and visually reviewed closed table, not a printed total or package-name heuristic.")}}


def authenticate_terminal_inventory(extraction: Mapping, knowledge: Mapping, facts: Mapping,
                                    *, source_evidence: Callable) -> dict:
    """Return derived count evidence after checking the full source chain.

    The selected review transcript is a structured inventory, registered through
    ``register_pdf_page_review`` after visual inspection of the complete pin
    figure. This bounded v1 parser supports numeric, single-package pin tables;
    BGA matrices and multi-package table columns require a different parser and
    are deliberately not guessed here.
    """
    chunks = {str(row[key]): row for row in knowledge.get("evidence", [])
              for key in ("id", "original_id", "source_ref") if row.get(key) is not None}
    review = chunks.get(str(extraction.get("terminal_inventory_review")))
    if not review or review.get("provenance") != "reviewed_original_pdf_transcription":
        raise ValueError("complete terminal inventory requires an original-PDF visual review")
    proof = source_evidence({"chunk_id": review["id"], "quote": review["content"]}, knowledge)
    if not proof or "terminal_inventory_completeness" not in proof.get("reviewed_claim_scopes", []):
        raise ValueError("review does not authorize complete terminal inventory")
    _authenticate_reviewed_receipt(review, proof, knowledge, required_scopes={"terminal_inventory_completeness"})
    try:
        inventory = json.loads(review["content"])
    except (TypeError, ValueError) as exc:
        raise ValueError("review lacks a structured manufacturer inventory") from exc
    from .cross_figure_terminal_inventory import CROSS_FIGURE_SCHEMA, authenticate_cross_figure_inventory
    if isinstance(inventory, Mapping) and inventory.get("schema") == CROSS_FIGURE_SCHEMA:
        return authenticate_cross_figure_inventory(inventory, extraction, knowledge, facts,
                                                   proof, chunks, source_evidence)
    from .grouped_terminal_inventory import GROUPED_INVENTORY_SCHEMA, authenticate_grouped_inventory
    if isinstance(inventory, Mapping) and inventory.get("schema") == GROUPED_INVENTORY_SCHEMA:
        return authenticate_grouped_inventory(inventory, extraction, knowledge, facts,
                                               proof, chunks, source_evidence)
    if isinstance(inventory, Mapping) and inventory.get("schema") == VISUAL_INVENTORY_SCHEMA:
        return _visual_table_inventory(inventory, extraction, knowledge, facts, proof, chunks, source_evidence)
    if isinstance(inventory, Mapping) and inventory.get("schema") == DIAGRAM_INVENTORY_SCHEMA:
        return _visual_table_inventory(inventory, extraction, knowledge, facts, proof, chunks, source_evidence, diagram=True)
    if (not isinstance(inventory, Mapping) or inventory.get("schema") != INVENTORY_SCHEMA
            or inventory.get("supplier_part") != knowledge.get("supplier_part")
            or inventory.get("document_sha256") != proof["document_sha256"]
            or inventory.get("device_name") != facts.get("device_name")
            or inventory.get("package") != facts.get("package")
            or inventory.get("complete_figure") is not True):
        raise ValueError("review must identify the exact device/package and a complete terminal figure")

    def source(claim: Mapping) -> tuple[Mapping, dict]:
        if not isinstance(claim, Mapping):
            raise ValueError("source locator required")
        chunk = chunks.get(str(claim.get("source_ref")))
        if not chunk or chunk.get("provenance") != "original_pdf_native":
            raise ValueError("inventory table and boundaries require original PDF page text")
        checked = source_evidence({"chunk_id": chunk["id"], "quote": claim.get("quote")}, knowledge)
        if not checked or checked.get("document_sha256") != proof["document_sha256"]:
            raise ValueError("inventory source quote does not authenticate against the same PDF")
        return chunk, checked

    table = inventory.get("table") or {}
    start, start_proof = source(table.get("start"))
    last, last_proof = source(table.get("last_row"))
    following, following_proof = source(table.get("next_section"))
    first_page, last_page = start_proof["page"], last_proof["page"]
    if (not first_page <= last_page < first_page + 100
            or following_proof["page"] not in {last_page, last_page + 1}):
        raise ValueError("pin table boundaries must span consecutive original pages")
    native = {row["page_idx"] + 1: row for row in knowledge.get("evidence", [])
              if row.get("provenance") == "original_pdf_native"
              and row.get("document_sha256") == proof["document_sha256"]}
    if set(range(first_page, last_page + 1)) - set(native):
        raise ValueError("a complete pin-table page is missing")
    start_quote, last_quote = table["start"]["quote"], table["last_row"]["quote"]
    if start_quote not in start["content"] or last_quote not in last["content"]:
        raise ValueError("table boundaries must be literal, unambiguous original text")
    if start["content"].count(start_quote) != 1 or last["content"].count(last_quote) != 1:
        raise ValueError("table boundary occurs more than once")
    if following is last and last["content"].find(table["next_section"]["quote"]) <= last["content"].find(last_quote):
        raise ValueError("next section must follow the last terminal row")
    parsed, row_evidence = {}, []
    for page in range(first_page, last_page + 1):
        chunk = native[page]; content = chunk["content"]
        if page == first_page:
            content = content.split(start_quote, 1)[1]
        if page == last_page:
            if last_quote not in content:
                raise ValueError("last terminal row precedes the table header")
            content = content.split(last_quote, 1)[0] + last_quote
        for line in content.splitlines():
            match = _ROW.match(unicodedata.normalize("NFKC", line))
            if not match:
                continue
            numbers = _numbers(match[1])
            if any(number in parsed for number in numbers) or len(numbers) != len(set(numbers)):
                raise ValueError("manufacturer table repeats a physical terminal number")
            checked = source_evidence({"chunk_id": chunk["id"], "quote": line}, knowledge)
            if not checked:
                raise ValueError("manufacturer terminal row failed source authentication")
            for number in numbers:
                parsed[number] = {"pin_name": match[3], "number_annotation": match[2]}
            row_evidence.append({"pin_numbers": numbers, "pin_name": match[3],
                                 "number_annotation": match[2], "evidence": checked})
    if not parsed or len(parsed) > 2000:
        raise ValueError("source table lacks a bounded terminal inventory")
    figure = inventory.get("figure_terminals")
    if (not isinstance(figure, list) or not all(isinstance(row, Mapping) and isinstance(row.get("pin_number"), str)
            and row.get("kind") in {"peripheral", "exposed_pad"} for row in figure)):
        raise ValueError("review must enumerate each peripheral and exposed figure terminal")
    figure_numbers = [row["pin_number"] for row in figure]
    if len(figure_numbers) != len(set(figure_numbers)) or set(figure_numbers) != set(parsed):
        raise ValueError("complete reviewed figure and source table inventories differ")
    _, package_proof = source(inventory.get("package_source"))
    package = _normal(facts["package"]).replace("-", "").replace(" ", "")
    package_match = _PACKAGE.fullmatch(package)
    peripherals = {row["pin_number"] for row in figure if row["kind"] == "peripheral"}
    if (not package_match or package.casefold() not in re.sub(r"[\s-]", "", _normal(package_proof["quote"])).casefold()
            or len(peripherals) != int(package_match[1])):
        raise ValueError("package lead designation and reviewed peripheral inventory disagree")
    for row in figure:
        if row["kind"] == "exposed_pad" and not parsed[row["pin_number"]]["number_annotation"]:
            raise ValueError("additional pad needs an explicit manufacturer number annotation; unnumbered pads need typed aliases")
    actual = facts.get("pins", [])
    actual_numbers = [pin["pin_number"] for pin in actual]
    if (len(actual_numbers) != len(set(actual_numbers)) or set(actual_numbers) != set(parsed)
            or len(actual) != len(extraction.get("pins", [])) or extraction.get("pins_complete") is not True
            or type(extraction.get("pin_count")) is not int or extraction["pin_count"] != len(parsed)):
        raise ValueError("authenticated extraction must equal the complete unique manufacturer inventory")
    for pin in actual:
        names = {_normal(parsed[pin["pin_number"]]["pin_name"]), _normal(parsed[pin["pin_number"]]["number_annotation"] or "")}
        if _normal(pin["pin_name"]) not in names or any(pin["evidence"][field].get("source_kind") != "datasheet"
                for field in ("pin_number", "pin_name")):
            raise ValueError("candidate pin identity is outside its manufacturer table row")
    return {**deepcopy(proof), "derivation": {
        "method": "complete_manufacturer_terminal_inventory", "parser": "numeric_single_package_table_v1",
        "manufacturer_printed_pin_count": None, "computed_terminal_count": len(parsed),
        "manufacturer_pin_numbers": sorted(parsed, key=lambda number: (int(number), number)),
        "peripheral_terminal_count": len(peripherals),
        "numbered_exposed_pad_count": len(figure) - len(peripherals),
        "table_boundaries": {"start": start_proof, "last_row": last_proof, "next_section": following_proof},
        "package_evidence": package_proof, "source_rows": row_evidence,
        "limitation": "Count is calculated from the complete reviewed source inventory; it is not an explicitly printed manufacturer total."}}
