#!/usr/bin/env python3
"""器件选型阶段的最小 Web 服务：API + 单页界面。

只依赖标准库和已装的 mysql-connector，不引入 Web 框架——会议要求"先出界面
再迭代"，能用、可读、好改比框架重要。

接口：

    GET  /                      单页界面
    GET  /api/health            数据库与预设库状态
    GET  /api/state             当前需求清单 + 最近一次选型结果
    POST /api/run               用新的需求清单跑一次选型
    POST /api/spec              只更新需求清单（不跑）
    POST /api/decision          记录用户对候选的决定
    GET  /api/search?q=         按型号/关键词查器件
    POST /api/chat              对话：本地基于真实数据作答
"""

from __future__ import annotations

import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import Database  # noqa: E402
from src.config import pcbagent_root, studio_root  # noqa: E402
from src.datasheet import lookup as datasheet_lookup  # noqa: E402
from src.decisions import DecisionStore  # noqa: E402
from src.handoff import build_handoff, contract_only, validate_with_downstream  # noqa: E402
from src.llm import LLMTranslator  # noqa: E402
from src.milvus import DatasheetLibrary  # noqa: E402
from src.projects import ProjectStore  # noqa: E402
from src.presets import PresetLibrary  # noqa: E402
from src.service import SelectionService, load_spec  # noqa: E402
from src.studio import StudioClient, fetch_spec  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
DEFAULT_SPEC = ROOT / "specs" / "example_power.json"
DEFAULT_DECISIONS = ROOT / "output" / "decisions.json"
DEFAULT_PROJECTS = ROOT / "output" / "projects"
PCBAgent_ROOT = pcbagent_root()
STUDIO_BASE = os.environ.get("DEVICE_SELECTION_STUDIO", "http://[::1]:8878")
STUDIO_ROOT = studio_root()

DECISIONS = ("keep", "exclude", "discuss", "confirmed")


class Session:
    """会话状态。支持两种模式：

    * **项目模式**：从上游工作台打开一个项目，自动取交接包、跑选型、落盘；
    * **示例模式**：没打开项目时用内置示例清单，方便随时点开看看。

    `spec` / `result` / `decisions` 对外仍是普通属性，内部指向当前模式。
    """

    def __init__(self, spec_path: Path) -> None:
        self.lock = threading.Lock()
        # 接上数据手册库：结构化参数表缺的字段去手册里补，补到会带原文引用。
        # 连不上也不影响选型，只是补不到。
        library = DatasheetLibrary()
        status = library.available()
        self.datasheet_status = status
        self.service = SelectionService(datasheet=library if status.get("ok") else None)
        # 大模型只做意图翻译：规则接不住的句子才轮到它，判定一概不由它做。
        self.llm = LLMTranslator()
        self.presets = PresetLibrary()
        self.spec_path = spec_path
        self._demo_spec: dict[str, Any] = load_spec(str(spec_path))
        self._demo_result: dict[str, Any] = self.service.run(self._demo_spec)
        self._demo_decisions = DecisionStore(DEFAULT_DECISIONS)

        self.studio = StudioClient(STUDIO_BASE)
        self.store = ProjectStore(
            DEFAULT_PROJECTS,
            service=self.service,
            remote_lister=lambda: self.studio.projects(),
            fetcher=lambda pid: fetch_spec(self.studio, pid, self.store.dir_of(pid)),
        )
        self.current_id = ""

    # ------------------------------------------------------------ 当前上下文
    @property
    def current_entry(self):
        return self.store.entry(self.current_id) if self.current_id else None

    @property
    def spec(self) -> dict[str, Any]:
        entry = self.current_entry
        return entry.spec if entry and entry.spec else self._demo_spec

    @spec.setter
    def spec(self, value: dict[str, Any]) -> None:
        entry = self.current_entry
        if entry is not None:
            entry.spec = value
        else:
            self._demo_spec = value

    @property
    def result(self) -> dict[str, Any]:
        entry = self.current_entry
        return entry.result if entry and entry.result else self._demo_result

    @result.setter
    def result(self, value: dict[str, Any]) -> None:
        entry = self.current_entry
        if entry is not None:
            entry.result = value
        else:
            self._demo_result = value

    @property
    def decisions(self) -> DecisionStore:
        if self.current_id:
            return self.store.decisions(self.current_id)
        return self._demo_decisions

    def open_project(
        self, project_id: str, *, run: bool = False, force: bool = False
    ) -> dict[str, Any]:
        """打开项目。``run=False`` 只取交接件，选型由用户按按钮触发。"""

        entry = self.store.open(project_id, run=run, force=force)
        self.current_id = project_id
        return entry.summary()

    def use_demo(self) -> None:
        self.current_id = ""

    def workspace(self) -> dict[str, Any]:
        """界面一次拿全：项目列表 + 当前项目 + 结果。"""

        return {
            "projects": self.store.listing(),
            "remote": {
                "base": STUDIO_BASE,
                "ok": self.store.remote_projects().get("ok"),
                "error": self.store.remote_projects().get("error", ""),
            },
            "current": self.current_id,
            "current_summary": self.current_entry.summary() if self.current_entry else None,
            "demo": self._demo_spec.get("project", {}),
            "spec": self.spec,
            "result": self.result,
            "decisions": self.decisions.all(),
        }

    def item_id_of(self, model: str) -> str:
        """型号 → 它在哪个选型项里。用户决定要挂到选型项上。"""

        for item in self.result.get("items", []):
            for candidate in item.get("candidates", []):
                if str(candidate.get("model")) == str(model):
                    return str(item["selection_item"]["id"])
        return ""

    def rerun(self) -> dict[str, Any]:
        self.result = self.service.run(self.spec)
        # 项目模式下把最新结果落盘，下次打开直接读
        if self.current_id:
            entry = self.current_entry
            if entry is not None:
                entry.status = "ready"
                self.store._save(self.current_id, entry, None)
        return self.result

    def snapshot(self) -> dict[str, Any]:
        return self.workspace()

    # ------------------------------------------------------------- 交付物
    def export_candidates(self) -> dict[str, Any]:
        """最终 CandidateSet：候选证据 + 用户决定，两者分栏记录。"""

        decisions = self.decisions.all()
        items: list[dict[str, Any]] = []
        confirmed_models: list[str] = []

        for item in self.result.get("items", []):
            item_id = str(item["selection_item"]["id"])
            candidates = []
            for candidate in item.get("candidates", []):
                record = decisions.get(f"{item_id}::{candidate.get('model')}")
                enriched = dict(candidate)
                enriched["verification_status"] = (
                    "verified" if candidate.get("supplier_part") else "unverified"
                )
                enriched["user_decision"] = (
                    {
                        "decision": record.get("decision"),
                        "label": record.get("decision_label"),
                        "reason": record.get("reason"),
                        "decided_at": record.get("decided_at"),
                    }
                    if record
                    else None
                )
                if record and record.get("decision") in ("confirmed", "keep"):
                    confirmed_models.append(str(candidate.get("model")))
                candidates.append(enriched)

            decided = [c for c in candidates if c.get("user_decision")]
            selected = [
                c for c in decided if c["user_decision"]["decision"] in ("confirmed", "keep")
            ]
            if selected and all(
                c["user_decision"]["decision"] == "confirmed" for c in selected
            ):
                status = "confirmed"
            elif selected:
                status = "selected"
            elif decided:
                status = "discussing"
            else:
                status = "undecided"

            items.append(
                {
                    **item,
                    "candidates": candidates,
                    "decision_status": status,
                    "selected_models": [c["model"] for c in selected],
                }
            )

        payload = {
            **{k: v for k, v in self.result.items() if k != "items"},
            "schema": "pcbagent.candidate_set.v1",
            "items": items,
            "decision_summary": {
                "recorded": len(decisions),
                "selected_models": sorted(set(confirmed_models)),
                "items_confirmed": sum(1 for i in items if i["decision_status"] == "confirmed"),
                "items_selected": sum(
                    1 for i in items if i["decision_status"] in ("confirmed", "selected")
                ),
                "items_pending": sum(
                    1 for i in items if i["decision_status"] == "undecided"
                ),
            },
        }
        return payload

    def export_handoff(self) -> tuple[dict[str, Any], str | None]:
        """生成下游输入并用官方校验器验证。失败时返回 (诊断, 错误说明)。"""

        try:
            payload = build_handoff(
                self.result,
                self.decisions.all(),
                description=(
                    f"器件选型阶段导出｜需求版本 {self.result.get('requirement_version') or '未标注'}"
                ),
            )
        except ValueError as exc:
            return {"schema": "pcbagent.netlist_input.v1", "modules": [], "error": str(exc)}, str(exc)

        contract = contract_only(payload)
        try:
            validate_with_downstream(contract, str(PCBAgent_ROOT))
            payload["validated_by"] = "netlist_end_to_end.generation_input.validate_generation_input"
            payload["validation"] = "passed"
        except Exception as exc:  # noqa: BLE001 - 校验失败要把原因原样带给用户
            payload["validation"] = "failed"
            payload["validation_error"] = f"{type(exc).__name__}: {exc}"
            return payload, str(exc)
        return payload, None

    # ------------------------------------------------------------- 器件详情
    def device_detail(self, device_id: int) -> dict[str, Any]:
        """点型号时弹窗用：身份 + 全部参数 + 手册片段。"""

        row = self.service.catalog.by_id(int(device_id))
        if row is None:
            raise ValueError(f"器件库里没有 device_id={device_id}")
        attributes = self.service.attributes.for_device(row.device_id)
        c_number = self.service._c_number(attributes, row)

        ordered = sorted(
            attributes.values(), key=lambda a: (a.priority_rank, a.field_key)
        )
        params = [a.as_dict() for a in ordered]
        key_params = [
            a.as_dict()
            for a in ordered
            if a.priority_rank < 9999 or a.is_required
        ]

        spans: list[dict[str, Any]] = []
        if self.service.datasheet is not None and c_number:
            try:
                for span in self.service.datasheet.spans(c_number, limit=6):
                    text = span.content.strip()
                    spans.append(
                        {
                            "title": span.title,
                            "page": span.page,
                            "database": span.database,
                            "locator": f"{span.database}.DataSheet:{span.span_id}",
                            "snippet": text[:1200],
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - 手册取不到不该挡住参数
                spans = [{"error": f"{type(exc).__name__}: {str(exc)[:120]}"}]

        evidence = {}
        for attribute in ordered:
            evidence[attribute.evidence_status] = (
                evidence.get(attribute.evidence_status, 0) + 1
            )

        return {
            "identity": row.as_dict(),
            "supplier_part": c_number,
            "parameters": params,
            "key_parameters": key_params,
            "datasheet": spans,
            "datasheet_available": bool(spans) and not spans[0].get("error"),
            "evidence": evidence,
        }

    # --------------------------------------------------------- 一键确认推荐
    def confirm_recommended(self, *, mode: str = "all") -> dict[str, Any]:
        """一键把选型项确认掉。

        ``mode="all"``（默认）：**每个有候选的选型项都选上，不留空**。
        结论不完美的照选，但会在 ``needs_review`` 里写明问题，界面上打标。

        ``mode="strict"``：只选 `recommended` / `acceptable` 的，其余留空。

        两种模式都跳过"已经确认过的"，避免重复计数。
        """

        confirmed: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        flagged: list[dict[str, str]] = []
        existing = self.decisions.all()
        strict = str(mode or "all").lower() == "strict"

        for item in self.result.get("items", []):
            selection = item.get("selection_item") or {}
            item_id = str(selection.get("id") or "")
            candidates = item.get("candidates") or []
            if not candidates:
                skipped.append({"item_id": item_id, "reason": "没有候选器件"})
                continue
            top = candidates[0]
            evaluation = top.get("evaluation") or {}
            fit = evaluation.get("fit")

            # 这个项有没有问题、问题是什么
            issue = ""
            if item.get("unverified"):
                issue = "未核对：没有可判定的条件，仅按类别召回"
            elif fit == "conditional":
                unknown = [
                    v.get("field") for v in evaluation.get("unknown", [])
                    if v.get("hard")
                ]
                issue = "硬性条件未知" + (f"：{'、'.join(unknown[:4])}" if unknown else "")
            elif fit == "excluded":
                violated = [
                    v.get("field") for v in evaluation.get("violated", [])
                    if v.get("hard")
                ]
                issue = "全部候选都违反硬性条件" + (
                    f"：{'、'.join(violated[:4])}" if violated else ""
                )
            elif fit not in ("recommended", "acceptable"):
                issue = f"结论为 {fit or '未知'}"

            if strict and issue:
                skipped.append(
                    {"item_id": item_id, "reason": f"{issue}，严格模式下不自动确认"}
                )
                continue

            key = f"{item_id}::{top.get('model')}"
            if (existing.get(key) or {}).get("decision") == "confirmed":
                skipped.append({"item_id": item_id, "reason": "已经是已确认状态"})
                continue

            self.decisions.record(
                item_id=item_id,
                model=str(top.get("model") or ""),
                decision="confirmed",
                reason=(
                    "一键确认（需复核：" + issue + "）"
                    if issue
                    else "一键确认系统推荐"
                ),
                origin="auto",
                needs_review=issue,
            )
            confirmed.append(
                {
                    "item_id": item_id,
                    "model": top.get("model"),
                    "supplier_part": top.get("supplier_part"),
                    "fit": fit,
                    "confidence": evaluation.get("confidence"),
                    "needs_review": issue,
                }
            )
            if issue:
                flagged.append(
                    {
                        "item_id": item_id,
                        "model": str(top.get("model") or ""),
                        "issue": issue,
                    }
                )

        return {
            "confirmed": confirmed,
            "flagged": flagged,
            "skipped": skipped,
            "counts": {
                "confirmed": len(confirmed),
                "flagged": len(flagged),
                "skipped": len(skipped),
                "total": len(self.result.get("items", [])),
            },
            "mode": "strict" if strict else "all",
            "decisions": self.decisions.all(),
        }

    # ------------------------------------------------- 自动选型并导出下游
    def auto_export(self, project_id: str) -> dict[str, Any]:
        """一次做完：取交接包 → 跑选型 → 全部确认 → 导出下游输入。

        给工作台的「自动选型并导出」用。结论不完美的项也会被选上并打标，
        但**没有候选**的项无法凭空生成，如实列在 skipped 里。
        """

        entry = self.store.open(project_id, run=True)
        self.current_id = project_id
        if entry.status != "ready":
            return {
                "ok": False,
                "status": entry.status,
                "error": entry.error or "项目尚未就绪，无法自动选型",
            }

        confirm = self.confirm_recommended(mode="all")
        try:
            payload = build_handoff(
                entry.result,
                self.decisions.all(),
                description=(
                    f"器件选型阶段自动导出｜需求版本 "
                    f"{entry.result.get('requirement_version') or '未标注'}"
                ),
            )
        except ValueError as exc:
            return {
                "ok": False,
                "status": "no_selection",
                "error": str(exc),
                "counts": confirm["counts"],
            }

        contract = contract_only(payload)
        validation = "passed"
        validation_error = ""
        try:
            validate_with_downstream(contract, str(PCBAgent_ROOT))
        except Exception as exc:  # noqa: BLE001
            validation = "failed"
            validation_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        payload["validation"] = validation
        if validation_error:
            payload["validation_error"] = validation_error
        payload["auto_selection"] = {
            "confirmed": confirm["counts"]["confirmed"],
            "flagged": confirm["counts"]["flagged"],
            "flagged_items": confirm["flagged"],
        }

        target = self.store.dir_of(project_id) / "generation-input.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return {
            "ok": validation == "passed",
            "status": "exported",
            "project_id": project_id,
            "counts": confirm["counts"],
            "flagged": confirm["flagged"],
            "skipped": confirm["skipped"],
            "not_exported": payload["diagnostics"]["skipped"],
            "modules": len(payload["modules"]),
            "validation": validation,
            "validation_error": validation_error,
            "download": f"/api/projects/{project_id}/generation-input.json",
        }

    # --------------------------------------------- 需求回填（选型 → 需求）
    def supplement_proposals(self, project_id: str | None = None) -> dict[str, Any]:
        """算出"选型之后已经能确定、但需求里还写着待定"的条目。"""

        from src.supplement import pending, propose

        pid = str(project_id or self.current_id or "")
        if not pid:
            raise ValueError("当前是示例模式，没有上游需求可回填")
        entry = self.store.entry(pid)
        if entry is None or entry.status != "ready":
            raise ValueError("该项目尚未完成选型，暂无可回填的内容")
        raw = self.store._read_raw(pid)
        if raw is None:
            raise ValueError("找不到该项目的上游交接件副本")

        # 交接件是打开项目时的快照；用户在说明书里确认或修改过之后，
        # 要以工作台当前文档为准，否则已经回填过的条目会反复出现在建议里。
        try:
            live = self.studio.project(pid)
            live_rows = {
                str(row.get("id")): row
                for row in (live.get("document") or {}).get("requirements") or []
            }
        except Exception:  # noqa: BLE001 - 工作台暂时不可达时退回快照
            live_rows = {}
        if live_rows:
            merged: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for row in raw.get("requirements") or []:
                rid = str(row.get("id") or "")
                seen_ids.add(rid)
                merged.append({**row, **live_rows.get(rid, {})})
            for rid, row in live_rows.items():
                if rid not in seen_ids:
                    merged.append(dict(row))
            raw = {**raw, "requirements": merged}

        proposals = propose(
            raw_spec=raw,
            result=entry.result,
            decisions=self.store.decisions(pid).all(),
            attributes_for=self.service.attributes.for_device,
        )
        covered = {p.requirement_id for p in proposals}
        pending_rows = [row for row in pending(raw) if row["id"] not in covered]
        if proposals:
            note = (
                "这些需求当初只能写「待定」，现在选完器件已经能给出具体值。"
                "采纳后会以「建议」状态写回需求说明书，仍需你最终确认。"
            )
        elif pending_rows:
            note = (
                f"还有 {len(pending_rows)} 条待定需求，但现有器件参数不足以唯一确定它们。"
                "例如接口数量取决于统计口径，不能从单颗器件的参数直接抄出，需要回到需求说明书确认。"
            )
        else:
            note = "这个项目没有待定需求，不需要补充。"
        return {
            "project_id": pid,
            "count": len(proposals),
            "proposals": [p.as_dict() for p in proposals],
            "pending": pending_rows,
            "note": note,
        }

    def apply_supplements(
        self, keys: Sequence[str], project_id: str | None = None
    ) -> dict[str, Any]:
        """把选中的建议写回工作台的需求说明书。

        走的是工作台自己的需求编辑接口：它会新建一版文档、留下改动记录，
        并把内容同步到说明书。写入状态一律是"建议"，不冒充用户原话。
        """

        pid = str(project_id or self.current_id or "")
        if not pid:
            raise ValueError("当前是示例模式，没有可回填的项目")
        preview = self.supplement_proposals(pid)
        wanted = set(str(k) for k in keys or [])
        chosen = [p for p in preview["proposals"] if p["key"] in wanted]
        if not chosen:
            raise ValueError("没有选中任何一条建议")

        raw = self.store._read_raw(pid) or {}
        requirements = {
            str(r.get("id")): dict(r) for r in raw.get("requirements") or []
        }

        applied: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        done: set[str] = set()
        for proposal in chosen:
            rid = proposal["requirement_id"]
            if rid in done:
                continue
            requirement = requirements.get(rid) or {}
            try:
                project = self.studio.project(pid)
                version = int((project.get("document") or {}).get("version") or 0)
                self.studio.patch_requirement(
                    pid,
                    base_version=version,
                    requirement_id=rid,
                    name=proposal["requirement_name"],
                    value=proposal["proposed_value"],
                    unit=proposal["unit"],
                    category=proposal["category"],
                    priority=str(requirement.get("priority") or "must"),
                    state="suggested",
                    implementation=str(requirement.get("implementation") or "hardware"),
                )
                done.add(rid)
                applied.append(
                    {
                        "requirement_id": rid,
                        "name": proposal["requirement_name"],
                        "value": proposal["proposed_value"],
                        "unit": proposal["unit"],
                        "from_model": proposal["model"],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它
                failed.append(
                    {
                        "requirement_id": rid,
                        "name": proposal["requirement_name"],
                        "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                    }
                )

        return {
            "project_id": pid,
            "applied": applied,
            "failed": failed,
            "counts": {"applied": len(applied), "failed": len(failed)},
            "next": "打开需求说明书即可看到这些条目已按「建议」补充，确认后可改为正式要求。",
        }

    # ------------------------------------------------------------- 对话逻辑
    def _models(self) -> dict[str, dict[str, Any]]:
        table: dict[str, dict[str, Any]] = {}
        for item in self.result.get("items", []):
            for candidate in item.get("candidates", []):
                table.setdefault(str(candidate["model"]), candidate)
        return table

    def _explain(self, model: str) -> str:
        target = None
        for item in self.result.get("items", []):
            for candidate in item.get("candidates", []):
                if str(candidate["model"]).upper() == model.upper():
                    target = (item, candidate)
                    break
            if target:
                break
        if target is None:
            return f"当前清单里没有 {model}。我可以先在器件库里查一下它的身份。"
        item, candidate = target
        evaluation = candidate["evaluation"]
        lines = [
            f"{candidate['model']}（{candidate.get('manufacturer') or '厂商未记录'}，"
            f"{candidate.get('supplier_part') or '无 C 编号'}）属于选型项 {item['selection_item']['id']}。",
            f"结论：{_fit_label(evaluation['fit'])}，证据置信度 {evaluation['confidence']}。",
        ]
        for verdict in evaluation["satisfied"]:
            lines.append(f"  满足 · {verdict['field']}：器件 {verdict['device_value']}")
        for verdict in evaluation["violated"]:
            lines.append(
                f"  不满足 · {verdict['field']}：器件 {verdict['device_value']}（{verdict['note']}）"
            )
        for verdict in evaluation["unknown"]:
            lines.append(
                f"  待补证据 · {verdict['field']}：{verdict['note']}"
            )
        if candidate["price_status"] != "confirmed":
            lines.append("  成本：库里没有价格证据，按待确认处理，不能按 0 元算。")
        return "\n".join(lines)

    def _summarize(self) -> str:
        summary = self.result["summary"]
        lines = [
            f"当前需求版本 {self.result.get('requirement_version') or '未标注'}，"
            f"共 {summary['total_items']} 个选型项。",
            f"有推荐结果 {summary['items_with_recommendation']} 项，阻塞 {summary['items_blocked']} 项。",
        ]
        if summary["cost_unconfirmed_items"]:
            lines.append(
                "以下选型项的成本缺少证据，未计入合计："
                + "、".join(summary["cost_unconfirmed_items"])
            )
        for item in self.result["items"]:
            head = item["selection_item"]["id"]
            best = item["primary_candidates"]
            lines.append(
                f"  {head}：{('主推 ' + '、'.join(best)) if best else '没有可用候选'}"
            )
        return "\n".join(lines)

    def chat(self, message: str) -> dict[str, Any]:
        text = str(message or "").strip()
        if not text:
            return {"reply": "请描述你的需求，或直接问某个候选为什么被推荐。", "changed": False}

        # 1) 查器件身份
        match = re.search(r"查(?:一下)?\s*([A-Za-z0-9\-/_.]+)", text)
        if match:
            query = match.group(1)
            found = self.service.search(query, limit=5)
            if not found["results"]:
                return {"reply": f"器件目录里没有匹配 {query} 的记录。", "changed": False}
            head = found["results"][0]
            reply = (
                f"{head['model']}：{head['leaf_name_zh'] or head['leaf_code']}，"
                f"{head.get('manufacturer') or '厂商未记录'}，"
                f"核心等级 {head.get('core_level') or '未标注'}，"
                f"C 编号 {head.get('supplier_part') or '未记录'}。"
            )
            return {"reply": reply, "changed": False, "search": found}

        # 2) 改预算并重跑
        budget = re.search(r"预算[^\d]{0,6}(\d+(?:\.\d+)?)", text)
        if budget:
            value = float(budget.group(1))
            self.spec.setdefault("global_constraints", {})["budget_total_cny"] = value
            self.rerun()
            return {"reply": f"预算已改为 {value} 元，清单已重算。", "changed": True}

        # 3) 记录用户决定
        #    必须排在"解释某个型号"之前：否则「保留 TPS5450DDAR」会因为句子里
        #    出现了型号而被当成解释请求,用户的决定就丢了。
        decide = re.search(r"(保留|排除|待讨论|确认)\s*([A-Za-z0-9\-/_.]{3,})", text)
        if decide:
            mapping = {"保留": "keep", "排除": "exclude", "待讨论": "discuss", "确认": "confirmed"}
            decision = mapping[decide.group(1)]
            model = decide.group(2)
            item_id = self.item_id_of(model)
            if not item_id:
                return {
                    "reply": f"当前清单里没有 {model}，先把它作为候选召回出来再记录决定。",
                    "changed": False,
                }
            self.decisions.record(
                item_id=item_id, model=model, decision=decision, reason=text
            )
            return {
                "reply": (
                    f"已记录并保存：{item_id} 的 {model} → {_decision_label(decision)}。"
                    "资料核验状态与选择状态分开保存。"
                ),
                "changed": False,
            }

        # 4) 改条件并重跑
        change = re.search(
            r"(SEL-\d+)(.*?)"
            r"(>=|<=|>|<|改成|不再高于|不高于|不低于|至少|最多|超过|低于)\s*"
            r"(\d+(?:\.\d+)?)\s*([A-Za-z%°µμ/]*)",
            text,
            re.DOTALL,
        )
        if change:
            applied = self._apply_condition(
                item_id=change.group(1),
                field_word=change.group(2),
                op=change.group(3),
                value=float(change.group(4)),
                unit=change.group(5) or "",
            )
            if applied:
                self.rerun()
                return {"reply": applied, "changed": True}
            return {
                "reply": (
                    f"没认出 {change.group(1)} 要改哪个字段。目前支持："
                    "输入电压范围、输出电压、输出电流、开关频率、工作温度、封装。"
                    "例如「把 SEL-002 的输出电流改成 800mA」。"
                ),
                "changed": False,
            }

        # 5) 解释某个型号（放在具体指令之后，避免把决定或改条件当成提问）
        asked = re.search(r"(为什么|原因|解释|说明)[^\w]*([A-Za-z0-9\-/_.]{3,})", text)
        if asked:
            return {"reply": self._explain(asked.group(2)), "changed": False}
        # 句子里只要出现了当前清单里的型号，就当解释请求。
        # 这条必须留在规则里：否则「为什么推荐 XXX」会因为中间夹了中文而漏掉，
        # 白白绕一圈大模型，用户等几十秒。
        for model in self._models():
            if model.lower() in text.lower():
                return {"reply": self._explain(model), "changed": False}

        # 规则接不住，才交给大模型做意图翻译；它只负责把话翻译成结构化意图，
        # 校验不过或调用失败就退回摘要，绝不猜。
        translated = self._llm_intent(text)
        if translated is not None:
            return translated

        return {"reply": self._summarize(), "changed": False}

    def _item_brief(self) -> list[dict[str, Any]]:
        """给大模型的上下文：只有编号、名称和现有条件，没有别的。"""

        rows: list[dict[str, Any]] = []
        for item in self.result.get("items", []):
            selection = item.get("selection_item") or {}
            conditions = [
                f"{c['field']} {c['op']} {c.get('value')}{c.get('unit') or ''}"
                for c in (selection.get("hard_conditions") or [])
                + (selection.get("preferred_conditions") or [])
            ]
            rows.append(
                {
                    "id": selection.get("id"),
                    "name": selection.get("module_name") or selection.get("category"),
                    "conditions": "；".join(conditions),
                }
            )
        return rows

    def _llm_intent(self, text: str) -> dict[str, Any] | None:
        if not self.llm.available():
            return None
        intent = self.llm.translate(text, items=self._item_brief())
        if intent is None:
            return None
        kind = intent["kind"]
        if kind == "summarize":
            return {"reply": self._summarize(), "changed": False, "via": "llm"}
        if kind == "search":
            found = self.service.search(intent["query"], limit=5)
            if not found["results"]:
                return {
                    "reply": f"器件目录里没有匹配 {intent['query']} 的记录。",
                    "changed": False,
                    "via": "llm",
                }
            head = found["results"][0]
            return {
                "reply": (
                    f"{head['model']}：{head['leaf_name_zh'] or head['leaf_code']}，"
                    f"{head.get('manufacturer') or '厂商未记录'}，"
                    f"C 编号 {head.get('supplier_part') or '未记录'}。"
                ),
                "changed": False,
                "via": "llm",
            }
        if kind == "explain":
            return {"reply": self._explain(intent["model"]), "changed": False, "via": "llm"}
        if kind == "decision":
            item_id = self.item_id_of(intent["model"])
            if not item_id:
                return {
                    "reply": f"当前清单里没有 {intent['model']}，先把它召回出来再记录决定。",
                    "changed": False,
                    "via": "llm",
                }
            self.decisions.record(
                item_id=item_id,
                model=intent["model"],
                decision=intent["decision"],
                reason=f"由大模型理解：{text}",
            )
            return {
                "reply": (
                    f"已记录并保存：{item_id} 的 {intent['model']} → "
                    f"{_decision_label(intent['decision'])}。"
                ),
                "changed": False,
                "via": "llm",
            }
        return self._apply_translated_condition(intent, text)

    def _apply_translated_condition(
        self, intent: Mapping[str, Any], original: str
    ) -> dict[str, Any] | None:
        """执行大模型翻译出来的条件。字段与比较符已在翻译层校验过。"""

        item_id = str(intent.get("item_id") or "")
        target = "hard_conditions" if intent.get("target") == "hard" else "preferred_conditions"
        for item in self.spec.get("selection_items", []):
            if str(item.get("id")) != item_id:
                continue
            bucket = item.setdefault(target, [])
            for entry in list(bucket):
                if entry.get("field") == intent["field"]:
                    bucket.remove(entry)
            entry: dict[str, Any] = {
                "field": intent["field"],
                "op": intent["op"],
                "value": intent["value"],
            }
            if intent.get("unit"):
                entry["unit"] = intent["unit"]
            entry["reason"] = f"由大模型理解用户说法：{original}"
            bucket.append(entry)
            self.rerun()
            shown = intent["value"]
            if isinstance(shown, list) and len(shown) == 2:
                shown = f"{shown[0]}~{shown[1]}"
            return {
                "reply": (
                    f"（大模型理解）已把 {item_id} 的 {intent['field']} 设为 "
                    f"{intent['op']} {shown}{intent.get('unit') or ''}，清单已重算。\n"
                    "判定仍由规则完成，请核对这条理解是否符合你的本意。"
                ),
                "changed": True,
                "via": "llm",
            }
        return None

    def _apply_condition(
        self,
        *,
        item_id: str,
        field_word: str,
        op: str,
        value: float,
        unit: str,
    ) -> str | None:
        """把一句自然语言落到具体字段上。词表很小，命中不了就如实说明。"""

        # 按最长匹配取字段。这里不能用「电压」这种宽泛词兜底：用户说
        # 「输入电压范围」，宽泛词会把它错配到 output_voltage 并静默改错条件，
        # 比直接说"没听懂"更危险。
        field_patterns = (
            ("输入电压范围", "input_voltage_range"),
            ("输入电压", "input_voltage_range"),
            ("输出电压", "output_voltage"),
            ("输出电流", "output_current"),
            ("开关频率", "switching_frequency"),
            ("工作温度", "operating_temperature"),
            ("封装", "package"),
        )
        key = None
        for word, field in sorted(field_patterns, key=lambda pair: -len(pair[0])):
            if word in field_word:
                key = field
                break
        if key is None:
            return None
        operator = {
            ">=": ">=",
            ">": ">=",
            "不低于": ">=",
            "至少": ">=",
            "<=": "<=",
            "<": "<=",
            "不高于": "<=",
            "不再高于": "<=",
            "最多": "<=",
            "超过": ">=",
            "低于": "<=",
        }.get(op, ">=")
        if op == "改成":
            # 「输出电流改成 800mA」在工程语义上是能力下限，不是要求器件恰好等于 800mA；
            # 而输出电压和封装是设定值，要按相等比。
            if key in ("output_voltage", "package"):
                operator = "is"
            elif key == "input_voltage_range":
                # 范围字段：要求器件的能力区间必须覆盖这个电压点
                operator = "cover"
            else:
                operator = ">="
        for item in self.spec.get("selection_items", []):
            if str(item.get("id")) != item_id:
                continue
            bucket = item.setdefault("preferred_conditions", [])
            for entry in list(bucket):
                if entry.get("field") == key:
                    bucket.remove(entry)
            bucket.append({"field": key, "op": operator, "value": value, "unit": unit})
            return f"已把 {item_id} 的 {key} 条件设为 {operator} {value}{unit}，清单已重算。"
        return f"{item_id} 不在当前需求清单里。"


def _fit_label(fit: str) -> str:
    return {
        "recommended": "推荐",
        "acceptable": "可接受",
        "conditional": "有条件（证据待补）",
        "excluded": "已淘汰（违反硬性条件）",
    }.get(fit, fit)


def _decision_label(decision: str) -> str:
    return {
        "keep": "保留",
        "exclude": "排除",
        "discuss": "待讨论",
        "confirmed": "已确认",
    }.get(decision, decision)


class Handler(SimpleHTTPRequestHandler):
    session: Session

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC), **kwargs)

    # -------------------------------------------------------------- 工具方法
    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 需求分析工作台（另一个端口）要能调这里的接口
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        return

    # -------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._json(
                {
                    "database": Database().status(),
                    "preset_library": PresetLibrary().summary(),
                    "datasheet_library": self.session.datasheet_status,
                    "llm": self.session.llm.describe(),
                }
            )
            return
        if parsed.path == "/api/state":
            self._json(self.session.snapshot())
            return
        if parsed.path == "/api/projects":
            self._json(
                {
                    "projects": self.session.store.listing(),
                    "remote": {
                        "base": STUDIO_BASE,
                        "ok": self.session.store.remote_projects().get("ok"),
                        "error": self.session.store.remote_projects().get("error", ""),
                    },
                    "current": self.session.current_id,
                }
            )
            return
        if parsed.path == "/api/supplements":
            project = (parse_qs(parsed.query).get("project") or [""])[0]
            try:
                self._json(self.session.supplement_proposals(project or None))
            except ValueError as exc:
                self._json({"error": str(exc), "proposals": []}, 409)
            return
        if parsed.path == "/api/search":
            query = (parse_qs(parsed.query).get("q") or [""])[0]
            self._json(self.session.service.search(query, limit=8))
            return
        if parsed.path.startswith("/api/devices/"):
            raw = parsed.path.rsplit("/", 1)[-1]
            try:
                detail = self.session.device_detail(int(raw))
            except (TypeError, ValueError) as exc:
                self._json({"error": str(exc)}, 404)
                return
            self._json(detail)
            return
        if parsed.path.startswith("/api/projects/") and parsed.path.endswith(
            "/generation-input.json"
        ):
            project_id = parsed.path.split("/")[3]
            target = self.session.store.dir_of(project_id) / "generation-input.json"
            if not target.exists():
                self._json({"error": "尚未导出，请先执行自动选型"}, 404)
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="netlist-input-{project_id[:8]}.json"',
            )
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/export/candidates":
            # 最终交付物：候选清单 + 用户决定 + 交接状态
            self._json(self.session.export_candidates())
            return
        if parsed.path == "/api/export/handoff":
            # 下游生成入口格式
            payload = self.session.export_handoff()
            self._json(payload[0] if payload[1] is None else {"error": payload[1]}, 200 if payload[1] is None else 409)
            return
        if parsed.path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        payload = self._body()
        with self.session.lock:
            if parsed.path == "/api/projects/open":
                project_id = str(payload.get("project_id") or "").strip()
                if not project_id:
                    self._json({"error": "project_id is required"}, 400)
                    return
                try:
                    summary = self.session.open_project(
                        project_id,
                        run=bool(payload.get("run")),
                        force=bool(payload.get("force")),
                    )
                except Exception as exc:  # noqa: BLE001
                    self._json(
                        {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 502
                    )
                    return
                self._json({"summary": summary, "state": self.session.snapshot()})
                return
            if parsed.path == "/api/projects/demo":
                self.session.use_demo()
                self._json({"state": self.session.snapshot()})
                return
            if parsed.path == "/api/projects/writeback":
                if not self.session.current_id:
                    self._json({"error": "当前是示例模式，没有可写回的项目"}, 400)
                    return
                try:
                    result = self.session.store.writeback(self.session.current_id)
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": f"{type(exc).__name__}: {str(exc)[:200]}"}, 409)
                    return
                self._json(
                    {
                        "applied": result["applied"],
                        "summary": result["summary"],
                        "spec_id": result["spec"].get("spec_id"),
                    }
                )
                return
            if parsed.path == "/api/projects/auto-export":
                project_id = str(payload.get("project_id") or "").strip()
                if not project_id:
                    self._json({"ok": False, "error": "project_id is required"}, 400)
                    return
                try:
                    result = self.session.auto_export(project_id)
                except Exception as exc:  # noqa: BLE001
                    self._json(
                        {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"},
                        502,
                    )
                    return
                self._json(result, 200 if result.get("ok") else 409)
                return
            if parsed.path == "/api/run":
                spec = payload.get("spec")
                if isinstance(spec, dict):
                    self.session.spec = spec
                result = self.session.rerun()
                self._json({"result": result, "spec": self.session.spec})
                return
            if parsed.path == "/api/spec":
                spec = payload.get("spec")
                if isinstance(spec, dict):
                    self.session.spec = spec
                self._json({"spec": self.session.spec})
                return
            if parsed.path == "/api/decision":
                model = str(payload.get("model") or "")
                decision = str(payload.get("decision") or "")
                item_id = str(payload.get("item_id") or "") or self.session.item_id_of(model)
                if not model or not item_id:
                    self._json({"error": "model and item_id are required"}, 400)
                    return
                if decision in ("", "none", "clear"):
                    self.session.decisions.clear(item_id=item_id, model=model)
                elif decision in DECISIONS:
                    self.session.decisions.record(
                        item_id=item_id,
                        model=model,
                        decision=decision,
                        reason=str(payload.get("reason") or ""),
                    )
                else:
                    self._json({"error": "invalid decision"}, 400)
                    return
                self._json({"decisions": self.session.decisions.all()})
                return
            if parsed.path == "/api/decisions/confirm-recommended":
                self._json(
                    self.session.confirm_recommended(
                        mode=str(payload.get("mode") or "all")
                    )
                )
                return
            if parsed.path == "/api/supplements/apply":
                keys = payload.get("keys") or []
                project_id = str(payload.get("project_id") or "") or None
                try:
                    self._json(self.session.apply_supplements(keys, project_id))
                except ValueError as exc:
                    self._json({"error": str(exc)}, 409)
                return
            if parsed.path == "/api/chat":
                self._json(self.session.chat(str(payload.get("message") or "")))
                return
        self._json({"error": "unknown endpoint"}, 404)


def main() -> int:
    parser = argparse.ArgumentParser(description="器件选型阶段 Web 界面")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--spec", default=str(DEFAULT_SPEC))
    args = parser.parse_args()

    print("正在连接器件目录并跑一次选型……")
    Handler.session = Session(Path(args.spec))
    summary = Handler.session.result["summary"]
    print(f"选型项 {summary['total_items']} 个，有推荐 {summary['items_with_recommendation']} 个")
    print(f"打开 http://127.0.0.1:{args.port}/")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
