#!/usr/bin/env python3
"""批量跑案例库，做回归对比。

对每个案例：跑一次选型 → 与 `expected.json` 里的历史选择对照 → 汇总。

对照口径要说清楚：历史选择**不是唯一正确答案**。当时有相当一部分模块
根本没选出器件，那些空结果本身就是要改进的证据。所以这里同时看两个指标：

* 覆盖：多少选型项这次给出了推荐（越高说明召回和判定越能干活）；
* 一致：在历史上真的选出器件的模块里，这次的首选和它是否相同（越低越需要人工看）。
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.service import SelectionService, load_spec  # noqa: E402

DEFAULT_CASES = ROOT / "cases"
DEFAULT_OUT = ROOT / "output" / "case_runs"


def run_one(service: SelectionService, case_dir: Path) -> dict[str, Any]:
    spec = load_spec(str(case_dir / "requirement_spec.json"))
    expected_path = case_dir / "expected.json"
    expected = (
        json.loads(expected_path.read_text(encoding="utf-8"))
        if expected_path.exists()
        else {}
    )

    started = time.time()
    result = service.run(spec)
    elapsed = time.time() - started

    comparisons: list[dict[str, Any]] = []
    stats: Counter = Counter()
    for item in result["items"]:
        item_id = item["selection_item"]["id"]
        baseline = (expected.get("modules") or {}).get(item_id) or {}
        historical = [
            str(entry.get("model") or "")
            for entry in (baseline.get("historical_selection") or [])
            if entry.get("model")
        ]
        primary = item["primary_candidates"]

        stats["items"] += 1
        if primary:
            stats["items_with_recommendation"] += 1
        else:
            stats["items_blocked"] += 1
        if historical:
            stats["items_with_history"] += 1
            if primary and primary[0] in historical:
                stats["first_choice_agrees"] += 1
            else:
                stats["first_choice_differs"] += 1
        elif primary:
            stats["history_empty_new_recommendation"] += 1

        comparisons.append(
            {
                "item_id": item_id,
                "module_name": item["selection_item"].get("module_name"),
                "recall": item["recall"],
                "candidate_count": item["candidate_count"],
                "primary_candidates": primary,
                "historical_selection": historical,
                "agreement": (
                    "same"
                    if historical and primary and primary[0] in historical
                    else "different"
                    if historical
                    else "no_history"
                ),
                "blocked_by": item["blocked_by"],
            }
        )

    return {
        "case": case_dir.name,
        "elapsed_s": round(elapsed, 2),
        "summary": dict(stats),
        "result_summary": result["summary"],
        "comparisons": comparisons,
    }


def write_summary(out_root: Path, runs: list[Mapping[str, Any]]) -> dict[str, Any]:
    totals: Counter = Counter()
    for run in runs:
        for key, value in run["summary"].items():
            totals[key] += value
    summary = {
        "cases": len(runs),
        "elapsed_s": round(sum(float(r["elapsed_s"]) for r in runs), 1),
        "totals": dict(totals),
        "per_case": [
            {"case": r["case"], "elapsed_s": r["elapsed_s"], **r["summary"]}
            for r in runs
        ],
    }
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    lines = [
        "# 案例回归结果",
        "",
        f"- 案例 {summary['cases']} 个，总耗时 {summary['elapsed_s']} 秒",
        f"- 选型项 {totals.get('items', 0)} 个：给出推荐 {totals.get('items_with_recommendation', 0)} 个，"
        f"阻塞 {totals.get('items_blocked', 0)} 个",
        f"- 历史上选出过器件的模块 {totals.get('items_with_history', 0)} 个："
        f"首选一致 {totals.get('first_choice_agrees', 0)} 个，不一致 {totals.get('first_choice_differs', 0)} 个",
        f"- 历史上没选出来、这次给出了推荐的 {totals.get('history_empty_new_recommendation', 0)} 个",
        "",
        "| 案例 | 耗时(s) | 选型项 | 有推荐 | 阻塞 | 历史有选择 | 首选一致 | 首选不同 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in summary["per_case"]:
        lines.append(
            f"| {row['case']} | {row['elapsed_s']} | {row.get('items', 0)} |"
            f" {row.get('items_with_recommendation', 0)} | {row.get('items_blocked', 0)} |"
            f" {row.get('items_with_history', 0)} | {row.get('first_choice_agrees', 0)} |"
            f" {row.get('first_choice_differs', 0)} |"
        )
    (out_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量跑选型案例")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个案例")
    parser.add_argument("--only", default="", help="只跑名字匹配的案例，逗号分隔")
    args = parser.parse_args(list(argv) if argv is not None else None)

    root = Path(args.cases)
    dirs = sorted(d for d in root.iterdir() if (d / "requirement_spec.json").exists())
    if args.only:
        wanted = {w.strip() for w in args.only.split(",") if w.strip()}
        dirs = [d for d in dirs if d.name in wanted]
    if args.limit:
        dirs = dirs[: args.limit]
    if not dirs:
        print(f"没有找到案例:{root}")
        return 1

    out_root = Path(args.out)
    service = SelectionService()
    runs: list[dict[str, Any]] = []
    for case_dir in dirs:
        try:
            run = run_one(service, case_dir)
        except Exception as exc:  # noqa: BLE001 - 单案例失败不中断整批
            print(f"案例 {case_dir.name} 失败：{type(exc).__name__}: {exc}")
            continue
        runs.append(run)
        out_root.mkdir(parents=True, exist_ok=True)
        (out_root / f"{case_dir.name}.json").write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        row = run["summary"]
        print(
            f"{case_dir.name}: {run['elapsed_s']}s 选型项 {row.get('items', 0)}"
            f" 有推荐 {row.get('items_with_recommendation', 0)}"
            f" 阻塞 {row.get('items_blocked', 0)}"
        )

    total = write_summary(out_root, runs)
    t = total["totals"]
    print(
        f"\n合计 {total['cases']} 个案例：有推荐 {t.get('items_with_recommendation', 0)}"
        f" / 选型项 {t.get('items', 0)}；历史有选择 {t.get('items_with_history', 0)} 项，"
        f"其中首选一致 {t.get('first_choice_agrees', 0)}。"
    )
    print(f"报告：{out_root / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
