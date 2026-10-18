"""项目工作区：把上游工作台的项目变成本阶段可以直接打开的任务。

以前本阶段一次只能跑一个 spec 文件，要换项目就得改命令行参数。现在改成
"项目"模型：界面上点一个项目，后台自动去工作台取最新交接包、跑选型、把结果
缓存到磁盘；再打开同一个项目就直接读缓存，不重复跑。

磁盘结构：

    output/projects/<project_id>/
        meta.json               项目元信息、上游版本、最后一次运行时间
        requirement-spec.in.json  上游交接包里的原始 spec
        selection-spec.json      转换后本阶段输入
        candidate-set.json       候选清单
        decisions.json           用户决定（与证据分开存）
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from .chenxin import apply_result, to_selection_spec, validate_with_chenxin
from .catalog import DeviceCatalog
from .decisions import DecisionStore
from .service import SelectionService


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class ProjectEntry:
    """一个项目在本阶段的状态。"""

    project_id: str
    title: str = ""
    remote_version: int = 0
    remote_revision: int = 0
    # new 未打开 | loaded 已取到交接件但未跑选型 | running 正在跑
    # ready 选型完成 | waiting 上游还没产出选型项 | error 真出错
    status: str = "new"
    error: str = ""
    opened_at: str = ""
    elapsed_s: float = 0.0
    spec: dict[str, Any] = dataclass_field(default_factory=dict)
    result: dict[str, Any] = dataclass_field(default_factory=dict)
    source_meta: dict[str, Any] = dataclass_field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        stats = self.result.get("summary") or {}
        items = stats.get("total_items")
        if items is None:
            items = len(self.spec.get("selection_items") or [])
        return {
            "project_id": self.project_id,
            "title": self.title,
            "status": self.status,
            "error": self.error,
            "opened_at": self.opened_at,
            "elapsed_s": self.elapsed_s,
            "remote_version": self.remote_version,
            "remote_revision": self.remote_revision,
            "items": items,
            "runnable": bool(self.spec.get("selection_items")) and self.status != "waiting",
            "recommended": stats.get("items_with_recommendation", 0),
            "unverified": stats.get("items_unverified", 0),
            "blocked": stats.get("items_blocked", 0),
            "unresolved": self.unresolved_conditions(),
        }

    def unresolved_conditions(self) -> int:
        """有多少条约束没能变成判定条件——这个数直接决定覆盖率。"""

        converted = 0
        for item in self.spec.get("selection_items") or []:
            converted += len(item.get("hard_conditions") or [])
            converted += len(item.get("preferred_conditions") or [])
        raw = self.source_meta.get("raw_constraints") or 0
        return max(int(raw) - converted, 0)


class ProjectStore:
    """项目集合。远端列表来自工作台，结果缓存在本地。"""

    def __init__(
        self,
        root: Path | str,
        *,
        service: SelectionService,
        remote_lister: Callable[[], list[dict[str, Any]]] | None = None,
        fetcher: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.service = service
        # 复核上游给的器件身份时要用（标「matched」不等于真的在目录里）
        self.catalog = DeviceCatalog(service.db)
        self.remote_lister = remote_lister
        self.fetcher = fetcher
        self._entries: dict[str, ProjectEntry] = {}
        self._remote: list[dict[str, Any]] = []
        self._remote_at = 0.0
        self._remote_error = ""

    # ------------------------------------------------------------------ 目录
    def dir_of(self, project_id: str) -> Path:
        safe = "".join(ch for ch in project_id if ch.isalnum() or ch in "-_")[:64]
        path = self.root / (safe or "unknown")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def remote_projects(self, *, ttl: float = 20.0) -> dict[str, Any]:
        import time

        if self.remote_lister is None:
            return {"ok": False, "error": "上游工作台未配置", "projects": []}
        if self._remote and (time.time() - self._remote_at) < ttl:
            return {"ok": True, "projects": self._remote, "cached": True}
        try:
            self._remote = list(self.remote_lister())
            self._remote_at = time.time()
            self._remote_error = ""
        except Exception as exc:  # noqa: BLE001
            self._remote_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return {
                "ok": False,
                "error": self._remote_error,
                "projects": self._remote,
            }
        return {"ok": True, "projects": self._remote}

    def listing(self) -> list[dict[str, Any]]:
        """合并远端项目与本阶段的处理状态，供界面展示。"""

        remote = self.remote_projects()
        rows: list[dict[str, Any]] = []
        for item in remote.get("projects") or []:
            project_id = str(item.get("id") or "")
            entry = self._entries.get(project_id)
            row = {
                "project_id": project_id,
                "title": str(item.get("title") or "未命名"),
                "version": item.get("version"),
                "requirement_count": item.get("requirement_count"),
                "updated_at": item.get("updated_at"),
                "is_example": bool(item.get("is_example")),
                "stage": "未处理",
                "status": "idle",
            }
            if entry is not None:
                row.update(
                    {
                        "stage": "已处理" if entry.status == "ready" else entry.status,
                        "status": entry.status,
                        "items": entry.result.get("summary", {}).get("total_items", 0),
                        "recommended": entry.result.get("summary", {}).get(
                            "items_with_recommendation", 0
                        ),
                        "unverified": entry.result.get("summary", {}).get(
                            "items_unverified", 0
                        ),
                        "opened_at": entry.opened_at,
                    }
                )
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ 打开
    def open(
        self, project_id: str, *, run: bool = True, force: bool = False
    ) -> ProjectEntry:
        """打开项目：取最新交接包 →（可选）跑选型 → 落盘。

        ``run=False`` 只把交接件取回来转换好，不跑选型；界面上由用户按按钮
        再触发，避免切换项目时反复自动跑、把对话区刷乱。
        """

        if not force:
            cached = self._entries.get(project_id)
            if cached is None:
                cached = self._load_from_disk(project_id)
                if cached is not None:
                    self._entries[project_id] = cached
            if cached is not None:
                # 已经跑过就不重复跑；只想看结果时（run=False）也算命中
                if cached.status == "ready":
                    return cached
                if cached.status == "loaded" and (not run or cached.result):
                    return cached
                if cached.status == "waiting" and not force:
                    return cached

        if self.fetcher is None:
            raise RuntimeError("上游工作台未配置，无法自动取交接包")

        entry = ProjectEntry(project_id=project_id, status="running")
        self._entries[project_id] = entry
        import time

        started = time.time()
        try:
            raw_spec, meta = self.fetcher(project_id)
            entry.title = str(raw_spec.get("title") or meta.get("title") or "")
            entry.remote_version = int(raw_spec.get("requirement_version") or 0)
            entry.remote_revision = int(raw_spec.get("domain_revision") or 0)

            selection_spec, diag = to_selection_spec(raw_spec, catalog=self.catalog)
            entry.spec = selection_spec
            entry.opened_at = _now()
            entry.source_meta = {
                "handoff": meta,
                "conversion": diag,
                "raw_constraints": sum(
                    len(item.get("constraints") or [])
                    for item in raw_spec.get("selection_items") or []
                ),
                "spec_id": raw_spec.get("spec_id"),
                "handoff_status": raw_spec.get("handoff_status"),
                "review_status": raw_spec.get("review_status"),
                "requirements": len(raw_spec.get("requirements") or []),
                "modules": len(raw_spec.get("modules") or []),
            }

            if not selection_spec.get("selection_items"):
                # 上游还没做完需求分析：工作台自己会标 draft / blocked。
                # 这不是选型失败，如实说明状态，别让用户以为是本阶段出错了。
                review = str(raw_spec.get("review_status") or "未知")
                handoff = str(raw_spec.get("handoff_status") or "未知")
                modules = len(raw_spec.get("modules") or [])
                entry.status = "waiting"
                entry.error = (
                    f"上游还没产出选型项：审核状态 {review}，交接状态 {handoff}，"
                    f"已确认模块 {modules} 个。请先在需求分析工作台完成分析并审核通过。"
                )
                entry.elapsed_s = round(time.time() - started, 2)
                self._save(project_id, entry, raw_spec)
                return entry

            entry.status = "loaded"
            if run:
                entry.result = self.service.run(selection_spec)
                entry.status = "ready"
            entry.elapsed_s = round(time.time() - started, 2)
            self._save(project_id, entry, raw_spec)
        except Exception as exc:  # noqa: BLE001 - 失败要如实展示，不吞
            entry.status = "error"
            entry.error = f"{type(exc).__name__}: {str(exc)[:300]}"
            entry.elapsed_s = round(time.time() - started, 2)
            self._save(project_id, entry, None)
        return entry

    def entry(self, project_id: str) -> ProjectEntry | None:
        cached = self._entries.get(project_id)
        if cached is not None:
            return cached
        loaded = self._load_from_disk(project_id)
        if loaded is not None:
            self._entries[project_id] = loaded
        return loaded

    def decisions(self, project_id: str) -> DecisionStore:
        return DecisionStore(self.dir_of(project_id) / "decisions.json")

    def writeback(self, project_id: str) -> dict[str, Any]:
        """把已确认的器件写回上游 spec，并生成下游输入。"""

        entry = self.entry(project_id)
        if entry is None or entry.status != "ready":
            raise RuntimeError("项目尚未完成选型，无法写回")
        raw_spec = self._read_raw(project_id)
        if raw_spec is None:
            raise RuntimeError("找不到上游交接包副本，无法写回")
        store = self.decisions(project_id)
        applied = apply_result(raw_spec, entry.result, store.all())
        payload = {
            "project_id": project_id,
            "spec": applied["spec"],
            "applied": applied["applied"],
            "summary": applied["summary"],
        }
        target = self.dir_of(project_id) / "requirement-spec.out.json"
        target.write_text(
            json.dumps(applied["spec"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return payload

    # ------------------------------------------------------------------ 落盘
    def _save(
        self,
        project_id: str,
        entry: ProjectEntry,
        raw_spec: Mapping[str, Any] | None,
    ) -> None:
        target = self.dir_of(project_id)
        (target / "meta.json").write_text(
            json.dumps(
                entry.summary() | {"source": entry.source_meta},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if raw_spec is not None:
            (target / "requirement-spec.in.json").write_text(
                json.dumps(raw_spec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if entry.spec:
            (target / "selection-spec.json").write_text(
                json.dumps(entry.spec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        if entry.result:
            (target / "candidate-set.json").write_text(
                json.dumps(entry.result, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _load_from_disk(self, project_id: str) -> ProjectEntry | None:
        target = self.dir_of(project_id)
        meta_path = target / "meta.json"
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        entry = ProjectEntry(
            project_id=project_id,
            title=str(meta.get("title") or ""),
            remote_version=int(meta.get("remote_version") or 0),
            remote_revision=int(meta.get("remote_revision") or 0),
            status=str(meta.get("status") or "new"),
            error=str(meta.get("error") or ""),
            opened_at=str(meta.get("opened_at") or ""),
            elapsed_s=float(meta.get("elapsed_s") or 0.0),
            source_meta=dict(meta.get("source") or {}),
        )
        spec_path = target / "selection-spec.json"
        result_path = target / "candidate-set.json"
        if spec_path.exists():
            entry.spec = json.loads(spec_path.read_text(encoding="utf-8"))
        if result_path.exists():
            entry.result = json.loads(result_path.read_text(encoding="utf-8"))
        return entry

    def _read_raw(self, project_id: str) -> dict[str, Any] | None:
        path = self.dir_of(project_id) / "requirement-spec.in.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
