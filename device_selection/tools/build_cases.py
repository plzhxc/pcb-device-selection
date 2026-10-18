#!/usr/bin/env python3
"""把历史选型产物批量转成可复跑的测试案例。

素材：`workspace/web/output/case*/console/stages/03_device_selection__*.json`
（真实跑出来的选型结果，共 34 份），每个模块带 `estimated_parameters`
（含 importance / value / tolerance）和当时选中的器件。

转换口径：

* `importance = critical` → 硬性条件（违反即淘汰）；
* `importance = important` → 偏好条件；
* `importance = optional` → 不进条件，只记进 meta；
* 字段名先归一化再对照数据库真实字段，对不上的不硬塞，记进 `unmapped`；
* 器件类别 `leaf_code` 优先用当时确认器件在库里的真实归属，其次按类别关键词找。

每个案例输出到 `cases/<case>/`，另外生成 `index.json` 与 `README.md` 覆盖率报告。
"""

from __future__ import annotations

import argparse
import glob
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog import DeviceCatalog  # noqa: E402
from src.db import Database  # noqa: E402
from src.units import normalize_scalar, parse_interval, parse_quantity  # noqa: E402

DEFAULT_SOURCES = (
    "/share/home/zhengzhiqi/workspace/web/output/case*/console/stages/"
    "03_device_selection__*.json"
)
DEFAULT_OUT = ROOT / "cases"

SCHEMA_IN = "pcbagent.requirement_spec.v1"


class EmptyCase(Exception):
    """历史产物里没有可转换的模块（通常是那次运行失败了）。"""

# 历史参数键 → 数据库字段名。只放能确认存在的映射，其余如实记为未映射。
ALIAS: dict[str, str] = {
    "package_type": "package",
    "package_style": "package",
    "case_package": "package",
    "operating_temperature_range": "operating_temperature",
    "temperature_range": "operating_temperature",
    "supply_voltage_range": "input_voltage_range",
    "max_clock_frequency": "clock_frequency",
    "clock_speed": "clock_frequency",
    "max_output_current": "output_current",
    "continuous_output_current": "output_current",
    "output_current_max": "output_current",
    "mounting_style": "mounting_type",
    "mounting_method": "mounting_type",
    "current_rating_per_pin": "rated_current",
    "contact_current_rating": "rated_current",
}

# 键尾部的取值后缀，例如 output_voltage_3v3 → output_voltage、output_current_5a → output_current
_VALUE_TAIL = re.compile(
    r"(_(v|\d+v\d*|\d+_?\d*v|\d+a|\d+m?a|\d+m?hz|max|min|typ|nom|\d+))+$"
)

# 数值型字段的基准单位。只有从取值本身读不出单位时才用它兜底。
# 不用 tolerance 文本猜单位——那是一堆「±2%」之类的精度描述，会把单位带偏。
FIELD_UNIT: dict[str, str] = {
    "input_voltage_range": "V",
    "output_voltage": "V",
    "rated_voltage": "V",
    "output_current": "A",
    "rated_current": "A",
    "quiescent_current_iq": "A",
    "switching_frequency": "Hz",
    "clock_frequency": "Hz",
    "operating_temperature": "°C",
    "power": "W",
}


def normalize_key(key: str, fields: set[str]) -> str | None:
    raw = re.sub(r"[^0-9a-z]+", "_", str(key).lower()).strip("_")
    for candidate in (raw, ALIAS.get(raw, ""), _VALUE_TAIL.sub("", raw)):
        if not candidate:
            continue
        if candidate in fields:
            return candidate
        mapped = ALIAS.get(candidate)
        if mapped and mapped in fields:
            return mapped
    return None


def extract_value(spec: Mapping[str, Any]) -> tuple[Any, str, str]:
    """返回 (条件值, 单位, 比较符)。"""

    value = spec.get("value")
    if isinstance(value, bool):
        return ("是" if value else "否"), "", "is"
    if isinstance(value, (int, float)):
        return float(value), "", ">="
    if isinstance(value, Mapping):
        low, high = value.get("min"), value.get("max")
        if low is not None and high is not None:
            return [float(low), float(high)], "", "cover"
        if low is not None:
            return float(low), "", ">="
        if high is not None:
            return float(high), "", "<="
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [float(value[0]), float(value[1])], "", "cover"
        except (TypeError, ValueError):
            return str(value), "", "is"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "", ""
        interval = parse_interval(text)
        if interval is not None:
            return [interval.low, interval.high], interval.unit, "cover"
        quantity = parse_quantity(text)
        if quantity is not None:
            return quantity.value, quantity.unit, ">="
        if len(text) <= 24:
            return text, "", "is"
    return None, "", ""


def build_condition(key: str, spec: Mapping[str, Any], field: str) -> dict[str, Any] | None:
    value, unit, op = extract_value(spec)
    if value is None:
        return None
    # 历史参数常自带 unit（例如 switching_frequency 写着 kHz、值是 300~1000），
    # 用 clamp 到基准单位，避免把 300kHz 当成 300Hz。
    declared = str(spec.get("unit") or "").strip()
    if declared:
        if isinstance(value, (int, float)):
            scaled = normalize_scalar(value, declared)
            if scaled is not None:
                value, unit = _round(scaled.value), scaled.unit or unit
        elif isinstance(value, list) and len(value) == 2:
            low = normalize_scalar(value[0], declared)
            high = normalize_scalar(value[1], declared)
            if low is not None and high is not None:
                value, unit = [_round(low.value), _round(high.value)], low.unit or unit
    if not unit:
        unit = FIELD_UNIT.get(field, "")
    if isinstance(value, (int, float)):
        value = _round(float(value))
    elif isinstance(value, list):
        value = [_round(float(v)) for v in value]
    condition: dict[str, Any] = {"field": field, "op": op, "value": value}
    if unit:
        condition["unit"] = unit
    reasoning = str(spec.get("reasoning") or "").strip()
    if reasoning:
        condition["reason"] = reasoning[:200]
    condition["source_parameter"] = key
    return condition


def slugify(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "-", str(text)).strip("-").lower()


def _round(value: float) -> float:
    """单位换算会产生 9.999999999999999e-05 这类浮点噪声，收一下。"""

    return round(value, 12)


def module_title(raw: Mapping[str, Any]) -> str:
    name = str(raw.get("name") or "").strip()
    intent = str(raw.get("category_semantic_intent") or "").strip()
    return name or intent or str(raw.get("id") or "未命名模块")


def resolve_leaf(
    module: Mapping[str, Any],
    catalog: DeviceCatalog,
    leaf_index: Mapping[str, list[str]],
) -> tuple[str, str]:
    """返回 (leaf_code, 依据说明)。"""

    confirmed = module.get("confirmed_device") or {}
    model = str(confirmed.get("model") or confirmed.get("name") or "").strip()
    if model:
        try:
            hits = catalog.find_by_model(model, limit=1)
        except Exception:  # noqa: BLE001 - 查不到就退回关键词
            hits = []
        if hits:
            return hits[0].leaf_code, f"confirmed_device:{model}"

    mapping = module.get("category_mapping") or {}
    words: list[str] = []
    for key in ("primary", "secondary"):
        if mapping.get(key):
            words.append(str(mapping[key]))
    words.extend(str(w) for w in (mapping.get("keywords") or []))
    if module.get("type"):
        words.append(str(module["type"]))

    best = ""
    best_len = 0
    for word in words:
        token = str(word).strip()
        if len(token) < 2:
            continue
        for leaf_code, names in leaf_index.items():
            for name in names:
                if not name:
                    continue
                if token in name or name in token:
                    if len(name) > best_len:
                        best, best_len = leaf_code, len(name)
    if best:
        return best, f"category_keyword:{best_len}"
    return "", "unresolved"


def load_leaf_index(database: Database) -> dict[str, list[str]]:
    rows = database.query(
        "SELECT leaf_code, MAX(leaf_name_zh) AS zh, MAX(leaf_name_en) AS en,"
        " MAX(legacy_level2) AS l2 FROM dsearch_v_device_search_profiled_incremental"
        " GROUP BY leaf_code"
    )
    index: dict[str, list[str]] = {}
    for row in rows:
        index[str(row["leaf_code"])] = [
            str(row.get("zh") or ""),
            str(row.get("en") or ""),
            str(row.get("l2") or ""),
        ]
    return index


def convert_case(
    source: Path,
    *,
    catalog: DeviceCatalog,
    fields: set[str],
    leaf_index: Mapping[str, list[str]],
) -> dict[str, Any]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    data = payload.get("data") or payload
    modules = data.get("module_results") or []
    if not modules:
        # 历史运行本身失败（例如 agent 报错）的产物没有可转换的模块，
        # 不能当成有效案例灌进来，否则会污染回归基线。如实记下原因后跳过。
        raise EmptyCase(
            f"历史产物状态 {payload.get('status')!r}，错误 {str(payload.get('error'))[:120]!r}，无模块"
        )
    case_name = source.parent.parent.parent.name  # .../case17/console/stages/xx.json
    slug = slugify(case_name)

    items: list[dict[str, Any]] = []
    expected: dict[str, Any] = {}
    unmapped: Counter = Counter()
    used_ids: set[str] = set()
    stats = Counter()

    for index, entry in enumerate(modules, start=1):
        module = entry.get("module") or {}
        raw_id = str(module.get("id") or f"M-{index:03d}")
        item_id = raw_id if raw_id not in used_ids else f"{raw_id}-{index}"
        used_ids.add(item_id)

        leaf_code, leaf_source = resolve_leaf(module, catalog, leaf_index)
        stats[f"leaf_{leaf_source.split(':')[0]}"] += 1

        hard: list[dict[str, Any]] = []
        preferred: list[dict[str, Any]] = []
        optional: list[str] = []

        for key, spec in (module.get("estimated_parameters") or {}).items():
            spec = spec if isinstance(spec, Mapping) else {}
            importance = str(spec.get("importance") or "").strip()
            field = normalize_key(key, fields)
            if field is None:
                unmapped[key] += 1
                stats["unmapped"] += 1
                continue
            condition = build_condition(key, spec, field)
            if condition is None:
                unmapped[f"{key}(值不可比较)"] += 1
                stats["value_unusable"] += 1
                continue
            if importance == "critical":
                hard.append(condition)
                stats["hard"] += 1
            elif importance == "important":
                preferred.append(condition)
                stats["preferred"] += 1
            else:
                optional.append(field)
                stats["optional"] += 1

        confirmed = module.get("confirmed_device") or {}
        model = str(confirmed.get("model") or confirmed.get("name") or "").strip()
        item: dict[str, Any] = {
            "id": item_id,
            "module_name": module_title(module),
            "module_type": str(module.get("type") or ""),
            "category": str((module.get("category_mapping") or {}).get("primary") or ""),
            "leaf_code": leaf_code,
            "quantity": int(module.get("quantity") or 1),
            "related_requirements": [
                str(x) for x in (module.get("covers") or []) if str(x).strip()
            ],
            "hard_conditions": hard,
            "preferred_conditions": preferred,
            "keywords": [
                str(w)
                for w in ((module.get("category_mapping") or {}).get("keywords") or [])
            ][:6],
        }
        if model:
            item["specified_device"] = {"model": model, "intent": "reference"}
        if optional:
            item["note"] = "可选参数（未进条件）：" + "、".join(sorted(set(optional)))
        items.append(item)

        selected = entry.get("selected_devices") or []
        first = selected[0] if selected else {}
        device = first.get("device") or first or {}
        expected[item_id] = {
            "module_name": item["module_name"],
            "historical_selection": [
                {
                    "model": str((row.get("device") or row or {}).get("model")
                                 or (row.get("device") or row or {}).get("name") or ""),
                    "supplier_part": str((row.get("device") or row or {}).get("supplier_part") or ""),
                    "verification_status": str((row.get("device") or row or {}).get("verification_status") or ""),
                }
                for row in selected
            ],
            "historical_fit": str((first.get("evaluation") or {}).get("fit") or first.get("fit") or ""),
            "confirmed_device": model,
            "evaluation_source": str((first.get("evaluation") or {}).get("source") or ""),
        }
        if device:
            stats["modules_with_selection"] += 1

    spec = {
        "schema": SCHEMA_IN,
        "requirement_version": f"req-{slug}-v1",
        "project": {
            "name": f"{case_name}（由历史选型产物转换）",
            "summary": f"来源 {source}",
        },
        "global_constraints": {
            "budget_total_cny": float(data.get("total_budget") or 0) or None,
            "currency": "CNY",
            "note": "预算取自历史产物，仅供参考",
        },
        "selection_items": items,
    }

    meta = {
        "case": case_name,
        "source_file": str(source),
        "requirement_version": spec["requirement_version"],
        "modules": len(items),
        "conditions": {
            "hard": stats["hard"],
            "preferred": stats["preferred"],
            "optional_skipped": stats["optional"],
            "unmapped": stats["unmapped"],
            "value_unusable": stats["value_unusable"],
        },
        "leaf_resolution": {
            "from_confirmed_device": stats["leaf_confirmed_device"],
            "from_category_keyword": stats["leaf_category_keyword"],
            "unresolved": stats["leaf_unresolved"],
        },
        "modules_with_historical_selection": stats["modules_with_selection"],
        "unmapped_parameters": dict(unmapped.most_common(40)),
        "historical_total_budget": data.get("total_budget"),
    }

    expected_payload = {
        "case": case_name,
        "requirement_version": spec["requirement_version"],
        "note": "historical_selection 是当时那次运行选中的器件，用来做对照基线，不等于唯一正确答案。",
        "modules": expected,
    }
    return {"spec": spec, "expected": expected_payload, "meta": meta, "slug": slug}


def write_case(out_root: Path, case: Mapping[str, Any]) -> None:
    target = out_root / case["slug"]
    target.mkdir(parents=True, exist_ok=True)
    for name, key in (
        ("requirement_spec.json", "spec"),
        ("expected.json", "expected"),
        ("meta.json", "meta"),
    ):
        (target / name).write_text(
            json.dumps(case[key], ensure_ascii=False, indent=2), encoding="utf-8"
        )


def write_index(
    out_root: Path,
    cases: list[Mapping[str, Any]],
    skipped_sources: list[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    rows = []
    totals = Counter()
    for case in cases:
        meta = case["meta"]
        conditions = meta["conditions"]
        totals["cases"] += 1
        totals["modules"] += meta["modules"]
        totals["hard"] += conditions["hard"]
        totals["preferred"] += conditions["preferred"]
        totals["unmapped"] += conditions["unmapped"]
        rows.append(
            {
                "case": meta["case"],
                "path": f"{case['slug']}/requirement_spec.json",
                "requirement_version": meta["requirement_version"],
                "modules": meta["modules"],
                "hard_conditions": conditions["hard"],
                "preferred_conditions": conditions["preferred"],
                "unmapped_parameters": conditions["unmapped"],
                "leaf_unresolved": meta["leaf_resolution"]["unresolved"],
                "modules_with_historical_selection": meta[
                    "modules_with_historical_selection"
                ],
            }
        )
    index = {
        "schema": "pcbagent.device_selection.cases.v1",
        "generated_from": DEFAULT_SOURCES,
        "totals": dict(totals),
        "skipped_sources": list(skipped_sources or []),
        "cases": sorted(rows, key=lambda r: r["case"]),
    }
    (out_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return index


def write_readme(
    out_root: Path,
    index: Mapping[str, Any],
    skipped: list[Mapping[str, str]] | None = None,
) -> None:
    totals = index["totals"]
    skipped = list(skipped or [])
    rows = sorted(index["cases"], key=lambda r: -r["modules"])
    lines = [
        "# 器件选型测试案例库",
        "",
        "本目录由 `tools/build_cases.py` 从历史选型产物自动生成，用来固定回归口径：",
        "每次改动选型逻辑后跑同一批案例，对比结论有没有变。",
        "",
        "## 来源",
        "",
        "`workspace/web/output/case*/console/stages/03_device_selection__*.json`",
        "——真实跑出来的选型结果，每个模块带参数（含重要度）和当时选中的器件。",
        "",
        "## 转换口径",
        "",
        "| 历史内容 | 转成什么 |",
        "| --- | --- |",
        "| `importance = critical` 的参数 | 硬性条件（违反即淘汰） |",
        "| `importance = important` 的参数 | 偏好条件 |",
        "| `importance = optional` 的参数 | 不进条件，记进 `meta.json` |",
        "| `confirmed_device` | 选型项的指定器件（意图记为「仅作参考」） |",
        "| 当时选中的器件 | `expected.json` 的对照基线 |",
        "| 参数键对不上数据库字段 | 不硬塞，记进 `meta.json` 的 `unmapped_parameters` |",
        "",
        "字段名先归一化再对照数据库真实字段；类别 `leaf_code` 优先取当时确认器件在库里的真实归属。",
        "",
        "## 规模",
        "",
        f"- 案例 **{totals.get('cases', 0)}** 个，模块 **{totals.get('modules', 0)}** 个",
        f"- 硬性条件 **{totals.get('hard', 0)}** 条，偏好条件 **{totals.get('preferred', 0)}** 条",
        f"- 未能映射的参数 **{totals.get('unmapped', 0)}** 条（保留在 meta 里待人工确认）",
        "",
        "## 被跳过的素材",
        "",
        "历史产物里状态为失败的运行没有可转换的模块，不能灌进案例库污染基线，",
        "原因记录在 `index.json` 的 `skipped_sources`：",
        "",
        *(
            [f"- `{s['source'].split('/output/')[-1]}`：{s['reason']}" for s in skipped]
            or ["- （无）"]
        ),
        "",
        "## 怎么用",
        "",
        "```bash",
        "# 跑单个案例",
        "python3 cli.py run --spec cases/case17/requirement_spec.json --out output/case17.json",
        "",
        "# 批量回归（对比结论与历史基线的差异）",
        "python3 tools/run_cases.py",
        "```",
        "",
        "## 逐案例",
        "",
        "| 案例 | 模块 | 硬性条件 | 偏好条件 | 未映射参数 | 类别未解析 | 有历史选择 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| [{row['case']}]({row['path']}) | {row['modules']} | {row['hard_conditions']} |"
            f" {row['preferred_conditions']} | {row['unmapped_parameters']} |"
            f" {row['leaf_unresolved']} | {row['modules_with_historical_selection']} |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- `expected.json` 只是历史运行的对照基线，**不是唯一正确答案**；",
        "  当时有相当一部分模块没有选出器件，那些空结果本身就是需要改进的证据。",
        "- `meta.json` 里的 `unmapped_parameters` 是下一步可以补映射的地方，",
        "  补一条映射，案例覆盖就多一块。",
    ]
    (out_root / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量生成选型测试案例")
    parser.add_argument("--sources", default=DEFAULT_SOURCES)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个案例（调试用）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    sources = sorted(Path(p) for p in glob.glob(args.sources))
    if args.limit:
        sources = sources[: args.limit]
    if not sources:
        print(f"没有找到素材：{args.sources}")
        return 1

    database = Database()
    catalog = DeviceCatalog(database)
    rows = database.query(
        "SELECT field_key FROM dsearch_v_device_attribute_priority_incremental GROUP BY field_key"
    )
    fields = {str(r["field_key"]) for r in rows}
    leaf_index = load_leaf_index(database)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    cases = []
    skipped_sources: list[dict[str, str]] = []
    for source in sources:
        try:
            cases.append(
                convert_case(
                    source, catalog=catalog, fields=fields, leaf_index=leaf_index
                )
            )
        except EmptyCase as exc:
            skipped_sources.append({"source": str(source), "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 单个案例失败不该中断整批
            print(f"跳过 {source}：{type(exc).__name__}: {exc}")

    for case in cases:
        write_case(out_root, case)
    index = write_index(out_root, cases, skipped_sources)
    write_readme(out_root, index, skipped_sources)

    totals = index["totals"]
    print(
        f"生成 {totals['cases']} 个案例、{totals['modules']} 个模块；"
        f"硬性条件 {totals['hard']} 条，偏好条件 {totals['preferred']} 条，"
        f"未映射参数 {totals['unmapped']} 条。"
    )
    print(f"索引：{out_root / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
