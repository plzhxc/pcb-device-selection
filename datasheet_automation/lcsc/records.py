"""Pure LCSC parsing and ranking -- no network, no session state.

Turns one ``__NEXT_DATA__`` product record into an :class:`LcscProduct`, scores a
candidate against a query, and normalizes file URLs. Kept pure so search ranking
and record mapping are testable offline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from datasheet_automation.core.io import normalize_model
from datasheet_automation.core.models import LcscFile, LcscProduct


LCSC_ITEM_URL = "https://item.szlcsc.com/{product_id}.html"
LCSC_FILE_BASE_URL = "https://atta.szlcsc.com"


def extract_next_data(html: str) -> dict[str, Any]:
    try:
        from bs4 import BeautifulSoup
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "beautifulsoup4 is required for LCSC parsing"
        ) from exc
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None or not script.string:
        raise RuntimeError("cannot find script#__NEXT_DATA__ in LCSC response")
    return json.loads(script.string)


def normalize_file_url(raw_url: str) -> str:
    raw_url = str(raw_url or "").strip()
    if not raw_url:
        return ""
    if raw_url.startswith("//"):
        return "https:" + raw_url
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url
    if raw_url.startswith("/"):
        return LCSC_FILE_BASE_URL + raw_url
    return raw_url


def record_to_product(record: dict[str, Any], query: str) -> LcscProduct:
    product_vo = record.get("productVO") or {}
    product_id = str(product_vo.get("productId") or "")
    files = []
    for file_type_vo in product_vo.get("fileTypeVOList") or []:
        file_type = str(file_type_vo.get("fileType") or "")
        for detail in file_type_vo.get("detailVOList") or []:
            raw_url = detail.get("fileUrl") or detail.get("linkAddress") or ""
            url = normalize_file_url(raw_url)
            if not url:
                continue
            files.append(LcscFile(
                file_type=file_type,
                file_name=str(detail.get("fileName") or Path(url).name),
                url=url,
            ))

    # In-stock items carry the full RMB ladder here (GET, no signed POST needed).
    tiers = parse_price_tiers(product_vo.get("productPriceList"))
    first_price = tiers[0]["price"] if tiers else None
    price_source = "szlcsc-search:productPriceList" if tiers else ""

    # stockNumber may legitimately be 0 (out of stock); ``x or y`` would swallow
    # that to the fallback, so test for presence explicitly.
    stock = product_vo.get("stockNumber")
    if stock is None:
        stock = product_vo.get("totalStockNumber")

    return LcscProduct(
        product_code=str(product_vo.get("productCode") or ""),
        product_id=product_id,
        product_model=str(product_vo.get("productModel") or ""),
        product_name=str(product_vo.get("productName") or ""),
        product_type=str(product_vo.get("productType") or ""),
        brand=str(
            product_vo.get("productGradePlateName")
            or product_vo.get("productGradePlateNameWithoutHighlight")
            or record.get("lightBrandName")
            or ""
        ),
        package=str(product_vo.get("encapsulationModel") or record.get("lightStandard") or ""),
        description=str(product_vo.get("remark") or record.get("remarkPrefixTitle") or ""),
        stock=stock,
        price=first_price,
        price_tiers=tiers,
        price_source=price_source,
        param_map=dict(record.get("paramLinkedMap") or {}),
        files=files,
        item_url=LCSC_ITEM_URL.format(product_id=product_id) if product_id else "",
        source_query=query,
    )


def parse_price_tiers(payload: Any) -> list[dict[str, Any]]:
    """Normalize a price list into ladder tiers, from search OR the signed API.

    Each tier is ``{"qty_start": int, "qty_end": int | None, "price": float}``;
    ``qty_end`` is None when the source end marker is -1 ("qty_start and up").
    ``qty_start`` defaults to 1 when the source omits it (single-tier lists).

    Field names follow the search ``productPriceList`` shape
    (``startPurchasedNumber`` / ``endPurchasedNumber`` / ``productPrice``); the
    signed endpoint is assumed to reuse it. NOTE: verify the signed payload's
    field names against a real decrypted sample once that POST succeeds, then
    adjust only here.
    """

    if isinstance(payload, dict):
        rows = (
            payload.get("productPriceList")
            or payload.get("priceList")
            or payload.get("prices")
            or []
        )
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []

    tiers: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("productPrice") if row.get("productPrice") is not None else row.get("price"))
        except (TypeError, ValueError):
            continue
        start_raw = row.get("startPurchasedNumber", row.get("startNumber"))
        end_raw = row.get("endPurchasedNumber", row.get("endNumber"))
        try:
            qty_start = int(start_raw)
        except (TypeError, ValueError):
            qty_start = 1
        try:
            qty_end = int(end_raw)
        except (TypeError, ValueError):
            qty_end = -1
        tiers.append({
            "qty_start": qty_start,
            "qty_end": None if qty_end < 0 else qty_end,
            "price": price,
        })
    return tiers


def candidate_score(query: str, product: LcscProduct) -> float:
    q_norm = normalize_model(query)
    code_norm = normalize_model(product.product_code)
    model_norm = normalize_model(product.product_model)
    name_norm = normalize_model(product.product_name)
    score = 0.0
    if q_norm == code_norm:
        score += 1000
    if q_norm == model_norm:
        score += 900
    if q_norm and q_norm in model_norm:
        score += 300
    if q_norm and q_norm in name_norm:
        score += 120
    if product.datasheet_files():
        score += 80
    if product.product_code:
        score += 10
    if product.stock:
        score += min(float(product.stock), 100000.0) / 100000.0
    return score
