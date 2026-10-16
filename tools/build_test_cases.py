#!/usr/bin/env python3
"""从原始案例生成手工测试用例（含两阶段的预期输出）。

素材是 `case_test/backup/cases/case*/input.txt` —— 那批原始需求描述。
对每个选中的案例：

1. 抄录原始输入（给需求分析工作台用）；
2. 从输入文本里**明确提到**的器件出发，在真实器件目录里确认身份；
3. 生成一份 RequirementSpec 并跑一次选型，把结果作为阶段二的预期输出；
4. 写出阶段一可核对的检查点（指定器件、功能点、必需模块）。

预期输出全部来自实际运行，不是手写的。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog import DeviceCatalog  # noqa: E402
from src.db import Database  # noqa: E402
from src.service import SelectionService  # noqa: E402

SOURCE_ROOT = Path("/share/home/zhengzhiqi/workspace/case_test/backup/cases")
SAMPLE_ROOT = ROOT.parent / "chenxin_handoff" / "samples"
OUT_DIR = ROOT / "testcases"

# 每批测试选这几个案例：覆盖单芯片、电源、混合信号、无线
DEFAULT_CASES = (
    "case31-key-sound-toy",
    "case30-pn532-nfc",
    "case25-stm32g4-foc",
    "case29-lora-radio",
)

# 用例 ↔ 工作台真实交接件的对应（按标题关键词匹配 samples/ 里的 RequirementSpec）
SAMPLE_HINTS: dict[str, tuple[str, ...]] = {
    "case31-key-sound-toy": ("按键发声玩具",),
    "case30-pn532-nfc": ("PN532RFIDNFC调试器", "PN532RFID"),
    "case25-stm32g4-foc": ("FOC电机驱动学习板",),
    "case29-lora-radio": ("基于LoRa的无线通信装置",),
}

# 手工加强版条件：原文里**明确写了数字**的指标，逐条标注出处原话。
# 工作台产出的约束大多没有数值，加这一版是为了把判定逻辑（满足/未知/阻塞）也测到。
MANUAL_CONDITIONS: dict[str, list[dict[str, Any]]] = {
    "case30-pn532-nfc": [
        {
            "model": "RT9013-33GB",
            "field": "output_voltage",
            "op": "==",
            "value": 3.3,
            "unit": "V",
            "quote": "由RT9013双路LDO（1.2V内核+3.3V IO）提供低噪声稳压电源管理",
        }
    ],
    "case25-stm32g4-foc": [
        {
            "model": "RS624XQ",
            "field": "channel_count",
            "op": ">=",
            "value": 4,
            "quote": "通过RS624XQ四通道轨到轨运放实现三相低边电流采样",
        }
    ],
    "case29-lora-radio": [
        {
            "model": "SPX3819",
            "field": "output_voltage",
            "op": "==",
            "value": 3.3,
            "unit": "V",
            "quote": "SPX3819超低噪声LDO稳压（3.3V输出）",
        },
        {
            "model": "TP4056X",
            "field": "maximum_charging_current",
            "op": ">=",
            "value": 1,
            "unit": "A",
            "quote": "由TP4056X线性锂电池充电管理芯片（最大1A充电电流）",
        },
        {
            "model": "SX1308",
            "field": "output_current",
            "op": ">=",
            "value": 2,
            "unit": "A",
            "quote": "SX1308 boost升压芯片（2A开关电流，最高28V输出）",
        },
    ],
}

# 型号提取：不能用 \b —— 中文和拉丁字母在 Python 里都算 \w，边界不存在，
# 「采用CX588」这种紧贴中文的型号会被整条漏掉。
MODEL_PATTERN = re.compile(r"([A-Za-z]{2,}[A-Za-z0-9]*(?:[-/][A-Za-z0-9.]+){0,4})")

# 泛称与协议名，不是型号
STOPWORDS = frozenset(
    {
        "ANT", "IO", "LDO", "LED", "PCB", "PIN", "UART", "USB", "RFID", "NFC", "SPI",
        "ADC", "DAC", "PWM", "MCU", "CPU", "RAM", "ROM", "LDO", "DCDC", "ESD", "EMI",
        "USB-C", "TYPE-C", "NTAG", "Mifare", "Classic", "FeliCa", "NTAG21x",
    }
)


def read_input(case: str) -> str:
    return (SOURCE_ROOT / case / "input.txt").read_text(encoding="utf-8").strip()


def mentioned_models(text: str) -> list[str]:
    """从需求描述里粗提型号串。

    判据：含数字、长度够、不是泛称。真正的身份确认交给器件目录，
    这里只负责"提名"，找不到就如实标待核验。
    """

    found: list[str] = []
    for raw in MODEL_PATTERN.findall(text):
        token = raw.strip(" -.")
        if len(token) < 4 or token.upper() in STOPWORDS:
            continue
        if not any(ch.isdigit() for ch in token):
            continue
        if token.lower().startswith(("http", "com", "www")):
            continue
        if "/" in token and not re.fullmatch(r"[A-Za-z0-9]+(/[A-Za-z0-9.]+)?", token):
            continue
        if token not in found:
            found.append(token)
    return found


def resolve(models: Iterable[str], catalog: DeviceCatalog) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for model in models:
        try:
            hits = catalog.find_by_model(model, limit=1)
        except Exception:  # noqa: BLE001
            hits = []
        if hits:
            device = hits[0]
            rows.append(
                {
                    "mention": model,
                    "matched": True,
                    "model": device.model,
                    "device_id": device.device_id,
                    "leaf_code": device.leaf_code,
                    "leaf_name_zh": device.leaf_name_zh,
                    "supplier_part": "",
                }
            )
        else:
            rows.append({"mention": model, "matched": False})
    return rows


def build_spec(case: str, text: str, devices: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, device in enumerate(
        [d for d in devices if d.get("matched")], start=1
    ):
        items.append(
            {
                "id": f"SEL-{index:03d}",
                "module_name": f"{device['mention']} 所在功能模块",
                "category": device["leaf_name_zh"],
                "leaf_code": device["leaf_code"],
                "quantity": 1,
                "specified_device": {"model": device["model"], "intent": "reference"},
                "note": "由原始需求文本中明确提到的型号生成，意图记为参考",
            }
        )
    return {
        "schema": "pcbagent.requirement_spec.v1",
        "requirement_version": f"req-{case}-v1",
        "project": {"name": case, "summary": text.splitlines()[0][:120]},
        "global_constraints": {"currency": "CNY", "note": "测试用例，不含预算"},
        "selection_items": items,
    }


def load_sample_spec(case: str) -> tuple[dict[str, Any] | None, str]:
    """找用例对应的真实交接件。"""

    hints = SAMPLE_HINTS.get(case, ())
    if not hints:
        return None, ""
    for path in sorted(SAMPLE_ROOT.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        title = str(spec.get("title") or "")
        if any(hint in title for hint in hints):
            return spec, str(spec.get("spec_id") or path.stem)
    return None, ""


def to_selection_input(sample: dict[str, Any]) -> dict[str, Any]:
    """把工作台的真实 spec 转成本阶段输入。"""

    from src.chenxin import to_selection_spec  # noqa: PLC0415

    converted, _ = to_selection_spec(sample, catalog=DeviceCatalog(Database()))
    return converted


def with_manual_conditions(
    spec: dict[str, Any], case: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """在真实 spec 上追加原文写明数字的条件（不改动原字段）。"""

    payload = json.loads(json.dumps(spec, ensure_ascii=False))
    added: list[dict[str, Any]] = []
    for rule in MANUAL_CONDITIONS.get(case, []):
        model = rule["model"]
        applied = False
        for item in payload.get("selection_items", []):
            specified = (item.get("specified_device") or {}).get("model") or ""
            mention = " ".join(
                [str(item.get("module_name") or ""), str(item.get("category") or "")]
            )
            # 真实 spec 里存的是原文里的写法（SPX3819），库里查到的是全称
            # （SPX3819M5-L-3-3/TR），两个方向都要能对上。
            matched = bool(specified) and (
                model in specified or specified in model
            )
            if not matched and model not in mention:
                continue
            condition = {
                "field": rule["field"],
                "op": rule["op"],
                "value": rule["value"],
                "reason": f"原文：{rule['quote']}",
            }
            if rule.get("unit"):
                condition["unit"] = rule["unit"]
            item.setdefault("hard_conditions", []).append(condition)
            added.append({"item_id": item["id"], "model": model, **condition, "quote": rule["quote"]})
            applied = True
            break
        if not applied:
            added.append({"item_id": "", "model": model, "applied": False,
                          "quote": rule["quote"], "note": "在真实 spec 的选型项里没找到该型号"})
    return payload, added


def render_case(
    case: str,
    text: str,
    devices: list[dict[str, Any]],
    sample_id: str,
    real_spec: dict[str, Any] | None,
    real_result: dict[str, Any] | None,
    manual_spec: dict[str, Any] | None,
    manual_result: dict[str, Any] | None,
    manual_added: list[dict[str, Any]],
) -> str:
    matched = [d for d in devices if d.get("matched")]
    unmatched = [d for d in devices if not d.get("matched")]
    lines = [
        f"## 测试用例：{case}",
        "",
        "### 阶段一输入（粘进需求分析工作台）",
        "",
        "```text",
        text,
        "```",
        "",
        "### 阶段一预期输出",
        "",
        "工作台会把它整理成需求清单与选型项。可核对的检查点：",
        "",
        f"- **明确提到的器件 {len(devices)} 个**，其中能在器件目录里确认身份的 {len(matched)} 个：",
        "",
    ]
    for device in matched:
        lines.append(
            f"  - `{device['mention']}` → 目录型号 **{device['model']}**"
            f"（{device['leaf_name_zh'] or device['leaf_code']}，device_id={device['device_id']}）"
        )
    if unmatched:
        lines += ["", "  以下出现在原文里但目录里没查到，应标成「待核验」而不是编造：", ""]
        for device in unmatched:
            lines.append(f"  - `{device['mention']}`")
    lines += [
        "",
        "- **检查点 A**：原文点名的器件必须出现在选型项的「指定/优先/参考」里，且意图不能升级成「必须」；",
        "- **检查点 B**：原文里的功能点数（「支持」后面列的那串）应当逐条落到需求条目上，不能丢；",
        "- **检查点 C**：查不到的器件保留为待核验，不得替换成相近型号；",
        "- **检查点 D**：交接状态应是 `ready` 或 `ready_with_open_items`，不应是 `blocked`。",
        "",
        "### 阶段二输入",
        "",
    ]
    if real_spec is None:
        lines += [
            f"原始案例里点名的器件在器件目录中都查不到（见上），工作台**不会**产出可选的",
            "选型项。因此阶段二应输出「无候选 / 待补充」，这本身就是预期结果——",
            "它验证的是红线「查不到不许编」。",
            "",
        ]
    else:
        lines += [
            f"用工作台真实交接件（`{sample_id}`）转成的输入，"
            f"已存为 `{case}/requirement_spec.real.json`。",
            "",
            "**A. 真实交接件跑出来的结果（当前实现的实际表现）**",
            "",
        ]
        lines += _result_block(real_result)
        if manual_spec is not None:
            lines += [
                "",
                "**B. 原文写明数字的加强版（用来测判定逻辑）**",
                "",
                "在真实交接件上追加了这些条件（每条都标了原文出处）：",
                "",
            ]
            for entry in manual_added:
                if entry.get("applied") is False:
                    lines.append(f"- ⚠️ {entry['model']}：{entry['note']}（原文：{entry['quote']}）")
                else:
                    lines.append(
                        f"- {entry['item_id']} `{entry['field']} {entry['op']} "
                        f"{entry['value']}{entry.get('unit') or ''}` ← {entry['quote']}"
                    )
            lines += ["", f"输入见 `{case}/requirement_spec.manual.json`，结果：", ""]
            lines += _result_block(manual_result)

    lines += [
        "",
        "### 怎么判定通过",
        "",
        "1. 阶段一检查点 A~D 全部成立（重点看有没有把「参考」升级成「必须」、有没有编造型号）；",
        "2. 阶段二给出的推荐项，逐条展开证据应能指回器件目录记录或数据手册片段；",
        "3. 标成「未核对」或「阻塞」的项，要能说清是缺条件、缺候选还是证据不足；",
        "4. 不得出现「没有证据却显示为推荐」的绿色项。",
        "",
        f"（本用例的阶段二结果是实测导出，见 `{case}/result.json`）",
    ]
    return "\n".join(lines)


def _result_block(result: dict[str, Any]) -> list[str]:
    summary = result["summary"]
    lines = [
        f"- 选型项 **{summary['total_items']}**；给出推荐 **{summary['items_with_recommendation']}**；"
        f"未核对 **{summary['items_unverified']}**；阻塞 **{summary['items_blocked']}**。",
        "",
        "| 选型项 | 指定器件 | 召回方式 | 候选数 | 预期结论 | 预期首选 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in result["items"]:
        selection = item["selection_item"]
        pick = "、".join(item["primary_candidates"][:2]) or "（无）"
        if item.get("unverified"):
            state = "未核对"
        elif item["primary_candidates"]:
            state = "有推荐"
        else:
            state = "阻塞"
        lines.append(
            f"| {selection['id']} | {selection.get('specified_device', {}).get('model') or '—'} |"
            f" {item['recall']} | {item['candidate_count']} | {state} | {pick} |"
        )

    checked = [
        i for i in result["items"] if i["primary_candidates"] and not i.get("unverified")
    ]
    if checked:
        lines += ["", "**逐条判定（页面上展开「证据」逐条核对）：**", ""]
        for item in checked:
            lines.append(
                f"- {item['selection_item']['id']}（{item['selection_item'].get('module_name')}）"
                f" 首选 {item['candidates'][0]['model']}，结论 {item['candidates'][0]['evaluation']['fit']}："
            )
            for verdict in item["candidates"][0]["evaluation"]["satisfied"]:
                lines.append(f"  - 满足 `{verdict['field']}`：器件 {verdict['device_value']}")
            for verdict in item["candidates"][0]["evaluation"]["violated"]:
                lines.append(f"  - 不满足 `{verdict['field']}`：器件 {verdict['device_value']}")
            for verdict in item["candidates"][0]["evaluation"]["unknown"]:
                lines.append(f"  - 未知 `{verdict['field']}`：{verdict['note'][:90]}")

    blocked = [
        i for i in result["items"] if i.get("unverified") or not i["primary_candidates"]
    ]
    if blocked:
        lines += ["", "**应显示为非绿色的项：**", ""]
        for item in blocked:
            reasons = item["blocked_by"] or item.get("quality_notes") or []
            lines.append(
                f"- {item['selection_item']['id']}：{'；'.join(reasons) or '未做参数核对'}"
            )
    return lines


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成两阶段手工测试用例")
    parser.add_argument("--cases", nargs="*", default=list(DEFAULT_CASES))
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args(list(argv) if argv is not None else None)

    database = Database()
    catalog = DeviceCatalog(database)
    service = SelectionService()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    sections: list[str] = []
    index: list[dict[str, Any]] = []
    for case in args.cases:
        text = read_input(case)
        devices = resolve(mentioned_models(text), catalog)
        case_dir = out_root / case
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "input.txt").write_text(text, encoding="utf-8")

        sample, sample_id = load_sample_spec(case)
        real_spec = real_result = None
        manual_spec = manual_result = None
        manual_added: list[dict[str, Any]] = []
        if sample is not None:
            real_spec = to_selection_input(sample)
            real_result = service.run(real_spec)
            (case_dir / "requirement_spec.real.json").write_text(
                json.dumps(real_spec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (case_dir / "result.real.json").write_text(
                json.dumps(real_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if MANUAL_CONDITIONS.get(case):
                manual_spec, manual_added = with_manual_conditions(real_spec, case)
                manual_result = service.run(manual_spec)
                (case_dir / "requirement_spec.manual.json").write_text(
                    json.dumps(manual_spec, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (case_dir / "result.manual.json").write_text(
                    json.dumps(manual_result, ensure_ascii=False, indent=2), encoding="utf-8"
                )

        sections.append(
            render_case(
                case,
                text,
                devices,
                sample_id,
                real_spec,
                real_result,
                manual_spec,
                manual_result,
                manual_added,
            )
        )
        index.append(
            {
                "case": case,
                "mentioned_devices": len(devices),
                "resolved_devices": sum(1 for d in devices if d.get("matched")),
                "sample_spec_id": sample_id,
                "selection_items": (real_result or {}).get("summary", {}).get("total_items", 0),
                "recommended": (real_result or {}).get("summary", {}).get("items_with_recommendation", 0),
                "unverified": (real_result or {}).get("summary", {}).get("items_unverified", 0),
                "manual_recommended": (manual_result or {}).get("summary", {}).get("items_with_recommendation"),
            }
        )
        print(
            f"{case}: 提到器件 {len(devices)}，目录确认 "
            f"{sum(1 for d in devices if d.get('matched'))}，"
            f"真实交接件 {sample_id or '（无）'}，"
            f"选型项 {(real_result or {}).get('summary', {}).get('total_items', 0)}，"
            f"加强版有推荐 {(manual_result or {}).get('summary', {}).get('items_with_recommendation', '-')}"
        )

    header = [
        "# 器件选型阶段 · 手工测试用例",
        "",
        "从原始案例（`case_test/backup/cases/case*/input.txt`）里挑出来的，覆盖单芯片、",
        "电源、混合信号、无线四类。每个用例给出**两个阶段**的输入与预期输出。",
        "",
        "| 用例 | 原文点名器件 | 目录可确认 | 选型项 | 有推荐 | 未核对 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in index:
        header.append(
            f"| [{row['case']}]({row['case']}/input.txt) | {row['mentioned_devices']} |"
            f" {row['resolved_devices']} | {row['selection_items']} | {row['recommended']} |"
            f" {row['unverified']} |"
        )
    header += [
        "",
        "## 测试时怎么走",
        "",
        "1. **阶段一**：打开需求分析工作台 <http://localhost:8878/>，新建需求，"
        "把用例里的 `input.txt` 原文粘进对话框；按「阶段一预期输出」的检查点 A~D 核对。",
        "2. **阶段二**：交接完成后，用 `tools/bridge_studio.py run --project <项目编号>` "
        "把我的阶段接上去；或直接把用例里的 `requirement_spec.json` 跑一遍：",
        "",
        "   ```bash",
        "   python3 cli.py run --spec testcases/<case>/requirement_spec.json --out /tmp/r.json",
        "   ```",
        "",
        "3. 页面上展开每一行的「证据」，逐条对照本文档的预期。",
        "",
        "---",
        "",
    ]
    (out_root / "README.md").write_text(
        "\n".join(header + sections) + "\n", encoding="utf-8"
    )
    (out_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已写入 {out_root / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
