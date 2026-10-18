#!/usr/bin/env python3
"""命令行入口：跑一次选型，或做数据库健康检查。

用法：

    python3 cli.py health
    python3 cli.py demo
    python3 cli.py run --spec specs/example_power.json --out output/result.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import Database  # noqa: E402
from src.config import pcbagent_root  # noqa: E402
from src.decisions import DecisionStore  # noqa: E402
from src.handoff import build_handoff, contract_only, validate_with_downstream  # noqa: E402
from src.service import SelectionService, demo_spec, load_spec  # noqa: E402

PCBAgent_ROOT = pcbagent_root()


def _dump(payload: object, out: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"已写入 {target}")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="器件选型阶段命令行")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="检查数据库连接")

    demo = sub.add_parser("demo", help="用内置示例跑一次完整选型")
    demo.add_argument("--out", default="")

    run = sub.add_parser("run", help="用指定需求清单跑选型")
    run.add_argument("--spec", required=True)
    run.add_argument("--out", default="")

    search = sub.add_parser("search", help="按型号或关键词查器件")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=6)

    handoff = sub.add_parser(
        "handoff", help="把候选清单与用户决定转成下游生成入口格式"
    )
    handoff.add_argument("--spec", default=str(ROOT / "specs" / "example_power.json"))
    handoff.add_argument("--decisions", default=str(ROOT / "output" / "decisions.json"))
    handoff.add_argument("--out", default=str(ROOT / "output" / "netlist_input.json"))

    handoff.add_argument(
        "--no-validate",
        action="store_true",
        help="跳过下游官方校验器（默认会调用）",
    )

    args = parser.parse_args(argv)

    if args.command == "health":
        _dump(Database().status(), None)
        return 0

    service = SelectionService()
    if args.command == "demo":
        _dump(service.run(demo_spec()), args.out)
        return 0
    if args.command == "run":
        _dump(service.run(load_spec(args.spec)), args.out)
        return 0
    if args.command == "search":
        _dump(service.search(args.query, limit=args.limit), None)
        return 0
    if args.command == "handoff":
        result = service.run(load_spec(args.spec))
        store = DecisionStore(args.decisions)
        try:
            payload = build_handoff(
                result,
                store.all(),
                description=f"器件选型阶段导出｜需求版本 {result.get('requirement_version') or '未标注'}",
            )
        except ValueError as exc:
            print(f"无法导出：{exc}")
            return 2
        if not args.no_validate:
            try:
                validate_with_downstream(contract_only(payload), str(PCBAgent_ROOT))
                payload["validation"] = "passed"
                payload["validated_by"] = (
                    "netlist_end_to_end.generation_input.validate_generation_input"
                )
            except Exception as exc:  # noqa: BLE001
                payload["validation"] = "failed"
                payload["validation_error"] = f"{type(exc).__name__}: {exc}"
                _dump(payload, args.out)
                print("下游校验未通过，已保留诊断信息供排查。")
                return 3
        _dump(payload, args.out)
        print(f"导出 {len(payload['modules'])} 个模块；跳过 {len(payload['diagnostics']['skipped'])} 项。")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
