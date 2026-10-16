"""与需求分析工作台（requirements_studio）的接口适配。

chenxin 侧的契约是 `pcbagent.requirement-spec/1.0`（Pydantic，`extra='forbid'`），
与我在用的 `pcbagent.requirement_spec.v1` 不是同一套字段。本模块负责两件事：

1. `to_selection_spec`：把他们的 RequirementSpec 转成本阶段的输入；
2. `apply_result`：把选型结果写回他们的 spec（追加 device_references 并挂到
   对应选型项的 specified_references 上），这样他们的 `generation_adapter`
   就能直接产出网表生成入口的输入。

转换时必须守住一条：**他们的 `constraints` 大多数不是工程判定条件**。
实测 621 条约束里 483 条是 `kind=user_device_description`，是"用户这么说"
的溯源记录；只有 59 条 `design_requirement` 才是设计约束，且其中大多数
数值写在 `text` 里而不是 `minimum`/`maximum`。把描述性记录当硬性条件会
让每个候选都判成"未知"，所以这里只转真约束，其余如实记为说明。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .units import _canonical_unit, parse_quantity

CHENXIN_SCHEMA = "pcbagent.requirement-spec/1.0"
MY_SCHEMA = "pcbagent.requirement_spec.v1"

# 他们的关系词 → 本阶段的比较符
RELATION_OPS: dict[str, str] = {
    "at_least": ">=",
    "at_most": "<=",
    "equals": "==",
    "covers": "cover",
    "contains": "contains",
    "not_contains": "not_contains",
}

# 这些字段是文本性判定，不需要单位
_TEXT_FIELDS = frozenset({"interface", "feature", "resolution", "package", "topology", "protocol"})


def _clean_unit(raw: Any) -> str:
    """把 'A RMS/路' 这类写法收敛到规范单位；收敛不了就返回空。"""

    text = str(raw or "").strip()
    if not text:
        return ""
    if _canonical_unit(text):
        return _canonical_unit(text)[0]
    for cut in (" ", "/", "（", "("):
        head = text.split(cut)[0].strip()
        if head and _canonical_unit(head):
            return _canonical_unit(head)[0]
    # 取第一个能识别的单位片段
    for token in text.replace("/", " ").split():
        quantity = parse_quantity(f"1 {token}")
        if quantity is not None and quantity.unit:
            return quantity.unit
    return ""


def _constraint_value(constraint: Mapping[str, Any]) -> Any:
    """约束的取值：优先 min/max，其次从 text 里解析。"""

    low, high = constraint.get("minimum"), constraint.get("maximum")
    if low is not None and high is not None:
        return [float(low), float(high)]
    if low is not None:
        return float(low)
    if high is not None:
        return float(high)
    text = str(constraint.get("text") or "").strip()
    if not text:
        return None
    quantity = parse_quantity(text)
    if quantity is not None:
        return quantity.value
    try:
        return float(text)
    except ValueError:
        return text


def _is_engineering_constraint(constraint: Mapping[str, Any], field: str) -> tuple[bool, str]:
    """判断一条约束能不能当判定条件用。返回 (可用, 原因)。

    **判据是"有没有可比较的取值"，不是 kind 标签。** 实测工作台把大量
    "输出 3.3V""最大 1A 充电电流"这类带数字的设计要求也标成了
    `user_device_description`，而且数字写在 `text` 里、`minimum`/`maximum` 留空。
    早先按 kind 一刀切，等于把 97 条可用约束全丢了。
    """

    if not field:
        return False, "约束没有 field_key，无法对应器件字段"
    if constraint.get("minimum") is not None or constraint.get("maximum") is not None:
        return True, ""
    text = str(constraint.get("text") or "").strip()
    if not text:
        return False, "约束没有取值"
    if field in _TEXT_FIELDS:
        return True, ""
    if parse_quantity(text) is not None or _looks_numeric(text):
        return True, ""
    return False, "约束里没有可比较的数值"


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _reference_model(reference: Mapping[str, Any]) -> str:
    """不核验的取法：优先用它给的身份，其次用原文写法。"""

    identity = reference.get("identity") or {}
    model = str(identity.get("model") or "").strip()
    if model:
        return model
    return str(reference.get("mention") or "").strip()


def _reverify_identity(
    reference: Mapping[str, Any],
    catalog: Any,
) -> tuple[str, str]:
    """复核上游给出的器件身份是否真实存在。

    上游的 `resolution_status` 标「matched」不等于这个型号真的在目录里——
    实测有把 `FMMT593`、`AO3401A` 标成已定位、但库里根本没有的情况。
    盲目沿用会让我这边跟着用错身份，所以这里一律拿目录复验一遍。

    返回 (采用的型号, 结论)：

    * `confirmed` —— 上游给的身份在目录里确实存在；
    * `corrected` —— 上游给的身份查不到，但原文写法能查到，改用原文写法；
    * `unresolved` —— 两边都查不到，保留原文写法并标未确认；
    * `absent` —— 上游没给身份也没给原文写法。
    """

    identity = reference.get("identity") or {}
    claimed = str(identity.get("model") or "").strip()
    mention = str(reference.get("mention") or "").strip()

    if claimed and catalog.exact(claimed) is not None:
        return claimed, "confirmed"

    # 上游给的身份不可信，退到原文写法。
    #
    # 这里**只用精确匹配**，不做前缀解析。上游标「系列约束，型号待选」时给的是
    # 系列名（RT9013、ESP32-WROOM-32），如果拿前缀把它钉成一个具体型号
    # （RT9013 → RT9013-33GB），召回会从"一批同类"缩成"一个"，反而把本来能过的
    # 选型项卡死。系列名就该原样保留，交给召回层按类别去铺开。
    if mention:
        if catalog.exact(mention) is not None:
            return mention, "corrected" if claimed else "confirmed"
        return claimed or mention, "unresolved"

    return claimed, "confirmed" if claimed else "absent"


def _first_requirement(
    item: Mapping[str, Any],
    requirement_index: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """取该选型项关联的第一条需求，用于给器件引用找可核查的用户原话。"""

    for rid in item.get("requirement_ids") or []:
        requirement = requirement_index.get(str(rid))
        if requirement and str(requirement.get("source_quote") or "").strip():
            return dict(requirement)
    return {}


def to_selection_spec(
    spec: Mapping[str, Any],
    catalog: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """他们的 RequirementSpec → 本阶段输入。返回 (我的 spec, 转换诊断)。

    传入 `catalog`（`DeviceCatalog`）时会**复核每一个器件身份**：上游标
    「matched」不等于那个型号真的在目录里，实测有把库里根本没有的型号标成已定位的
    情况（`FMMT593`、`AO3401A`）。复核结论记在诊断里，可追溯。
    """

    schema = str(spec.get("schema_version") or "")
    if schema != CHENXIN_SCHEMA:
        raise ValueError(f"期望 {CHENXIN_SCHEMA!r}，实际 {schema!r}")

    requirements = {
        str(r.get("id")): dict(r) for r in spec.get("requirements") or []
    }
    references = {
        str(r.get("id")): dict(r) for r in spec.get("device_references") or []
    }

    items: list[dict[str, Any]] = []
    diag = {
        "conditions": 0,
        "notes_only": 0,
        "unmapped_fields": 0,
        "specified": 0,
        "preferred": 0,
        "reference_only": 0,
        "identity_confirmed": 0,
        "identity_corrected": 0,
        "identity_unresolved": 0,
        "identity_corrections": [],
        "mention_hints": 0,
        "notes": [],
    }

    for raw in spec.get("selection_items") or []:
        item = raw if isinstance(raw, Mapping) else {}
        mapping = item.get("catalog_mapping") or {}
        leaf_codes = [str(x) for x in (mapping.get("leaf_codes") or []) if str(x).strip()]

        component_quantity = item.get("component_quantity")
        quantity = (
            int(component_quantity)
            if isinstance(component_quantity, int) and component_quantity >= 1
            else max(int(item.get("circuit_quantity") or 1), 1)
        )

        hard: list[dict[str, Any]] = []
        preferred: list[dict[str, Any]] = []
        notes: list[str] = []

        for constraint in item.get("constraints") or []:
            if not isinstance(constraint, Mapping):
                continue
            field = str(constraint.get("field_key") or "").strip()
            usable, why = _is_engineering_constraint(constraint, field)
            if not usable:
                diag["notes_only"] += 1
                text = str(constraint.get("text") or "").strip()
                if text:
                    notes.append(text)
                elif why:
                    notes.append(why)
                continue
            relation = str(constraint.get("relation") or "equals")
            op = RELATION_OPS.get(relation)
            if op is None:
                diag["unmapped_fields"] += 1
                notes.append(f"{field}：不认识的关系词 {relation!r}")
                continue
            value = _constraint_value(constraint)
            if value is None:
                diag["unmapped_fields"] += 1
                notes.append(f"{field}：约束没有可用取值")
                continue
            condition: dict[str, Any] = {
                "field": field,
                "op": op,
                "value": value,
                "source_constraint_id": str(constraint.get("id") or ""),
            }
            unit = _clean_unit(constraint.get("unit"))
            if unit:
                condition["unit"] = unit
            if constraint.get("requirement_id"):
                condition["related_requirement"] = str(constraint["requirement_id"])
            reason = str(constraint.get("source_quote") or constraint.get("text") or "").strip()
            if reason:
                condition["reason"] = reason[:200]
            # 他们的 priority 几乎都是 must（实测 613/621），拿它分硬性/偏好会
            # 把所有约束都变成淘汰线。这里改按取值形态分：能数值比较的算硬性，
            # 文本包含类算偏好，避免"描述里没提到"就直接淘汰器件。
            target = hard if op in (">=", "<=", ">", "<", "==", "cover") else preferred
            target.append(condition)
            diag["conditions"] += 1
            if target is preferred:
                diag["notes"].append(f"{field} 转为偏好条件（{op}）")

        specified = [str(x) for x in (item.get("specified_references") or [])]
        prefer_refs = [str(x) for x in (item.get("preferred_references") or [])]
        ref_only = [str(x) for x in (item.get("reference_only") or [])]

        # 上游把器件分成「指定 / 优先 / 仅参考」三类，但还有一类没进这三个数组：
        # `family`（系列约束，具体型号待选）和 `ambiguous`（存在不同候选）。
        # 这类用户其实说了型号或系列名（如 RT9013、ESP32-WROOM-32），只是没定到具体料号。
        # 以前这些提及被完全丢掉，召回只能靠类别；现在把它们作为**召回关键词**用起来，
        # 但只作提示，不作硬性约束。
        hint_mentions: list[str] = []
        for rid in item.get("reference_ids") or []:
            if str(rid) in specified or str(rid) in prefer_refs or str(rid) in ref_only:
                continue
            reference = references.get(str(rid)) or {}
            mention = str(reference.get("mention") or "").strip()
            if mention and mention not in hint_mentions:
                hint_mentions.append(mention)

        diag["specified"] += len(specified)
        diag["preferred"] += len(prefer_refs)
        diag["reference_only"] += len(ref_only)

        chosen_pool = specified or prefer_refs or ref_only
        model = ""
        intent = ""
        identity_verdict = ""
        if chosen_pool:
            reference = references.get(chosen_pool[0]) or {}
            if catalog is not None:
                model, identity_verdict = _reverify_identity(reference, catalog)
                diag[f"identity_{identity_verdict}"] = (
                    diag.get(f"identity_{identity_verdict}", 0) + 1
                )
                claimed = str((reference.get("identity") or {}).get("model") or "").strip()
                if identity_verdict in ("corrected", "unresolved") and len(
                    diag["identity_corrections"]
                ) < 40:
                    diag["identity_corrections"].append(
                        {
                            "item_id": str(item.get("id") or ""),
                            "claimed": claimed,
                            "mention": str(reference.get("mention") or ""),
                            "used": model,
                            "verdict": identity_verdict,
                            "upstream_status": str(
                                reference.get("resolution_status") or ""
                            ),
                        }
                    )
            else:
                model = _reference_model(reference)
            intent = "reference" if ref_only and not specified else (
                "must" if specified else "preferred"
            )

        entry: dict[str, Any] = {
            "id": str(item.get("id") or ""),
            "module_name": str(item.get("name") or ""),
            "module_type": str(item.get("type_name") or ""),
            "category": str(item.get("type_name") or ""),
            "leaf_code": leaf_codes[0] if leaf_codes else "",
            "leaf_codes": leaf_codes,
            "quantity": quantity,
            "related_requirements": [str(x) for x in (item.get("requirement_ids") or [])],
            "hard_conditions": hard,
            "preferred_conditions": preferred,
            "purpose": str(item.get("purpose") or ""),
            "boundary": str(item.get("boundary") or ""),
            "chenxin_module_id": str(item.get("module_id") or ""),
            "chenxin_selection_item_id": str(item.get("id") or ""),
        }
        if hint_mentions:
            # 与显式给的关键词合并，交给召回层按前缀/关键词去找
            entry["keywords"] = hint_mentions[:4]
            entry["mention_hints"] = hint_mentions[:4]
            diag["mention_hints"] += len(hint_mentions[:4])
        if model:
            entry["specified_device"] = {"model": model, "intent": intent}
        if identity_verdict:
            entry["identity_verdict"] = identity_verdict
        if notes:
            entry["note"] = "；".join(dict.fromkeys(notes))[:600]
        if specified:
            entry["specified_reference_ids"] = specified
        items.append(entry)

    result = {
        "schema": MY_SCHEMA,
        "requirement_version": f"{spec.get('spec_id')}",
        "project": {
            "name": str(spec.get("title") or ""),
            "summary": str(spec.get("summary") or ""),
        },
        "global_constraints": {
            "currency": "CNY",
            "note": "成本来自预设元件库参考价；需求侧未提供预算",
        },
        "selection_items": items,
        "source": {
            "system": "requirements_studio",
            "schema_version": CHENXIN_SCHEMA,
            "spec_id": str(spec.get("spec_id") or ""),
            "project_id": str(spec.get("project_id") or ""),
            "requirement_version": spec.get("requirement_version"),
            "review_status": str(spec.get("review_status") or ""),
            "handoff_status": str(spec.get("handoff_status") or ""),
        },
    }
    diag["items"] = len(items)
    diag["items_with_leaf_codes"] = sum(1 for i in items if i["leaf_codes"])
    diag["items_with_specified_device"] = sum(1 for i in items if i.get("specified_device"))
    return result, diag


def apply_result(
    spec: Mapping[str, Any],
    candidate_set: Mapping[str, Any],
    decisions: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """把选型结果写回他们的 spec。

    做法是**追加** device_references 并把新引用挂到对应选型项的
    `specified_references` 上，不动他们原有的任何字段。这样他们的
    `handoff.generation_adapter` 无需改动就能把选定器件投影成生成入口输入。
    """

    updated = _deep_copy(spec)
    requirement_index = {
        str(r.get("id")): dict(r) for r in updated.get("requirements") or []
    }
    references = list(updated.get("device_references") or [])
    used_ids = {str(r.get("id")) for r in references if isinstance(r, Mapping)}
    decisions = decisions or {}

    candidates_by_item = {
        str(item.get("selection_item", {}).get("id")): item
        for item in candidate_set.get("items") or []
    }
    applied: list[dict[str, Any]] = []

    for item in updated.get("selection_items") or []:
        item_id = str(item.get("id") or "")
        source = candidates_by_item.get(item_id)
        if not source:
            continue
        picked = [
            candidate
            for candidate in source.get("candidates") or []
            if (decisions.get(f"{item_id}::{candidate.get('model')}") or {}).get("decision")
            == "confirmed"
        ]
        for candidate in picked:
            # 他们的契约要求每条器件引用都能指回一句用户原话
            # （`device_references[].source_quote` 必须逐字出现在
            # `source_messages[source_message_id].content` 里）。我选出的器件
            # 不是用户说的，而是满足某条需求选出来的——所以引用挂在**它服务的那条
            # 需求**的原话上，来源链条是「用户原话 → 需求 → 目录召回 → 用户确认」。
            anchor = _first_requirement(item, requirement_index)
            reference_id = f"REF-SEL-{item_id}-{len(applied) + 1}"
            while reference_id in used_ids:
                reference_id += "x"
            used_ids.add(reference_id)
            references.append(
                {
                    "id": reference_id,
                    "mention": str(candidate.get("model") or ""),
                    "identity_scope": "exact_part",
                    "intent": "must",
                    "lifecycle": "active",
                    "resolution_status": "matched" if candidate.get("supplier_part") else "unresolved",
                    "identity": {
                        "model": str(candidate.get("model") or ""),
                        "manufacturer": str(candidate.get("manufacturer") or ""),
                        "supplier_part": str(candidate.get("supplier_part") or ""),
                    },
                    "requirement_ids": list(item.get("requirement_ids") or []),
                    "module_ids": [str(item.get("module_id") or "")],
                    "purpose": str(item.get("purpose") or ""),
                    "quantity": int(item.get("circuit_quantity") or 1),
                    "document_status": "not_checked",
                    "evidence_ids": [],
                    "identity_decision": None,
                    "device_key": None,
                    "lookup_message": "由器件选型阶段从真实器件目录召回并经用户确认。",
                    "source_message_id": anchor.get("source_message_id", ""),
                    "source_quote": anchor.get("source_quote", ""),
                }
            )
            specified = list(item.get("specified_references") or [])
            specified.append(reference_id)
            item["specified_references"] = specified
            applied.append(
                {
                    "selection_item_id": item_id,
                    "module_id": str(item.get("module_id") or ""),
                    "reference_id": reference_id,
                    "model": str(candidate.get("model") or ""),
                    "supplier_part": str(candidate.get("supplier_part") or ""),
                    "fit": (candidate.get("evaluation") or {}).get("fit"),
                }
            )

    updated["device_references"] = references
    return {
        "spec": updated,
        "applied": applied,
        "summary": {
            "applied_selections": len(applied),
            "selection_items_total": len(updated.get("selection_items") or []),
        },
    }


def validate_with_chenxin(spec: Mapping[str, Any], studio_root: str) -> dict[str, Any]:
    """调用 chenxin 自己的 Pydantic 契约校验，确保写回后仍然合法。"""

    import sys
    from pathlib import Path

    root = str(Path(studio_root).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from backend.handoff import RequirementSpec  # noqa: PLC0415

    model = RequirementSpec.model_validate(dict(spec))
    return model.model_dump()


def _deep_copy(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, ensure_ascii=False))
