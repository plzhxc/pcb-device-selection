"""器件参数读取与归一化。

数据来源是 dsearch_v_device_attribute_priority_incremental：每一行就是一个
属性，带 field_key、值、优先级、是否必需、是否有证据、来源定位。这套结构
天然对应选型需要的三件事：

* priority_rank / is_required → 哪些算硬性条件；
* is_missing / evidence_status → 未知与待补证据；
* source_locator → 结论能不能指回原始记录。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .db import Database
from .units import Interval, Quantity, describe, from_columns

_VIEW = "dsearch_v_device_attribute_priority_incremental"

_COLUMNS = (
    "field_key, field_name_zh, field_value_text, field_value_num, field_unit,"
    " value_origin, source_table, source_column, source_locator, priority_rank,"
    " importance_score, is_required, field_group, missing_policy, evidence_basis,"
    " evidence_status, needs_engineer_review, is_missing"
)

_META_PREFIXES = ("legacy_", "dsearch_", "drelease_")
_META_KEYS = frozenset(
    {
        "device_id",
        "part_number",
        "part_number_raw",
        "domain_code",
        "family_code",
        "leaf_code",
        "brand",
        "brand_origin",
        "manufacturer",
        "product_tag",
        "packaging_method",
        "datasheet",
        "component_role",
        "device_form",
        "pin_functions",
    }
)


@dataclass(slots=True)
class Attribute:
    """一个器件属性，含归一化结果与证据状态。"""

    field_key: str
    name_zh: str
    text: str
    raw_number: Any
    raw_unit: str
    quantity: Quantity | Interval | None
    parse_source: str
    priority_rank: int
    importance: int
    is_required: bool
    is_missing: bool
    evidence_status: str
    evidence_basis: str
    source_locator: str
    needs_review: bool

    @property
    def is_numeric(self) -> bool:
        return self.quantity is not None

    @property
    def display_value(self) -> str:
        if self.text:
            return self.text
        return describe(self.quantity)

    @property
    def unit_known(self) -> bool:
        if self.quantity is None:
            return False
        return bool(self.quantity.unit)

    @property
    def usable_as_evidence(self) -> bool:
        """能不能作为「满足」的证据。

        红线：未知参数不算满足；文本解析出来但没有单位换算依据的数值，
        也不能算满足——除非比较双方单位恰好一致。
        """

        if self.is_missing:
            return False
        if self.evidence_status == "EXPECTED_BUT_MISSING":
            return False
        if self.quantity is not None:
            return True
        return bool(self.text)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_key": self.field_key,
            "name_zh": self.name_zh,
            "value_text": self.text,
            "value_normalized": describe(self.quantity),
            "parse_source": self.parse_source,
            "unit_known": self.unit_known,
            "priority_rank": self.priority_rank,
            "is_required": self.is_required,
            "is_missing": self.is_missing,
            "evidence_status": self.evidence_status,
            "source_locator": self.source_locator,
            "needs_engineer_review": self.needs_review,
        }


def is_meta_key(field_key: str) -> bool:
    key = str(field_key or "")
    if key in _META_KEYS:
        return True
    return any(key.startswith(prefix) for prefix in _META_PREFIXES)


class AttributeStore:
    """按 device_id 读取属性，同一进程内做缓存。"""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or Database()
        self._cache: dict[int, dict[str, Attribute]] = {}

    def for_device(self, device_id: int) -> dict[str, Attribute]:
        key = int(device_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {_VIEW} WHERE device_id = %s"
            " ORDER BY priority_rank ASC, field_key ASC",
            (key,),
        )
        attributes: dict[str, Attribute] = {}
        for raw in rows:
            field_key = str(raw.get("field_key") or "")
            if not field_key or is_meta_key(field_key):
                continue
            quantity, source = from_columns(
                raw.get("field_value_num"),
                raw.get("field_value_text"),
                raw.get("field_unit"),
            )
            attributes[field_key] = Attribute(
                field_key=field_key,
                name_zh=str(raw.get("field_name_zh") or field_key),
                text=str(raw.get("field_value_text") or "").strip(),
                raw_number=raw.get("field_value_num"),
                raw_unit=str(raw.get("field_unit") or ""),
                quantity=quantity,
                parse_source=source,
                priority_rank=int(raw.get("priority_rank") or 9999),
                importance=int(raw.get("importance_score") or 0),
                is_required=bool(raw.get("is_required")),
                is_missing=bool(raw.get("is_missing")),
                evidence_status=str(raw.get("evidence_status") or ""),
                evidence_basis=str(raw.get("evidence_basis") or ""),
                source_locator=str(raw.get("source_locator") or ""),
                needs_review=bool(raw.get("needs_engineer_review")),
            )
        self._cache[key] = attributes
        return attributes

    def for_devices(
        self,
        device_ids: Iterable[int],
    ) -> dict[int, dict[str, Attribute]]:
        return {int(d): self.for_device(int(d)) for d in device_ids}

    @staticmethod
    def key_fields(attributes: dict[str, Attribute]) -> list[str]:
        """按优先级挑出关键参数，供 UI 与提示词复用。"""

        ranked = [
            (attr.priority_rank, attr.field_key)
            for attr in attributes.values()
            if attr.priority_rank < 9999 or attr.is_required
        ]
        ranked.sort()
        return [key for _, key in ranked]

    @staticmethod
    def missing_fields(attributes: dict[str, Attribute]) -> list[str]:
        return sorted(
            attr.field_key
            for attr in attributes.values()
            if attr.is_missing or attr.evidence_status == "EXPECTED_BUT_MISSING"
        )

    def field_catalog(self, limit: int = 200) -> list[dict[str, Any]]:
        """字段字典：UI 里给用户挑条件用。"""

        return self.db.query(
            f"SELECT field_key, MAX(field_name_zh) AS name_zh,"
            f" COUNT(*) AS n, MIN(priority_rank) AS best_rank,"
            f" SUM(is_required) AS required_n FROM {_VIEW}"
            " GROUP BY field_key ORDER BY required_n DESC, n DESC LIMIT %s",
            (int(limit),),
        )
