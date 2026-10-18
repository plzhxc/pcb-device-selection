"""兼容入口：把 case reference 目录内的 ``*.enet``（多页原理图）合并成
统一格式的 netlist dict（与 gen netlist 的 gge<N> schema 兼容）。

.enet 文件格式（嘉立创 EDA 导出）：
{
  "version": "2.0.0",
  "components": {
    "gge<N>": {
      "props": {
        "Designator": "U1", "DeviceName": "...", "Supplier Part": "C...",
        "Manufacturer Part": "...", "Description": "...", ...
      },
      "pinInfoMap": {
        "<pin>": {"name": "...", "number": "...", "net": "<net_name>", ...},
        ...
      }
    },
    ...
  }
}

输出格式（兼容我们的 gen netlist）:
{
  "gge<N>": {
    "props": {
      "designator": "U1",
      "deviceName": "STM32F405RGT6",
      "supplierPart": "C15742",        # 注意：sm camel case 跟 ref 一致
      "value": "...",
      "deviceType": "chip",
      "deviceNotes": "...",
      "_sheet": "Schematic4"           # 额外字段：来自哪一页
    },
    "pins": {"1": "VBAT", "2": "PC13", ...}
  }
}
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from validation_platform.reference.enet import (
    classify_device_type as _reference_classify_device_type,
    parse_enet_sheet as _parse_reference_enet_sheet,
)


def _classify_device_type(props: Dict[str, Any], *, pin_count: int = 0) -> str:
    """从 enet props 推断标准 deviceType。"""
    return _reference_classify_device_type(props, pin_count=pin_count)


def parse_enet_sheet(path: str) -> Dict[str, Dict[str, Any]]:
    """解析单个 .enet 文件，返回 gge<N> → {props, pins} dict。
    keys 用 sheet 名作前缀避免跨表冲突。
    """
    return _parse_reference_enet_sheet(path)


def parse_all_sheets(ref_dir: str) -> Dict[str, Dict[str, Any]]:
    """合并目录下所有 .enet 文件。"""
    combined: Dict[str, Dict[str, Any]] = {}
    for path in sorted(Path(ref_dir).glob("*.enet")):
        combined.update(parse_enet_sheet(str(path)))
    return combined


def list_main_ics(netlist: Dict[str, Dict[str, Any]], min_pin: int = 4) -> List[Dict[str, Any]]:
    """从合并 netlist 提取核心 IC 清单（按 supplier_part 分组）。"""
    by_sp: Dict[str, Dict[str, Any]] = {}
    for gge, dev in netlist.items():
        p = dev.get("props", {})
        sp = p.get("supplierPart", "")
        if not sp or not sp.startswith("C"):
            continue
        # 过滤：被动器件 + connector 跳过
        dt = p.get("deviceType", "")
        if dt in ("capacitance", "resistance", "inductance"):
            continue
        if len(dev.get("pins", {})) < min_pin:
            continue
        if sp not in by_sp:
            by_sp[sp] = {
                "supplier_part": sp,
                "device_name":   p.get("deviceName", ""),
                "device_type":   dt,
                "pin_count":     len(dev.get("pins", {})),
                "designators":   [],
                "sheets":        set(),
                "notes":         p.get("deviceNotes", "")[:120],
            }
        by_sp[sp]["designators"].append(p.get("designator", ""))
        by_sp[sp]["sheets"].add(p.get("_sheet", ""))
    out = []
    for sp, info in sorted(by_sp.items()):
        info["sheets"] = sorted(info["sheets"])
        info["quantity"] = len(info["designators"])
        out.append(info)
    return out


def device_count_summary(netlist: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """概览：按 deviceType 计数 + total。"""
    from collections import Counter
    type_counts = Counter()
    for gge, dev in netlist.items():
        type_counts[dev.get("props", {}).get("deviceType", "other")] += 1
    return {"total": len(netlist), "by_type": dict(type_counts)}
