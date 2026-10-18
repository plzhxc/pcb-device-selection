"""Authenticate reviewed manufacturer ordering-code composition.

The complete order code is a derived identity, never a fabricated literal quote
from a family datasheet. Original PDF review receipts provide the human review;
this function checks its bounded template, source clauses and exact assembly.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import re

from .evidence import authenticate_source_quote, normalize_source_text

ORDERING_SCHEMA = "pcbagent.manufacturer_ordering_identity.v1"
ORDERING_TABLE_SCHEMA = "pcbagent.manufacturer_ordering_identity.v2"


def authenticate_ordering_identity(extraction: Mapping, knowledge: Mapping) -> dict:
    """Return device-name-only derived evidence, or reject an incomplete chain.

    V1 supports a literal family prefix followed by unique letter placeholders.
    Each placeholder has a reviewed finite set of literal choices. An omitted
    optional slot also omits its surrounding punctuation, as explicitly reviewed
    against the manufacturer's ordering legend. No suffix is dropped merely to
    make the requested model match, and nothing here authenticates a pin field.
    """
    chunks = {str(row[key]): row for row in knowledge.get("evidence", [])
              for key in ("id", "original_id", "source_ref") if row.get(key) is not None}
    review = chunks.get(str(extraction.get("ordering_identity_review")))
    if not review or review.get("provenance") != "reviewed_original_pdf_transcription":
        raise ValueError("ordering identity needs an independently reviewed original PDF")
    checked = authenticate_source_quote(knowledge, review, review.get("content"),
        fields=["ordering_code_identity"])
    if not checked or checked.get("source_kind") != "datasheet":
        raise ValueError("ordering review receipt or its exact claim scope is unauthenticated")
    try:
        proof = json.loads(review["content"])
    except (TypeError, ValueError) as exc:
        raise ValueError("ordering review must be a structured derivation") from exc
    if (not isinstance(proof, Mapping) or proof.get("schema") not in {ORDERING_SCHEMA, ORDERING_TABLE_SCHEMA}
            or proof.get("supplier_part") != knowledge.get("supplier_part")
            or proof.get("document_sha256") != checked.get("document_sha256")
            or proof.get("device_name") != extraction.get("device_name")
            or proof.get("reviewed_scope") != "device_name_only"
            or proof.get("punctuation_rule") != "omit_wrapper_with_empty_optional_slot"):
        raise ValueError("ordering review does not bind the exact part, document and device name")
    table = proof["schema"] == ORDERING_TABLE_SCHEMA
    if table and proof.get("source_format") != "single_page_native_table_columns":
        raise ValueError("table ordering requires an explicit reviewed native column layout")
    if table:
        from .terminal_inventory import _authenticate_reviewed_receipt
        _authenticate_reviewed_receipt(review, checked, knowledge, required_scopes={"ordering_code_identity"})

    def source(claim: Mapping) -> dict:
        if not isinstance(claim, Mapping):
            raise ValueError("literal ordering source locator required")
        row = chunks.get(str(claim.get("source_ref")))
        if not row or row.get("provenance") != "original_pdf_native":
            raise ValueError("ordering clauses require native text from the original PDF")
        result = authenticate_source_quote(knowledge, row, claim.get("quote"),
                                          fields=["ordering_code_identity"])
        if not result or result.get("source_kind") != "datasheet" or result.get("document_sha256") != checked["document_sha256"]:
            raise ValueError("ordering clause does not authenticate against the same original PDF")
        return result

    prefix = proof.get("family_prefix")
    template = proof.get("printed_template")
    segments = proof.get("segments")
    if (not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,63}", prefix)
            or not isinstance(template, str) or not isinstance(segments, list)
            or not 1 <= len(segments) <= 16):
        raise ValueError("bounded literal family prefix, template and slots required")
    template_evidence = source(proof.get("template_evidence"))
    if normalize_source_text(template) != normalize_source_text(template_evidence["quote"]):
        raise ValueError("printed template must be the exact original quote")
    assembled, reconstructed, seen, clauses = prefix, prefix, set(), []
    for item in segments:
        if not isinstance(item, Mapping):
            raise ValueError("ordering slot must be structured")
        symbol, value, choices = item.get("slot"), item.get("value"), item.get("choices")
        left, right = item.get("prefix"), item.get("suffix")
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z]", symbol) or symbol in seen
                or not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9]*", value)
                or not isinstance(choices, list) or not 1 <= len(choices) <= 30
                or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9]*", v) for v in choices)
                or len(set(choices)) != len(choices) or value not in choices
                or left not in {"", "-", "(", "#"} or right not in {"", ")"}
                or ((left == "(") != (right == ")"))):
            raise ValueError("unique slots, literal choices and balanced bounded wrappers required")
        clause = source(item.get("source"))
        if table:
            header = source(item.get("column_header"))
            tokens = header["quote"].split()
            if (not 2 <= len(tokens) <= 16 or tokens.count(symbol) != 1
                    or any(e.get("page") != checked.get("page") for e in (header, clause, template_evidence))
                    or "" in choices):
                raise ValueError("one same-page printed column header and explicit nonempty options required")
            for evidence in (header, clause, template_evidence):
                original = chunks[evidence["source_ref"]]["content"]
                if original.count(evidence["quote"]) != 1:
                    raise ValueError("table ordering requires unique literal native spans, including original line breaks")
            original_lines = [normalize_source_text(line) for line in chunks[header["source_ref"]]["content"].splitlines() if line.strip()]
            if normalize_source_text(header["quote"]) not in original_lines:
                raise ValueError("column header must preserve one complete original line")
            option_rows = [normalize_source_text(line) for line in clause["quote"].splitlines() if line.strip()]
            parsed = [re.fullmatch(r"([A-Za-z0-9]+)\s*:\s*(\S.*)", line) for line in option_rows]
            if (any(row is None for row in parsed) or len(parsed) != len(choices)
                    or [row[1] for row in parsed] != choices):
                raise ValueError("reviewed choices must equal every literal option-row label in order")
            clause = {**clause, "column_header_evidence": header,
                "scope_limitation": "Option labels compose the device name only; row parameters are not electrical guarantees."}
        else:
            match = re.match(rf"^{re.escape(symbol)}\s*=\s*(.+)$", normalize_source_text(clause["quote"]))
            if not match:
                raise ValueError("each option clause must define its own printed placeholder")
            option_text = match[1]
            for choice in choices:
                if choice and not re.search(rf"(?<![A-Za-z0-9]){re.escape(choice)}(?![A-Za-z0-9])", option_text):
                    raise ValueError("reviewed option is absent from its literal manufacturer clause")
        if "" in choices:
            omission = item.get("omission_quote")
            if (not isinstance(omission, str) or omission not in clause["quote"]
                    or not re.search(r"\bnone\b|\boptional\b|not necessary", omission, re.I)):
                raise ValueError("optional omission requires explicit manufacturer wording")
        reconstructed += left + symbol + right
        if value:
            assembled += left + value + right
        seen.add(symbol)
        clauses.append({"slot": symbol, "selected_value": value, "evidence": clause})
    if reconstructed != template or assembled != extraction.get("device_name"):
        raise ValueError("the reviewed complete template does not assemble the exact requested model")
    return {**checked, "extraction_method": "reviewed_manufacturer_ordering_code_derivation",
        "authorized_field": "device_name", "authorized_value": assembled,
        "derivation": {"method": "reviewed_literal_ordering_template", "schema": proof["schema"],
            "printed_template": template, "computed_device_name": assembled,
            "manufacturer_printed_complete_order_code": None,
            "template_evidence": template_evidence, "selected_options": clauses,
            "limitation": "Composed ordering identity only; no package, pin or electrical fact is inferred."}}


__all__ = ["ORDERING_SCHEMA", "ORDERING_TABLE_SCHEMA", "authenticate_ordering_identity"]
