#!/usr/bin/env python3
"""拿需求分析工作台的真实 RequirementSpec 做接口回归。

流程：读取他们的 spec → 转换成本阶段输入 → 跑选型 → 统计覆盖率与阻塞原因。
加 `--writeback` 时还会把结果写回他们的 spec，并调用他们自己的 Pydantic 契约校验。

用法：

    python3 tools/run_chenxin_specs.py                 # 全部样例
    python3 tools/run_chenxin_specs.py --limit 5       # 只跑前 5 份
    python3 tools/run_chenxin_specs.py --writeback     # 顺带验证写回
"""

from __future__ import annotations

import argparse
from collections import Counter
import glob
import json
from pathlib import Path
import sys
import time
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.chenxin import apply_result, to_selection_spec, validate_with_chenxin  # noqa: E402
from src.catalog import DeviceCatalog  # noqa: E402
from src.config import samples_root, studio_root  # noqa: E402
from src.db import Database  # noqa: E402
from src.milvus import DatasheetLibrary  # noqa: E402
from src.service import SelectionService  # noqa: E402

DEFAULT_SAMPLES = samples_root()
STUDIO_ROOT = studio_root()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用上游真实 spec 做接口回归")
    parser.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    parser.add_argument("--studio", default=str(STUDIO_ROOT))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--writeback", action="store_true", help="并验证写回上游契约")
    parser.add_argument(
        "--datasheet",
        action="store_true",
        help="启用数据手册库补缺失参数（会慢一些）",
    )
    parser.add_argument("--out", default=str(ROOT / "output" / "chenxin_specs.json"))
    args = parser.parse_args(list(argv) if argv is not None else None)

    files = sorted(glob.glob(str(Path(args.samples) / "*.json")))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"没有找到样例：{args.samples}")
        return 1

    library = None
    if args.datasheet:
        candidate = DatasheetLibrary()
        status = candidate.available()
        print(f"手册库：{'可用' if status.get('ok') else '不可用 ' + str(status.get('error'))}")
        library = candidate if status.get("ok") else None
    service = SelectionService(datasheet=library)
    totals: Counter = Counter()
    diag_totals: Counter = Counter()
    blocked: Counter = Counter()
    writeback_ok = writeback_bad = 0
    per_file: list[dict[str, Any]] = []
    started = time.time()

    for path in files:
        try:
            spec = json.loads(Path(path).read_text(encoding="utf-8"))
            mine, diag = to_selection_spec(spec, catalog=DeviceCatalog(service.db))
            result = service.run(mine)
        except Exception as exc:  # noqa: BLE001 - 单份失败不中断
            print(f"跳过 {Path(path).name}：{type(exc).__name__}: {exc}")
            continue

        summary = result["summary"]
        totals["items"] += summary["total_items"]
        totals["recommended"] += summary["items_with_recommendation"]
        totals["blocked"] += summary["items_blocked"]
        totals["unverified"] += summary.get("items_unverified", 0)
        totals["clean_recommended"] += summary["items_with_recommendation"]
        for key in ("conditions", "notes_only", "specified", "preferred", "reference_only"):
            diag_totals[key] += diag.get(key, 0)
        for item in result["items"]:
            if not item["primary_candidates"] and item["blocked_by"]:
                blocked[str(item["blocked_by"][0])[:80]] += 1

        row = {
            "spec": Path(path).name,
            "spec_id": mine["source"]["spec_id"],
            "items": summary["total_items"],
            "recommended": summary["items_with_recommendation"],
            "blocked": summary["items_blocked"],
            "conditions": diag.get("conditions", 0),
            "notes_only": diag.get("notes_only", 0),
        }

        if args.writeback:
            decisions: dict[str, dict[str, Any]] = {}
            for item in result["items"]:
                if item["primary_candidates"]:
                    sid = item["selection_item"]["id"]
                    decisions[f"{sid}::{item['primary_candidates'][0]}"] = {
                        "item_id": sid,
                        "model": item["primary_candidates"][0],
                        "decision": "confirmed",
                    }
            applied = apply_result(spec, result, decisions)
            row["applied_selections"] = applied["summary"]["applied_selections"]
            try:
                validate_with_chenxin(applied["spec"], args.studio)
                writeback_ok += 1
                row["writeback"] = "passed"
            except Exception as exc:  # noqa: BLE001
                writeback_bad += 1
                row["writeback"] = f"failed: {str(exc)[:160]}"
        per_file.append(row)

    payload = {
        "samples": len(per_file),
        "elapsed_s": round(time.time() - started, 1),
        "coverage": {
            "items": totals["items"],
            "recommended": totals["clean_recommended"],
            "unverified": totals["unverified"],
            "blocked": totals["blocked"],
            "rate": round(totals["clean_recommended"] / max(totals["items"], 1), 4),
        },
        "conversion": {
            "engineering_conditions": diag_totals["conditions"],
            "notes_only": diag_totals["notes_only"],
            "specified_references": diag_totals["specified"],
            "preferred_references": diag_totals["preferred"],
            "reference_only": diag_totals["reference_only"],
        },
        "writeback": {"passed": writeback_ok, "failed": writeback_bad},
        "blocked_reasons": dict(blocked.most_common(15)),
        "per_spec": per_file,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cov = payload["coverage"]
    print(
        f"\n上游 spec {payload['samples']} 份：选型项 {cov['items']}，"
        f"经过参数核对并有推荐 {cov['recommended']}（{cov['rate'] * 100:.1f}%），"
        f"未核对 {cov['unverified']}，阻塞 {cov['blocked']}"
    )
    print(
        f"约束转换：工程条件 {diag_totals['conditions']} 条，"
        f"仅作说明 {diag_totals['notes_only']} 条（描述性记录不参与判定）"
    )
    if args.writeback:
        print(f"写回契约校验：通过 {writeback_ok} / 失败 {writeback_bad}")
    print(f"报告：{out}")

    db_status = Database().status()
    if not db_status.get("ok"):
        print(f"注意：数据库状态异常 {db_status.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
