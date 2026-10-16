#!/usr/bin/env python3
"""把本阶段与需求分析工作台串成一条链。

链路：

    工作台（requirements_studio，HTTP）
      └─ 取最新交接包 → requirement-spec.json
           └─ 本阶段：转换 → 候选召回 → 三态判定 → 用户确认
                └─ 写回 spec（追加 device_references）
                     └─ 工作台的 generation_adapter → 网表生成入口输入

用法：

    python3 tools/bridge_studio.py list
    python3 tools/bridge_studio.py run --project <project_id> --auto-confirm
    python3 tools/bridge_studio.py run --project <project_id> --decisions output/decisions.json

说明：最后一步会直接调用工作台自己的 `backend.handoff.generation_adapter`，
所以需要能读到他们的代码树（默认 `/share/home/chenxin/work/01_PCBAgent`）。
这样用的是他们的下游契约实现，不是我另写一份，避免版本漂移。
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_STUDIO = "/share/home/chenxin/work/01_PCBAgent"
DEFAULT_BASE = "http://[::1]:8878"
DEFAULT_OUT = ROOT / "output" / "bridge"


class StudioClient:
    """需求分析工作台的 HTTP 客户端。"""

    def __init__(self, base: str = DEFAULT_BASE) -> None:
        self.base = base.rstrip("/")

    def _call(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        raw: bool = False,
    ) -> Any:
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path} 失败 {exc.code}：{detail}") from exc
        if raw:
            return body
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body

    def health(self) -> dict:
        return self._call("GET", "/api/health")

    def projects(self) -> list[dict]:
        return self._call("GET", "/api/projects")

    def project(self, project_id: str) -> dict:
        return self._call("GET", f"/api/projects/{project_id}")

    def handoff_check(self, project_id: str) -> dict:
        return self._call("POST", f"/api/projects/{project_id}/handoff-check")

    def create_handoff(
        self,
        project_id: str,
        *,
        base_version: int,
        base_domain_revision: int,
        allow_draft: bool = True,
    ) -> dict:
        return self._call(
            "POST",
            f"/api/projects/{project_id}/handoff",
            {
                "request_id": uuid.uuid4().hex,
                "base_version": base_version,
                "base_domain_revision": base_domain_revision,
                "allow_draft": allow_draft,
            },
        )

    def download_handoff(self, project_id: str, artifact_id: str) -> bytes:
        # 这个接口返回的是 ZIP 二进制，不能按 JSON 解析
        return self._call(
            "GET", f"/api/projects/{project_id}/handoff/{artifact_id}", raw=True
        )


def fetch_spec(client: StudioClient, project_id: str, out_dir: Path) -> tuple[dict, dict]:
    """取一份最新交接包并解出 RequirementSpec。"""

    project = client.project(project_id)
    version = int((project.get("document") or {}).get("version") or 0)
    revision = int(project.get("domain_revision") or 0)

    readiness = client.handoff_check(project_id)
    artifact = client.create_handoff(
        project_id,
        base_version=version,
        base_domain_revision=revision,
        allow_draft=True,
    )
    content = client.download_handoff(project_id, artifact["id"])

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "handoff.zip").write_bytes(content)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        spec = json.loads(archive.read("requirement-spec.json"))
        names = archive.namelist()
    (out_dir / "requirement-spec.in.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    meta = {
        "project_id": project_id,
        "title": project.get("title"),
        "document_version": version,
        "domain_revision": revision,
        "artifact_id": artifact["id"],
        "handoff_files": names,
        "readiness": {
            "status": readiness.get("status"),
            "blockers": readiness.get("blockers"),
            "contract_valid": readiness.get("contract_valid"),
        },
    }
    return spec, meta


def load_generation_adapter(studio_root: str):
    """导入工作台自己的下游适配器，保证用的是他们的契约实现。"""

    studio_path = str(Path(studio_root) / "requirements_studio")
    agent_path = str(Path(studio_root))
    for path in (agent_path, studio_path):
        if path not in sys.path:
            sys.path.insert(0, path)
    from backend.handoff import generation_adapter  # noqa: PLC0415

    return generation_adapter


def auto_decide(result: dict, *, status: str = "confirmed") -> dict[str, dict[str, Any]]:
    """把每个**已核对过参数**的选型项的首选候选标成用户决定（仅用于演示/联调）。

    没有可判定条件的选型项不自动确认：那种候选只是按类别召回的，从未做过参数
    核对，自动确认会把未核对的东西送进下游。
    """

    decisions: dict[str, dict[str, Any]] = {}
    for item in result["items"]:
        if item.get("unverified"):
            continue
        item_id = item["selection_item"]["id"]
        if not item["primary_candidates"]:
            continue
        model = item["primary_candidates"][0]
        decisions[f"{item_id}::{model}"] = {
            "item_id": item_id,
            "model": model,
            "decision": status,
            "reason": "bridge 自动选定（演示用，等待人工复核）",
        }
    return decisions


def run(
    client: StudioClient,
    *,
    project_id: str,
    out_root: Path,
    studio_root: str,
    decisions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    from src.chenxin import apply_result, to_selection_spec, validate_with_chenxin  # noqa: PLC0415
    from src.service import SelectionService  # noqa: PLC0415

    out_dir = out_root / project_id[:12]
    spec, meta = fetch_spec(client, project_id, out_dir)

    mine, diag = to_selection_spec(spec)
    result = SelectionService().run(mine)
    (out_dir / "candidate-set.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not decisions:
        decisions = auto_decide(result)

    applied = apply_result(spec, result, decisions)
    contract_ok = True
    contract_error = ""
    try:
        validate_with_chenxin(applied["spec"], str(Path(studio_root) / "requirements_studio"))
    except Exception as exc:  # noqa: BLE001
        contract_ok = False
        contract_error = f"{type(exc).__name__}: {str(exc)[:200]}"
    (out_dir / "requirement-spec.out.json").write_text(
        json.dumps(applied["spec"], ensure_ascii=False, indent=2), encoding="utf-8"
    )

    adapter_state: dict[str, Any] = {}
    if contract_ok:
        try:
            adapter = load_generation_adapter(studio_root)
            adapter_state = adapter(applied["spec"])
            (out_dir / "generation-input.json").write_text(
                json.dumps(adapter_state, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            adapter_state = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}

    summary = result["summary"]
    report = {
        "project": meta,
        "selection": {
            "items": summary["total_items"],
            "recommended": summary["items_with_recommendation"],
            "blocked": summary["items_blocked"],
            "coverage": round(
                summary["items_with_recommendation"] / max(summary["total_items"], 1), 4
            ),
        },
        "conversion": diag,
        "decisions": {"count": len(decisions), "status": sorted({d["decision"] for d in decisions.values()})},
        "writeback": {
            "applied": applied["summary"]["applied_selections"],
            "contract_valid": contract_ok,
            "contract_error": contract_error,
        },
        "downstream": {
            "state": adapter_state.get("state"),
            "accepted": adapter_state.get("accepted"),
            "can_start_generation": adapter_state.get("can_start_generation"),
            "prepared_modules": adapter_state.get("prepared_modules"),
            "selection_needed": len(adapter_state.get("selection_needed") or []),
            "error": adapter_state.get("error"),
        },
        "artifacts": sorted(p.name for p in out_dir.iterdir()),
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把器件选型阶段接到需求分析工作台")
    parser.add_argument("command", choices=["list", "run", "health"])
    parser.add_argument("--base", default=DEFAULT_BASE, help="工作台地址")
    parser.add_argument("--project", default="", help="项目编号")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--studio", default=DEFAULT_STUDIO, help="工作台代码树根目录")
    parser.add_argument(
        "--decisions",
        default="",
        help="用已有的用户决定文件；不填则自动选定首选候选（演示用）",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    client = StudioClient(args.base)

    if args.command == "health":
        print(json.dumps(client.health(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "list":
        rows = client.projects()
        print(f"共 {len(rows)} 个项目：")
        for row in rows[:30]:
            print(f"  {row['id']}  v{row.get('version')}  {str(row.get('title'))[:40]}")
        return 0

    if not args.project:
        print("run 需要 --project <project_id>；先用 list 查看。")
        return 2

    decisions: dict[str, dict[str, Any]] = {}
    if args.decisions:
        payload = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
        decisions = payload.get("decisions") or payload

    started = time.time()
    report = run(
        client,
        project_id=args.project,
        out_root=Path(args.out),
        studio_root=args.studio,
        decisions=decisions,
    )
    sel = report["selection"]
    print(
        f"\n项目：{report['project']['title']}\n"
        f"选型：{sel['items']} 项，有推荐 {sel['recommended']}（{sel['coverage'] * 100:.1f}%），"
        f"阻塞 {sel['blocked']}\n"
        f"决定：{report['decisions']['count']} 条 {report['decisions']['status']}\n"
        f"写回：应用 {report['writeback']['applied']} 个，契约校验 "
        f"{'通过' if report['writeback']['contract_valid'] else '未通过 ' + report['writeback']['contract_error']}\n"
        f"下游：state={report['downstream']['state']} "
        f"can_start_generation={report['downstream']['can_start_generation']} "
        f"已准备模块={report['downstream']['prepared_modules']} "
        f"仍待选型={report['downstream']['selection_needed']}"
    )
    if report["downstream"].get("error"):
        print(f"下游适配报错：{report['downstream']['error']}")
    print(f"耗时 {time.time() - started:.1f}s，产物在 {Path(args.out) / args.project[:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
