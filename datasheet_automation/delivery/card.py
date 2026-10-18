"""Render one LCSC product into a structured Chinese Markdown card.

The card is a compact, human- and agent-readable companion to the datasheet:
overview, parameter table, features, application areas, and a market section
(stock / price / links). The document is canonically Chinese; a lightweight
frontmatter declares the actual language and source so a wmsc (English) fallback
stays self-describing. A missing section keeps its heading with an empty body so
the section skeleton stays stable across parts.

Note: the market section carries volatile fields (stock, price). Unlike the
stable metadata, these change over time, so re-running prepare can produce a
different card. That is intentional -- the delivered card is meant to carry the
current market snapshot, not just static datasheet metadata.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from datasheet_automation.core.io import write_text
from datasheet_automation.core.models import LcscProduct


def write_product_card(
    product: LcscProduct,
    pdf_desc: Mapping[str, Any],
    out_path: str | Path,
    *,
    lang: str,
    source: str,
) -> Path:
    """Render ``product`` + ``pdf_desc`` to a Markdown card at ``out_path``."""

    return write_text(out_path, render_product_card(product, pdf_desc, lang=lang, source=source))


def render_product_card(
    product: LcscProduct,
    pdf_desc: Mapping[str, Any],
    *,
    lang: str,
    source: str,
) -> str:
    code = product.product_code or ""
    model = product.product_model or product.product_name or ""
    title = f"{code} {model}".strip() or code or model or "未知器件"

    sections: list[tuple[str, str]] = [
        ("概述", _overview_section(product, pdf_desc)),
        ("参数", _param_table(pdf_desc.get("params") or {})),
        ("特性", _bullet_section(pdf_desc.get("features"))),
        ("应用领域", _bullet_section(pdf_desc.get("applications"))),
        ("行情", _market_section(product)),
    ]

    # Market fields are volatile; the frontmatter stamps when/what-currency/where
    # they were fetched so downstream never treats them as long-lived facts.
    lines: list[str] = ["---", f"lichuang_id: {code}", f"lang: {lang}", f"source: {source}"]
    if product.fetched_at:
        lines.append(f"fetched_at: {product.fetched_at}")
    lines.append(f"currency: {product.currency or 'CNY'}")
    if product.price_source:
        lines.append(f"price_source: {product.price_source}")
    coupon_until = _coupon_valid_until(product.coupons)
    if coupon_until:
        lines.append(f"coupon_valid_until: {coupon_until}")
    lines.extend(["---", f"# {title}"])
    for heading, body in sections:
        lines.extend(["", f"## {heading}"])
        if body:
            lines.append(body)
    lines.append("")
    return "\n".join(lines)


def _overview_section(product: LcscProduct, pdf_desc: Mapping[str, Any]) -> str:
    overview = str(pdf_desc.get("overview") or product.overview or "").strip()
    facts: list[str] = []
    brand = str(pdf_desc.get("brand") or product.brand or "").strip()
    package = str(product.package or "").strip()
    category = str(pdf_desc.get("category") or product.category or "").strip()
    packaging = str(pdf_desc.get("packaging") or product.packaging or "").strip()
    weight = str(pdf_desc.get("gross_weight") or product.gross_weight or "").strip()
    for label, value in (
        ("品牌", brand),
        ("封装", package),
        ("分类", category),
        ("包装方式", packaging),
        ("毛重", weight),
    ):
        if value:
            facts.append(f"- {label}：{value}")

    parts: list[str] = []
    if overview:
        parts.append(overview)
    if facts:
        parts.append("\n".join(facts))
    return "\n\n".join(parts)


def _market_section(product: LcscProduct) -> str:
    """Volatile market snapshot: stock, price ladder, coupons, and LCSC links.

    Kept out of ``overview`` so the static-metadata section stays stable while
    these fields are free to change on re-run.
    """

    facts: list[str] = []
    if product.stock is not None:
        facts.append(f"- 库存：{product.stock}")
    if product.price is not None:
        facts.append(f"- 单价：¥{_money(product.price)}")
    ladder = _price_ladder(product.price_tiers)
    if ladder:
        facts.append(ladder)
    if product.reel_price is not None and product.reel_qty:
        facts.append(f"- 整盘：{product.reel_qty} 个/盘，¥{_money(product.reel_price)}")
    for line in _coupon_lines(product.coupons):
        facts.append(line)
    item_url = str(product.item_url or "").strip()
    if item_url:
        facts.append(f"- 商品页：{item_url}")
    pid = str(product.product_id or "").strip()
    if pid:
        facts.append(f"- 立创商品ID：{pid}")
    return "\n".join(facts)


def _price_ladder(tiers: Any) -> str:
    """Render the RMB quantity ladder as a compact table; '' when absent."""
    rows = []
    for tier in tiers or []:
        if not isinstance(tier, dict) or tier.get("price") is None:
            continue
        start = tier.get("qty_start")
        end = tier.get("qty_end")
        qty = f"{start}+" if end is None else f"{start}-{end}"
        rows.append((qty, _money(tier["price"])))
    if not rows:
        return ""
    out = ["- 阶梯价：", "", "| 数量 | 单价 |", "| --- | --- |"]
    out.extend(f"| {qty} | ¥{price} |" for qty, price in rows)
    return "\n".join(out)


def _coupon_valid_until(coupons: Any) -> str:
    """Earliest coupon expiry across coupons; '' when none carry one."""
    ends = [str(c.get("valid_until") or "").strip()
            for c in (coupons or []) if isinstance(c, dict) and c.get("valid_until")]
    return min(ends) if ends else ""


def _money(value: Any) -> str:
    """Format a money amount without a trailing '.0' (9.0 -> '9', 9.9 -> '9.9')."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(f)) if f == int(f) else f"{f:g}"


def _coupon_lines(coupons: Any) -> list[str]:
    lines: list[str] = []
    for c in coupons or []:
        if not isinstance(c, dict):
            continue
        amount = c.get("amount")
        if amount is None:
            continue
        threshold = c.get("threshold")
        kind = "运费券" if (c.get("type") == "freight" or c.get("free_express")) else "优惠券"
        if threshold:
            lines.append(f"- {kind}：满¥{_money(threshold)}减¥{_money(amount)}")
        else:
            lines.append(f"- {kind}：减¥{_money(amount)}")
    return lines


def _param_table(params: Mapping[str, Any]) -> str:
    rows = [
        (str(name).strip(), str(value).strip())
        for name, value in params.items()
        if str(name).strip() and str(value).strip() and str(value).strip() != "-"
    ]
    if not rows:
        return ""
    out = ["| 参数 | 值 |", "| --- | --- |"]
    out.extend(f"| {name} | {value} |" for name, value in rows)
    return "\n".join(out)


def _bullet_section(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    bullets = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith(("-", "*", "•")):
            line = f"- {line}"
        else:
            line = "- " + line.lstrip("-*• ").strip()
        bullets.append(line)
    return "\n".join(bullets)
