"""候选召回：从真实器件目录里拿器件，不用模型记忆造型号。

召回顺序遵循"精确优先、类别兜底"：
1. 精确型号 / 料号命中；
2. 按 taxonomy 的 leaf_code 召回同类真实器件；
3. 按 legacy 二级类别召回。
每一步都带来源说明，调用方据此解释候选是怎么来的。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .db import Database

_SEARCH_VIEW = "dsearch_v_device_search_profiled_incremental"

_COLUMNS = (
    "device_id, part_number_raw, part_number_normalized, manufacturer_normalized,"
    " legacy_level1, legacy_level2, domain_name_zh, family_name_zh, leaf_code,"
    " leaf_name_zh, leaf_name_en, taxonomy_path, component_role, device_form,"
    " description_text, phase2_is_core_component, phase2_core_level,"
    " phase2_datasheet_need_level, milvus_source_locator"
)


@dataclass(frozen=True, slots=True)
class DeviceRow:
    """目录里的一条真实器件记录。"""

    device_id: int
    part_number: str
    manufacturer: str
    level1: str
    level2: str
    leaf_code: str
    leaf_name_zh: str
    leaf_name_en: str
    taxonomy_path: str
    role: str
    device_form: str
    description: str
    is_core: bool
    core_level: str
    datasheet_need_level: str
    milvus_locator: str

    @property
    def model(self) -> str:
        return self.part_number

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "model": self.part_number,
            "manufacturer": self.manufacturer,
            "category_level_1": self.level1,
            "category_level_2": self.level2,
            "leaf_code": self.leaf_code,
            "leaf_name_zh": self.leaf_name_zh,
            "leaf_name_en": self.leaf_name_en,
            "taxonomy_path": self.taxonomy_path,
            "component_role": self.role,
            "device_form": self.device_form,
            "description": self.description,
            "is_core_component": self.is_core,
            "core_level": self.core_level,
            "datasheet_need_level": self.datasheet_need_level,
            "milvus_locator": self.milvus_locator,
        }


def _row(raw: dict[str, Any]) -> DeviceRow:
    return DeviceRow(
        device_id=int(raw.get("device_id") or 0),
        part_number=str(raw.get("part_number_normalized") or raw.get("part_number_raw") or ""),
        manufacturer=str(raw.get("manufacturer_normalized") or ""),
        level1=str(raw.get("legacy_level1") or ""),
        level2=str(raw.get("legacy_level2") or ""),
        leaf_code=str(raw.get("leaf_code") or ""),
        leaf_name_zh=str(raw.get("leaf_name_zh") or ""),
        leaf_name_en=str(raw.get("leaf_name_en") or ""),
        taxonomy_path=str(raw.get("taxonomy_path") or ""),
        role=str(raw.get("component_role") or ""),
        device_form=str(raw.get("device_form") or ""),
        description=str(raw.get("description_text") or ""),
        is_core=bool(raw.get("phase2_is_core_component")),
        core_level=str(raw.get("phase2_core_level") or ""),
        datasheet_need_level=str(raw.get("phase2_datasheet_need_level") or ""),
        milvus_locator=str(raw.get("milvus_source_locator") or ""),
    )


class DeviceCatalog:
    """器件目录的只读入口。"""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or Database()

    # ---------------------------------------------------------------- 精确
    def find_by_model(self, model: str, *, limit: int = 5) -> list[DeviceRow]:
        """按型号精确查身份，附带前缀兜底（型号常带后缀差异）。"""

        text = str(model or "").strip()
        if not text:
            return []
        exact = self.db.query(
            f"SELECT {_COLUMNS} FROM {_SEARCH_VIEW}"
            " WHERE part_number_normalized = %s OR part_number_raw = %s"
            " ORDER BY phase2_is_core_component DESC, device_id ASC LIMIT %s",
            (text, text, int(limit)),
        )
        if exact:
            return [_row(item) for item in exact]
        return self.find_by_prefix(text, limit=limit)

    def find_by_prefix(self, prefix: str, *, limit: int = 10) -> list[DeviceRow]:
        text = str(prefix or "").strip()
        if not text:
            return []
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {_SEARCH_VIEW}"
            " WHERE part_number_normalized LIKE %s ESCAPE '\\\\'"
            " ORDER BY CHAR_LENGTH(part_number_normalized) ASC,"
            " phase2_is_core_component DESC, device_id ASC LIMIT %s",
            (f"{text}%", int(limit)),
        )
        return [_row(item) for item in rows]

    def find_by_supplier_part(self, c_number: str, *, limit: int = 5) -> list[DeviceRow]:
        """按立创 C 编号查。库里的编号存在 source locator 里，这里做兜底匹配。"""

        text = str(c_number or "").strip().upper()
        if not text:
            return []
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {_SEARCH_VIEW}"
            " WHERE source_record_locator LIKE %s OR parameter_source_locator LIKE %s"
            " OR description_text LIKE %s"
            " ORDER BY phase2_is_core_component DESC, device_id ASC LIMIT %s",
            (f"%{text}%", f"%{text}%", f"%{text}%", int(limit)),
        )
        return [_row(item) for item in rows]

    # ---------------------------------------------------------------- 类别
    def find_by_leaf(self, leaf_code: str, *, limit: int = 20) -> list[DeviceRow]:
        """按 taxonomy 叶子召回——这是"按类别找真实候选"的主路径。"""

        code = str(leaf_code or "").strip()
        if not code:
            return []
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {_SEARCH_VIEW} WHERE leaf_code = %s"
            " ORDER BY phase2_is_core_component DESC, phase2_core_level ASC, device_id ASC"
            " LIMIT %s",
            (code, int(limit)),
        )
        return [_row(item) for item in rows]

    def find_by_legacy_level2(self, level2: str, *, limit: int = 20) -> list[DeviceRow]:
        value = str(level2 or "").strip()
        if not value:
            return []
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {_SEARCH_VIEW} WHERE legacy_level2 = %s"
            " ORDER BY phase2_is_core_component DESC, device_id ASC LIMIT %s",
            (value, int(limit)),
        )
        return [_row(item) for item in rows]

    def keyword(self, text: str, *, limit: int = 20) -> list[DeviceRow]:
        value = str(text or "").strip()
        if not value:
            return []
        rows = self.db.query(
            f"SELECT {_COLUMNS} FROM {_SEARCH_VIEW}"
            " WHERE part_number_normalized LIKE %s ESCAPE '\\\\'"
            " OR description_text LIKE %s ESCAPE '\\\\'"
            " ORDER BY phase2_is_core_component DESC, device_id ASC LIMIT %s",
            (f"%{value}%", f"%{value}%", int(limit)),
        )
        return [_row(item) for item in rows]

    # ---------------------------------------------------------------- 类目
    def leaves(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """可用类别清单，供 UI 选择选型项类别。"""

        return self.db.query(
            f"SELECT leaf_code, MAX(leaf_name_zh) AS leaf_name_zh,"
            f" MAX(legacy_level1) AS level1, MAX(legacy_level2) AS level2,"
            f" COUNT(*) AS devices FROM {_SEARCH_VIEW}"
            " GROUP BY leaf_code ORDER BY devices DESC LIMIT %s",
            (int(limit),),
        )

    # ------------------------------------------------------------ 候选集合
    def recall(
        self,
        *,
        model: str = "",
        leaf_code: str = "",
        leaf_codes: Sequence[str] = (),
        level2: str = "",
        keywords: Sequence[str] = (),
        limit: int = 12,
    ) -> tuple[list[DeviceRow], str]:
        """返回 (候选, 召回方式说明)。

        设计红线：召回不到就如实返回空列表，绝不用模型记忆补造型号。
        """

        if model:
            hits = self.find_by_model(model, limit=limit)
            if hits:
                return hits, f"exact_model:{model}"
            # 指定型号在目录里不存在时，不能编一个替代品顶上，但也不该就此
            # 交白卷——继续按类别召回备选，并把"型号未命中"如实写进召回说明。
            fallback, how = self.recall(
                leaf_code=leaf_code,
                leaf_codes=leaf_codes,
                level2=level2,
                keywords=keywords,
                limit=limit,
            )
            if fallback:
                return fallback, f"exact_model_miss:{model} → {how}"
            return [], f"exact_model_miss:{model}"

        # 需求侧给出的是一组候选类别（父类别展开），逐个召回后合并去重。
        wanted = [code for code in ([leaf_code] if leaf_code else []) + list(leaf_codes) if code]
        wanted = list(dict.fromkeys(wanted))
        if wanted:
            merged: list[DeviceRow] = []
            seen: set[int] = set()
            for code in wanted:
                for hit in self.find_by_leaf(code, limit=limit):
                    if hit.device_id in seen:
                        continue
                    seen.add(hit.device_id)
                    merged.append(hit)
                if len(merged) >= limit:
                    break
            if merged:
                label = wanted[0] if len(wanted) == 1 else f"{wanted[0]}+{len(wanted) - 1}"
                return merged[:limit], f"leaf_code:{label}"

        if leaf_code:
            hits = self.find_by_leaf(leaf_code, limit=limit)
            if hits:
                return hits, f"leaf_code:{leaf_code}"

        if level2:
            hits = self.find_by_legacy_level2(level2, limit=limit)
            if hits:
                return hits, f"legacy_level2:{level2}"

        merged: list[DeviceRow] = []
        seen: set[int] = set()
        for word in keywords:
            for hit in self.keyword(word, limit=limit):
                if hit.device_id in seen:
                    continue
                seen.add(hit.device_id)
                merged.append(hit)
            if len(merged) >= limit:
                break
        if merged:
            return merged[:limit], f"keywords:{','.join(keywords)}"
        return [], "no_recall"
