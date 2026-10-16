"""交接：把候选清单与用户决定转成下游生成入口能吃的格式。

下游契约是 `pcbagent.netlist_input.v1`（见
`01_PCBAgent/netlist_end_to_end/generation_input.py`），要求：

* 至少 1 个 module，最多 128 个；
* 每个 module 必须有非空的 `id` 与 `name`，且 id 唯一、前后无空白；
* 每个 module 的 `selected_devices` 非空；
* 每个器件必须有 `model` 或 `supplier_part`。

本模块的红线：

1. **只有用户确认或保留的候选才导出**，待讨论和排除一律不导出；
2. 没有选定器件的选型项**不导出**，并写进 `skipped` 说明原因，不用占位件凑数；
3. 导出前后都可以用下游官方校验器验证，校验不过就整个失败，不产出半成品。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from .decisions import SELECTED, SHORTLIST

SCHEMA = "pcbagent.netlist_input.v1"
MAX_MODULES = 128

_WHITESPACE = re.compile(r"\s+")


def _clean_id(value: str) -> str:
    """下游要求 id 唯一且前后无空白。"""

    return _WHITESPACE.sub("_", str(value or "").strip())


def _candidate_index(item: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(candidate.get("model")): candidate
        for candidate in item.get("candidates", [])
        if candidate.get("model")
    }


def select_for_item(
    item: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """挑出这个选型项里用户选定的候选，返回 (器件, 跳过原因)。"""

    selection_item = item.get("selection_item") or {}
    item_id = str(selection_item.get("id") or "")
    index = _candidate_index(item)
    chosen: list[dict[str, Any]] = []
    skipped: list[str] = []

    for model, record in decisions.items():
        if str(record.get("item_id")) != item_id:
            continue
        decision = str(record.get("decision") or "")
        if decision not in SELECTED:
            if decision in SHORTLIST:
                skipped.append(
                    f"{model}：状态是「保留」（候选短名单），尚未「确认」，未导出"
                )
            continue
        candidate = index.get(str(model))
        if candidate is None:
            skipped.append(f"{model}：决定里有它，但当前候选清单里没有这个型号，未导出")
            continue
        if candidate.get("evaluation", {}).get("excluded"):
            # 理论上不会发生（排除的候选不该被确认），真发生就拦下来
            skipped.append(f"{model}：违反了硬性条件，拒绝导出")
            continue
        if not (candidate.get("model") or candidate.get("supplier_part")):
            skipped.append(f"{model}：既没有型号也没有料号，拒绝导出")
            continue
        chosen.append(candidate)

    return chosen, skipped


def _device_block(candidate: Mapping[str, Any], selection_item: Mapping[str, Any]) -> dict[str, Any]:
    """构造下游的 device 对象，字段尽量贴它已有的习惯。"""

    evaluation = candidate.get("evaluation") or {}
    satisfied = [
        f"{verdict.get('field')}: {verdict.get('device_value')}"
        for verdict in evaluation.get("satisfied", [])
    ]
    risks = [
        f"{verdict.get('field')}: {verdict.get('note')}"
        for verdict in evaluation.get("unknown", [])
    ]
    if candidate.get("price_status") != "confirmed":
        risks.append("成本缺少证据，价格待确认")

    device: dict[str, Any] = {
        "name": str(candidate.get("model") or ""),
        "model": str(candidate.get("model") or ""),
        "role": str(selection_item.get("category") or ""),
        "verification_status": (
            "confirmed" if candidate.get("supplier_part") else "unverified"
        ),
        "evidence": (
            f"器件目录 device_id={candidate.get('device_id')}"
            + (f"，立创料号 {candidate.get('supplier_part')}" if candidate.get("supplier_part") else "")
            + f"，来源 {candidate.get('leaf_code')}"
        ),
        "matched_requirements": satisfied,
        "risks": risks,
        "selection_fit": evaluation.get("fit"),
    }
    if candidate.get("manufacturer"):
        device["manufacturer"] = str(candidate["manufacturer"])
    if candidate.get("supplier_part"):
        device["supplier_part"] = str(candidate["supplier_part"])
    return device


def build_handoff(
    result: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]],
    *,
    description: str = "",
) -> dict[str, Any]:
    """候选清单 + 用户决定 → `pcbagent.netlist_input.v1`。

    返回的内容里除契约字段外，另加 `diagnostics` 说明被跳过的项；
    写盘前由调用方剥掉它再交给下游校验器。
    """

    decisions_by_item: dict[str, dict[str, dict[str, Any]]] = {}
    for record in decisions.values():
        decisions_by_item.setdefault(str(record.get("item_id")), {})[
            str(record.get("model"))
        ] = dict(record)

    modules: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    used_ids: set[str] = set()

    for item in result.get("items", []):
        selection_item = item.get("selection_item") or {}
        item_id = str(selection_item.get("id") or "")
        item_decisions = decisions_by_item.get(item_id, {})

        chosen, reasons = select_for_item(item, item_decisions)
        for reason in reasons:
            skipped.append({"item_id": item_id, "reason": reason})

        if not chosen:
            # 已经逐条说明过原因的就不要再补一条泛泛的，避免同一个选型项出现两遍
            if not reasons:
                pending = [
                    f"{model}（{record.get('decision_label') or record.get('decision')}）"
                    for model, record in item_decisions.items()
                    if record.get("decision") not in SELECTED
                ]
                detail = (
                    "用户决定为：" + "、".join(pending)
                    if pending
                    else "用户尚未做选择"
                )
                skipped.append(
                    {"item_id": item_id, "reason": f"没有选定器件，未导出（{detail}）"}
                )
            continue

        module_id = _clean_id(item_id)
        if not module_id or module_id in used_ids:
            skipped.append({"item_id": item_id, "reason": f"模块编号 {module_id!r} 非法或重复，未导出"})
            continue
        used_ids.add(module_id)

        module_name = str(
            selection_item.get("module_name")
            or selection_item.get("category")
            or item_id
        )
        module: dict[str, Any] = {
            "id": module_id,
            "name": module_name,
            "quantity": max(int(selection_item.get("quantity") or 1), 1),
        }
        if selection_item.get("module_type"):
            module["type"] = str(selection_item["module_type"])
        if selection_item.get("leaf_code"):
            module["category"] = str(selection_item["leaf_code"])
        if selection_item.get("related_requirements"):
            module["related_requirements"] = list(selection_item["related_requirements"])

        modules.append(
            {
                "module": module,
                "selected_devices": [
                    {
                        "device": _device_block(candidate, selection_item),
                        "quantity": max(int(selection_item.get("quantity") or 1), 1),
                        "reason": "用户已确认/保留（资料核验状态与选择状态分别记录）",
                    }
                    for candidate in chosen
                ],
                "provenance": {
                    "selection_item_id": item_id,
                    "recall": item.get("recall"),
                    "requested_version": result.get("requirement_version"),
                },
            }
        )

    if not modules:
        raise ValueError(
            "没有任何选型项被用户选定，无法生成下游输入（先在界面上做选择）"
        )
    if len(modules) > MAX_MODULES:
        raise ValueError(f"模块数 {len(modules)} 超过下游上限 {MAX_MODULES}")

    return {
        "schema": SCHEMA,
        "modules": modules,
        "description": description
        or f"由器件选型阶段导出，需求版本 {result.get('requirement_version') or '未标注'}",
        "diagnostics": {
            "requirement_version": result.get("requirement_version"),
            "exported_modules": len(modules),
            "skipped": skipped,
            "red_lines": {
                "only_user_selected": True,
                "excluded_never_exported": True,
                "no_placeholder_devices": True,
            },
        },
    }


def contract_only(handoff: Mapping[str, Any]) -> dict[str, Any]:
    """剥掉本阶段自己的诊断字段，只留下游契约允许的键。"""

    allowed = ("schema", "modules", "description", "system", "links")
    return {key: handoff[key] for key in allowed if key in handoff}


def validate_with_downstream(payload: Mapping[str, Any], pcbagent_root: str) -> dict[str, Any]:
    """调用下游官方校验器，确保导出的东西真的能被生成入口接受。"""

    import sys

    root = str(pcbagent_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    from netlist_end_to_end.generation_input import validate_generation_input  # noqa: PLC0415

    return validate_generation_input(dict(payload))
