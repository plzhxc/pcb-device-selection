from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class LcscFile:
    file_type: str
    file_name: str
    url: str


@dataclass
class LcscProduct:
    product_code: str
    product_id: str
    product_model: str
    product_name: str
    product_type: str
    brand: str = ""
    package: str = ""
    description: str = ""
    stock: Optional[int] = None
    price: Optional[float] = None
    # RMB quantity ladder; each tier is {"qty_start": int, "qty_end": int | None,
    # "price": float} with qty_end None meaning "qty_start and up". Source varies
    # (search productPriceList for in-stock; signed API when enabled) -- see
    # ``price_source``. ``price`` stays the first tier for compatibility.
    price_tiers: list[dict[str, Any]] = field(default_factory=list)
    price_source: str = ""              # where price/tiers came from
    reel_price: Optional[float] = None  # whole-reel total (encaptionPrice)
    reel_qty: Optional[int] = None      # units per reel (minWholeNumber)
    coupons: list[dict[str, Any]] = field(default_factory=list)
    # Market snapshot metadata: these fields are volatile, so the card records
    # when/what-currency they were fetched to stop downstream treating them as
    # long-lived facts.
    fetched_at: str = ""                # ISO-8601 collection time
    currency: str = "CNY"
    overview: str = ""
    features: str = ""
    applications: str = ""
    category: str = ""
    packaging: str = ""
    gross_weight: str = ""              # human display, e.g. "0.29 g"
    gross_weight_raw: Optional[float] = None  # original value as fetched
    gross_weight_unit: str = ""         # source unit of gross_weight_raw (e.g. "kg")
    param_map: dict[str, Any] = field(default_factory=dict)
    files: list[LcscFile] = field(default_factory=list)
    item_url: str = ""
    source_query: str = ""

    def datasheet_files(self) -> list[LcscFile]:
        return [f for f in self.files if f.file_type == "pdf_property"]

    def first_datasheet(self) -> Optional[LcscFile]:
        files = self.datasheet_files()
        if files:
            return files[0]
        for file in self.files:
            if file.url.lower().endswith(".pdf"):
                return file
        return None

    def asdict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeviceArtifacts:
    run_dir: Path
    product_card_md: Optional[Path] = None
    pdf_path: Optional[Path] = None
    mineru_dir: Optional[Path] = None
    raw_content_list_json: Optional[Path] = None

    def asdict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in asdict(self).items()}


@dataclass
class CollectionReport:
    query: str
    selected_product: Optional[dict[str, Any]] = None
    artifacts: Optional[dict[str, Any]] = None
    lcsc_candidates: list[dict[str, Any]] = field(default_factory=list)
    mineru: Optional[dict[str, Any]] = None
    warnings: list[str] = field(default_factory=list)

    def asdict(self) -> dict[str, Any]:
        return asdict(self)
