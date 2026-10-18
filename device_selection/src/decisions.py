"""用户决定的持久化。

设计文档要求「保存用户的选择及理由，形成已确认器件清单」，而且资料核验状态
与用户选择状态要分开记录。所以决定单独存一份文件，不混进候选证据里：

* 证据（`CandidateSet`）由检索和判定产生，本阶段不会改写；
* 决定（本文件）由用户操作产生，只有记录和撤销，不影响证据。

键用 `选型项编号::型号`，同一个型号出现在不同选型项下也不会串。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "pcbagent.device_decisions.v1"

# 只有「已确认」才算最终选定，可以进入下游。
# 「保留」只是进入候选短名单继续比较，还没有拍板——它和「已确认」必须区分开，
# 否则等于替用户做了决定。
SELECTED = ("confirmed",)
SHORTLIST = ("keep",)
PENDING = ("discuss",)
REJECTED = ("exclude",)
ALL_DECISIONS = ("keep", "exclude", "discuss", "confirmed")

_LABEL = {"keep": "保留", "exclude": "排除", "discuss": "待讨论", "confirmed": "已确认"}


def key_of(item_id: str, model: str) -> str:
    return f"{str(item_id).strip()}::{str(model).strip()}"


def label_of(decision: str) -> str:
    return _LABEL.get(str(decision), str(decision))


class DecisionStore:
    """决定的读写。文件不存在时视为空。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._entries: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        self._entries = {}
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if not isinstance(payload, Mapping):
            return
        entries = payload.get("decisions")
        if isinstance(entries, Mapping):
            self._entries = {
                str(k): dict(v)
                for k, v in entries.items()
                if isinstance(v, Mapping)
            }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SCHEMA,
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "decisions": self._entries,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ------------------------------------------------------------------ 读写
    def record(
        self,
        *,
        item_id: str,
        model: str,
        decision: str,
        reason: str = "",
        origin: str = "manual",
        needs_review: str = "",
    ) -> dict[str, Any]:
        if decision not in ALL_DECISIONS:
            raise ValueError(f"decision must be one of {ALL_DECISIONS}")
        key = key_of(item_id, model)
        entry = {
            "item_id": str(item_id),
            "model": str(model),
            "decision": decision,
            "decision_label": label_of(decision),
            "reason": str(reason or ""),
            # origin: manual 人工点选 / auto 一键确认
            # needs_review: 非空表示"选上了但结论可疑"，界面要标出来
            "origin": str(origin or "manual"),
            "needs_review": str(needs_review or ""),
            "decided_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
        self._entries[key] = entry
        self.save()
        return entry

    def clear(self, *, item_id: str, model: str) -> None:
        self._entries.pop(key_of(item_id, model), None)
        self.save()

    def all(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._entries.items()}

    def for_item(self, item_id: str) -> dict[str, dict[str, Any]]:
        return {
            str(v.get("model") or ""): dict(v)
            for v in self._entries.values()
            if str(v.get("item_id")) == str(item_id)
        }

    def selected(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self._entries.values() if v.get("decision") in SELECTED]

    def bulk(self, entries: Iterable[Mapping[str, Any]]) -> int:
        """按 `选型项::型号` 的扁平形式批量写入，用于前端一次性同步。"""

        count = 0
        for raw in entries:
            item_id = str(raw.get("item_id") or "")
            model = str(raw.get("model") or "")
            decision = str(raw.get("decision") or "")
            if not item_id or not model or decision not in ALL_DECISIONS:
                continue
            self._entries[key_of(item_id, model)] = {
                "item_id": item_id,
                "model": model,
                "decision": decision,
                "decision_label": label_of(decision),
                "reason": str(raw.get("reason") or ""),
                "decided_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            }
            count += 1
        if count:
            self.save()
        return count
