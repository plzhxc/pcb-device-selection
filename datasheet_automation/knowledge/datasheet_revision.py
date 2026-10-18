"""Explicit, field-scoped selection of an independently reviewed PDF revision.

The original primary PDF remains the canonical supplier identity. Raw revision
assets are immutable; a separate content-addressed applicability receipt grants
bounded uses. No revision is selected by date or by being the newest SQL row.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

KIND = "manufacturer_datasheet_revision"
RECEIPT_SCHEMA = "pcbagent.manufacturer_datasheet_revision_applicability.v1"
SELECTION_SCHEMA = "pcbagent.source_version_selection.v1"
PURPOSE_FIELDS = {
    "catalog_identity": frozenset({"device_name", "package", "pin_number", "pin_name",
        "numeric_terminal_diagram", "terminal_inventory_completeness"}),
    "terminal_roles": frozenset({"semantic_role", "capabilities"}),
}
PURPOSE_FIELDS["identity_and_terminal_roles"] = frozenset().union(*PURPOSE_FIELDS.values())
IDENTITY_REVIEW_FIELDS = frozenset({"device_name", "package", "revision_label", "manufacturer"})
_HASH = re.compile(r"[0-9a-f]{64}")


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _asset(bundle: Mapping, sha: str, mime: str) -> Mapping:
    rows = [a for a in bundle.get("source_assets", []) if a.get("source_sha256") == sha]
    if (not isinstance(sha, str) or not _HASH.fullmatch(sha) or len(rows) != 1
            or rows[0].get("source_verified") is not True or rows[0].get("source_mime") != mime
            or rows[0].get("is_primary") not in (False, 0)):
        raise ValueError("revision requires a unique verified nonprimary source asset")
    return rows[0]


def _literal(value: str, text: str) -> bool:
    # Preserve suffixes and package dimensions; no fuzzy model aliases.
    value = " ".join(value.split())
    text = " ".join(text.split())
    return bool(value) and re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", text) is not None


def _literal_package(value: str, text: str) -> bool:
    # Only layout spacing around an explicit dimensional multiplication sign.
    # Family letters, dimensions and suffixes remain significant.
    def normalize(item: str) -> str:
        return re.sub(r"(?<=\d)\s*[xX×]\s*(?=\d)", "x", item)
    return _literal(normalize(value), normalize(text))


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href" and value)


def _identity_origin(bundle: Mapping, model: str, pdf_sha: str, page_sha: str,
                     page_url: str | None = None) -> tuple[Mapping, Mapping]:
    """Authenticate the existing exact manufacturer link, without selecting a revision."""
    pdf = _asset(bundle, pdf_sha, "application/pdf")
    quality = pdf.get("quality") or {}
    if (quality.get("supplier_part") != bundle.get("supplier_part")
            or quality.get("document_role") != KIND or quality.get("source_kind") != KIND
            or quality.get("model") != model):
        raise ValueError("raw revision source must retain its exact classification and model")
    html = _asset(bundle, page_sha, "text/html")
    raw_html = html.get("original_html_text")
    if (not isinstance(raw_html, str) or hashlib.sha256(raw_html.encode()).hexdigest() != page_sha
            or (page_url is not None and html.get("source_url") != page_url)
            or not _literal(model, raw_html)):
        raise ValueError("revision requires the verified exact manufacturer product page")
    links = _Links(); links.feed(raw_html)
    if (urlparse(html.get("source_url", "")).scheme != "https"
            or urlparse(pdf.get("source_url", "")).scheme != "https"
            or pdf["source_url"] not in {urljoin(html["source_url"], h) for h in links.hrefs}):
        raise ValueError("manufacturer product page does not directly link this PDF")
    return pdf, html


def read_revision_identity_pdf(service: Any, supplier_part: str, sha: str, *,
                               manufacturer_page_sha256: str) -> dict:
    """Read a stored original only for explicit identity review before selection.

    This does not register applicability, make it primary or grant catalog/role
    fields. The separate review API limits this context to four identity fields.
    """
    from .evidence import primary_document_sha256
    bundle = _metadata(service, supplier_part)
    model = (bundle.get("document") or {}).get("product_name")
    if not isinstance(model, str) or not model or sha == primary_document_sha256(bundle):
        raise ValueError("a distinct original revision and exact canonical model are required")
    asset, _ = _identity_origin(bundle, model, sha, manufacturer_page_sha256)
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute("SELECT source_bytes,source_url FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s AND source_mime='application/pdf' AND is_primary=FALSE", (supplier_part, sha))
        row = cur.fetchone()
    finally: cn.rollback(); cn.close()
    if not row or hashlib.sha256(bytes(row[0])).hexdigest() != sha or row[1] != asset["source_url"]:
        raise ValueError("identity review requires the unchanged stored original revision PDF")
    return {"source_bytes": bytes(row[0]), "source_url": row[1], "quality": {
        "source_kind": "manufacturer_revision_identity_review", "document_role": KIND,
        "qualification": "manufacturer_revision_identity_review_only", "supplier_part": supplier_part,
        "claim_scope": "explicitly_reviewed_fields", "reviewed_claim_scopes": sorted(IDENTITY_REVIEW_FIELDS),
        "revision_identity_origin_sha256": manufacturer_page_sha256}}


def _reviewed_identity_witness(bundle: Mapping, witness: Mapping, applicability: Mapping) -> dict:
    """Validate an existing scoped page review independently of applicability."""
    from .evidence import quote_in_source
    from .terminal_inventory import _authenticate_reviewed_receipt
    field = witness.get("field")
    scope = "device_name" if field == "model" else field
    ref = witness.get("review_source_ref")
    match = re.fullmatch(r"pdf:([0-9a-f]{64}):page:([1-9][0-9]*):review:([0-9a-f]{64})", str(ref))
    if (scope not in IDENTITY_REVIEW_FIELDS or not match
            or set(witness) != {"field", "document_sha256", "page", "quote", "review_source_ref"}
            or witness.get("document_sha256") != match[1] or type(witness.get("page")) is not int
            or witness["page"] != int(match[2])):
        raise ValueError("reviewed identity witness must bind one exact field and original page")
    asset = _asset(bundle, match[3], "application/json")
    record = asset.get("record")
    if (not isinstance(record, Mapping) or record.get("reviewer") == applicability.get("reviewer")
            or record.get("render_scale") != 2 or record.get("source_version_selection") is not None):
        raise ValueError("identity witness requires an independent original-page review before selection")
    if match[1] == applicability.get("revision_pdf_sha256"):
        reviewed_scopes = record.get("reviewed_claim_scopes")
        if (record.get("revision_identity_origin_sha256") != applicability.get("manufacturer_page_sha256")
                or not isinstance(reviewed_scopes, list)
                or not all(isinstance(item, str) for item in reviewed_scopes)
                or not set(reviewed_scopes) <= IDENTITY_REVIEW_FIELDS):
            raise ValueError("unselected revision review grants identity fields only")
        _identity_origin(bundle, applicability["model"], match[1], applicability["manufacturer_page_sha256"],
                         applicability["manufacturer_page_url"])
    elif match[1] != applicability.get("canonical_primary_document_sha256"):
        raise ValueError("identity review belongs to neither exact original PDF")
    quality = {key: record.get(key) for key in ("rendered_image_sha256", "transcript_sha256",
        "reviewed_claim_scopes", "reviewer", "review_method")}
    quality["review_receipt_sha256"] = match[3]
    chunk = {"provenance": "reviewed_original_pdf_transcription", "lichuang_id": bundle.get("supplier_part"),
        "content": record.get("transcript"), "document_sha256": match[1], "page_idx": int(match[2]) - 1,
        "source_ref": ref, "source_quality": quality}
    proof = {"document_sha256": match[1], "page": int(match[2]), "source_ref": ref, **quality}
    _authenticate_reviewed_receipt(chunk, proof, bundle, required_scopes={scope})
    value = applicability.get(field)
    if (not isinstance(witness.get("quote"), str) or not quote_in_source(witness["quote"], chunk["content"])
            or not isinstance(value, str) or not (
                _literal_package(value, witness["quote"]) if field == "package" else _literal(value, witness["quote"]))):
        raise ValueError("reviewed identity value is absent from the exact original-page quotation")
    return dict(record)


def _check_receipt(bundle: Mapping, receipt_sha: str) -> dict:
    from .evidence import primary_document_sha256
    asset = _asset(bundle, receipt_sha, "application/json")
    receipt = asset.get("record")
    if (not isinstance(receipt, Mapping) or _sha(receipt) != receipt_sha
            or receipt.get("schema") != RECEIPT_SCHEMA
            or receipt.get("supplier_part") != bundle.get("supplier_part")
            or receipt.get("canonical_primary_document_sha256") != primary_document_sha256(bundle)
            or receipt.get("document_role") != KIND or receipt.get("source_kind") != KIND
            or receipt.get("claim_scope") != "explicitly_reviewed_fields"
            or receipt.get("review_method") != "original_manufacturer_bytes_and_exact_identity_chain"
            or not isinstance(receipt.get("reviewer"), str) or not receipt["reviewer"].strip()):
        raise ValueError("revision applicability receipt identity/hash/primary mismatch")
    for field in ("model", "package", "revision_label", "manufacturer"):
        if not isinstance(receipt.get(field), str) or not receipt[field].strip():
            raise ValueError("revision requires exact model, package, revision and manufacturer")
    if (bundle.get("document") or {}).get("product_name") != receipt["model"]:
        raise ValueError("revision model differs from canonical supplier source identity")
    scopes = receipt.get("allowed_claim_scopes")
    if (not isinstance(scopes, list) or not scopes or scopes != sorted(set(scopes))
            or not set(scopes) <= set().union(*PURPOSE_FIELDS.values())):
        raise ValueError("revision applicability permits only explicit identity/role scopes")
    pdf = _asset(bundle, receipt.get("revision_pdf_sha256"), "application/pdf")
    quality = pdf.get("quality") or {}
    if (receipt["revision_pdf_sha256"] == primary_document_sha256(bundle)
            or quality.get("supplier_part") != bundle.get("supplier_part")
            or quality.get("document_role") != KIND or quality.get("source_kind") != KIND
            or quality.get("model") != receipt["model"]
            or pdf.get("source_url") != receipt.get("revision_pdf_url")):
        raise ValueError("raw revision source must retain its exact original classification and identity")
    html = _asset(bundle, receipt.get("manufacturer_page_sha256"), "text/html")
    raw_html = html.get("original_html_text")
    if (not isinstance(raw_html, str) or hashlib.sha256(raw_html.encode()).hexdigest() != html["source_sha256"]
            or html.get("source_url") != receipt.get("manufacturer_page_url")
            or not _literal(receipt["model"], raw_html)):
        raise ValueError("revision requires the verified exact manufacturer product page")
    links = _Links(); links.feed(raw_html)
    if (urlparse(receipt["manufacturer_page_url"]).scheme != "https"
            or urlparse(receipt["revision_pdf_url"]).scheme != "https"
            or receipt["revision_pdf_url"] not in {urljoin(receipt["manufacturer_page_url"], h) for h in links.hrefs}):
        raise ValueError("manufacturer product page does not directly link this PDF")
    # These original-page witnesses are verified from PDF bytes on registration.
    # Their immutable receipt and parent original hashes remain in the snapshot.
    for key, fields, expected_sha in (("revision_identity_support", ("model", "package", "revision_label"), receipt["revision_pdf_sha256"]),
                                      ("primary_identity_support", ("model", "package"), receipt["canonical_primary_document_sha256"])):
        support = receipt.get(key)
        if not isinstance(support, list) or not support:
            raise ValueError("revision requires original old/new identity witnesses")
        for field in fields:
            if not any(isinstance(row, Mapping) and row.get("field") == field
                    and row.get("document_sha256") == expected_sha
                    and type(row.get("page")) is int and row["page"] > 0
                    and isinstance(row.get("quote"), str)
                    and (_literal_package(receipt[field], row["quote"]) if field == "package" else _literal(receipt[field], row["quote"]))
                    for row in support):
                raise ValueError("revision model/package/date lacks an exact original-page witness")
        visual = [row for row in support if isinstance(row, Mapping) and "review_source_ref" in row]
        for witness in visual:
            _reviewed_identity_witness(bundle, witness, receipt)
        if key == "revision_identity_support" and visual and not any(
                row.get("field") == "manufacturer" for row in visual):
            raise ValueError("scanned revision identity also requires reviewed manufacturer identity")
    return dict(receipt)


def authenticate_revision_selection(bundle: Mapping, selection: Mapping | None = None,
                                    *, purpose: str | None = None, fields: Any = ()) -> dict:
    """Revalidate a frozen explicit selection; performs no I/O."""
    selection = selection if selection is not None else bundle.get("source_version_selection")
    required = {"schema", "supplier_part", "canonical_primary_document_sha256", "selected_document_sha256",
        "applicability_receipt_sha256", "source_kind", "model", "package", "revision_label", "purpose", "fields", "selection_sha256"}
    if not isinstance(selection, Mapping) or set(selection) != required:
        raise ValueError("explicit complete revision selection required")
    payload = {key: value for key, value in selection.items() if key != "selection_sha256"}
    if _sha(payload) != selection["selection_sha256"] or selection["schema"] != SELECTION_SCHEMA:
        raise ValueError("revision selection hash/schema mismatch")
    receipt = _check_receipt(bundle, selection["applicability_receipt_sha256"])
    for key, expected in {"supplier_part": receipt["supplier_part"], "canonical_primary_document_sha256": receipt["canonical_primary_document_sha256"],
            "selected_document_sha256": receipt["revision_pdf_sha256"], "source_kind": KIND,
            "model": receipt["model"], "package": receipt["package"], "revision_label": receipt["revision_label"]}.items():
        if selection[key] != expected:
            raise ValueError("revision selection differs from its exact applicability receipt")
    selected = selection["fields"]
    compatible_purpose = purpose is None or purpose == selection["purpose"] or (
        selection["purpose"] == "identity_and_terminal_roles" and purpose in {"catalog_identity", "terminal_roles"})
    if (selection["purpose"] not in PURPOSE_FIELDS or not compatible_purpose
            or not isinstance(selected, list) or not selected or selected != sorted(set(selected))
            or not set(selected) <= PURPOSE_FIELDS[selection["purpose"]]
            or not set(selected) <= set(receipt["allowed_claim_scopes"])
            or not set(fields) <= set(selected)):
        raise ValueError("revision purpose/field scope mismatch")
    return {"selection": dict(selection), "receipt": receipt}


def revision_source_quality(checked: Mapping) -> dict:
    receipt = checked["receipt"]
    return {"qualification": "manufacturer_datasheet_revision_reviewed", "supplier_part": receipt["supplier_part"],
        "source_kind": KIND, "document_role": KIND, "applicability": dict(receipt),
        "claim_scope": "explicitly_reviewed_fields", "reviewed_claim_scopes": receipt["allowed_claim_scopes"],
        "applicability_receipt_sha256": checked["selection"]["applicability_receipt_sha256"]}


def _metadata(service: Any, part: str) -> dict:
    from .source_evidence import read_source_documents
    record = service.read_device_knowledge(part, evidence_limit=1)
    return {**record, "evidence": [], "source_assets": read_source_documents(service, part)}


def select_datasheet_revision(service: Any, supplier_part: str, *, applicability_receipt_sha256: str,
                              purpose: str, fields: list[str]) -> dict:
    bundle = _metadata(service, supplier_part)
    receipt = _check_receipt(bundle, applicability_receipt_sha256)
    selected = {"schema": SELECTION_SCHEMA, "supplier_part": supplier_part,
        "canonical_primary_document_sha256": receipt["canonical_primary_document_sha256"],
        "selected_document_sha256": receipt["revision_pdf_sha256"], "source_kind": KIND,
        "applicability_receipt_sha256": applicability_receipt_sha256,
        "model": receipt["model"], "package": receipt["package"], "revision_label": receipt["revision_label"],
        "purpose": purpose, "fields": sorted(set(fields))}
    selected["selection_sha256"] = _sha(selected)
    authenticate_revision_selection(bundle, selected)
    return selected


def read_revision_pdf(service: Any, supplier_part: str, sha: str, *, source_selection: Mapping) -> dict:
    bundle = _metadata(service, supplier_part)
    checked = authenticate_revision_selection(bundle, source_selection)
    if sha != source_selection["selected_document_sha256"]:
        raise ValueError("requested PDF differs from selected revision")
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute("SELECT source_bytes,source_url FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s AND source_mime='application/pdf' AND is_primary=FALSE", (supplier_part, sha))
        row = cur.fetchone()
    finally: cn.rollback(); cn.close()
    if not row or hashlib.sha256(bytes(row[0])).hexdigest() != sha:
        raise ValueError("selected original revision bytes unavailable")
    return {"source_bytes": bytes(row[0]), "source_url": row[1], "quality": revision_source_quality(checked)}


def register_datasheet_revision(service: Any, supplier_part: str, *, source_pdf_sha256: str,
                                applicability: Mapping) -> dict:
    """Append an applicability receipt; preserve original assets and primary."""
    from .secondary_pdf import pdf_text_pages, index_secondary_pdf, SECONDARY_CHUNK_DDL
    from .evidence import quote_in_source
    from .web_source import SOURCE_DDL
    bundle = _metadata(service, supplier_part)
    raw_asset = _asset(bundle, source_pdf_sha256, "application/pdf")
    receipt = deepcopy(dict(applicability))
    receipt.update(schema=RECEIPT_SCHEMA, supplier_part=supplier_part, revision_pdf_sha256=source_pdf_sha256,
        revision_pdf_url=raw_asset["source_url"], document_role=KIND, source_kind=KIND,
        claim_scope="explicitly_reviewed_fields", review_method="original_manufacturer_bytes_and_exact_identity_chain")
    receipt["allowed_claim_scopes"] = sorted(set(receipt.get("allowed_claim_scopes", [])))
    receipt_sha = _sha(receipt)
    probe = {**bundle, "source_assets": [*bundle["source_assets"], {"source_sha256": receipt_sha,
        "source_verified": True, "source_mime": "application/json", "is_primary": False, "record": receipt}]}
    if any(a["source_sha256"] == receipt_sha for a in bundle["source_assets"]):
        probe = bundle
    _check_receipt(probe, receipt_sha)
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute("SELECT pdf_bytes,pdf_sha256 FROM datasheet_documents WHERE lichuang_id=%s", (supplier_part,))
        old = cur.fetchone()
        cur.execute("SELECT source_bytes FROM datasheet_source_documents WHERE lichuang_id=%s AND source_sha256=%s", (supplier_part, source_pdf_sha256))
        raw = cur.fetchone()
    finally: cn.rollback(); cn.close()
    if (not old or not raw or hashlib.sha256(bytes(old[0])).hexdigest() != receipt["canonical_primary_document_sha256"]
            or hashlib.sha256(bytes(raw[0])).hexdigest() != source_pdf_sha256):
        raise ValueError("original old/new PDF bytes differ from applicability")
    for key, data in (("primary_identity_support", bytes(old[0])), ("revision_identity_support", bytes(raw[0]))):
        pages = pdf_text_pages(data)
        for witness in receipt[key]:
            page = witness["page"]
            if "review_source_ref" in witness:
                review = _reviewed_identity_witness(bundle, witness, receipt)
                from .pdf_review import _render
                image, image_pdf_sha = _render(service, supplier_part, page,
                    source_pdf_sha256=witness["document_sha256"],
                    **({"revision_identity_origin_sha256": receipt["manufacturer_page_sha256"]}
                       if key == "revision_identity_support" else {}))
                if (image_pdf_sha != witness["document_sha256"]
                        or hashlib.sha256(image).hexdigest() != review["rendered_image_sha256"]):
                    raise ValueError("reviewed identity rendering differs from the original PDF page")
                continue
            if not 1 <= page <= len(pages) or not quote_in_source(witness["quote"], pages[page - 1]):
                raise ValueError("revision identity witness absent from original PDF page")
    cn = service._mysql("datasheet_assets")
    try:
        cur = cn.cursor(); cur.execute(SOURCE_DDL); cur.execute(SECONDARY_CHUNK_DDL)
        cur.execute("INSERT IGNORE INTO datasheet_source_documents (lichuang_id,source_sha256,source_url,source_mime,source_bytes,is_primary,quality_json) VALUES (%s,%s,%s,'application/json',%s,FALSE,%s)",
            (supplier_part, receipt_sha, f"datasheet_revision_applicability:{receipt_sha}", _encoded(receipt),
             json.dumps({"source_kind": "datasheet_revision_applicability", "revision_pdf_sha256": source_pdf_sha256})))
        cn.commit()
    finally: cn.close()
    selected = select_datasheet_revision(service, supplier_part, applicability_receipt_sha256=receipt_sha,
        purpose="catalog_identity", fields=sorted(PURPOSE_FIELDS["catalog_identity"] & set(receipt["allowed_claim_scopes"])))
    indexed = index_secondary_pdf(service, supplier_part, source_pdf_sha256, source_selection=selected)
    return {"supplier_part": supplier_part, "source_pdf_sha256": source_pdf_sha256,
        "canonical_primary_document_sha256": receipt["canonical_primary_document_sha256"],
        "applicability_receipt_sha256": receipt_sha, "source_kind": KIND,
        "applicability": receipt, "index": indexed}


def read_selected_source_evidence(service: Any, supplier_part: str, *, source_selection: Mapping,
                                  pages: list[int] | None = None, query: str | None = None,
                                  limit: int = 200) -> dict:
    from .secondary_pdf import pdf_text_pages
    from .pdf_review import read_pdf_page_reviews
    if type(limit) is not int or not 1 <= limit <= 1000 or (pages is not None and (
            not isinstance(pages, list) or not pages or any(type(p) is not int or p < 1 for p in pages))):
        raise ValueError("bounded selected PDF pages/limit required")
    bundle = _metadata(service, supplier_part)
    checked = authenticate_revision_selection(bundle, source_selection)
    sha = source_selection["selected_document_sha256"]
    source = read_revision_pdf(service, supplier_part, sha, source_selection=source_selection)
    texts = pdf_text_pages(source["source_bytes"])
    if pages is not None and max(pages) > len(texts):
        raise ValueError("selected page exceeds original revision")
    bundle.update(source_version_selection=dict(source_selection), selected_source_document={
        "pdf_sha256": sha, "pdf_url": source["source_url"], "product_name": source_selection["model"],
        "package": source_selection["package"], "revision_label": source_selection["revision_label"]})
    rows = []
    quality = revision_source_quality(checked)
    for index, text in enumerate(texts):
        if not text or (pages is not None and index + 1 not in pages): continue
        ref = f"pdf:{sha}:page:{index + 1}"
        rows.append({"id": ref, "source_ref": ref, "lichuang_id": supplier_part, "document_sha256": sha,
            "source_url": source["source_url"], "page_idx": index, "locator": f"page:{index + 1}",
            "content": text, "content_type": "text", "title": "Selected manufacturer datasheet revision",
            "source_kind": KIND, "provenance": "original_secondary_pdf_native", "source_quality": quality})
    for review in read_pdf_page_reviews(service, supplier_part, sha):
        if pages is not None and review["page"] not in pages: continue
        if not set(review["reviewed_claim_scopes"]) <= set(checked["receipt"]["allowed_claim_scopes"]): continue
        ref = f"pdf:{sha}:page:{review['page']}:review:{review['receipt_sha256']}"
        rows.append({"id": f"pdf_review:{review['receipt_sha256']}", "source_ref": ref,
            "lichuang_id": supplier_part, "document_sha256": sha, "source_url": source["source_url"],
            "page_idx": review["page"] - 1, "locator": f"page:{review['page']}",
            "content": review["transcript"], "content_type": "text", "title": "Reviewed selected manufacturer revision",
            "source_kind": KIND, "provenance": "reviewed_original_pdf_transcription",
            "source_quality": {**quality, "review_receipt_sha256": review["receipt_sha256"],
                **{key: review[key] for key in ("rendered_image_sha256", "transcript_sha256", "reviewed_claim_scopes", "reviewer", "review_method")}}})
    if query:
        terms = query.lower().split()
        rows.sort(key=lambda row: -sum(row["content"].lower().count(t) for t in terms))
    # Explicit page requests are complete, including every scoped review on the
    # requested pages. A nominal native-page limit cannot discard those reviews.
    selected_rows = rows if pages is not None else rows[:limit]
    bundle.update(evidence=selected_rows, native_pdf_page_count=len(texts), native_pdf_pages_scanned=len(texts),
        native_pdf_evidence_total_before_limit=len(rows), native_pdf_evidence_truncated=len(selected_rows) < len(rows),
        symbol_mappings=[], source_scope="explicit_manufacturer_datasheet_revision")
    return bundle


def catalog_revision_authenticator(knowledge: Mapping):
    """Bind conservative CatalogStore writes to the frozen extraction sources."""
    frozen = deepcopy(dict(knowledge))
    def authenticate(part: str, evidence: Mapping, *, entity: str, field: str,
                     value: Any, catalog_pin_number: str | None = None) -> dict:
        from .evidence import authenticate_source_quote
        from .pin_extraction import _source_evidence, _quoted_identity
        authenticate_revision_selection(frozen, purpose="catalog_identity")
        if part != frozen.get("supplier_part") or field not in {"device_name", "pin_number", "pin_name", "pin_count"}:
            raise ValueError("revision catalog fact exceeds identity scope")
        rows = [row for row in frozen.get("evidence", []) if row.get("source_ref") == evidence.get("source_ref")]
        if len(rows) != 1:
            raise ValueError("revision catalog source is missing or ambiguous")
        if field == "pin_count":
            from .terminal_inventory import authenticate_terminal_inventory
            context = evidence.get("revision_inventory_context") or {}
            checked = authenticate_terminal_inventory(context.get("extraction", {}), frozen,
                context.get("facts", {}), source_evidence=_source_evidence)
            if (checked.get("derivation") != evidence.get("derivation")
                    or checked["derivation"].get("computed_terminal_count") != value
                    or entity != "device"):
                raise ValueError("revision count derivation changed")
        else:
            checked = authenticate_source_quote(frozen, rows[0], evidence.get("quote"), fields=[field])
            if checked is None or not _quoted_identity(field, value, checked["quote"]):
                raise ValueError("revision catalog identity not present in scoped original quote")
            if entity == "pin":
                if field == "pin_number" and value != catalog_pin_number:
                    raise ValueError("revision pin number differs from target")
                if not _literal(str(catalog_pin_number), checked["quote"]):
                    raise ValueError("revision pin label quote does not identify its target pin")
                if not revision_pin_identity(frozen, evidence, catalog_pin_number, field, value):
                    raise ValueError("revision numeric diagram does not bind this number and label")
        if any(checked.get(k) != evidence.get(k) for k in ("source_kind", "source_ref", "document_sha256", "source_selection_sha256")):
            raise ValueError("revision catalog citation selection changed")
        return {"status": "verified", "evidence": {**deepcopy(dict(evidence)),
            "authorized_field": field, "authorized_value": value, "catalog_pin_number": catalog_pin_number}}
    return authenticate


def revision_pin_identity(knowledge: Mapping, evidence: Mapping, pin: str, field: str, value: Any) -> bool:
    """This narrow consumer accepts individually reviewed numeric diagram rows.

    Co-occurrence in a whole figure is insufficient when labels repeat. Other
    revision inventory forms need their own existing typed source adapter.
    """
    from .evidence import authenticate_source_quote, quote_in_source
    rows = [row for row in knowledge.get("evidence", []) if row.get("source_ref") == evidence.get("source_ref")]
    if len(rows) != 1 or rows[0].get("provenance") != "reviewed_original_pdf_transcription":
        return False
    if authenticate_source_quote(knowledge, rows[0], evidence.get("quote"), fields=[field]) is None:
        return False
    try:
        transcript = json.loads(rows[0]["content"])
    except (TypeError, ValueError):
        return False
    if transcript.get("schema") != "pcbagent.manufacturer_numeric_terminal_diagram.v1":
        return False
    terminals = [row for row in transcript.get("terminals", []) if row.get("pin_number") == pin]
    if len(terminals) != 1 or terminals[0].get(field) != value:
        return False
    literal = _encoded(terminals[0]).decode()
    return quote_in_source(literal, evidence["quote"]) or quote_in_source(evidence["quote"], literal)
