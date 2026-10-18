"""候选评估：满足 / 不满足 / 未知，三态分开记。

规则来自设计文档：

* 违反硬性条件的候选直接淘汰，不进推荐列表；
* 关键证据缺失标「待补证据」，不能当作满足；
* 未知参数不能作为满足条件的证据；
* 价格缺失标「待确认」，不按 0 元计算。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Iterable, Sequence

from .attributes import Attribute
from .units import Interval, comparable, describe, normalize_scalar, spans

_NUMERIC_OPS = (">=", "<=", ">", "<", "==", "!=")
_TEXT_OPS = ("is", "is_not", "in", "not_in", "contains", "not_contains")
_COVER_OPS = ("cover",)

ALL_OPS = _NUMERIC_OPS + _TEXT_OPS + _COVER_OPS


@dataclass(slots=True)
class Condition:
    """一条选型条件。``hard`` 决定它是淘汰线还是偏好。"""

    field: str
    op: str = ">="
    value: Any = None
    unit: str = ""
    hard: bool = True
    reason: str = ""
    related_requirement: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, hard: bool) -> "Condition":
        op = str(raw.get("op") or ">=").strip()
        if op not in ALL_OPS:
            raise ValueError(f"unsupported operator {op!r}")
        field_name = str(raw.get("field") or "").strip()
        if not field_name:
            raise ValueError("condition requires a field")
        return cls(
            field=field_name,
            op=op,
            value=raw.get("value"),
            unit=str(raw.get("unit") or "").strip(),
            hard=bool(raw.get("hard", hard)),
            reason=str(raw.get("reason") or ""),
            related_requirement=str(raw.get("related_requirement") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "op": self.op,
            "value": self.value,
            "unit": self.unit,
            "hard": self.hard,
            "reason": self.reason,
            "related_requirement": self.related_requirement,
        }


@dataclass(slots=True)
class Verdict:
    """一条条件的判定结果。"""

    condition: Condition
    state: str
    device_value: str
    note: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.condition.field,
            "op": self.condition.op,
            "required": self.condition.value,
            "unit": self.condition.unit,
            "hard": self.condition.hard,
            "state": self.state,
            "device_value": self.device_value,
            "note": self.note,
            "related_requirement": self.condition.related_requirement,
        }


@dataclass(slots=True)
class Evaluation:
    """一个候选在一个选型项下的评估汇总。"""

    satisfied: list[Verdict] = dataclass_field(default_factory=list)
    violated: list[Verdict] = dataclass_field(default_factory=list)
    unknown: list[Verdict] = dataclass_field(default_factory=list)
    missing_evidence: list[str] = dataclass_field(default_factory=list)
    notes: list[str] = dataclass_field(default_factory=list)

    @property
    def excluded(self) -> bool:
        """违反硬性条件即淘汰。"""

        return any(v.condition.hard for v in self.violated)

    @property
    def hard_unknown(self) -> list[Verdict]:
        return [v for v in self.unknown if v.condition.hard]

    @property
    def fit(self) -> str:
        if self.excluded:
            return "excluded"
        if not (self.satisfied or self.violated or self.unknown):
            # 一条条件都没有：候选只是被类别召回选中，从未经过参数核对。
            # 这种情况绝不能显示成「推荐」，否则等于把未核对当成已核对。
            return "unjudged"
        if self.hard_unknown:
            return "conditional"
        if self.unknown:
            return "acceptable"
        return "recommended"

    @property
    def confidence(self) -> float:
        """置信度只反映证据完整度，不代表主观偏好。"""

        total = len(self.satisfied) + len(self.violated) + len(self.unknown)
        if total == 0:
            return 0.0
        return round(len(self.satisfied) / total, 2)

    @property
    def has_conditions(self) -> bool:
        return bool(self.satisfied or self.violated or self.unknown)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fit": self.fit,
            "has_conditions": self.has_conditions,
            "confidence": self.confidence,
            "excluded": self.excluded,
            "satisfied": [v.as_dict() for v in self.satisfied],
            "violated": [v.as_dict() for v in self.violated],
            "unknown": [v.as_dict() for v in self.unknown],
            "missing_evidence": self.missing_evidence,
            "notes": self.notes,
        }


def _required_interval(value: Any, unit: str) -> Interval | None:
    """把条件值换算到基准单位，与器件侧使用同一套规则。"""

    if isinstance(value, (list, tuple)) and len(value) == 2:
        low = normalize_scalar(value[0], unit)
        high = normalize_scalar(value[1], unit)
        if low is None or high is None:
            return None
        return Interval(
            min(low.value, high.value),
            max(low.value, high.value),
            low.unit or high.unit,
            low.unit_known or high.unit_known,
        )
    quantity = normalize_scalar(value, unit)
    if quantity is None:
        return None
    return Interval(quantity.value, quantity.value, quantity.unit, quantity.unit_known)


def _same_text(left: Any, right: Any) -> bool:
    clean = lambda text: "".join(str(text or "").split()).lower()
    return clean(left) == clean(right)


def _judge_numeric(attribute: Attribute, condition: Condition) -> Verdict:
    quantity = attribute.quantity
    device_spans = spans(quantity)
    required = _required_interval(condition.value, condition.unit)
    device_value = attribute.display_value

    if device_spans is None or required is None or quantity is None:
        return Verdict(condition, "unknown", device_value, "缺少可比较的数值")

    device_interval = Interval(
        min(device_spans),
        max(device_spans),
        quantity.unit,
        bool(quantity.unit),
    )
    if not comparable(device_interval, required):
        note = (
            f"单位不一致或未知（器件 {device_interval.unit or '无单位'}，"
            f"条件 {required.unit or '无单位'}），按红线不能判为满足"
        )
        return Verdict(condition, "unknown", device_value, note)

    low, high = device_interval.low, device_interval.high
    op = condition.op
    if op == "cover":
        ok = low <= required.low and high >= required.high
        return Verdict(
            condition,
            "satisfied" if ok else "violated",
            device_value,
            "器件能力区间覆盖需求区间" if ok else "器件能力区间不足以覆盖需求区间",
        )
    if op == ">=":
        ok = high >= required.low
    elif op == ">":
        ok = high > required.low
    elif op == "<=":
        ok = low <= required.high
    elif op == "<":
        ok = low < required.high
    elif op == "==":
        ok = low <= required.high and high >= required.low
    else:
        ok = not (low == required.low and high == required.high)
    return Verdict(
        condition,
        "satisfied" if ok else "violated",
        device_value,
        f"器件 {describe(quantity)} {op} 条件 {describe(required)}",
    )


def _judge_text(attribute: Attribute, condition: Condition) -> Verdict:
    device_value = attribute.display_value
    wanted = condition.value
    op = condition.op
    if op in ("in", "not_in"):
        options = [
            str(item)
            for item in (wanted if isinstance(wanted, (list, tuple)) else [wanted])
        ]
        hit = any(
            _same_text(device_value, option) or option.lower() in device_value.lower()
            for option in options
        )
        ok = hit if op == "in" else not hit
        return Verdict(
            condition,
            "satisfied" if ok else "violated",
            device_value,
            f"取值{'命中' if op == 'in' else '避开'}允许集合 {options}",
        )
    if op in ("contains", "not_contains"):
        hit = str(wanted or "").lower() in device_value.lower()
        ok = hit if op == "contains" else not hit
        return Verdict(
            condition,
            "satisfied" if ok else "violated",
            device_value,
            f"取值{'包含' if hit else '不包含'} {wanted!r}",
        )
    ok = _same_text(device_value, wanted)
    if op == "is_not":
        ok = not ok
    return Verdict(
        condition,
        "satisfied" if ok else "violated",
        device_value,
        f"取值比较 {wanted!r}",
    )


def judge(attribute: Attribute | None, condition: Condition) -> Verdict:
    """判定单个条件。没有这个属性就是未知，绝不是满足。"""

    if attribute is None:
        return Verdict(condition, "unknown", "—", "器件记录中没有该字段")
    if attribute.is_missing or attribute.evidence_status == "EXPECTED_BUT_MISSING":
        return Verdict(
            condition, "unknown", attribute.display_value or "—", "该字段标记为缺失"
        )
    if attribute.quantity is not None and condition.op in _NUMERIC_OPS + _COVER_OPS:
        return _judge_numeric(attribute, condition)
    if condition.op in _NUMERIC_OPS + _COVER_OPS:
        return Verdict(
            condition,
            "unknown",
            attribute.display_value or "—",
            "字段是文本描述，没有可换算的数值",
        )
    return _judge_text(attribute, condition)


def evaluate_candidate(
    attributes: dict[str, Attribute],
    *,
    hard: Sequence[Condition] = (),
    preferred: Sequence[Condition] = (),
    extra_missing: Iterable[str] = (),
) -> Evaluation:
    result = Evaluation()
    for condition in list(hard) + list(preferred):
        verdict = judge(attributes.get(condition.field), condition)
        if verdict.state == "satisfied":
            result.satisfied.append(verdict)
        elif verdict.state == "violated":
            result.violated.append(verdict)
        else:
            result.unknown.append(verdict)
            suffix = (
                f"（来自需求 {condition.related_requirement}）"
                if condition.related_requirement
                else ""
            )
            result.missing_evidence.append(f"{condition.field}：{verdict.note}{suffix}")
    result.missing_evidence.extend(str(item) for item in extra_missing)
    return result
