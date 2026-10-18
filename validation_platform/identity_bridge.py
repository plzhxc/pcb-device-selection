"""External identity bridging for netlist comparison.

参考网表（尤其 PCB extraction 格式）的器件身份是型号名或来源 token，
supplierPart 常为空；生成网表用精确 C 编号。比较前用外部身份映射
（reference core registry 的 型号 ↔ C 编号）把两侧都归一到 C 编号空间，
否则同一颗芯片会被判成两个身份（如 AGM6014A vs C3200405）。

identity map JSON 形状::

    {"aliases": {"STM32F407VET6": "C18202", "24_243_1": "C3200405", ...}}

键在匹配前做与比较器一致的 token 归一（大写、去分隔符）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Tuple

_C_NUMBER_RE = re.compile(r"^C\d+$")


def _token_key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(text or "").strip().upper()).strip("_")


def load_identity_map(path: str | Path) -> Dict[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    aliases = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(aliases, dict):
        raise ValueError(f"identity map {path} 缺少 aliases dict")
    normalized: Dict[str, str] = {}
    for key, value in aliases.items():
        token = _token_key(key)
        target = str(value or "").strip().upper()
        if token and _C_NUMBER_RE.match(target):
            normalized[token] = target
    return normalized


def apply_identity_bridge(
    netlist: Dict[str, Any], aliases: Dict[str, str]
) -> Tuple[Dict[str, Any], int]:
    """Return a copy with supplierPart canonicalized via ``aliases``.

    只在器件当前没有精确 C 编号时改写；命中依据依次为
    supplierPart（非 C 值，如型号误填）、deviceName、Manufacturer Part。
    """
    if not aliases:
        return netlist, 0
    bridged: Dict[str, Any] = {}
    applied = 0
    for dev_id, entry in (netlist or {}).items():
        props = dict((entry or {}).get("props") or {})
        supplier = str(props.get("supplierPart") or props.get("Supplier Part") or "").strip()
        if not _C_NUMBER_RE.match(supplier.upper()):
            for candidate in (
                supplier,
                props.get("deviceName"),
                props.get("Manufacturer Part"),
                # 位号通道：per-case 映射来自 reference core registry 的证据行，
                # 只对无 C 编号的一侧（参考网表）生效。
                props.get("designator"),
            ):
                token = _token_key(str(candidate or ""))
                if token and token in aliases:
                    props["supplierPart"] = aliases[token]
                    applied += 1
                    break
        new_entry = dict(entry or {})
        new_entry["props"] = props
        bridged[dev_id] = new_entry
    return bridged, applied


__all__ = ["apply_identity_bridge", "load_identity_map"]
