"""LcscClient: orchestrate LCSC search / download / card fields over two seams.

The Fetcher owns HTTP; the ChallengeSolver list owns WAF cookie computation. This
client only sequences them: fetch, detect a challenge, ask the solvers for
cookies, hand them to the fetcher, refetch. Parsing and ranking are delegated to
the pure ``records`` module.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

from datasheet_automation.core.app_logging import get_logger
from datasheet_automation.core.io import ensure_dir, safe_filename
from datasheet_automation.core.models import LcscProduct
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from . import records
from .challenge import ChallengeSolver, NodeSolver, NodeUnavailableError, Rc4Solver
from .pricing import NodePriceSigner, PriceSigner
from .transport import Fetcher, RequestsFetcher


log = get_logger()


def _as_float(value: Any) -> Optional[float]:
    # None stays None; 0 is a legitimate value and must survive.
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _weight_grams(kg: Any) -> str:
    """Format a kilogram weight as grams with a unit via Decimal (no float mul).

    Returns '' only when absent/unparsable -- a real 0 renders as '0 g'.
    """
    if kg is None or str(kg).strip() == "":
        return ""
    try:
        grams = Decimal(str(kg)) * Decimal("1000")  # exact scale, no float error
    except (InvalidOperation, ValueError):
        return ""
    # Drop trailing zeros without scientific notation (0.135 g, 12 g, 0 g).
    grams = grams.normalize()
    text = format(grams, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} g"


def _coupons(raw: Any) -> list[dict[str, Any]]:
    coupons: list[dict[str, Any]] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        # Coupon validity: field name isn't confirmed in the sample, so probe the
        # common LCSC end-time keys; None when absent (never fabricate an expiry).
        valid_until = ""
        for key in ("useEndTime", "validEndTime", "couponEndTime", "endTime", "expireTime"):
            v = row.get(key)
            if v:
                valid_until = str(v).strip()
                break
        coupons.append({
            "type": str(row.get("couponType") or "").strip(),
            "amount": _as_float(row.get("couponAmount")),
            "threshold": _as_float(row.get("couponThresholdMoney")),
            "free_express": bool(row.get("isFreeExpress")),
            "valid_until": valid_until,
        })
    return coupons


LCSC_SEARCH_URL = "https://so.szlcsc.com/global.html?k={query}"
LCSC_WMSC_DETAIL_URL = "https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}"


class LcscClient:
    """Fetch product, parameter and datasheet metadata from LCSC SSR pages."""

    def __init__(
        self,
        fetcher: Fetcher | None = None,
        solvers: list[ChallengeSolver] | None = None,
        price_signer: PriceSigner | None = None,
    ):
        self.fetcher = fetcher or RequestsFetcher()
        # Node first (robust to obfuscation changes); pure-Python RC4 as fallback.
        self.solvers = solvers if solvers is not None else [NodeSolver(), Rc4Solver()]
        self.price_signer = price_signer or NodePriceSigner()

    def search(self, query: str, limit: int = 10) -> list[LcscProduct]:
        query = str(query or "").strip()
        if not query:
            raise ValueError("query is empty")
        html = self._get_search_html(query)
        data = records.extract_next_data(html)
        result = (
            data.get("props", {})
            .get("pageProps", {})
            .get("soData", {})
            .get("searchResult", {})
        )
        rows = result.get("productRecordList") or []
        products = [records.record_to_product(row, query) for row in rows]
        products = [p for p in products if p.product_code or p.product_model]
        products.sort(key=lambda p: records.candidate_score(query, p), reverse=True)
        return products[:limit]

    def download_datasheet(
        self,
        product: LcscProduct,
        output_dir: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        file = product.first_datasheet()
        if file is None:
            raise RuntimeError(f"{product.product_code} has no datasheet PDF in LCSC metadata")

        output_dir = ensure_dir(output_dir)
        filename = (
            f"{safe_filename(product.product_code)}_"
            f"{safe_filename(product.product_model)}.pdf"
        )
        pdf_path = output_dir / filename
        if pdf_path.exists() and not overwrite:
            return pdf_path

        response = self.fetcher.get(file.url, stream=True)
        content_type = response.headers.get("content-type", "")
        if response.status_code != 200:
            raise RuntimeError(f"datasheet download failed: {response.status_code} {file.url}")
        if "pdf" not in content_type.lower() and not file.url.lower().endswith(".pdf"):
            raise RuntimeError(f"datasheet URL did not return a PDF: {content_type} {file.url}")

        with open(pdf_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    fh.write(chunk)
        return pdf_path

    def fetch_pdf_desc(
        self,
        product: LcscProduct,
        *,
        warnings: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Fetch the Chinese product card fields from the LCSC detail page.

        Returns ``{}`` and records a warning when the detail page cannot be
        reached or solved, so callers can fall back to the wmsc API.
        """

        product_id = str(product.product_id or "").strip()
        if not product_id:
            self._note(warnings, "detail_skipped: product has no product_id")
            return {}
        url = records.LCSC_ITEM_URL.format(product_id=product_id)
        try:
            data = self._get_detail_next_data(url)
        except NodeUnavailableError as exc:
            log.warning("card fields: detail page needs Node WAF solver, degrading to wmsc | %s", exc)
            self._note(warnings, f"detail_waf_node_missing: {exc}")
            return {}
        except Exception as exc:  # network / WAF / parse -> degrade to wmsc
            log.warning("card fields: detail page unavailable, degrading to wmsc | %s", exc)
            self._note(warnings, f"detail_unavailable: {exc}")
            return {}

        page = data.get("props", {}).get("pageProps", {})
        web = page.get("webData") or {}
        vo = (page.get("productPdfDesc") or {}).get("productPDFDescVO") or {}
        catalog = web.get("currentCatalog") or {}
        brand_vo = web.get("brandVO") or {}
        record = web.get("productRecord") or {}
        params = {
            str(row.get("parameterName") or "").strip(): str(row.get("parameterValue") or "").strip()
            for row in (web.get("paramList") or [])
            if str(row.get("parameterName") or "").strip()
        }
        weight_kg = _as_float(record.get("productWeight"))  # source unit is kg
        return {
            "source": "szlcsc-detail",
            "lang": "zh",
            "fetched_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "currency": "CNY",
            "overview": str(vo.get("productIntro") or "").strip(),
            "features": str(vo.get("productFeatures") or "").strip(),
            "applications": str(vo.get("pdfApplicationAreas") or "").strip(),
            "category": str(catalog.get("catalogName") or "").strip(),
            "brand": str(brand_vo.get("brandName") or "").strip(),
            "packaging": str(record.get("productArrange") or "").strip(),
            # productWeight is kilograms; expose grams (Decimal, no float mul) plus
            # the raw value + source unit so a future unit change is detectable.
            "gross_weight": _weight_grams(record.get("productWeight")),
            "gross_weight_raw": weight_kg,
            "gross_weight_unit": "kg" if weight_kg is not None else "",
            # Scalar RMB price + reel context (present even when the ladder is not).
            # None stays None; 0 is legitimate and preserved.
            "price": _as_float(page.get("price")),
            "stock": _as_int(record.get("stockNumber")),
            "reel_price": _as_float(record.get("encaptionPrice")),
            "reel_qty": _as_int(record.get("minWholeNumber")),
            "coupons": _coupons(web.get("productCouponList")),
            "params": params,
        }

    def fetch_price_ladder(
        self,
        product: LcscProduct,
        *,
        warnings: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        """Fetch the full RMB price ladder via the signed price API.

        Returns the normalized tiers, or ``[]`` (with a warning) when the signed
        POST is blocked or Node is unavailable -- the card still ships without a
        ladder rather than failing collection.
        """

        product_id = str(product.product_id or "").strip()
        if not product_id:
            return []
        payload = self.price_signer.fetch_ladder(product_id)
        if payload is None:
            self._note(warnings, "price_ladder_unavailable: signed price API blocked or node missing")
            return []
        tiers = records.parse_price_tiers(payload)
        if not tiers:
            self._note(warnings, "price_ladder_empty: signed payload had no tiers")
        return tiers

    def fetch_wmsc(
        self,
        code: str,
        *,
        warnings: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Fallback: fetch language-neutral / English fields from the wmsc API."""

        code = str(code or "").strip()
        if not code:
            return {}
        url = LCSC_WMSC_DETAIL_URL.format(code=quote(code))
        try:
            response = self.fetcher.get(url)
            result = (response.json() or {}).get("result") or {}
        except Exception as exc:
            log.warning("card fields: wmsc API unavailable | %s", exc)
            self._note(warnings, f"wmsc_unavailable: {exc}")
            return {}
        if not result:
            self._note(warnings, "wmsc_empty_result")
            return {}
        params = {
            str(row.get("paramNameEn") or row.get("paramName") or "").strip():
                str(row.get("paramValueEn") or row.get("paramValue") or "").strip()
            for row in (result.get("paramVOList") or [])
            if str(row.get("paramNameEn") or row.get("paramName") or "").strip()
        }
        self._note(warnings, "product_card_features_missing: wmsc fallback lacks Chinese features/applications")
        return {
            "source": "wmsc-api",
            "lang": "en",
            "overview": str(result.get("productIntroEn") or "").strip(),
            "features": "",
            "applications": "",
            "category": str(result.get("catalogName") or "").strip(),
            "brand": str(result.get("brandNameEn") or "").strip(),
            "packaging": str(result.get("minPacketUnit") or "").strip(),
            "gross_weight": str(result.get("productWeight") or "").strip(),
            "params": params,
        }

    # -- challenge orchestration -------------------------------------------------

    def _solve(self, html: str, url: str) -> tuple[dict[str, str], bool]:
        """Try each solver in order; return (cookies, node_missing).

        ``node_missing`` lets the detail path emit its distinct node-missing
        warning while search silently degrades.
        """

        node_missing = False
        for solver in self.solvers:
            try:
                cookies = solver.solve(html, url, user_agent=self.fetcher.user_agent)
            except (NodeUnavailableError, subprocess.TimeoutExpired):
                node_missing = True
                continue
            if cookies:
                return cookies, node_missing
        return {}, node_missing

    def _set_cookies(self, url: str, cookies: dict[str, str]) -> None:
        host = urlparse(url).hostname or "item.szlcsc.com"
        for name, value in cookies.items():
            for domain in (host, ".szlcsc.com"):
                self.fetcher.set_cookie(name, value, domain=domain, path="/")

    def _get_detail_next_data(self, url: str) -> dict[str, Any]:
        response = self.fetcher.get(url)
        text = response.text
        for _ in range(2):
            if "__NEXT_DATA__" in text:
                return records.extract_next_data(text)
            if not self._is_waf_challenge(text):
                break
            cookies, node_missing = self._solve(text, url)
            if not cookies:
                if node_missing:
                    raise NodeUnavailableError("node runtime not found for LCSC WAF")
                break
            self._set_cookies(url, cookies)
            time.sleep(1)
            response = self.fetcher.get(url)
            text = response.text
        if "__NEXT_DATA__" not in text:
            raise RuntimeError("LCSC detail page still blocked after WAF solve")
        return records.extract_next_data(text)

    def _get_search_html(self, query: str) -> str:
        url = LCSC_SEARCH_URL.format(query=quote(query))
        response = self.fetcher.get(url)
        text = response.text
        if response.status_code not in (200, 203):
            raise RuntimeError(f"LCSC search failed: {response.status_code} {url}")
        for _ in range(2):
            if "__NEXT_DATA__" in text:
                return text
            cookies, _ = self._solve(text, getattr(response, "url", None) or url)
            if not cookies:
                break
            self._set_cookies(getattr(response, "url", None) or url, cookies)
            time.sleep(1)
            response = self.fetcher.get(url)
            text = response.text
        if "__NEXT_DATA__" not in text:
            raise RuntimeError("LCSC search response does not contain __NEXT_DATA__")
        return text

    @staticmethod
    def _is_waf_challenge(text: str) -> bool:
        # Only the genuine Aliyun challenge signature (callers test __NEXT_DATA__
        # first), so ordinary error pages aren't misread as WAF.
        return "arg1=" in text[:600]

    @staticmethod
    def _note(warnings: Optional[list[str]], message: str) -> None:
        if warnings is not None:
            warnings.append(message)
