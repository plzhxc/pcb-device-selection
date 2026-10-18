"""Exact source-bound model display aliases; no model, pin or electrical inference.

Receivers pass their independently frozen knowledge, never a certificate's
replacement source bundle. Source-asset hashes detect edits; they are not a
substitute for the approved source registry that supplied the snapshot.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re

from .evidence import authenticate_source_quote, normalize_source_text, primary_document_sha256

ALIAS_SCHEMA = "pcbagent.model_display_alias.v1"
PROJECTION_SCHEMA = "pcbagent.connection_free_device_inventory.v1"
QUALIFICATION = "independently_reviewed_model_display_alias"
PROJECTION_QUALIFICATION = "reviewed_original_cad_identity_projection"
_FIELDS = {"schema", "supplier_part", "component_type", "observed_display", "canonical_model", "package",
           "marking", "manufacturer_label", "cad_source", "manufacturer_evidence", "review"}
_INSTANCE_FIELDS = {"component_type", "datasheet_url", "designator", "device_id", "manufacturer", "model",
                    "package", "source_component"}
_CLAIMS = {"canonical_model": "device_name", "package": "package", "marking": "device_marking",
           "manufacturer_label": "manufacturer"}
_SHA = re.compile(r"[0-9a-f]{64}")


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value.strip() == value and "\x00" not in value


def _json_asset(record, part, sha, qualification):
    assets = [a for a in record.get("source_assets", []) if isinstance(a, Mapping)
              and a.get("source_sha256") == sha]
    if (not isinstance(sha, str) or not _SHA.fullmatch(sha) or len(assets) != 1):
        raise ValueError("one exact frozen identity source asset is required")
    asset = assets[0]; quality = asset.get("quality", {}); value = asset.get("record")
    if (asset.get("source_verified") is not True or asset.get("source_mime") != "application/json"
            or not isinstance(quality, Mapping) or quality.get("qualification") != qualification
            or quality.get("supplier_part") != part or not isinstance(value, Mapping)
            or _digest(value) != sha):
        raise ValueError("identity source bytes, owner or qualification differ")
    if qualification == QUALIFICATION and quality.get("reviewed_claim_scopes") != ["model_display_identity_only"]:
        raise ValueError("display alias asset is not restricted to the reviewed identity scope")
    if qualification == PROJECTION_QUALIFICATION and (quality.get("claim_scope") != "original_identity_metadata_only"
            or quality.get("reference_connections_included") is not False or not _text(quality.get("reviewer"))):
        raise ValueError("original identity projection needs its independent whitelist review")
    return value


def _check_alias(record, part, kind, display, canonical, asset):
    proof = _json_asset(record, part, asset.get("source_sha256"), QUALIFICATION)
    if (set(proof) != _FIELDS or proof.get("schema") != ALIAS_SCHEMA
            or proof.get("supplier_part") != part or proof.get("component_type") != kind
            or proof.get("observed_display") != display or proof.get("canonical_model") != canonical
            or not all(_text(proof.get(key)) for key in
                ("observed_display", "canonical_model", "package", "marking", "manufacturer_label"))):
        raise ValueError("alias must bind exact source identity, type and both original names")
    review = proof.get("review")
    if (not isinstance(review, Mapping) or set(review) != {"method", "scope", "reviewer", "claims_sha256"}
            or review["method"] != "independent_cad_and_manufacturer_identity_review"
            or review["scope"] != "model_display_identity_only" or not _text(review["reviewer"])
            or review["claims_sha256"] != _digest({k:v for k,v in proof.items() if k != "review"})):
        raise ValueError("exact display relation needs its independent identity review")
    cad = proof.get("cad_source")
    if (not isinstance(cad, Mapping) or set(cad) != {"projection_sha256", "instance_index", "original_document_sha256",
            "source_component", "manufacturer"} or type(cad["instance_index"]) is not int):
        raise ValueError("closed identity-only CAD projection locator required")
    original = _json_asset(record, part, cad["projection_sha256"], PROJECTION_QUALIFICATION)
    if (set(original) != {"schema", "case_id", "read_scope", "source_path", "source_sha256", "instances"}
            or original.get("schema") != PROJECTION_SCHEMA or original.get("source_sha256") != cad["original_document_sha256"]
            or not isinstance(original.get("source_sha256"), str) or not _SHA.fullmatch(original["source_sha256"])
            or not isinstance(original.get("instances"), list) or not 0 <= cad["instance_index"] < len(original["instances"])
            or any(not isinstance(row, Mapping) or set(row) != _INSTANCE_FIELDS for row in original["instances"])):
        raise ValueError("only the registered original identity whitelist may establish the display")
    row = original["instances"][cad["instance_index"]]
    if (row["device_id"] != part or row["component_type"] != kind or row["model"] != display
            or row["package"] != proof["package"] or row["manufacturer"] != cad["manufacturer"]
            or row["source_component"] != cad["source_component"]
            or row["datasheet_url"] != record.get("document", {}).get("pdf_url")):
        raise ValueError("original CAD identity or its exact manufacturer source link differs")
    # source_path is provenance text only; no runtime file access is permitted.
    claims = proof.get("manufacturer_evidence")
    if not isinstance(claims, Mapping) or set(claims) != set(_CLAIMS):
        raise ValueError("canonical model, package, manufacturer and printed marking all need evidence")
    primary = primary_document_sha256(record)
    for field, scope in _CLAIMS.items():
        cite = claims[field]
        if (not isinstance(cite, Mapping) or set(cite) != {"source_ref", "document_sha256", "quote"}
                or cite["document_sha256"] != primary or not isinstance(cite["quote"], str)):
            raise ValueError("alias citations must identify the exact current primary manufacturer PDF")
        value = normalize_source_text(proof[field]); quote = normalize_source_text(cite["quote"])
        if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(value) + r"(?![A-Za-z0-9_])", quote):
            raise ValueError("an exact identity value is absent from its manufacturer quotation")
        authenticated = []
        for chunk in record.get("evidence", []):
            if isinstance(chunk, Mapping) and chunk.get("source_ref") == cite["source_ref"]:
                auth = authenticate_source_quote(record, chunk, cite["quote"], fields=[scope])
                if (auth and auth.get("document_sha256") == primary and auth.get("source_kind") == "datasheet"
                        and auth.get("provenance") in {"original_pdf_native", "reviewed_original_pdf_transcription"}):
                    authenticated.append(auth)
        if len(authenticated) != 1:
            raise ValueError("manufacturer identity citation or explicit field scope is not authenticated")
    return asset["source_sha256"]


def authenticate_model_display(record: Mapping, *, supplier_part: str, component_type: str,
                               display_name: str, canonical_model: str) -> dict:
    """Return exact/authenticated_alias/unresolved without changing either name.

    A same-part source proof is specific to the observed display. There is no
    suffix stripping, marking concatenation or approximate model comparison.
    Complete catalog pins, source identity, quantity and electrical checks are
    still independently required by the existing consumers.
    """
    result = {"status": "unresolved", "supplier_part": supplier_part, "observed_display": display_name,
              "canonical_model": canonical_model, "proof_sha256": None, "diagnostics": []}
    if (not isinstance(record, Mapping) or not _text(display_name) or not _text(canonical_model)
            or not isinstance(record.get("device"), Mapping)
            or record["device"].get("supplier_part") != supplier_part
            or record.get("device", {}).get("device_name") != canonical_model):
        result["diagnostics"].append("input display and canonical source identity must be explicit")
        return result
    if display_name == canonical_model:
        result["status"] = "exact"
        return result
    if not isinstance(record.get("source_assets"), list):
        result["diagnostics"].append("frozen source_assets must be an explicit list")
        return result
    candidates = [asset for asset in record["source_assets"] if isinstance(asset, Mapping)
                  and isinstance(asset.get("record"), Mapping) and asset["record"].get("schema") == ALIAS_SCHEMA
                  and asset["record"].get("observed_display") == display_name]
    if len(candidates) != 1:
        result["diagnostics"].append("one independently registered exact display-name relation is required")
        return result
    try:
        result["proof_sha256"] = _check_alias(record, supplier_part, component_type, display_name,
                                              canonical_model, candidates[0])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        result["diagnostics"].append(str(exc))
        return result
    result["status"] = "authenticated_alias"
    return result


__all__ = ["authenticate_model_display", "ALIAS_SCHEMA", "PROJECTION_SCHEMA", "QUALIFICATION", "PROJECTION_QUALIFICATION"]
