"""需求回填：选型之后，把当初定不下来的需求补回去。

背景：需求分析阶段有些条目只能留"待定"，因为它们依赖后面才确定的东西
——用哪颗芯片、走什么拓扑、开关频率多少。等器件选完，这些值其实已经确定，
只是还躺在需求清单里没写。

本模块负责找出这类条目并给出建议值，**不直接改需求**。用户确认后，由调用方
通过工作台自己的"编辑需求"接口写入，并标成 `suggested`（系统建议，待确认），
不是 `stated`（用户原话）。

判定规则很保守，宁可少提也不乱提：

1. 只处理 `tbd`（待定）或值为空的条目；
2. 只处理**这个选型项自己关联**的需求，不跨模块猜；
3. 参数名要和需求名对得上（互相包含，或命中同一个关键词）；
4. 器件该参数必须有确定值，缺失就当没确定；
5. 建议值来自器件的**实际参数**，并附上型号与出处，供人核对。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Sequence

# 需求名与参数名之间允许的"同义"关系。只放高置信度的，避免把
# "输出电压" 错配到 "输入电压范围" 上。
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("频率", ("开关频率", "工作频率", "时钟频率", "频率")),
    ("电流", ("输出电流", "输入电流", "工作电流", "静态电流", "电流")),
    ("电压", ("输出电压", "输入电压", "供电电压", "工作电压", "电压")),
    ("温度", ("工作温度", "环境温度", "温度范围", "温度")),
    ("封装", ("封装", "外形")),
    ("接口", ("接口", "协议")),
    ("容量", ("容量", "存储", "内存")),
    ("精度", ("精度", "误差", "分辨率")),
    ("通道", ("通道", "路数")),
)

_BLANK_VALUES = frozenset({"", "tbd", "待定", "未定", "待确认", "暂无", "n/a", "na"})


def _norm(text: Any) -> str:
    return re.sub(r"[\s_\-·（）()【】\[\]、，,。.;；:：/]+", "", str(text or "")).lower()


def _family(text: str) -> str | None:
    """粗粒度归族：频率/电流/电压/温度…"""

    value = _norm(text)
    for family, words in _KEYWORDS:
        if any(_norm(word) in value for word in words):
            return family
    return None


def _is_blank(requirement: Mapping[str, Any]) -> bool:
    state = str(requirement.get("state") or "")
    value = str(requirement.get("value") or "").strip()
    if state == "tbd":
        return True
    if state in ("not_applicable", "conflict"):
        return False
    return value.lower() in _BLANK_VALUES


def _match(requirement_name: str, attribute_name: str) -> bool:
    """需求名和参数名能不能对上是同一个东西。"""

    left, right = _norm(requirement_name), _norm(attribute_name)
    if not left or not right:
        return False
    if left in right or right in left:
        return True
    return _family(left) is not None and _family(left) == _family(right)


def pending(raw_spec: Mapping[str, Any]) -> list[dict[str, str]]:
    """列出仍然是待定/空值的需求，供界面解释"为什么没有建议"。

    这些条目不一定都能回填：例如"外设接口数量与统计口径"不是某个器件的
    单个参数，需要人来决定计数范围。如实列出来，比静默隐藏更好。
    """

    rows: list[dict[str, str]] = []
    for requirement in raw_spec.get("requirements") or []:
        if not _is_blank(requirement):
            continue
        rows.append(
            {
                "id": str(requirement.get("id") or ""),
                "name": str(requirement.get("name") or ""),
                "current_value": str(requirement.get("value") or "") or "待定",
            }
        )
    return rows


@dataclass(slots=True)
class Proposal:
    """一条待回填的建议。"""

    requirement_id: str
    requirement_name: str
    category: str
    current_value: str
    proposed_value: str
    unit: str
    item_id: str
    model: str
    device_id: int
    parameter: str
    evidence: str
    reason: str

    @property
    def key(self) -> str:
        return f"{self.requirement_id}::{self.item_id}::{self.model}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "requirement_id": self.requirement_id,
            "requirement_name": self.requirement_name,
            "category": self.category,
            "current_value": self.current_value,
            "proposed_value": self.proposed_value,
            "unit": self.unit,
            "selection_item_id": self.item_id,
            "model": self.model,
            "device_id": self.device_id,
            "parameter": self.parameter,
            "evidence": self.evidence,
            "reason": self.reason,
        }


def propose(
    *,
    raw_spec: Mapping[str, Any],
    result: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
    attributes_for: Any,
) -> list[Proposal]:
    """算出所有可回填的建议。

    ``attributes_for(device_id)`` 返回该器件的属性字典，用于取实际参数值。
    """

    requirements = {
        str(r.get("id")): dict(r) for r in raw_spec.get("requirements") or []
    }
    if not requirements:
        return []

    proposals: list[Proposal] = []
    seen: set[str] = set()
    # 记录每个选型项选定的器件与它的参数，供"未关联需求"的兜底匹配使用
    project_pool: list[tuple[str, str, int, list[Any]]] = []

    for item in result.get("items") or []:
        selection = item.get("selection_item") or {}
        item_id = str(selection.get("id") or "")
        related = [str(x) for x in (selection.get("related_requirements") or [])]

        # 优先用用户确认过的器件；没有就看首选
        candidate = None
        for entry in item.get("candidates") or []:
            record = decisions.get(f"{item_id}::{entry.get('model')}") or {}
            if record.get("decision") == "confirmed":
                candidate = entry
                break
        if candidate is None:
            candidates = item.get("candidates") or []
            if candidates:
                candidate = candidates[0]
        if candidate is None:
            continue

        model = str(candidate.get("model") or "")
        device_id = int(candidate.get("device_id") or 0)
        if not model:
            continue

        try:
            attributes = attributes_for(device_id)
        except Exception:  # noqa: BLE001 - 取不到属性就当没建议
            continue

        # 只拿该器件"关键参数"里的确定值
        pool: list[Any] = [
            a for a in attributes.values()
            if not a.is_missing and a.evidence_status != "EXPECTED_BUT_MISSING"
        ]
        pool.sort(key=lambda a: (a.priority_rank, a.field_key))
        project_pool.append((item_id, model, device_id, pool))

        if not related:
            continue

        for requirement_id in related:
            requirement = requirements.get(requirement_id)
            if requirement is None or not _is_blank(requirement):
                continue
            name = str(requirement.get("name") or "")
            for attribute in pool[:40]:
                if not _match(name, attribute.name_zh) and not _match(
                    name, attribute.field_key
                ):
                    continue
                value = attribute.display_value
                if not value or value == "—":
                    continue
                unit = str(requirement.get("unit") or "") or (
                    attribute.quantity.unit if attribute.quantity else ""
                )
                proposal = Proposal(
                    requirement_id=requirement_id,
                    requirement_name=name,
                    category=str(requirement.get("category") or "functions"),
                    current_value=str(requirement.get("value") or "") or "待定",
                    proposed_value=value,
                    unit=unit,
                    item_id=item_id,
                    model=model,
                    device_id=device_id,
                    parameter=attribute.name_zh or attribute.field_key,
                    evidence=attribute.evidence_status or "",
                    reason=(
                        f"选定 {model} 后，其{attribute.name_zh or attribute.field_key}"
                        f"为 {value}，可据此确定这条需求"
                    ),
                )
                if proposal.key in seen:
                    continue
                seen.add(proposal.key)
                proposals.append(proposal)
                break

    # 兜底：有些"待定"需求（例如"外设接口数量"）没有挂到具体选型项上。
    # 这时在整个项目里按参数名找，只有**唯一确定**的结果才提建议，
    # 多个器件给出不同值时说明存在歧义，宁可不提。
    covered = {p.requirement_id for p in proposals}
    for requirement_id, requirement in requirements.items():
        if requirement_id in covered or not _is_blank(requirement):
            continue
        name = str(requirement.get("name") or "")
        matches: dict[str, tuple[str, str, int, Any]] = {}
        for item_id, model, device_id, pool in project_pool:
            for attribute in pool[:40]:
                if not (
                    _match(name, attribute.name_zh)
                    or _match(name, attribute.field_key)
                ):
                    continue
                value = attribute.display_value
                if not value or value == "—":
                    continue
                matches.setdefault(
                    value, (item_id, model, device_id, attribute)
                )
                break
        if len(matches) != 1:
            continue
        value, (item_id, model, device_id, attribute) = next(iter(matches.items()))
        unit = str(requirement.get("unit") or "") or (
            attribute.quantity.unit if attribute.quantity else ""
        )
        proposal = Proposal(
            requirement_id=requirement_id,
            requirement_name=name,
            category=str(requirement.get("category") or "functions"),
            current_value=str(requirement.get("value") or "") or "待定",
            proposed_value=value,
            unit=unit,
            item_id=item_id,
            model=model,
            device_id=device_id,
            parameter=attribute.name_zh or attribute.field_key,
            evidence=attribute.evidence_status or "",
            reason=(
                f"这条需求没有挂到具体模块，但全项目内只有 {model} 的"
                f"{attribute.name_zh or attribute.field_key}（{value}）与它对得上，"
                "据此给出建议"
            ),
        )
        if proposal.key not in seen:
            seen.add(proposal.key)
            proposals.append(proposal)

    # 同一条需求可能挂在多个选型项上。只有所有相关器件给出同一个值时
    # 才保留一条建议；值不一致说明还不能确定，不能替用户挑一个。
    grouped: dict[str, list[Proposal]] = {}
    for proposal in proposals:
        grouped.setdefault(proposal.requirement_id, []).append(proposal)
    unique: list[Proposal] = []
    for group in grouped.values():
        values = {p.proposed_value for p in group}
        if len(values) == 1:
            unique.append(group[0])
    return unique
