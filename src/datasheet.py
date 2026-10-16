"""从数据手册片段里补器件参数。

存在的理由：结构化参数表里 65% 的关键参数是 `EXPECTED_BUT_MISSING`，
但这些值往往就写在数据手册正文里。本模块负责"按字段找片段、按片段取值"，
取到的值**必须带上原文引用和出处**，不能变成无源之数。

取值的红线：

* 只认能严格解析成量的片段（`3 V ~ 36 V`、`1.2 MHz`），解析不了就保持未知；
* 文不对题（片段里没有该字段的主题词）不算证据；
* 结论里一定写明来自哪一页手册，与结构化参数表的来源区分开。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Sequence

from .milvus import DatasheetLibrary, Span
from .units import Interval, Quantity, describe, parse_interval, parse_quantity

# 字段 → 手册里的中文说法。检索和取值都靠它，命中不了就如实说没找到。
FIELD_TOPICS: dict[str, tuple[str, ...]] = {
    "input_voltage_range": ("输入电压范围", "供电电压范围", "工作电压范围", "电源电压范围", "输入电压"),
    "supply_voltage": ("供电电压", "电源电压", "工作电压", "单电源", "供电范围"),
    "output_voltage": ("输出电压",),
    "output_current": ("输出电流", "持续输出电流", "最大输出电流"),
    "switching_frequency": ("开关频率", "开关电源频率", "振荡频率"),
    "operating_temperature": ("工作温度", "工作结温", "环境温度", "温度范围"),
    "gain_bandwidth_product": ("单位增益带宽", "增益带宽积", "增益带宽"),
    "quiescent_current_iq": ("静态电流", "静态电源电流", "静态工作电流"),
    "dropout_voltage": ("压差", "dropout", "跌落电压"),
    "channel_count": ("通道", "通道数", "路"),
    "package": ("封装", "封装形式", "外形"),
    "topology": ("拓扑", "降压", "升压", "开关型"),
    "resolution": ("分辨率", "位数", "bit"),
    "flash_capacity": ("闪存", "FLASH", "程序存储"),
    "ram_capacity": ("RAM", "内存", "随机存储"),
    "clock_frequency": ("主频", "最高频率", "工作频率"),
}

# 这些字段本来就是文本，不该被解析成数字
TEXT_FIELDS = frozenset({"package", "topology", "channel_count"})

# 字段期望的单位族。手册正文里数字很多，光看"带单位"不够——
# AMS1117 那条"输出电压 ... 通过一个 22µF 的电容"就会把容值当成电压。
FIELD_UNITS: dict[str, frozenset[str]] = {
    "input_voltage_range": frozenset({"V"}),
    "supply_voltage": frozenset({"V"}),
    "output_voltage": frozenset({"V"}),
    "dropout_voltage": frozenset({"V"}),
    "output_current": frozenset({"A"}),
    "quiescent_current_iq": frozenset({"A"}),
    "switching_frequency": frozenset({"Hz"}),
    "clock_frequency": frozenset({"Hz"}),
    "gain_bandwidth_product": frozenset({"Hz"}),
    "operating_temperature": frozenset({"°C"}),
    "flash_capacity": frozenset({"bit", "B"}),
    "ram_capacity": frozenset({"bit", "B"}),
}

# 检索用的主题词（BM25 查询串）
_SEARCH_TOPICS: dict[str, str] = {
    "input_voltage_range": "输入电压范围 供电电压范围 VIN",
    "supply_voltage": "供电电压 工作电压 电源电压",
    "output_voltage": "输出电压 VOUT",
    "output_current": "输出电流 持续输出电流 IOUT",
    "switching_frequency": "开关频率 振荡频率",
    "operating_temperature": "工作温度 结温 环境温度",
    "gain_bandwidth_product": "单位增益带宽 增益带宽",
    "quiescent_current_iq": "静态电流 静态电源电流",
    "dropout_voltage": "压差 dropout",
    "channel_count": "通道 通道数",
    "package": "封装 封装形式",
}

_LATEX = (
    (re.compile(r"\\mathrm\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\text\{([^}]*)\}"), r"\1"),
    (re.compile(r"\\sim"), "~"),
    (re.compile(r"\\pm"), "±"),
    (re.compile(r"\\Omega"), "Ω"),
    (re.compile(r"\\mu"), "µ"),
    (re.compile(r"\\,"), " "),
    (re.compile(r"\\;"), " "),
    (re.compile(r"\\[a-zA-Z]+"), " "),
    (re.compile(r"\$+"), ""),
    (re.compile(r"[ \t]{2,}"), " "),
)


def clean_text(text: str) -> str:
    """把 Markdown/LaTeX 外壳去掉，只留可读内容。"""

    out = str(text or "")
    for pattern, replacement in _LATEX:
        out = pattern.sub(replacement, out)
    out = re.sub(r"[#*_`>]", "", out)
    return out.strip()


@dataclass(slots=True)
class DatasheetFinding:
    """一次手册取数的结果。``found`` 为假时只保留引用，供人工查看。"""

    field_key: str
    found: bool
    quantity: Quantity | Interval | None
    text: str
    span: Span | None
    note: str = ""

    @property
    def is_text_field(self) -> bool:
        return self.field_key in TEXT_FIELDS

    @property
    def display(self) -> str:
        if self.text:
            return self.text
        return describe(self.quantity)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field_key": self.field_key,
            "found": self.found,
            "value": describe(self.quantity),
            "text": self.text,
            "source": (self.span.as_dict() | {"title": self.span.title}) if self.span else None,
            "note": self.note,
        }


def _lines(text: str) -> list[str]:
    parts: list[str] = []
    for raw in re.split(r"[\n。；;]+", clean_text(text)):
        line = raw.strip(" -·•\t")
        if line:
            parts.append(line)
    return parts


def _accept(field_key: str, quantity: Quantity | Interval | None) -> bool:
    """数值型字段只接受**带单位**的解析结果。

    手册正文里全是型号数字（LM358、SOP-8、TPS5450），宽松解析会把它们当成
    参数值。要求单位已知，并且落在该字段期望的单位族里，才能挡掉这类误命中。
    """

    if quantity is None:
        return False
    if not quantity.unit:
        return False
    expected = FIELD_UNITS.get(field_key)
    if expected and quantity.unit not in expected:
        return False
    return True


# 形如 "-40 125 °C" 的空格分隔范围：两个数，最后一个词是单位
_SPACED_RANGE = re.compile(
    r"(?P<low>[+-]?\d+(?:\.\d+)?)\s+(?P<high>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>[a-zA-ZµμΩ°℃/%]{1,6})\b"
)


def _spaced_range(text: str) -> Interval | None:
    """解析「-40 125 °C」这种没写分隔符的范围。"""

    for match in _SPACED_RANGE.finditer(text):
        unit = match.group("unit")
        resolved = parse_quantity(f"1 {unit}")
        if resolved is None or not resolved.unit:
            continue
        try:
            low = float(match.group("low")) * 1.0
            high = float(match.group("high")) * 1.0
        except ValueError:
            continue
        # 用同一套单位换算把两个端点收敛到基准单位
        scaled_low = parse_quantity(f"{match.group('low')} {unit}")
        scaled_high = parse_quantity(f"{match.group('high')} {unit}")
        if scaled_low is None or scaled_high is None:
            continue
        if not scaled_low.unit_known or not scaled_high.unit_known:
            continue
        del low, high
        return Interval(
            min(scaled_low.value, scaled_high.value),
            max(scaled_low.value, scaled_high.value),
            scaled_low.unit,
            True,
        )
    return None


def find_in_span(field_key: str, span: Span) -> DatasheetFinding | None:
    """在一个片段里找该字段。"""

    topics = FIELD_TOPICS.get(field_key)
    if not topics:
        return None
    fallback: DatasheetFinding | None = None
    for line in _lines(span.content):
        if not any(topic in line for topic in topics):
            continue
        snippet = line[:220]
        if field_key in TEXT_FIELDS:
            return DatasheetFinding(field_key, True, None, snippet, span)
        quantity = parse_interval(snippet) or parse_quantity(snippet)
        if not _accept(field_key, quantity):
            # 退一步：可能是没写分隔符的范围，例如「-40 125 °C」
            spaced = _spaced_range(snippet)
            if _accept(field_key, spaced):
                quantity = spaced
            else:
                if fallback is None:
                    fallback = DatasheetFinding(
                        field_key, False, None, snippet, span, "片段提到了该参数但没有可解析的数值"
                    )
                continue
        if not _accept(field_key, quantity):
            if fallback is None:
                fallback = DatasheetFinding(
                    field_key, False, None, snippet, span, "片段提到了该参数但没有可解析的数值"
                )
            continue
        return DatasheetFinding(field_key, True, quantity, snippet, span)
    return fallback


def lookup(
    library: DatasheetLibrary,
    c_number: str,
    field_key: str,
    *,
    span_limit: int = 24,
) -> DatasheetFinding:
    """按字段去手册里找值。找不到就返回 found=False 并说明原因。"""

    if not c_number:
        return DatasheetFinding(field_key, False, None, "", None, "器件没有立创编号，无法定位手册")
    query = _SEARCH_TOPICS.get(field_key, "")
    spans: list[Span] = []
    if query:
        # 只取 BM25 前几段会漏掉真正写着参数的页面（实测漏掉约一半），
        # 所以按字段查时把召回量放大。
        spans = library.spans(c_number, query_text=query, limit=span_limit)
    if not spans:
        spans = library.spans(c_number, limit=span_limit)
    if not spans:
        return DatasheetFinding(
            field_key, False, None, "", None, f"手册库里没有 {c_number} 的片段"
        )
    for span in spans:
        finding = find_in_span(field_key, span)
        if finding is not None:
            return finding
    return DatasheetFinding(
        field_key, False, None, "", spans[0], "手册片段里没有出现该参数的说法"
    )


def coverage(library: DatasheetLibrary, c_numbers: Iterable[str]) -> dict[str, int]:
    """统计一批器件在手册库里的覆盖情况。"""

    total = hit = 0
    missing: list[str] = []
    for c_number in c_numbers:
        total += 1
        if library.identity(c_number)["ok"]:
            hit += 1
        else:
            missing.append(str(c_number))
    return {"total": total, "with_datasheet": hit, "without": missing[:20]}


def to_attribute(finding: DatasheetFinding):
    """把手册取到的值包装成器件属性，供判定复用。

    证据状态标成 `DATASHEET`，与结构化参数表的 PROVEN / EXPECTED_BUT_MISSING
    区分开——来源不同，可信度不同，界面上也要看得出来。
    """

    from .attributes import Attribute  # 局部导入避免循环

    span = finding.span
    locator = f"{span.database}.DataSheet:{span.span_id}" if span else ""
    return Attribute(
        field_key=finding.field_key,
        name_zh=finding.field_key,
        text=finding.text,
        raw_number=None,
        raw_unit="",
        quantity=finding.quantity,
        parse_source="datasheet",
        priority_rank=9999,
        importance=0,
        is_required=False,
        is_missing=False,
        evidence_status="DATASHEET",
        evidence_basis=(span.title or "数据手册片段") if span else "数据手册",
        source_locator=locator,
        needs_review=True,
    )
