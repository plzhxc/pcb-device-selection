"""预设元件库的只读访问。

库文件沿用历史实现（legacy/device_selection/assets/preset_components.json），
本阶段只取两样东西：

* 成本参考：器件库本身没有任何价格字段（已核实），价格只能来自这里；
* 预设直达：连接器、按键、被动件等不需要大模型选型的类别。

红线：库里查不到价格就标「待确认」，不按 0 元算。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

# 正式位置是本阶段的 assets/；早期放在 legacy/ 里，保留作本地回退，
# 这样仓库不必连带提交归档代码。
DEFAULT_PRESET_PATH = _PACKAGE_ROOT / "assets" / "preset_components.json"
FALLBACK_PRESET_PATH = (
    _PACKAGE_ROOT / "legacy" / "device_selection" / "assets" / "preset_components.json"
)


def _resolve(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    if DEFAULT_PRESET_PATH.exists():
        return DEFAULT_PRESET_PATH
    return FALLBACK_PRESET_PATH


class PresetLibrary:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = _resolve(Path(path) if path else None)
        self._data: dict[str, Any] | None = None
        self._by_supplier: dict[str, dict[str, Any]] = {}

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            if not self.path.exists():
                self._data = {"kinds": {}, "parts": [], "module_presets": []}
            else:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            for part in self._data.get("parts", []):
                supplier = str(part.get("supplier_part") or "").upper()
                if supplier:
                    self._by_supplier[supplier] = part
        return self._data

    @property
    def available(self) -> bool:
        return self.path.exists()

    def kinds(self) -> Mapping[str, Mapping[str, Any]]:
        return self.data.get("kinds", {})

    def by_supplier_part(self, supplier_part: str) -> dict[str, Any] | None:
        _ = self.data  # 触发索引构建
        return self._by_supplier.get(str(supplier_part or "").upper())

    def price(self, supplier_part: str) -> tuple[float | None, str]:
        """返回 (单价, 来源说明)。拿不到就返回 (None, 'unconfirmed')。"""

        part = self.by_supplier_part(supplier_part)
        if part is None:
            return None, "unconfirmed"
        price = part.get("price_cny")
        if price is None:
            return None, "preset_without_price"
        try:
            return float(price), "preset_library"
        except (TypeError, ValueError):
            return None, "preset_without_price"

    def summary(self) -> dict[str, Any]:
        data = self.data
        return {
            "path": str(self.path),
            "available": self.available,
            "generated": data.get("generated"),
            "kinds": len(data.get("kinds", {})),
            "parts": len(data.get("parts", [])),
            "module_presets": len(data.get("module_presets", [])),
        }
