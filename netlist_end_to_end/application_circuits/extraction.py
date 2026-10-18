"""Propose unreviewed application fragments from bounded manufacturer PDF pages."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping
import hashlib
from importlib.resources import files
import io
import json
import math
import os
from pathlib import Path
import re
from string import Template
import time
from typing import Any
from urllib.parse import urlsplit

from agent_framework.llm import AsyncLLMClient, CallParams, NULL_OBSERVER, OpenAICompatibleProvider
from agent_framework.message import ImagePart, SystemMessage, TextPart, UserMessage
from pydantic import ValidationError

from .models import Device, Document, Fragment, canonical_digest


_MAX_PDF_BYTES = 60 * 1024 * 1024
_MAX_RAW_CHARS = 32768
_PROMPT_NAME = "application_circuit_extraction.md"
_MANUFACTURER_DOMAINS = {
    "analog devices": "analog.com", "analog devices inc": "analog.com", "adi": "analog.com",
    "nxp": "nxp.com", "nxp semiconductors": "nxp.com",
    "texas instruments": "ti.com", "texas instruments incorporated": "ti.com", "ti": "ti.com",
}


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field}: expected nonempty text of at most {maximum} characters")
    return value


def _validate_request(request: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"id", "title", "scope", "device", "document", "pdf_path", "pages"}
    if not isinstance(request, Mapping) or set(request) != fields:
        raise ValueError("Extraction request must contain exactly id/title/scope/device/document/pdf_path/pages")
    request_id = _text(request["id"], "id", 64)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", request_id) is None:
        raise ValueError("id: expected a fragment identifier")
    result = {
        "id": request_id,
        "title": _text(request["title"], "title", 256),
        "scope": _text(request["scope"], "scope", 4000),
        "device": Device.model_validate(request["device"]).model_dump(mode="json"),
        "document": Document.model_validate(request["document"]).model_dump(mode="json"),
        "pdf_path": _text(request["pdf_path"], "pdf_path", 4096),
    }
    pages = request["pages"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= 4:
        raise ValueError("pages: expected one to four one-based PDF page numbers")
    if any(type(page) is not int or page < 1 for page in pages) or len(pages) != len(set(pages)):
        raise ValueError("pages: page numbers must be unique positive integers")
    result["pages"] = list(pages)
    manufacturer = re.sub(r"[.,]", "", result["device"]["manufacturer"].casefold()).strip()
    domain = _MANUFACTURER_DOMAINS.get(manufacturer)
    url = urlsplit(result["document"]["url"])
    host = url.hostname or ""
    if (domain is None or url.scheme != "https" or url.username is not None or url.password is not None
            or url.query or url.fragment or url.port not in (None, 443)
            or not (host == domain or host.endswith(f".{domain}"))):
        raise ValueError("document.url: expected an HTTPS manufacturer URL supported by this pilot (ADI/NXP/TI)")
    return result


def _read_pdf(request: Mapping[str, Any]) -> bytes:
    path = Path(request["pdf_path"])
    if not path.is_file():
        raise ValueError("pdf_path: expected an existing regular PDF file")
    with path.open("rb") as stream:
        raw = stream.read(_MAX_PDF_BYTES + 1)
    if len(raw) > _MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the 60 MiB extraction limit")
    if not raw.startswith(b"%PDF-"):
        raise ValueError("Selected file does not have a PDF header")
    if hashlib.sha256(raw).hexdigest() != request["document"]["sha256"]:
        raise ValueError("Local PDF SHA-256 does not match the declared document")
    return raw


def _render_pages(raw: bytes, pages: list[int]) -> tuple[list[Any], list[dict[str, Any]]]:
    import pypdfium2 as pdfium
    from datasheet_automation.knowledge.native_pdf import _PDFIUM_LOCK

    parts, records = [], []
    with _PDFIUM_LOCK:
        document = pdfium.PdfDocument(raw)
        try:
            if max(pages) > len(document):
                raise ValueError("Requested page exceeds the PDF page count")
            for number in pages:
                page = document[number - 1]
                try:
                    width, height = page.get_size()
                    if not all(math.isfinite(side) and side > 0 for side in (width, height)):
                        raise ValueError("PDF page has invalid dimensions")
                    scale = min(2.0, 2200.0 / max(width, height))
                    bitmap = page.render(scale=scale)
                    try:
                        image = bitmap.to_pil()
                        try:
                            with io.BytesIO() as buffer:
                                image.save(buffer, format="PNG")
                                png = buffer.getvalue()
                        finally:
                            image.close()
                    finally:
                        bitmap.close()
                    textpage = page.get_textpage()
                    try:
                        native_text = textpage.get_text_range()
                    finally:
                        textpage.close()
                finally:
                    page.close()
                supplied_text = native_text[:20000]
                records.append({
                    "page": number, "image_sha256": hashlib.sha256(png).hexdigest(),
                    "native_text_sha256": hashlib.sha256(supplied_text.encode("utf-8")).hexdigest(),
                    "native_text_truncated": len(native_text) > len(supplied_text), "render_scale": scale,
                })
                parts.extend([
                    TextPart(f"PDF page {number} (one-based file page). Native text transcription follows; visual layout is in the image.\n{supplied_text}"),
                    ImagePart("data:image/png;base64," + base64.b64encode(png).decode("ascii"), "image/png"),
                ])
        finally:
            document.close()
    return parts, records


def _strict_json(raw: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Model JSON contains a duplicate object key")
            result[key] = value
        return result

    def nonfinite(_value: str) -> Any:
        raise ValueError("Model JSON contains a nonfinite number")

    value = json.loads(raw, object_pairs_hook=object_pairs, parse_constant=nonfinite)
    json.dumps(value, allow_nan=False)  # Also rejects exponent overflow such as 1e999.
    return value


def _candidate(raw: str, request: Mapping[str, Any]) -> Fragment:
    candidate = Fragment.model_validate(_strict_json(raw))
    if candidate.id != request["id"] or candidate.title != request["title"]:
        raise ValueError("Candidate id/title differs from the requested fragment")
    if candidate.device.model_dump(mode="json") != request["device"]:
        raise ValueError("Candidate device identity differs from the request")
    if [document.model_dump(mode="json") for document in candidate.documents] != [request["document"]]:
        raise ValueError("Candidate document identity differs from the supplied source")
    if any(evidence.page not in request["pages"] for evidence in candidate.evidence):
        raise ValueError("Candidate cites a PDF page that was not supplied")
    return candidate


async def extract_candidate(
    request: Mapping[str, Any], *, model: str = "qwen-vl-plus", timeout_s: float = 90
) -> dict[str, Any]:
    """Read selected PDF pages and propose a Fragment without authorizing its use.

    Invalid requests/source bindings raise ValueError before any model call.
    Model, transport and candidate-validation failures return bounded diagnostics.
    The document URL is caller-asserted; its domain and local byte integrity are
    checked here, but download provenance is not independently authenticated.
    """
    started = time.monotonic()
    request = _validate_request(request)
    _text(model, "model", 128)
    if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 600:
        raise ValueError("timeout_s: expected a finite number greater than zero and at most 600")
    raw = _read_pdf(request)
    parts, page_records = _render_pages(raw, request["pages"])
    context = {key: value for key, value in request.items() if key != "pdf_path"}
    template = files("netlist_end_to_end").joinpath("netlist", "prompts", _PROMPT_NAME).read_text(encoding="utf-8")
    prompt = Template(template).substitute(
        request_context=json.dumps(context, ensure_ascii=False, sort_keys=True, allow_nan=False),
        fragment_schema=json.dumps(Fragment.model_json_schema(), ensure_ascii=False, sort_keys=True, allow_nan=False),
    )
    report: dict[str, Any] = {
        "schema_version": "pcbagent.application_circuit_extraction.v1",
        "status": "extraction_failed", "candidate": None, "review_required": True,
        "scope": request["scope"], "request_id": request["id"],
        "source_sha256": request["document"]["sha256"], "source_document": request["document"],
        "request_context_sha256": canonical_digest(context),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_template_sha256": hashlib.sha256(template.encode("utf-8")).hexdigest(),
        "pages": page_records, "model_requested": model, "model_reported": None,
        "max_tokens": 5000, "max_attempts": 1, "timeout_s": timeout_s,
        "usage": None, "finish_reason": None, "raw_response": None,
        "source_qualification": "Manufacturer URL domain and local PDF digest checked; download provenance is caller-asserted.",
        "limitations": [
            "Model extraction is an unreviewed proposal; schema validity does not establish source entailment or connection correctness.",
            "This operation does not add to an accepted candidate library, instantiate a circuit, or write a database.",
        ],
    }
    api_key = (os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    base_url = os.environ.get("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").strip()
    if not api_key:
        report.update(error={"type": "credentials_missing", "message": "Configure QWEN_API_KEY or DASHSCOPE_API_KEY"}, elapsed_s=time.monotonic() - started)
        return report
    provider = OpenAICompatibleProvider(api_key=api_key, base_url=base_url, name="qwen", max_retries=0)
    client = AsyncLLMClient(provider, CallParams(model=model, temperature=0, max_tokens=5000, timeout_s=timeout_s), observer=NULL_OBSERVER)
    try:
        call_started = time.monotonic()
        response = await asyncio.wait_for(
            client.call([SystemMessage(prompt), UserMessage(parts)], max_attempts=1), timeout=timeout_s,
        )
        report.update(
            model_elapsed_s=time.monotonic() - call_started, model_reported=response.model,
            usage=response.usage.to_dict(), finish_reason=response.finish_reason.value,
            raw_response=response.content[:_MAX_RAW_CHARS],
            raw_response_truncated=len(response.content) > _MAX_RAW_CHARS,
            raw_response_sha256=hashlib.sha256(response.content.encode("utf-8")).hexdigest(),
        )
        if response.truncated or response.has_tool_calls or response.finish_reason.value != "stop":
            report["error"] = {"type": "incomplete_response", "message": "Model did not finish a plain JSON response normally"}
        else:
            try:
                candidate = _candidate(response.content, request)
            except ValidationError as error:
                report["error"] = {"type": "candidate_schema_invalid", "issues": [
                    {"location": list(issue["loc"]), "message": issue["msg"][:256]}
                    for issue in error.errors(include_input=False, include_url=False)[:20]
                ]}
            except (ValueError, TypeError, RecursionError):
                report["error"] = {"type": "candidate_binding_or_json_invalid", "message": "Response is not strict finite JSON or does not match source/identity/page bindings"}
            else:
                report.update(status="proposed_unreviewed", candidate=candidate.model_dump(mode="json"), candidate_sha256=canonical_digest(candidate))
    except asyncio.TimeoutError:
        report["error"] = {"type": "model_timeout", "message": "Single bounded model call timed out; no retry was performed"}
    except Exception as error:
        # Provider exception strings may contain endpoint details or credentials.
        report["error"] = {"type": type(error).__name__, "message": "Model call failed; provider details were intentionally omitted"}
    finally:
        try:
            await asyncio.wait_for(client.close(), timeout=5)
        except Exception:
            report["client_close_warning"] = True
    report["elapsed_s"] = time.monotonic() - started
    json.dumps(report, ensure_ascii=False, allow_nan=False)
    return report


__all__ = ["extract_candidate"]
