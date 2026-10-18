"""Acquisition and MinerU stage used by the complete pipeline.

LCSC search -> select -> download PDF -> MinerU parse -> flatten into a flat
delivery dir + product card. :mod:`core.pipeline` then consumes
the returned content list and runs the optimization Agents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Mapping, Optional

from datasheet_automation.lcsc.client import LcscClient
from datasheet_automation.delivery.card import write_product_card
from datasheet_automation.delivery.flatten import finalize_prepare_dir
from .io import ensure_dir, path_from_env_or_default, runtime_output_path, safe_filename
from .models import CollectionReport, DeviceArtifacts, LcscProduct
from datasheet_automation.parsing.protocol import DEFAULT_MINERU_SOURCE, MinerURequest
from datasheet_automation.parsing.transaction import find_mineru_outputs, parse_mineru
from .runtime import RuntimeServices


# Injectable fallback only: real callers pass output_root via config.  Worklib
# keeps transient run products in 07_TEMP, never beside maintained package code.
_WORKLIB_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = _WORKLIB_ROOT / "07_TEMP" / "datasheet_automation"


@dataclass
class CollectionConfig:
    output_root: Path = field(
        default_factory=lambda: path_from_env_or_default(
            "PCBAGENT_DATASHEET_OUTPUT", DEFAULT_OUTPUT_ROOT
        )
    )
    overwrite: bool = False
    candidate_limit: int = 10

    # Full RMB price ladder via the signed price API (the ONLY POST in the whole
    # pipeline -- /cid/verify in price_chain.js). Default OFF: in-stock ladders
    # come from the search productPriceList (GET), so normal collection sends zero
    # POSTs. Turn on only where the signed endpoint is reachable (clean IP/proxy).
    fetch_price_ladder: bool = False

    run_mineru: bool = True
    mineru_command: Optional[str] = None
    mineru_source: Path = field(default_factory=lambda: DEFAULT_MINERU_SOURCE)
    mineru_backend: str = "pipeline"
    mineru_method: str = "auto"
    mineru_lang: str = "ch"
    # Formula/table default OFF: keep both as screenshots (see protocol.py).
    mineru_formula: bool = False
    mineru_table: bool = False
    mineru_api_url: Optional[str] = None
    mineru_server_url: Optional[str] = None
    mineru_timeout_s: Optional[int] = None
    reuse_mineru_output: bool = True

    def __post_init__(self) -> None:
        self.output_root = runtime_output_path(self.output_root)


class DatasheetCollector:
    def __init__(
        self,
        config: Optional[CollectionConfig] = None,
        *,
        runtime: RuntimeServices | Mapping[str, Any] | None = None,
    ):
        self.config = config or CollectionConfig()
        self.runtime = RuntimeServices.from_value(runtime)
        self.lcsc = LcscClient(
            fetcher=self.runtime.fetcher, price_signer=self.runtime.price_signer
        )

    def search(self, query: str) -> list[LcscProduct]:
        return self.lcsc.search(query, limit=self.config.candidate_limit)

    def collect_and_parse(self, query: str) -> CollectionReport:
        self.runtime.trace("datasheet.collection.start", query=query)
        report = CollectionReport(query=query)
        candidates = self.search(query)
        report.lcsc_candidates = [p.asdict() for p in candidates]
        self.runtime.trace(
            "datasheet.collection.search.end",
            query=query,
            candidate_count=len(candidates),
        )
        if not candidates:
            raise RuntimeError(f"No LCSC candidates found for {query!r}")
        product = self._select_product(query, candidates)
        self.runtime.trace(
            "datasheet.collection.select.end",
            query=query,
            product_code=product.product_code,
            product_model=product.product_model,
        )

        # Flat delivery dir: PDF, card, MinerU assets, manifest, images/ land
        # directly under run_dir. Intermediate report JSON files are not
        # written -- their data lives in the returned report and on stdout.
        run_dir = ensure_dir(self.config.output_root / safe_filename(product.product_code or query))
        artifacts = DeviceArtifacts(run_dir=run_dir)

        artifacts.product_card_md = self._write_product_card(product, run_dir, report)
        report.selected_product = product.asdict()

        pdf_path = self.lcsc.download_datasheet(product, run_dir, overwrite=self.config.overwrite)
        artifacts.pdf_path = pdf_path
        self.runtime.trace(
            "datasheet.collection.download.end",
            product_code=product.product_code,
            pdf_path=str(pdf_path),
        )

        content_list_json, mineru_parse_dir = self._prepare_mineru_output(
            pdf_path=pdf_path, run_dir=run_dir, report=report,
        )
        artifacts.mineru_dir = mineru_parse_dir
        artifacts.raw_content_list_json = content_list_json
        self.runtime.trace(
            "datasheet.collection.mineru.end",
            product_code=product.product_code,
            content_list=str(content_list_json),
            output_dir=str(mineru_parse_dir),
            source=(report.mineru or {}).get("source"),
        )

        report.artifacts = artifacts.asdict()
        self.runtime.trace(
            "datasheet.collection.end",
            query=query,
            product_code=product.product_code,
            run_dir=str(run_dir),
        )
        return report

    @staticmethod
    def _select_product(query: str, candidates: list[LcscProduct]) -> LcscProduct:
        """Require exact identity whenever the caller supplied an LCSC C number."""

        normalized = str(query or "").strip().upper()
        if re.fullmatch(r"C\d+", normalized):
            exact = [
                product for product in candidates
                if str(product.product_code or "").strip().upper() == normalized
            ]
            if not exact:
                observed = ", ".join(
                    str(product.product_code or "<missing>") for product in candidates[:5]
                )
                raise RuntimeError(
                    f"LCSC did not return exact product_code={normalized}; "
                    f"observed candidates: {observed or '<none>'}"
                )
            return exact[0]
        return candidates[0]

    def _write_product_card(
        self,
        product: LcscProduct,
        run_dir: Path,
        report: CollectionReport,
    ) -> Path:
        """Fetch the product card fields and render ``{code}_card.md``.

        Primary path is the LCSC detail page (Chinese, WAF-solved); it falls back
        to the wmsc API (English) on any failure. Card generation never aborts
        collection -- warnings are surfaced in the report instead.
        """

        warnings: list[str] = []
        card = self.lcsc.fetch_pdf_desc(product, warnings=warnings)
        if not card:
            card = self.lcsc.fetch_wmsc(product.product_code, warnings=warnings)
        lang = str(card.get("lang") or "zh")
        source = str(card.get("source") or "szlcsc-detail")

        product.overview = str(card.get("overview") or "")
        product.features = str(card.get("features") or "")
        product.applications = str(card.get("applications") or "")
        product.category = str(card.get("category") or "")
        product.packaging = str(card.get("packaging") or "")
        product.gross_weight = str(card.get("gross_weight") or "")
        product.gross_weight_raw = card.get("gross_weight_raw")
        product.gross_weight_unit = str(card.get("gross_weight_unit") or "")
        product.fetched_at = str(card.get("fetched_at") or "")
        product.currency = str(card.get("currency") or "CNY")
        if card.get("brand"):
            product.brand = str(card["brand"])
        # Detail page carries the authoritative scalar price / stock (incl. 0),
        # reel context and coupons. None stays None; 0 is preserved.
        if card.get("price") is not None:
            product.price = card["price"]
        if card.get("stock") is not None:
            product.stock = card["stock"]
        product.reel_price = card.get("reel_price")
        product.reel_qty = card.get("reel_qty")
        product.coupons = card.get("coupons") or []
        # Price provenance: search productPriceList (set in records) is the in-stock
        # ladder; otherwise the detail scalar price; the signed API (opt-in) wins.
        if not product.price_source and product.price is not None:
            product.price_source = "szlcsc-detail:price"
        if self.config.fetch_price_ladder:
            tiers = self.lcsc.fetch_price_ladder(product, warnings=warnings)
            if tiers:
                product.price_tiers = tiers
                product.price_source = "szlcsc-signed:price/sign"

        card_path = run_dir / f"{safe_filename(product.product_code)}_card.md"
        write_product_card(product, card, card_path, lang=lang, source=source)
        report.warnings.extend(warnings)
        self.runtime.trace(
            "datasheet.collection.card.end",
            product_code=product.product_code,
            source=source,
            lang=lang,
            warnings=len(warnings),
        )
        return card_path

    def _prepare_mineru_output(
        self,
        *,
        pdf_path: Path,
        run_dir: Path,
        report: CollectionReport,
    ) -> tuple[Path, Path]:
        stem = pdf_path.stem
        # A prior fresh run already flattened + relabelled its assets into run_dir,
        # so reuse detects the delivered layout directly (manifest marks finalize
        # done) and skips both MinerU and finalize.
        flat_cl = run_dir / f"{stem}_content_list.json"
        flat_md = run_dir / f"{stem}.md"
        flat_manifest = run_dir / "manifest.json"
        if (
            self.config.reuse_mineru_output
            and not self.config.overwrite
            and flat_cl.is_file()
            and flat_md.is_file()
            and flat_manifest.is_file()
        ):
            reused = find_mineru_outputs(run_dir, stem, method=None)
            report.mineru = {
                "ok": True,
                **reused.asdict(),
                "source": "existing_output",
                "manifest": json.loads(flat_manifest.read_text(encoding="utf-8")),
            }
            return flat_cl, run_dir

        mineru_root = ensure_dir(run_dir / "mineru")
        request = MinerURequest(
            pdf_path=pdf_path,
            output_dir=mineru_root,
            command=self.config.mineru_command,
            source_root=self.config.mineru_source,
            backend=self.config.mineru_backend,
            method=self.config.mineru_method,
            lang=self.config.mineru_lang,
            formula=self.config.mineru_formula,
            table=self.config.mineru_table,
            api_url=self.config.mineru_api_url,
            server_url=self.config.mineru_server_url,
            timeout_s=self.config.mineru_timeout_s,
            overwrite=self.config.overwrite,
            reuse_existing=self.config.reuse_mineru_output,
            execute=self.config.run_mineru,
        )
        result = parse_mineru(request, self.runtime.mineru_backend)
        flat_result, pruned, manifest = finalize_prepare_dir(
            result, run_dir, mineru_root, pdf_name=pdf_path.name
        )
        report.mineru = {"ok": True, **flat_result.asdict(), "pruned": pruned, "manifest": manifest}
        unreferenced = manifest.get("unreferenced_images") or []
        if unreferenced:
            report.warnings.append(
                f"unreferenced_images:{len(unreferenced)}:{','.join(unreferenced)}"
            )
        return flat_result.content_list_json, flat_result.output_dir
