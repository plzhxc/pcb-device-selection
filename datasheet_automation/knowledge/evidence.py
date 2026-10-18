"""Conservative normalization for quotes copied from page-layout text."""
from __future__ import annotations

import re
import hashlib
import unicodedata
from collections.abc import Mapping


def normalize_source_text(value: str) -> str:
    """Normalize Unicode/layout spaces without joining separate numeric tokens.

    PDF line wraps can put spaces inside Chinese sentences, including between
    Chinese text and pin names. Those layout gaps have no lexical significance.
    ASCII-to-ASCII spacing is retained: `1 0 V` cannot authenticate `10 V`.
    Case, punctuation, signs, digits and unit letters otherwise remain distinct.
    """
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fffA-Za-z0-9])", "", text)
    return re.sub(r"(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])", "", text)


def quote_in_source(quote: str, source: str) -> bool:
    normalized = normalize_source_text(quote)
    return bool(normalized) and normalized in normalize_source_text(source)


def primary_document_sha256(bundle: Mapping) -> str | None:
    document = bundle.get("document") or {}
    quality = bundle.get("stored_source_quality") or bundle.get("source_quality") or {}
    value = document.get("pdf_sha256") or document.get("document_sha256") or quality.get("document_sha256")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def primary_source_registry(bundle: Mapping) -> dict[str, dict]:
    """Index a service-authenticated snapshot without promoting arbitrary RAG text.

    The caller must obtain this bundle from the original-source reader. Registry
    entries identify separate original assets; an image is never assigned its
    enclosing HTML page's digest or a fictional PDF page number.
    """
    part = bundle.get("supplier_part")
    document = bundle.get("document") or {}
    qualities = [bundle.get("stored_source_quality"), bundle.get("source_quality")]
    qualities += [quality.get("acquisition") for quality in list(qualities) if isinstance(quality, Mapping)]
    related_primary = any(isinstance(quality, Mapping) and (
        str(quality.get("original_source_kind", "")).startswith("related_")
        or quality.get("source_document_type") == "compliance_certificate") for quality in qualities)
    assets = {a.get("source_sha256"): a for a in bundle.get("source_assets", []) if a.get("source_verified") is True}
    result = {}
    for row in bundle.get("evidence", []):
        if row.get("lichuang_id") != part:
            continue
        sha, provenance = row.get("document_sha256"), row.get("provenance")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        selected_revision = bundle.get("source_version_selection")
        if selected_revision is not None and (not isinstance(selected_revision, Mapping)
                or sha != selected_revision.get("selected_document_sha256")):
            continue
        if sha == document.get("pdf_sha256") and related_primary:
            # Authentic PDF bytes or a visual transcription cannot promote a
            # compliance certificate/related note into exact device pin facts.
            # Independently authenticated secondary sources retain their scope.
            continue
        index, locator = row.get("page_idx"), row.get("locator")
        kind, scope = row.get("source_kind"), "documented_device_facts"
        secondary_asset = assets.get(sha, {})
        secondary_quality = secondary_asset.get("quality") or {}
        revision_checked = None
        if (secondary_quality.get("document_role") == "manufacturer_datasheet_revision"
                or secondary_quality.get("source_kind") == "manufacturer_datasheet_revision"
                or row.get("source_kind") == "manufacturer_datasheet_revision"):
            from .datasheet_revision import authenticate_revision_selection, revision_source_quality
            try:
                revision_checked = authenticate_revision_selection(bundle)
            except (ValueError, TypeError, KeyError):
                continue
            if sha != revision_checked["selection"]["selected_document_sha256"]:
                continue
            secondary_quality = revision_source_quality(revision_checked)
        secondary_scopes = secondary_quality.get("reviewed_claim_scopes")
        if "connection_policy_reviews" in secondary_quality and (
                type(secondary_asset.get("is_primary")) not in {bool, int} or secondary_asset.get("is_primary") != 0
                or not isinstance(secondary_scopes, list)
                or sum(a.get("source_sha256") == sha for a in bundle.get("source_assets", [])) != 1
                or set(secondary_scopes) & {"allow_nc", "required_connection"}):
            continue
        secondary_pdf = (secondary_asset.get("source_mime") == "application/pdf"
                         and secondary_quality.get("supplier_part") == part
                         and bool(secondary_quality.get("document_role"))
                         and isinstance(secondary_quality.get("applicability"), Mapping)
                         and bool(secondary_quality["applicability"])
                         and secondary_quality.get("claim_scope") == "explicitly_reviewed_fields"
                         and isinstance(secondary_scopes, list) and bool(secondary_scopes)
                         and all(isinstance(field, str) and field for field in secondary_scopes))
        secondary_kind = "manufacturer_application_note"
        if revision_checked is not None:
            secondary_kind = "manufacturer_datasheet_revision"
        elif (secondary_quality.get("document_role") == "manufacturer_ordering_notice"
                or secondary_quality.get("source_kind") == "manufacturer_ordering_notice"):
            if (not secondary_pdf or secondary_asset.get("is_primary") is True
                    or secondary_quality.get("qualification") != "manufacturer_secondary_pdf"
                    or secondary_quality.get("document_role") != "manufacturer_ordering_notice"
                    or secondary_quality.get("source_kind") != "manufacturer_ordering_notice"
                    or secondary_scopes != ["device_name"]):
                continue
            secondary_kind = "manufacturer_ordering_notice"
        elif secondary_pdf and (secondary_quality.get("document_role") not in {
                "application_note", "evaluation_board_manual", "hardware_design_guide"}
                or secondary_quality.get("source_kind", "manufacturer_application_note") != "manufacturer_application_note"):
            continue
        reviewed_fields = None
        intrinsic_policies = []
        if provenance == "original_pdf_native":
            if sha != document.get("pdf_sha256") or type(index) is not int or index < 0:
                continue
            kind, ref, locator = "datasheet", f"pdf:{sha}:page:{index + 1}", f"page:{index + 1}"
        elif provenance == "original_secondary_pdf_native":
            if not secondary_pdf or type(index) is not int or index < 0:
                continue
            kind, ref, locator = secondary_kind, f"pdf:{sha}:page:{index + 1}", f"page:{index + 1}"
            scope, reviewed_fields = "explicitly_reviewed_fields", secondary_scopes
        elif provenance == "reviewed_original_pdf_transcription":
            quality = row.get("source_quality") or {}
            receipt_sha = quality.get("review_receipt_sha256")
            receipt = assets.get(receipt_sha, {})
            rendered = assets.get(quality.get("rendered_image_sha256"), {})
            if ((sha != document.get("pdf_sha256") and not secondary_pdf) or type(index) is not int or index < 0
                    or receipt.get("source_mime") != "application/json" or rendered.get("source_mime") != "image/png"
                    or (receipt.get("quality") or {}).get("pdf_sha256") != sha
                    or (receipt.get("quality") or {}).get("page") != index + 1
                    or not quality.get("reviewer") or quality.get("review_method") != "explicit_visual_inspection_of_original_pdf_render"
                    or hashlib.sha256(str(row.get("content", "")).encode()).hexdigest() != quality.get("transcript_sha256")
                    or not isinstance(quality.get("reviewed_claim_scopes"), list) or not quality["reviewed_claim_scopes"]):
                continue
            if sha != document.get("pdf_sha256"):
                allowed = set(secondary_scopes)
                if "connection_policy_reviews" in secondary_quality:
                    from .secondary_pdf import secondary_review_scopes
                    try:
                        allowed, policies = secondary_review_scopes(secondary_quality, bundle, sha, index + 1)
                        intrinsic_policies = [p for p in policies if set(p["fields"]) & set(quality["reviewed_claim_scopes"])
                            and quote_in_source(p["quote"], str(row.get("content", "")))]
                        requested = set(quality["reviewed_claim_scopes"]) & {"allow_nc", "required_connection"}
                        if not requested <= {f for p in intrinsic_policies for f in p["fields"]}:
                            continue
                    except (TypeError, ValueError, KeyError):
                        continue
                if not set(quality["reviewed_claim_scopes"]) <= allowed:
                    continue
            kind = "datasheet" if sha == document.get("pdf_sha256") else secondary_kind
            ref, locator, scope = f"pdf:{sha}:page:{index + 1}:review:{receipt_sha}", f"page:{index + 1}", "explicitly_reviewed_fields"
            reviewed_fields = quality["reviewed_claim_scopes"]
            if revision_checked is not None or intrinsic_policies:
                from .terminal_inventory import _authenticate_reviewed_receipt
                try:
                    _authenticate_reviewed_receipt(row, {"document_sha256": sha, "page": index + 1,
                        "source_ref": ref, "review_receipt_sha256": receipt_sha,
                        "rendered_image_sha256": quality["rendered_image_sha256"],
                        "reviewed_claim_scopes": quality["reviewed_claim_scopes"]}, bundle,
                        required_scopes=set(quality["reviewed_claim_scopes"]))
                except (ValueError, TypeError, KeyError):
                    continue
        elif provenance == "original_manufacturer_html":
            asset = assets.get(sha, {})
            if not asset.get("is_primary") or asset.get("source_mime") != "text/html" or locator != "visible_text":
                continue
            kind, ref = "manufacturer_page", f"manufacturer_page:{sha}:visible_text"
        elif provenance == "reviewed_manufacturer_image_fields":
            from .manufacturer_image_review import validate_image_review
            quality = row.get("source_quality") or {}
            receipt_sha = quality.get("review_receipt_sha256")
            receipt = assets.get(receipt_sha, {}).get("record")
            try:
                checked = validate_image_review(bundle, receipt, receipt_sha)
            except (ValueError, TypeError, KeyError):
                continue
            if any(row.get(key) != checked.get(key) for key in ("content", "source_ref", "document_sha256", "locator", "source_quality", "source_url")):
                continue
            kind, ref, locator = "manufacturer_image", checked["source_ref"], checked["locator"]
            scope, reviewed_fields = "explicitly_reviewed_fields", receipt["reviewed_claim_scopes"]
        elif provenance == "reviewed_original_manufacturer_image":
            asset = assets.get(sha, {})
            quality = row.get("source_quality") or {}
            if not str(asset.get("source_mime", "")).startswith("image/") or quality.get("reviewed_image_sha256") != sha or not quality.get("review_method") or not locator:
                continue
            scope = quality.get("claim_scope")
            if scope != "pin_number_and_label_only":
                continue
            kind, ref = "manufacturer_image", f"manufacturer_image:{sha}:{locator}"
        else:
            continue
        if row.get("source_ref") != ref or (row.get("source_kind") and row["source_kind"] != kind):
            continue
        if revision_checked is not None:
            if row.get("source_kind") != "manufacturer_datasheet_revision" or reviewed_fields is None:
                continue
            reviewed_fields = sorted(set(reviewed_fields) & set(revision_checked["selection"]["fields"]))
            if not reviewed_fields:
                continue
        result[ref] = {"supplier_part": part, "source_kind": kind, "source_ref": ref,
                       "document_sha256": sha, "provenance": provenance, "locator": locator,
                       "claim_scope": scope}
        if scope == "explicitly_reviewed_fields":
            result[ref]["reviewed_claim_scopes"] = list(reviewed_fields)
            if provenance == "reviewed_original_pdf_transcription":
                result[ref].update(review_receipt_sha256=receipt_sha, rendered_image_sha256=quality["rendered_image_sha256"])
        if provenance == "reviewed_manufacturer_image_fields":
            result[ref].update(review_receipt_sha256=receipt_sha,
                parent_html_sha256=receipt["parent_html_sha256"],
                transcript_sha256=receipt["transcript_sha256"],
                model=receipt["model"], package_scope=receipt["package_scope"],
                catalog_identity_sha256=receipt["catalog_identity_sha256"])
        if kind in {"manufacturer_application_note", "manufacturer_ordering_notice", "manufacturer_datasheet_revision"}:
            result[ref].update(document_role=secondary_quality["document_role"], applicability=dict(secondary_quality["applicability"]))
        if intrinsic_policies:
            result[ref]["intrinsic_connection_policies"] = intrinsic_policies
        if revision_checked is not None:
            selection = revision_checked["selection"]
            result[ref].update(source_selection_sha256=selection["selection_sha256"],
                canonical_primary_document_sha256=selection["canonical_primary_document_sha256"],
                applicability_receipt_sha256=selection["applicability_receipt_sha256"],
                revision_label=selection["revision_label"], model=selection["model"], package=selection["package"])
    return result


def authenticate_source_quote(bundle: Mapping, chunk: Mapping, quote: str, *, fields: list[str]) -> dict | None:
    """Authenticate source attribution and permitted claim scope, not entailment."""
    source = primary_source_registry(bundle).get(chunk.get("source_ref"))
    originals = [row for row in bundle.get("evidence", []) if row.get("source_ref") == chunk.get("source_ref")]
    if (source is None or not isinstance(quote, str) or len(normalize_source_text(quote)) < 3
            or not originals or any(row.get("content") != chunk.get("content") for row in originals)
            or chunk.get("provenance") != source["provenance"]
            or chunk.get("lichuang_id") != source["supplier_part"]
            or chunk.get("document_sha256") != source["document_sha256"]
            or not quote_in_source(quote, str(chunk.get("content", "")))):
        return None
    if source["claim_scope"] == "pin_number_and_label_only" and not set(fields) <= {"pin_number", "pin_name"}:
        return None
    if source["claim_scope"] == "explicitly_reviewed_fields" and not set(fields) <= set(source["reviewed_claim_scopes"]):
        return None
    policies = source.get("intrinsic_connection_policies", [])
    policy_fields = set(fields) & {"allow_nc", "required_connection"}
    if policies and policy_fields and not any(policy_fields <= set(p["fields"])
            and normalize_source_text(quote) == normalize_source_text(p["quote"]) for p in policies):
        return None
    result = {k: v for k, v in source.items() if k != "supplier_part"}
    result.update(quote=quote, source_url=chunk.get("source_url") or (bundle.get("document") or {}).get("pdf_url"),
                  source_authentication_version=2 if source["provenance"] == "original_pdf_native" else 3)
    if source["source_kind"] in {"datasheet", "manufacturer_application_note", "manufacturer_ordering_notice", "manufacturer_datasheet_revision"}:
        result["page"] = chunk["page_idx"] + 1
    if source["source_kind"] == "manufacturer_datasheet_revision" and source["provenance"] == "reviewed_original_pdf_transcription":
        result["transcript_sha256"] = chunk["source_quality"]["transcript_sha256"]
    return result


__all__ = ["normalize_source_text", "quote_in_source", "primary_document_sha256",
           "primary_source_registry", "authenticate_source_quote"]
