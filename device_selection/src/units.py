"""数值与单位归一化。

这是选型阶段最容易被忽略、也最容易出错的一环。实测这个库的现状是：
130871 个 field_value_num 里有 127863 个没有 field_unit（98%），而且数值本身
也不一定是基准单位——例如 TPS5450 的开关频率文本是「500kHz」，落到
field_value_num 却是 500 而 field_unit 写着 Hz。

所以规则是：**以文本为准**。能从 field_value_text 解析出带单位的值，就用解析
结果；只有文本不可解析时才退回 field_value_num，并在没有单位时标记为
unit_unknown——按设计红线，这种值不能作为"满足"的证据。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Sequence

# 前缀 → 相对基准单位的倍率
_SI_PREFIX: dict[str, float] = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,
    "μ": 1e-6,
    "m": 1e-3,
    "": 1.0,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}

# 单位别名 → (基准单位, 相对基准的倍率)
_UNIT_ALIAS: dict[str, tuple[str, float]] = {
    "v": ("V", 1.0),
    "vdc": ("V", 1.0),
    "vac": ("V", 1.0),
    "kv": ("V", 1e3),
    "mv": ("V", 1e-3),
    "a": ("A", 1.0),
    "ma": ("A", 1e-3),
    "ua": ("A", 1e-6),
    "µa": ("A", 1e-6),
    "μa": ("A", 1e-6),
    "hz": ("Hz", 1.0),
    "khz": ("Hz", 1e3),
    "mhz": ("Hz", 1e6),
    "ghz": ("Hz", 1e9),
    "f": ("F", 1.0),
    "pf": ("F", 1e-12),
    "nf": ("F", 1e-9),
    "uf": ("F", 1e-6),
    "µf": ("F", 1e-6),
    "μf": ("F", 1e-6),
    "mf": ("F", 1e-3),
    "h": ("H", 1.0),
    "uh": ("H", 1e-6),
    "µh": ("H", 1e-6),
    "μh": ("H", 1e-6),
    "nh": ("H", 1e-9),
    "mh": ("H", 1e-3),
    "ohm": ("Ω", 1.0),
    "Ω": ("Ω", 1.0),
    "ω": ("Ω", 1.0),
    "kohm": ("Ω", 1e3),
    "kω": ("Ω", 1e3),
    "mohm": ("Ω", 1e-3),
    "mω": ("Ω", 1e-3),
    "mohm_": ("Ω", 1e-3),
    "w": ("W", 1.0),
    "mw": ("W", 1e-3),
    "kw": ("W", 1e3),
    "c": ("°C", 1.0),
    "°c": ("°C", 1.0),
    "℃": ("°C", 1.0),
    "s": ("s", 1.0),
    "ms": ("s", 1e-3),
    "us": ("s", 1e-6),
    "µs": ("s", 1e-6),
    "ns": ("s", 1e-9),
    "%": ("%", 1.0),
    "bit": ("bit", 1.0),
    "bits": ("bit", 1.0),
    "mhz_": ("Hz", 1e6),
}

_NUMBER = r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
# 形如「5.5V」「500kHz」「1.8V~3.3V」「-40°C~125°C」「4.7uF」「2.2kΩ」
_SCALAR = re.compile(
    rf"(?P<num>{_NUMBER})\s*(?P<unit>[a-zA-ZµμΩ°℃%/]{{0,8}})"
)
# 范围分隔符：~ ～ — – ... 以及中英文的「到 / 至 / to」。
# 中文手册里「7 V 至 44 V」「4.5V 到 5.5V」很常见，漏掉会直接取不到值。
_RANGE_SPLIT = re.compile(
    r"\s*(?:~|～|—|–|\bto\b|\bto~?\b|至|到|\.\.\.|\s-\s)\s*", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class Quantity:
    """一个归一化后的量。``unit`` 为空表示原文没有单位，比较时不可信。"""

    value: float
    unit: str
    source: str  # "text" | "column"
    unit_known: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "unit_known": self.unit_known,
        }


@dataclass(frozen=True, slots=True)
class Interval:
    """区间量，``low``/``high`` 已换算到基准单位。"""

    low: float
    high: float
    unit: str
    unit_known: bool

    @property
    def covered(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "low": self.low,
            "high": self.high,
            "unit": self.unit,
            "unit_known": self.unit_known,
        }


def _canonical_unit(raw: str) -> tuple[str, float] | None:
    if raw is None:
        return None
    key = str(raw).strip()
    if not key:
        return ("", 1.0)
    if key in _UNIT_ALIAS:
        return _UNIT_ALIAS[key]
    lowered = key.lower()
    if lowered in _UNIT_ALIAS:
        return _UNIT_ALIAS[lowered]
    # 前缀 + 基准单位，例如 kV / mΩ / nF
    for prefix, factor in sorted(_SI_PREFIX.items(), key=lambda kv: -len(kv[0])):
        if prefix and lowered.startswith(prefix.lower()):
            rest = lowered[len(prefix):]
            if rest in _UNIT_ALIAS:
                base, multiple = _UNIT_ALIAS[rest]
                return (base, factor * multiple)
    return None


def parse_quantity(text: Any, *, strict: bool = False) -> Quantity | None:
    """从一段文本里取第一个带单位的数值。

    ``strict=True`` 时要求整段文本就是一个量（例如 `5A`、`500kHz`），
    用来避免把 `SOP-8-EP-150mil` 这种封装字符串里的 `-8` 当成数值。
    """

    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    match = _SCALAR.search(raw)
    if not match:
        return None
    if strict and (raw[: match.start()].strip() or raw[match.end():].strip()):
        return None
    try:
        value = float(match.group("num"))
    except ValueError:
        return None
    unit_text = match.group("unit") or ""
    resolved = _canonical_unit(unit_text)
    if resolved is None:
        return Quantity(value=value, unit=unit_text, source="text", unit_known=False)
    base, factor = resolved
    return Quantity(value=value * factor, unit=base, source="text", unit_known=bool(base))


def parse_interval(text: Any, *, strict: bool = False) -> Interval | None:
    """解析形如 5.5V~36V / -40°C~125°C 的区间。"""

    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None
    parts = [p for p in _RANGE_SPLIT.split(raw) if p.strip()]
    if len(parts) < 2:
        quantity = parse_quantity(raw, strict=strict)
        if quantity is None:
            return None
        return Interval(quantity.value, quantity.value, quantity.unit, quantity.unit_known)
    low = parse_quantity(parts[0], strict=strict)
    # 第二个端点常省略单位（「-40~125°C」），借用第一个端点的单位补全
    high = parse_quantity(parts[1], strict=strict)
    if low is None or high is None:
        return None
    if strict and len(parts) > 2:
        return None
    if not high.unit_known and low.unit_known:
        factor = _canonical_unit(low.unit)
        if factor is not None:
            high = Quantity(high.value * factor[1], low.unit, "text", True)
    return Interval(
        min(low.value, high.value),
        max(low.value, high.value),
        low.unit if low.unit_known else high.unit,
        low.unit_known or high.unit_known,
    )


def from_columns(
    value_num: Any,
    value_text: Any,
    unit: Any,
) -> tuple[Quantity | Interval | None, str]:
    """字段取值的统一入口。

    返回 (解析结果, 判定依据)。依据取值为 text / column / text_range /
    column_without_unit / none，供上层解释"这个结论是从哪来的"。
    """

    # 器件属性一律按严格模式解析：文本要么整体是一个量，要么就不是数值。
    interval = parse_interval(value_text, strict=True)
    if interval is not None:
        return interval, "text_range"
    quantity = parse_quantity(value_text, strict=True)
    if quantity is not None:
        return quantity, "text"
    if value_num is None:
        return None, "none"
    try:
        number = float(value_num)
    except (TypeError, ValueError):
        return None, "none"
    resolved = _canonical_unit(unit) if unit else None
    if resolved is None:
        return Quantity(number, str(unit or ""), "column", False), "column_without_unit"
    base, factor = resolved
    return Quantity(number * factor, base, "column", bool(base)), "column"


def comparable(left: Quantity | Interval | None, right: Quantity | Interval | None) -> bool:
    """两个量能不能直接比大小：单位必须都已知且一致。"""

    if left is None or right is None:
        return False
    left_unit = left.unit
    right_unit = right.unit
    return bool(left_unit) and left_unit == right_unit


def normalize_scalar(value: Any, unit: Any) -> Quantity | None:
    """把「数值 + 单位字符串」换算到基准单位。

    条件里的单位是用户或上游写的自然写法（`mA`、`kHz`），必须和器件侧走
    同一套换算，否则两边单位字符串不一致会被误判成"未知"。
    """

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    resolved = _canonical_unit(str(unit or ""))
    if resolved is None:
        # 认不出的单位：保留原文字符串，但标记为不可信
        return Quantity(number, str(unit or ""), "text", False)
    base, factor = resolved
    return Quantity(number * factor, base, "text", bool(base))


def spans(quantity: Quantity | Interval | None) -> tuple[float, float] | None:
    if quantity is None:
        return None
    if isinstance(quantity, Interval):
        return (quantity.low, quantity.high)
    return (quantity.value, quantity.value)


def describe(quantity: Quantity | Interval | None) -> str:
    if quantity is None:
        return "—"
    if isinstance(quantity, Interval):
        unit = quantity.unit or "?"
        return f"{_trim(quantity.low)}~{_trim(quantity.high)} {unit}"
    unit = quantity.unit or "?"
    return f"{_trim(quantity.value)} {unit}"


def _trim(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def merge_units(items: Iterable[str]) -> str:
    seen: Sequence[str] = sorted({u for u in items if u})
    return seen[0] if len(seen) == 1 else ""
