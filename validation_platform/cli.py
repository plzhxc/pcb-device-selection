"""命令行入口：新格式完整图/引脚子图比较，以及旧格式正式三指标。

新格式自动识别 NETLIST_FORMAT.md v1.2/v1.3 的 pins 数组；输出单个 metrics.json。
--gen-pins gge1:1 gge2:3 --ref-pins gge8:1 gge9:3 比较两个端点间的外围子图。
新格式退出码：0 等价，1 不等价，2 歧义、搜索未完成或输入错误。

用法::

    # 方式 1：两个 flat netlist JSON
    python -m validation_platform.cli \\
        --gen path/to/gen.json \\
        --ref path/to/ref.json \\
        --out my_compare

    # 方式 2：gen JSON + .enet 参考目录
    python -m validation_platform.cli \\
        --gen gen.json \\
        --ref-enet path/to/reference \\
        --out my_compare

    # 方式 3：任一已注册 reference 格式（可自动检测）
    python -m validation_platform.cli \\
        --gen gen.json \\
        --ref-source path/to/reference \\
        --ref-format easyeda_pcb_extract_v1 \\
        --out my_compare

输入格式（gen 和 ref 都用同样的 flat dict）::

    {
      "gge1": {
        "props": {"designator": "R1", "deviceName": "RES 10k 0402",
                  "supplierPart": "C25804", "value": "10k",
                  "deviceType": "resistance"},
        "pins":  {"1": "VCC", "2": "PB12"}
      },
      ...
    }

输出（``--out`` 指定时）：
- ``metrics.json`` — 正式三指标
- ``gen_netlist_combined.json`` — 生成网表副本
- ``ref_netlist_combined.json`` — 参考网表副本
"""
from __future__ import annotations
import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Sequence

from validation_platform.coverage import compute_coverage
from validation_platform.ref_parser import device_count_summary
from validation_platform.reference import load_reference
from validation_platform.status_report import render_evaluation_status
from validation_platform.netlist_graph import is_cleaned_netlist, read_netlist_json


_OUTPUT_ROOT = Path(__file__).resolve().parents[2] / "07_TEMP" / "validation-platform"


def _output_path(value: str | Path) -> Path:
    requested = Path(value).expanduser()
    output = (
        requested.resolve()
        if requested.is_absolute()
        else (_OUTPUT_ROOT / requested).resolve()
    )
    root = _OUTPUT_ROOT.resolve()
    if output != root and not output.is_relative_to(root):
        raise argparse.ArgumentTypeError(
            f"validation output must stay inside {root}"
        )
    return output


def _load_netlist(p: Path) -> Dict[str, Any]:
    if not p.exists():
        raise FileNotFoundError(p)
    data = read_netlist_json(p)
    if not isinstance(data, dict):
        raise ValueError(f"{p} 不是 dict 形态的 flat netlist")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    ap = argparse.ArgumentParser(description="网表比较：新格式完整拓扑/引脚子图及取值；兼容旧格式三指标")
    ap.add_argument("--gen", required=True, help="生成网表 JSON；新格式还支持 case 目录")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--ref",      help="参考网表 JSON；新格式还支持 case 目录")
    grp.add_argument("--ref-enet", help="ref .enet 目录路径 (多页参考网表)")
    grp.add_argument(
        "--ref-source",
        help="通用 reference 文件或目录（新格式 JSON/.enet/PCB JSON/UTF-16 BOM）",
    )
    ap.add_argument(
        "--ref-format",
        help="显式 reference 格式；与 --ref-source/--ref-enet 配合使用",
    )
    ap.add_argument(
        "--out",
        type=_output_path,
        help="07_TEMP/validation-platform 内的落档目录；省略只打印终端",
    )
    ap.add_argument("--name", default="adhoc", help="报告里显示的 case 名（默认 adhoc）")
    ap.add_argument(
        "--identity-map",
        help="外部身份映射 JSON（{\"aliases\": {型号/来源token: C编号}}），"
             "比较前把两侧器件身份归一到 C 编号空间",
    )
    ap.add_argument("--netlist-format", choices=("auto", "cleaned", "legacy"), default="auto",
                    help="默认识别新格式 pins 数组；cleaned 启用完整图比较")
    ap.add_argument("--gen-pins", nargs=2, metavar=("FROM", "TO"),
                    help="生成侧两个核心端点，格式 gge编号:引脚编号；提取完整外围子图")
    ap.add_argument("--ref-pins", nargs=2, metavar=("FROM", "TO"),
                    help="参考侧对应端点，顺序与 --gen-pins 一致")
    ap.add_argument("--strict-net-names", action="store_true", help="新格式比较同时约束网名")
    ap.add_argument("--max-match-states", type=int, default=100000, help="新格式图匹配搜索上限")
    ap.add_argument("--match-timeout", type=float, default=10.0, help="每对子图匹配搜索时限（秒）")
    args = ap.parse_args(argv)

    try:
        gen = None if Path(args.gen).is_dir() else _load_netlist(Path(args.gen))
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    cleaned = args.netlist_format == "cleaned" or (args.netlist_format == "auto" and (
        Path(args.gen).is_dir() or is_cleaned_netlist(gen) or args.gen_pins or args.ref_pins
        or args.ref_format in {"cleaned", "cleaned_json_v1.2", "cleaned_json_v1.3"}))
    ref_path = args.ref or args.ref_source
    if not cleaned and args.netlist_format == "auto" and ref_path and Path(ref_path).suffix.lower() == ".json":
        try:
            cleaned = is_cleaned_netlist(read_netlist_json(ref_path))
        except (ValueError, OSError) as exc:
            ap.error(str(exc))
    if cleaned:
        if args.ref_format not in {None, "cleaned", "cleaned_json_v1.2", "cleaned_json_v1.3"}:
            ap.error("新格式图比较只接受 cleaned reference 格式")
        from validation_platform.graph_cli import run_graph_comparison
        try:
            return run_graph_comparison(args)
        except (ValueError, OSError) as exc:
            ap.error(str(exc))
    if args.gen_pins or args.ref_pins or args.strict_net_names:
        ap.error("引脚子图和网名约束选项只适用于新格式网表")
    if is_cleaned_netlist(gen):
        ap.error("新格式网表不能使用 legacy 比较；请使用 auto 或 cleaned")
    if args.ref and args.ref_format:
        ap.error("--ref-format 不能与 flat JSON --ref 一起使用")

    if gen is None:
        ap.error("旧格式生成网表必须是 JSON 文件")
    reference_metadata = None
    capability = None
    if args.ref:
        ref = _load_netlist(Path(args.ref))
        source_label = args.ref
    else:
        source_label = args.ref_source or args.ref_enet
        reference_format = args.ref_format
        if args.ref_enet and reference_format is None:
            reference_format = "easyeda_enet_v2"
        bundle = load_reference(source_label, reference_format=reference_format)
        ref = bundle.netlist
        capability = bundle.capability
        reference_metadata = {
            "capability": bundle.capability.as_dict(),
            "provenance": bundle.provenance.as_dict(),
            "warnings": list(bundle.warnings),
        }
    if not ref:
        raise ValueError(f"ref 没有解析到任何器件: {source_label}")
    print(f"[gen] {len(gen)} devices")
    print(f"[ref] {len(ref)} devices")

    identity_bridge_stats = None
    if args.identity_map:
        from validation_platform.identity_bridge import (
            apply_identity_bridge,
            load_identity_map,
        )

        aliases = load_identity_map(Path(args.identity_map))
        gen, gen_applied = apply_identity_bridge(gen, aliases)
        ref, ref_applied = apply_identity_bridge(ref, aliases)
        identity_bridge_stats = {
            "aliases": len(aliases),
            "gen_bridged": gen_applied,
            "ref_bridged": ref_applied,
        }
        print(f"[identity-bridge] aliases={len(aliases)} gen={gen_applied} ref={ref_applied}")

    formal_result = compute_coverage(gen, ref, capability=capability)
    formal = formal_result.get("formal_metrics") or {}
    count = formal.get("component_count") or {}
    value = formal.get("component_value_accuracy") or {}
    net = formal.get("net_connection_accuracy") or {}
    print()
    print("========== 正式验收指标 ==========")
    print(f"  正式结论: {formal.get('formal_status')} / pass={formal.get('pass')}")
    _print_count_metric(count)
    _print_accuracy_metric("元件取值准确率（连接关系约束）", value)
    _print_accuracy_metric("线网连接准确率", net)
    weak_value = formal.get("component_value_multiset")
    if weak_value:
        _print_accuracy_metric("弱证据：元件取值多重集", weak_value)
        print(f"    限制: {weak_value.get('evidence_limit')}")
    if reference_metadata:
        for warning in reference_metadata["warnings"]:
            print(f"  reference warning: {warning}")

    if args.out:
        out_dir = args.out
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        out_dir.chmod(0o700)
        ref_summary = device_count_summary(ref)
        (out_dir / "metrics.json").write_text(json.dumps({
            "formal_metrics": formal_result.get("formal_metrics") or {},
            "summary":     formal_result.get("summary") or {},
            "ref_summary": ref_summary,
            "reference":   reference_metadata,
            "identity_bridge": identity_bridge_stats,
            "ts":          datetime.now().isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        if reference_metadata:
            (out_dir / "reference_metadata.json").write_text(
                json.dumps(reference_metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        (out_dir / "evaluation_status.md").write_text(
            render_evaluation_status(formal, reference_metadata),
            encoding="utf-8",
        )
        (out_dir / "gen_netlist_combined.json").write_text(
            json.dumps(gen, ensure_ascii=False, indent=2), encoding="utf-8")
        (out_dir / "ref_netlist_combined.json").write_text(
            json.dumps(ref, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  落档: {out_dir}")
    return 0


def _print_count_metric(metric: Dict[str, Any]) -> None:
    if not metric.get("available", True):
        print(f"  元件数量偏差: ⏭ skipped ({metric.get('reason')})")
        return
    print(
        f"  元件数量偏差: type_abs_delta={metric.get('type_abs_delta_sum', '?')} "
        f"/ ref={metric.get('ref', '?')} "
        f"{'✅' if metric.get('pass') else '❌'}"
    )


def _print_accuracy_metric(label: str, metric: Dict[str, Any]) -> None:
    if not metric.get("available", True):
        print(f"  {label}: ⏭ skipped ({metric.get('reason')})")
        return
    print(
        f"  {label}: {float(metric.get('accuracy_pct', 0.0)):5.1f}%  "
        f"{'✅' if metric.get('pass') else '❌'} "
        f"({metric.get('matched', 0)}/{metric.get('ref_total', 0)})"
    )


if __name__ == "__main__":
    raise SystemExit(main())
