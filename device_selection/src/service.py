"""选型服务：需求条目 → 候选清单（CandidateSet）。

流程固定为四步，和历史实现同构，但输入换成需求侧确认后的选型项：

1. 由选型项生成条件（类别、数量、硬性条件、偏好条件）；
2. 召回真实候选（指定型号优先，其次按 taxonomy 类别）；
3. 逐条核对参数，三态判定，违反硬性条件者淘汰；
4. 组装成可展示、可讨论、可确认的清单。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Mapping, Sequence

from .attributes import AttributeStore
from .catalog import DeviceCatalog, DeviceRow
from .db import Database
from .evaluate import Condition, evaluate_candidate
from .presets import PresetLibrary

SCHEMA_IN = "pcbagent.requirement_spec.v1"
SCHEMA_OUT = "pcbagent.candidate_set.v1"

_C_NUMBER = re.compile(r"lichuang_id=(C\d+)", re.IGNORECASE)
_PRIMARY_LIMIT = 3


@dataclass(slots=True)
class SelectionItem:
    """一个选型项：器件类别 + 数量 + 条件 + 指定/偏好器件。"""

    id: str
    category: str = ""
    leaf_code: str = ""
    leaf_codes: list[str] = dataclass_field(default_factory=list)
    level2: str = ""
    quantity: int = 1
    related_requirements: list[str] = dataclass_field(default_factory=list)
    hard_conditions: list[Condition] = dataclass_field(default_factory=list)
    preferred_conditions: list[Condition] = dataclass_field(default_factory=list)
    keywords: list[str] = dataclass_field(default_factory=list)
    specified_model: str = ""
    specified_intent: str = ""
    module_name: str = ""
    module_type: str = ""
    purpose: str = ""
    boundary: str = ""
    chenxin_module_id: str = ""
    chenxin_selection_item_id: str = ""
    specified_reference_ids: list[str] = dataclass_field(default_factory=list)
    note: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SelectionItem":
        item_id = str(raw.get("id") or raw.get("selection_item_id") or "").strip()
        if not item_id:
            raise ValueError("selection item requires an id")
        specified = raw.get("specified_device") or {}
        if not isinstance(specified, Mapping):
            specified = {}
        leaf_codes = [str(x) for x in (raw.get("leaf_codes") or []) if str(x).strip()]
        leaf_code = str(raw.get("leaf_code") or "").strip()
        if leaf_code and leaf_code not in leaf_codes:
            leaf_codes.insert(0, leaf_code)
        if not leaf_code and leaf_codes:
            leaf_code = leaf_codes[0]
        return cls(
            id=item_id,
            category=str(raw.get("category") or ""),
            leaf_code=leaf_code,
            leaf_codes=leaf_codes,
            level2=str(raw.get("category_level_2") or raw.get("level2") or ""),
            quantity=int(raw.get("quantity") or 1),
            related_requirements=[str(x) for x in (raw.get("related_requirements") or [])],
            hard_conditions=[
                Condition.from_dict(dict(entry), hard=True)
                for entry in (raw.get("hard_conditions") or [])
                if isinstance(entry, Mapping)
            ],
            preferred_conditions=[
                Condition.from_dict(dict(entry), hard=True)
                for entry in (raw.get("preferred_conditions") or [])
                if isinstance(entry, Mapping)
            ],
            keywords=[str(x) for x in (raw.get("keywords") or [])],
            specified_model=str(specified.get("model") or raw.get("specified_model") or ""),
            specified_intent=str(specified.get("intent") or ""),
            module_name=str(raw.get("module_name") or ""),
            module_type=str(raw.get("module_type") or ""),
            purpose=str(raw.get("purpose") or ""),
            boundary=str(raw.get("boundary") or ""),
            chenxin_module_id=str(raw.get("chenxin_module_id") or ""),
            chenxin_selection_item_id=str(raw.get("chenxin_selection_item_id") or ""),
            specified_reference_ids=[
                str(x) for x in (raw.get("specified_reference_ids") or [])
            ],
            note=str(raw.get("note") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "leaf_code": self.leaf_code,
            "leaf_codes": self.leaf_codes,
            "level2": self.level2,
            "quantity": self.quantity,
            "related_requirements": self.related_requirements,
            "hard_conditions": [c.as_dict() for c in self.hard_conditions],
            "preferred_conditions": [c.as_dict() for c in self.preferred_conditions],
            "keywords": self.keywords,
            "specified_device": {
                "model": self.specified_model,
                "intent": self.specified_intent,
            },
            "module_name": self.module_name,
            "module_type": self.module_type,
            "purpose": self.purpose,
            "boundary": self.boundary,
            "chenxin_module_id": self.chenxin_module_id,
            "chenxin_selection_item_id": self.chenxin_selection_item_id,
            "specified_reference_ids": self.specified_reference_ids,
            "note": self.note,
        }


class SelectionService:
    """把需求清单跑成候选清单。"""

    def __init__(
        self,
        database: Database | None = None,
        *,
        catalog: DeviceCatalog | None = None,
        attributes: AttributeStore | None = None,
        presets: PresetLibrary | None = None,
        datasheet: Any = None,
    ) -> None:
        self.db = database or Database()
        self.catalog = catalog or DeviceCatalog(self.db)
        self.attributes = attributes or AttributeStore(self.db)
        self.presets = presets or PresetLibrary()
        # 需要时才连手册库：没有它也能跑，只是补不到缺失参数
        self.datasheet = datasheet

    def _c_number(self, attributes: Mapping[str, Any], row: DeviceRow) -> str:
        for attribute in attributes.values():
            match = _C_NUMBER.search(attribute.source_locator or "")
            if match:
                return match.group(1).upper()
        match = _C_NUMBER.search(row.milvus_locator or "")
        return match.group(1).upper() if match else ""

    def _supplement(
        self,
        attributes: Mapping[str, Any],
        row: DeviceRow,
        item: SelectionItem,
    ) -> dict[str, Any]:
        """结构化参数表缺的字段，去数据手册里补。补到就带上原文引用。"""

        if self.datasheet is None:
            return {}
        wanted = {
            c.field for c in list(item.hard_conditions) + list(item.preferred_conditions)
        }
        if not wanted:
            return {}
        c_number = self._c_number(attributes, row)
        if not c_number:
            return {}
        from .datasheet import lookup, to_attribute  # 局部导入，避免无谓依赖

        supplements: dict[str, Any] = {}
        for field_key in sorted(wanted):
            existing = attributes.get(field_key)
            if (
                existing is not None
                and not existing.is_missing
                and existing.evidence_status != "EXPECTED_BUT_MISSING"
            ):
                continue
            try:
                finding = lookup(self.datasheet, c_number, field_key)
            except Exception:  # noqa: BLE001 - 手册库不可用不该中断选型
                continue
            if finding.found:
                supplements[field_key] = to_attribute(finding)
        return supplements

    # ------------------------------------------------------------ 单项处理
    def _price(self, attributes: Mapping[str, Any], row: DeviceRow) -> dict[str, Any]:
        c_number = ""
        for attribute in attributes.values():
            match = _C_NUMBER.search(attribute.source_locator or "")
            if match:
                c_number = match.group(1).upper()
                break
        if not c_number:
            match = _C_NUMBER.search(row.milvus_locator or "")
            if match:
                c_number = match.group(1).upper()
        unit_price, source = self.presets.price(c_number) if c_number else (None, "unconfirmed")
        return {
            "supplier_part": c_number,
            "unit_price_cny": unit_price,
            "price_source": source,
            "price_status": "confirmed" if unit_price is not None else "unconfirmed",
        }

    def evaluate_item(self, item: SelectionItem) -> dict[str, Any]:
        rows, recall = self.catalog.recall(
            model=item.specified_model,
            leaf_code=item.leaf_code,
            leaf_codes=item.leaf_codes,
            level2=item.level2,
            keywords=item.keywords,
            limit=12,
        )

        candidates: list[dict[str, Any]] = []
        for row in rows:
            attributes = self.attributes.for_device(row.device_id)
            supplements = self._supplement(attributes, row, item)
            merged = {**attributes, **supplements}
            evaluation = evaluate_candidate(
                merged,
                hard=item.hard_conditions,
                preferred=item.preferred_conditions,
            )
            if supplements:
                for verdict in evaluation.satisfied + evaluation.violated + evaluation.unknown:
                    if verdict.condition.field in supplements:
                        attribute = supplements[verdict.condition.field]
                        verdict.note = (
                            f"{verdict.note}｜来源：数据手册「{attribute.evidence_basis}」"
                        )
            price = self._price(attributes, row)
            quantity = max(int(item.quantity or 1), 1)
            unit_price = price["unit_price_cny"]
            candidates.append(
                {
                    **row.as_dict(),
                    **price,
                    "quantity": quantity,
                    "line_total_cny": (
                        round(unit_price * quantity, 2) if unit_price is not None else None
                    ),
                    "evaluation": evaluation.as_dict(),
                    "key_attributes": [
                        attributes[key].as_dict()
                        for key in AttributeStore.key_fields(attributes)[:8]
                        if key in attributes
                    ],
                    "missing_fields": AttributeStore.missing_fields(attributes),
                    "datasheet_supplements": [
                        supplements[key].as_dict() for key in sorted(supplements)
                    ],
                }
            )

        order = {
            "recommended": 0,
            "acceptable": 1,
            "conditional": 2,
            "unjudged": 3,
            "excluded": 4,
        }
        candidates.sort(
            key=lambda c: (
                order.get(c["evaluation"]["fit"], 9),
                -c["evaluation"]["confidence"],
                c["device_id"],
            )
        )
        # 目录里同一个型号可能对应多条记录（不同入库批次），对用户只保留最优的一条
        deduped: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        for candidate in candidates:
            model = str(candidate.get("model") or "")
            if model and model in seen_models:
                continue
            if model:
                seen_models.add(model)
            deduped.append(candidate)
        candidates = deduped

        recommended = [c for c in candidates if c["evaluation"]["fit"] != "excluded"]
        blocked_by: list[str] = []
        quality: list[str] = []
        if item.hard_conditions or item.preferred_conditions:
            pass
        else:
            quality.append(
                "该选型项没有可判定的条件，候选只来自类别召回，未做参数核对"
            )
        if not rows:
            blocked_by.append("没有召回到任何真实候选：请补充类别、关键词或具体型号")
        elif not recommended:
            reasons: list[str] = []
            for candidate in candidates[:3]:
                for verdict in candidate["evaluation"]["violated"]:
                    if verdict["hard"]:
                        reasons.append(
                            f"{candidate['model']} 违反 {verdict['field']}（器件 {verdict['device_value']}）"
                        )
            blocked_by.extend(reasons or ["全部候选都未通过硬性条件"])
        if item.specified_model and not rows:
            blocked_by.append(f"指定的型号 {item.specified_model} 在器件目录中不存在")

        return {
            "selection_item": item.as_dict(),
            "recall": recall,
            "candidate_count": len(candidates),
            "primary_candidates": [c["model"] for c in recommended[:_PRIMARY_LIMIT]],
            "candidates": candidates,
            "blocked_by": blocked_by,
            "quality_notes": quality,
            "unverified": not (item.hard_conditions or item.preferred_conditions),
        }

    # ------------------------------------------------------------ 整体流程
    def run(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        if str(spec.get("schema") or SCHEMA_IN) != SCHEMA_IN:
            raise ValueError(f"requirement spec schema must be {SCHEMA_IN!r}")
        raw_items = spec.get("selection_items")
        if not isinstance(raw_items, Sequence) or not raw_items:
            raise ValueError("requirement spec requires a non-empty selection_items list")

        items = [
            SelectionItem.from_dict(dict(raw))
            for raw in raw_items
            if isinstance(raw, Mapping)
        ]
        results = [self.evaluate_item(item) for item in items]

        total_items = len(results)
        with_candidates = sum(1 for r in results if r["candidates"])
        actionable = sum(
            1
            for r in results
            if r["primary_candidates"] and not r.get("unverified")
        )
        unverified = sum(1 for r in results if r.get("unverified"))
        blocked = total_items - actionable
        known_cost = 0.0
        unknown_cost_items: list[str] = []
        for result in results:
            best = result["primary_candidates"]
            if not best:
                unknown_cost_items.append(result["selection_item"]["id"])
                continue
            for candidate in result["candidates"]:
                if candidate["model"] == best[0]:
                    if candidate["line_total_cny"] is None:
                        unknown_cost_items.append(result["selection_item"]["id"])
                    else:
                        known_cost += float(candidate["line_total_cny"])
                    break

        return {
            "schema": SCHEMA_OUT,
            "requirement_version": str(spec.get("requirement_version") or ""),
            "project": dict(spec.get("project") or {}),
            "global_constraints": dict(spec.get("global_constraints") or {}),
            "items": results,
            "summary": {
                "total_items": total_items,
                "items_with_candidates": with_candidates,
                "items_with_recommendation": actionable,
                "items_unverified": unverified,
                "items_blocked": blocked,
                "known_cost_cny": round(known_cost, 2),
                "cost_unconfirmed_items": sorted(set(unknown_cost_items)),
            },
            "evidence_policy": {
                "unknown_never_passes": True,
                "price_missing_is_not_zero": True,
                "candidates_from_catalog_only": True,
            },
            "data_sources": {
                "database": self.db.config.describe(),
                "preset_library": self.presets.summary(),
            },
        }

    # ------------------------------------------------------------ 辅助能力
    def search(self, query: str, *, limit: int = 12) -> dict[str, Any]:
        """UI 的器件搜索框。"""

        text = str(query or "").strip()
        if not text:
            return {"query": text, "results": []}
        hits = self.catalog.find_by_model(text, limit=limit)
        recall = f"exact_model:{text}"
        if not hits:
            hits = self.catalog.keyword(text, limit=limit)
            recall = f"keyword:{text}"
        results = []
        for row in hits:
            attributes = self.attributes.for_device(row.device_id)
            price = self._price(attributes, row)
            results.append(
                {
                    **row.as_dict(),
                    **price,
                    "key_attributes": [
                        attributes[key].as_dict()
                        for key in AttributeStore.key_fields(attributes)[:8]
                        if key in attributes
                    ],
                    "missing_fields": AttributeStore.missing_fields(attributes),
                }
            )
        return {"query": text, "recall": recall, "results": results}


def load_spec(path: str) -> dict[str, Any]:
    import json
    from pathlib import Path

    return json.loads(Path(path).read_text(encoding="utf-8"))


def demo_spec() -> dict[str, Any]:
    """一个可直接跑通的最小示例，取自历史案例里的降压电源模块。"""

    return {
        "schema": SCHEMA_IN,
        "requirement_version": "req-demo-0001",
        "project": {"name": "示例：24V 输入降压电源", "summary": "演示选型链路"},
        "global_constraints": {"budget_total_cny": 80, "currency": "CNY"},
        "selection_items": [
            {
                "id": "SEL-001",
                "category": "dc_dc_power_converter_chip",
                "leaf_code": "l_step_down_dc_dc_converter",
                "quantity": 2,
                "related_requirements": ["F01"],
                "hard_conditions": [
                    {
                        "field": "topology",
                        "op": "contains",
                        "value": "Buck",
                        "reason": "必须是降压拓扑",
                        "related_requirement": "F01",
                    },
                    {
                        "field": "package",
                        "op": "is_not",
                        "value": "BGA",
                        "related_requirement": "F01",
                    },
                ],
                "preferred_conditions": [
                    {
                        "field": "output_current",
                        "op": ">=",
                        "value": 5,
                        "unit": "A",
                        "related_requirement": "F01",
                    }
                ],
                "specified_device": {"model": "TPS5450DDAR", "intent": "preferred"},
            }
        ],
    }
