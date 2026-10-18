"""Canonical device type taxonomy used by netlist generation and review.

The raw ``deviceType`` field describes the physical part class.  Review and
topology comparison need a second decision: whether that part is a core IC
endpoint or only a local path element.  Keep that decision centralized here so
generation, top_port, lint, and dev tools stay consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


@dataclass(frozen=True)
class DeviceTypeSpec:
    canonical: str
    compare_type: str
    role: str
    allow_nc: bool


DEVICE_TYPE_TABLE: dict[str, DeviceTypeSpec] = {
    "resistance": DeviceTypeSpec("resistance", "R", "passive", False),
    "capacitance": DeviceTypeSpec("capacitance", "C", "passive", False),
    "inductance": DeviceTypeSpec("inductance", "L", "passive", False),
    "diode": DeviceTypeSpec("diode", "D_OR_LED", "protection_path", False),
    "tvs_diode": DeviceTypeSpec("tvs_diode", "D_OR_LED", "protection_path", False),
    "esd_protection": DeviceTypeSpec("esd_protection", "D_OR_LED", "protection_path", False),
    "triode": DeviceTypeSpec("triode", "Q", "discrete_path", False),
    "mos": DeviceTypeSpec("mos", "Q", "discrete_path", False),
    "switch": DeviceTypeSpec("switch", "SW", "passive", False),
    "oscillator": DeviceTypeSpec("oscillator", "XTAL", "passive", False),
    "fuse": DeviceTypeSpec("fuse", "FUSE", "passive", False),
    "connector": DeviceTypeSpec("connector", "CON", "system_boundary", False),
    # Audio transducers (microphones/speakers/buzzers) are local path/boundary
    # devices for the formal metrics.  Some reference netlists classify the
    # same physical parts as ``other``; compare them in the same bucket so the
    # component-count metric is not driven by label noise.
    "audio": DeviceTypeSpec("audio", "OTHER", "passive", False),
    "antenna": DeviceTypeSpec("antenna", "OTHER", "system_boundary", False),
    "chip": DeviceTypeSpec("chip", "IC", "core_ic", True),
    "module": DeviceTypeSpec("module", "IC", "core_ic", True),
    "mcu": DeviceTypeSpec("mcu", "IC", "core_ic", True),
    "processor": DeviceTypeSpec("processor", "IC", "core_ic", True),
    "controller": DeviceTypeSpec("controller", "IC", "core_ic", True),
    "wifi_module": DeviceTypeSpec("wifi_module", "IC", "core_ic", True),
    "other": DeviceTypeSpec("other", "OTHER", "unknown", False),
}

_ALIASES = {
    "resistor": "resistance",
    "r": "resistance",
    "cap": "capacitance",
    "capacitor": "capacitance",
    "c": "capacitance",
    "inductor": "inductance",
    "ferrite": "inductance",
    "ferrite_bead": "inductance",
    "bead": "inductance",
    "l": "inductance",
    "d": "diode",
    "led": "diode",
    "d_or_led": "diode",
    "esd": "esd_protection",
    "tvs": "tvs_diode",
    "tvs_array": "tvs_diode",
    "esd_array": "esd_protection",
    "bjt": "triode",
    "transistor": "triode",
    "mosfet": "mos",
    "key": "switch",
    "button": "switch",
    "crystal": "oscillator",
    "xtal": "oscillator",
    "osc": "oscillator",
    "conn": "connector",
    "header": "connector",
    "integrated_circuit": "chip",
    "ic": "chip",
}

PREFIX_TYPE_MAP = {
    "R": "resistance",
    "C": "capacitance",
    "L": "inductance",
    "FB": "inductance",
    "D": "diode",
    "LED": "diode",
    "TVS": "tvs_diode",
    "ESD": "esd_protection",
    "Q": "triode",
    "MOS": "mos",
    "SW": "switch",
    "K": "switch",
    "KEY": "switch",
    "Y": "oscillator",
    "X": "oscillator",
    "XTAL": "oscillator",
    "F": "fuse",
}
CONNECTOR_PREFIXES = {"J", "P", "JP", "CN", "CON", "CONN", "HDR", "HEADER", "FPC", "FFC"}

_STRONG_IC_MARKERS = (
    "触摸芯片",
    "电容式触摸",
    "控制器",
    "微控制器",
    "处理器",
    "microcontroller",
    "controller",
    "capacitive touch",
    "touch ic",
    "touch chip",
    "sensor ic",
    "driver ic",
    "switch ic",
    "hub ic",
    "hub controller",
    "usb hub",
    "usb switch",
    "asic",
    " mcu",
    " soc",
    " ic",
    "集线芯片",
    "集线器",
    "切换芯片",
    "开关芯片",
)
_WEAK_IC_MARKERS = ("芯片",)
_CONNECTOR_TEXT = (
    "connector",
    "conn-",
    "conn_",
    "header",
    "pin header",
    "socket",
    "receptacle",
    "wafer",
    "hdr-",
    "hdr_",
    "sh1.0",
    "xh2.54",
    "mx1.25",
    "pz254",
    "pz127",
    "fpc",
    "ffc",
    "type-c",
    "type c",
    "usb socket",
    "hdmi",
    "排针",
    "排母",
    "排座",
    "插针",
    "插座",
    "座子",
    "母座",
    "连接器",
    "接插件",
)
_KEYBOARD_SWITCH_TEXT = (
    "keyboard switch",
    "key switch",
    "hot-swap socket",
    "hot swap socket",
    "kailh pcb hot-swap",
    "机械键盘",
    "热插拔",
    "轴座",
)
_ESD_TEXT = (
    "esd",
    "electrostatic",
    "static protection",
    "protection array",
    "low capacitance protection",
    "低电容保护",
    "静电保护",
    "保护阵列",
)
_TVS_TEXT = ("tvs", "transient voltage suppressor", "浪涌保护", "瞬态抑制")
_DIODE_TEXT = ("diode", "schottky", "zener", "rectifier", "二极管", "肖特基", "稳压管")
_TRANSISTOR_TEXT = ("transistor", "triode", "bjt", "三极管", "晶体管")
_MOS_TEXT = ("mosfet", " mos ", " mos管", "场效应管")
_CORE_DRIVER_TEXT = (
    "gate driver",
    "motor driver",
    "bldc driver",
    "driver ic",
    "pmic",
    "驱动芯片",
    "栅极驱动",
    "电机驱动",
)
_INTEGRATED_PROTECTION_TEXT = (
    "protection ic",
    "protection chip",
    "battery protection ic",
    "power management ic",
    "保护ic",
    "保护芯片",
    "内置mosfet",
)
_ACTIVE_MODULE_TEXT = (
    "devkit",
    "dev kit",
    "development board",
    "core board",
    "模块",
    "开发板",
    "核心板",
)
_TRANSFORMER_TEXT = (
    "transformer",
    "xfmr",
    "flyback",
    "反激",
    "变压器",
    "pq5035",
    "epc10",
)
_OPTOCOUPLER_TEXT = (
    "optocoupler",
    "photo-coupler",
    "photocoupler",
    "opto-",
    "光耦",
)
_OPTOCOUPLER_MODEL = re.compile(
    r"\b(?:ORPC|PC|EL|LTV)[-_]?\d*817[A-Z0-9-]*\b",
    re.IGNORECASE,
)
_DISPLAY_TEXT = (
    "led matrix",
    "dot matrix",
    "点阵",
    "数码管",
)
_TRANSISTOR_ARRAY_MODEL = re.compile(
    r"\bULN(?:200[1-4]|280[1-4])\b",
    re.IGNORECASE,
)
_SENSOR_PATH_TEXT = (
    "photoresistor",
    "photo resistor",
    "light dependent resistor",
    "ldr sensor",
    "光敏电阻",
)
_PHOTORESISTOR_MODEL = re.compile(r"\bGL55\d{2}\b", re.IGNORECASE)
_CAPACITANCE_VALUE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:P|N|U|M|µ|μ)?F\s*$",
    re.IGNORECASE,
)
_CORE_PREFIXES = {
    "U",
    "IC",
    "CPU",
    "MCU",
    "MPU",
    "SOC",
    "FPGA",
    "LDO",
    "PMIC",
    "FLASH",
    "CODEC",
    "BOARD",
    "MODULE",
}
_PASSIVE_TEXT = (
    ("resistance", ("resistor", "resistance", " res ", "电阻")),
    ("capacitance", ("capacitor", "capacitance", " mlcc", " cap ", "电容")),
    ("inductance", ("inductor", "inductance", "ferrite", "bead", "choke", "磁珠", "电感")),
    (
        "switch",
        (
            "mechanical switch",
            "tactile switch",
            "toggle switch",
            "pushbutton",
            "轻触开关",
            "拨动开关",
            "按键开关",
        ),
    ),
    ("oscillator", ("crystal", "oscillator", "resonator", "xtal", "晶振", "谐振")),
    ("fuse", ("fuse", "ptc", "保险丝", "自恢复")),
    ("audio", ("buzzer", "speaker", "microphone", "蜂鸣器", "扬声器", "麦克风")),
    ("antenna", ("antenna", "ipex", "天线")),
)


def designator_prefix(designator: str) -> str:
    text = str(designator or "").lstrip("$").strip().upper()
    match = re.match(r"([A-Z]+)", text)
    return match.group(1) if match else ""


def props_text(props: dict[str, Any]) -> str:
    keys = (
        "deviceName",
        "DeviceName",
        "device_name",
        "Name",
        "Manufacturer Part",
        "LCSC Part Name",
        "deviceNotes",
        "description",
        "Description",
        "value",
        "Value",
        "Type",
        "deviceType",
        "device_type",
        "Supplier Footprint",
        "FootprintName",
    )
    return " ".join(str(props.get(key) or "") for key in keys).lower()


def canonical_device_type(props: dict[str, Any], *, pin_count: int | None = None) -> str:
    """Infer the canonical physical device type from props.

    Protection arrays may have U designators and chip packages, but they are not
    core IC endpoints for topology review.  Detect them before the generic U/IC
    rule.
    """
    if not isinstance(props, dict):
        return "other"
    physical_type = str(props.get("_physical_device_type") or "").strip()
    if physical_type:
        return canonical_type_name(physical_type)
    current = canonical_type_name(str(props.get("deviceType") or props.get("device_type") or ""))
    prefix = designator_prefix(str(props.get("designator") or props.get("Designator") or ""))
    text = f" {props_text(props)} "
    core_current = current in {"chip", "module", "mcu", "processor", "controller", "wifi_module"}

    # Explicit physical connector metadata is stronger evidence than words in
    # a module note. Connector templates often mention adjacent ESD parts;
    # those notes must not turn a multi-pin socket into a two-pin clamp.
    if current == "connector":
        return "connector"
    if prefix == "ESD" or any(token in text for token in _ESD_TEXT):
        return "esd_protection"
    if prefix == "TVS" or any(token in text for token in _TVS_TEXT):
        return "tvs_diode"
    if (
        prefix in _CORE_PREFIXES
        and pin_count is not None
        and pin_count >= 3
        and any(token in text for token in _INTEGRATED_PROTECTION_TEXT)
    ):
        return current if core_current else "chip"
    if prefix in CONNECTOR_PREFIXES or any(token in text for token in _CONNECTOR_TEXT):
        if not (
            any(marker in text for marker in _STRONG_IC_MARKERS)
            or any(token in text for token in _CORE_DRIVER_TEXT)
        ):
            return "connector"
    if any(token in text for token in _DISPLAY_TEXT):
        return "diode"
    if any(token in text for token in _TRANSFORMER_TEXT):
        return "other"
    if (
        any(token in text for token in _OPTOCOUPLER_TEXT)
        or _OPTOCOUPLER_MODEL.search(text)
    ):
        return "other"
    if _TRANSISTOR_ARRAY_MODEL.search(text):
        return "triode"
    if (
        any(token in text for token in _SENSOR_PATH_TEXT)
        or _PHOTORESISTOR_MODEL.search(text)
    ):
        # LDR/photoresistors are sensors whose board role is not equivalent to
        # a deterministic fixed-value R fixture. Keep them as diagnostic path
        # devices instead of inflating the formal resistor count.
        return "other"
    raw_value = str(props.get("value") or props.get("Value") or "").strip()
    if not core_current and _CAPACITANCE_VALUE.fullmatch(raw_value):
        # Reference sheets sometimes use compound designators such as CR1.
        # A physical capacitance unit is stronger evidence than the leading R.
        return "capacitance"
    if prefix in _CORE_PREFIXES and (
        core_current
        or any(token in text for token in _CORE_DRIVER_TEXT)
        or any(marker in text for marker in _STRONG_IC_MARKERS)
    ):
        return current if core_current else "chip"
    if prefix in {"D", "LED"} or any(token in text for token in _DIODE_TEXT):
        return "diode"
    if any(token in text for token in _KEYBOARD_SWITCH_TEXT):
        return "switch"
    if prefix in PREFIX_TYPE_MAP:
        return PREFIX_TYPE_MAP[prefix]
    for dtype, tokens in _PASSIVE_TEXT:
        if any(token in text for token in tokens):
            return dtype
    if re.search(r"(?:^|[ _/-])C\d{3,4}(?:$|[ _/-])", text):
        return "capacitance"
    if re.search(r"(?:^|[ _/-])R\d{3,4}(?:$|[ _/-])", text):
        return "resistance"
    connector_like = (
        prefix in CONNECTOR_PREFIXES
        or any(token in text for token in _CONNECTOR_TEXT)
    )
    if (
        prefix in {"BOARD", "MODULE"}
        or any(token in text for token in _ACTIVE_MODULE_TEXT)
    ) and pin_count is not None and pin_count >= 3:
        return "module"
    if core_current:
        return current
    if (
        prefix in _CORE_PREFIXES
        or (pin_count is not None and pin_count >= 4)
    ) and any(marker in text for marker in _STRONG_IC_MARKERS):
        return "chip"
    if (
        prefix in _CORE_PREFIXES
        and pin_count is not None
        and pin_count >= 4
        and current in {"switch", "audio", "other"}
    ):
        return "chip"
    if current in DEVICE_TYPE_TABLE and current not in {
        "chip", "module", "mcu", "processor", "controller", "wifi_module", "other"
    }:
        return current
    if any(token in text for token in ("buzzer", "speaker", "microphone", "蜂鸣器", "扬声器", "麦克风")):
        return "audio"
    if connector_like:
        return "connector"
    if prefix == "MOS" or any(token in text for token in _MOS_TEXT):
        return "mos"
    if prefix == "Q" or any(token in text for token in _TRANSISTOR_TEXT):
        return "triode"

    if prefix in _CORE_PREFIXES and pin_count is not None and pin_count <= 2:
        return "other"
    if prefix in _CORE_PREFIXES:
        return "chip"
    if any(marker in text for marker in _STRONG_IC_MARKERS):
        return "chip"
    if any(marker in text for marker in _WEAK_IC_MARKERS):
        return "chip"
    if pin_count is not None and pin_count >= 8 and any(token in text for token in ("controller", " ic ", "soc", "mcu")):
        return "chip"
    # 域前缀位号回退（放在所有关键字规则之后）：主板类设计常用
    # PC/EC/MC=电容、PR/MR/SR=电阻、PL=电感、PQ=MOS/三极管 等
    # "电源域字母 + 类型字母" 位号；全前缀不在映射表且以上判型全部
    # 落空时，按前缀末字母回退，否则这些真实 R/C/L/Q 会被判成 other。
    if (
        len(prefix) >= 2
        and prefix not in PREFIX_TYPE_MAP
        and prefix not in CONNECTOR_PREFIXES
    ):
        if prefix[-1] in {"R", "C", "L", "Q"}:
            return PREFIX_TYPE_MAP[prefix[-1]]
        # 域+类型+修饰 的三字母变体（如 PCT = P 域极性/钽电容）：
        # 末字母是修饰符时看倒数第二位的类型字母。
        if len(prefix) >= 3 and prefix[-2] in {"R", "C", "L", "Q"}:
            return PREFIX_TYPE_MAP[prefix[-2]]
    return current if current in DEVICE_TYPE_TABLE else "other"


def canonical_type_name(raw_type: str) -> str:
    raw = str(raw_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return "other"
    return _ALIASES.get(raw, raw if raw in DEVICE_TYPE_TABLE else "other")


def normalize_props_device_type(props: dict[str, Any], *, pin_count: int | None = None) -> bool:
    """Normalize ``props['deviceType']`` in place. Returns True if changed."""
    if not isinstance(props, dict):
        return False
    canonical = canonical_device_type(props, pin_count=pin_count)
    old = str(props.get("deviceType") or "")
    props["deviceType"] = canonical
    return old != canonical


def compare_type_for_props(props: dict[str, Any]) -> str:
    return DEVICE_TYPE_TABLE.get(canonical_device_type(props), DEVICE_TYPE_TABLE["other"]).compare_type


def is_core_ic_endpoint_props(props: dict[str, Any]) -> bool:
    return DEVICE_TYPE_TABLE.get(canonical_device_type(props), DEVICE_TYPE_TABLE["other"]).role == "core_ic"


def is_connector_props(props: dict[str, Any]) -> bool:
    return canonical_device_type(props) == "connector"


def is_hard_discrete_or_protection_props(props: dict[str, Any]) -> bool:
    dtype = canonical_device_type(props)
    return DEVICE_TYPE_TABLE.get(dtype, DEVICE_TYPE_TABLE["other"]).role in {
        "passive",
        "protection_path",
        "discrete_path",
    }
