from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .coverage import CATALOG, LABELS as COVERAGE_LABELS, TOPICS, review_document
from .convergence import FieldResolution, ModuleUpdate, ModuleLink, NumericConstraint, FIELDS, apply as apply_convergence
from .module_catalog import TYPES
from .device_domain import DeviceReferenceUpdate, DeviceConstraintUpdate, KnowledgeRequest, apply_updates as apply_device_updates
from .intake import IntakeCheck
from .delivery import DeliveryCheck,InterfaceContract

CATEGORIES = {
    "overview": "项目概述", "functions": "功能需求", "performance": "性能指标",
    "manufacturing": "生产与测试", "architecture": "功能框架", "selection": "器件与约束",
}
STATES = {"stated": "用户已提出", "suggested": "AI 建议", "tbd": "待确认", "conflict": "存在冲突", "not_applicable": "不适用"}
Category = Literal["overview", "functions", "performance", "manufacturing", "architecture", "selection"]
State = Literal["stated", "suggested", "tbd", "conflict", "not_applicable"]
Priority = Literal["must", "should", "could"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RequirementUpdate(StrictModel):
    id: str = Field(max_length=32, description="已有需求使用当前文档的 REQ-xxx 编号；新增需求使用 new-1 等临时编号。")
    category: Category
    name: str = Field(min_length=1, max_length=160)
    value: str = Field(max_length=3000)
    unit: str = Field(max_length=40)
    priority: Priority
    state: State
    source_message_id: str = Field(max_length=64)
    source_quote: str = Field(max_length=3000)
    spec_field_ids: list[str] = Field(default_factory=list, max_length=24, description='该项需求在 V3 规范中实际对应的字段 ID。只关联真正承载此内容的条款，不为覆盖率关联无关字段。')
    implementation: Literal['hardware','software','service','system'] = Field(
        default='hardware',
        description='需求的实现归属：hardware 仅指需要电路承接的功能，如供电、传感采集、电机驱动或实体按键；software 指固件或 App 功能（含 App 内按钮）；service 指外部服务；system 指整机目标、交付配套及不含电路的机械配件（如机架、保护罩），不能因实物属性而归为电路功能。混合需求按实际功能分别记录，不得为了消除模块缺口把真实电路功能改为 system。',
    )


class Question(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="问题自身的编号，与 REQ-xxx 需求编号不同，不得放入 remove_ids。")
    question: str = Field(min_length=1, max_length=500)
    reason: str = Field(max_length=500)
    options: list[str] = Field(max_length=4)
    requirement_ids: list[str] = Field(max_length=20)
    field_ids: list[str] = Field(default_factory=list,max_length=80)
    decision_key: str = Field(default='',max_length=120)
    priority: int = Field(default=2,ge=1,le=3)
    impact: str = Field(default='',max_length=600)
    option_impacts: list[str] = Field(default_factory=list,max_length=4)


class FlowStep(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    requirement_ids: list[str] = Field(max_length=20)


class CoverageUpdate(StrictModel):
    topic_id: str = Field(max_length=32)
    state: Literal["confirmed", "not_applicable", "design_stage", "tbd", "conflict"]
    conclusion: str = Field(min_length=1,max_length=3000)
    requirement_ids: list[str] = Field(max_length=80)
    source_message_id: str = Field(max_length=64)
    source_quote: str = Field(max_length=3000)


class AnalysisResult(StrictModel):
    reply: str = Field(min_length=1, max_length=12000)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(max_length=3000)
    updates: list[RequirementUpdate] = Field(max_length=80)
    remove_ids: list[str] = Field(max_length=80, description="仅列出用户明确取消的现有 REQ-xxx 需求编号。不用于移除已回答的问题，不与 updates 重叠；通常为空数组。")
    questions: list[Question] = Field(max_length=3, description="本轮新增或细化的最多 3 个问题，已有问题保留原 ID。未返回的旧问题仍保留在待办中。")
    resolved_question_ids: list[str] = Field(default_factory=list, max_length=80, description="本轮已经得到用户明确回答或被用户取消的旧问题编号；未答问题不能标记已解决。")
    next_analysis: str = Field(default="", max_length=500, description="尚可独立推进的一项具体分析任务，不依赖未答问题；没有则空字符串。不会据此无限循环。")
    pause_questions: bool = Field(default=False, description="仅当用户明确要求暂不再提问或先整理草稿时为 true；未决问题保留，不代表验收完成。")
    coverage: list[CoverageUpdate] = Field(default_factory=list,max_length=80,description="V3 章节核对的增量更新。每项应覆盖该小节全部适用字段；没有用户依据的内容仍待确认。")
    flow: list[FlowStep] = Field(max_length=9,description='非空时更新完整产品流程草案；空数组表示保留已有流程，不用于清空。')
    changes: list[str] = Field(max_length=20)
    analysis_revision: Literal[1,2,3] = 1
    guidance_revision: Literal[0,1] = 0
    intake_policy_revision: Literal[0,1] = 0
    intake_checks: list[IntakeCheck] = Field(default_factory=list,max_length=5,description='五项必要产品边界的增量检查，不是五道问卷。已有原话可确定的直接沿用，真正影响产品决定的未知保留open；纯工程实现不强迫用户逐项确定。')
    reviewed_requirement_ids: list[str] = Field(default_factory=list,max_length=160,description='本轮对照最新用户决定实际检查且内容仍然有效的现有需求 ID；已修改的放 updates。用于关联复核，不代表替用户确认或完成工程验证。')
    field_resolutions: list[FieldResolution] = Field(default_factory=list,max_length=160)
    module_updates: list[ModuleUpdate] = Field(default_factory=list,max_length=80)
    remove_module_ids: list[str] = Field(default_factory=list,max_length=80)
    module_links: list[ModuleLink] = Field(default_factory=list,max_length=120)
    numeric_constraints: list[NumericConstraint] = Field(default_factory=list,max_length=120)
    device_reference_updates: list[DeviceReferenceUpdate] = Field(default_factory=list,max_length=80)
    remove_device_reference_ids: list[str] = Field(default_factory=list,max_length=80)
    device_constraints: list[DeviceConstraintUpdate] = Field(default_factory=list,max_length=120)
    knowledge_requests: list[KnowledgeRequest] = Field(default_factory=list,max_length=20)
    delivery_revision: Literal[0,1] = 0
    delivery_checks:list[DeliveryCheck]=Field(default_factory=list,max_length=4)
    external_interfaces:list[InterfaceContract]=Field(default_factory=list,max_length=40)


def empty_document() -> dict:
    return {"title": "未命名需求", "title_locked":False, "summary": "", "requirements": [], "questions": [], "question_history": [], "questions_paused": False, "coverage": [], "review_status":"draft", "flow": [], "version": 0, "created_at":now(), "updated_at": now(), "changed_ids": [], "changes": [], "next_requirement_number": 1}


def analysis_schema(document: dict, source_ids: list[str] | None = None,
                    source_anchors: list[str] | None = None) -> dict:
    schema = AnalysisResult.model_json_schema()
    removals = schema["properties"]["remove_ids"]
    ids = [r["id"] for r in document["requirements"]]
    if ids:
        removals["items"]["enum"] = ids
    else:
        removals["maxItems"] = 0
    resolutions = schema["properties"]["resolved_question_ids"]
    question_ids = [q["id"] for q in document["questions"]]
    if question_ids:
        resolutions["items"]["enum"] = question_ids
    else:
        resolutions["maxItems"] = 0
    # Require all fields for structured output while accepting historical saved
    # responses that predate the question backlog fields in local replays.
    schema["required"] = list(schema["properties"])
    schema['properties']['guidance_revision']['enum']=[1]
    schema['properties']['intake_policy_revision']['enum']=[1]
    schema['properties']['delivery_revision']['enum']=[1]
    schema["$defs"]["CoverageUpdate"]["properties"]["topic_id"]["enum"] = list(TOPICS)
    schema['$defs']['FieldResolution']['properties']['field_ids']['items']['enum']=list(FIELDS)
    schema['$defs']['Question']['properties']['field_ids']['items']['enum']=list(FIELDS)
    schema['$defs']['RequirementUpdate']['properties']['spec_field_ids']['items']['enum']=list(FIELDS)
    schema['$defs']['ModuleUpdate']['properties']['type_id']['enum']=list(TYPES)
    if source_ids is not None:
        sources=list(dict.fromkeys(['',*source_ids]))
        for definition in schema['$defs'].values():
            source=definition.get('properties',{}).get('source_message_id')
            if source is not None:source['enum']=sources
    if source_anchors is not None:
        selectors=list(dict.fromkeys(source_anchors))
        if not selectors:raise ValueError('分析需要至少一条真实用户原话锚点。')
        for definition in schema['$defs'].values():
            properties=definition.get('properties',{})
            if 'source_message_id' in properties:
                properties['source_message_id']['enum']=['']
            if 'source_quote' in properties:
                quote=properties['source_quote']
                quote['enum']=([''] if quote.get('minLength',0)==0 else [])+selectors
    module_ids=[m['id'] for m in document.get('modules',[])]
    if module_ids:schema['properties']['remove_module_ids']['items']['enum']=module_ids
    else:schema['properties']['remove_module_ids']['maxItems']=0
    refs=[r['id'] for r in document.get('device_references',[]) if r.get('lifecycle')=='active']
    if refs:schema['properties']['remove_device_reference_ids']['items']['enum']=refs
    else:schema['properties']['remove_device_reference_ids']['maxItems']=0
    return schema


def apply_analysis(document: dict, result: AnalysisResult, messages: list[dict], message_id: str, answers: list[dict] | None = None, continuation: bool = False) -> dict:
    doc = copy.deepcopy(document)
    original_document = copy.deepcopy(document)
    known = {r["id"]: r for r in doc["requirements"]}
    original = copy.deepcopy(known)
    previous_questions = {q["id"] for q in doc["questions"]}
    remaining_questions = {q.id for q in result.questions}
    resolved = set(result.resolved_question_ids)
    if resolved - previous_questions:
        raise ValueError("本轮引用了不存在的待答问题，文档未保存。请重试。")
    if continuation and resolved:
        raise ValueError("独立分析不能代替用户回答问题，文档未保存。")
    deferred = {a["question_id"] for a in (answers or []) if a.get("deferred")}
    resolved -= deferred
    updated_ids = {r.id for r in result.updates}
    removals = []
    for rid in dict.fromkeys(result.remove_ids):
        # Older model responses sometimes put an answered question here. The
        # replacement questions list already removes it; no requirement is lost.
        if rid not in known and rid in previous_questions and rid not in remaining_questions:
            if not continuation and rid not in deferred:
                resolved.add(rid)
            continue
        if rid not in known:
            raise ValueError(f"删除编号「{rid[:64]}」不是当前需求编号，本轮未保存。请重试。")
        if rid in updated_ids:
            raise ValueError(f"需求「{rid}」被同时修改和删除，本轮未保存。请重试。")
        removals.append(rid)
    user_messages = {m["id"]: m["content"] for m in messages if m["role"] == "user"}
    number = max([int(rid[4:]) for rid in known if re.fullmatch(r"REQ-\d+", rid)] + [doc.get("next_requirement_number",1)-1])
    changed: list[str] = []
    aliases: dict[str, str] = {}
    seen: set[str] = set()
    for update in result.updates:
        row = update.model_dump()
        original_id = row["id"]
        if len(row['spec_field_ids']) != len(set(row['spec_field_ids'])) or any(fid not in FIELDS for fid in row['spec_field_ids']):
            raise ValueError('需求引用了未知或重复的规范条款，本轮未保存。')
        if not row['spec_field_ids'] and original_id in known:
            row['spec_field_ids']=known[original_id].get('spec_field_ids',[])
        if original_id in seen and original_id:
            raise ValueError("模型返回了重复需求编号，请重试。")
        seen.add(original_id)
        if original_id and original_id not in known and not original_id.startswith(("new", "NEW")):
            raise ValueError("模型引用了不存在的需求编号，请重试。")
        if original_id not in known:
            number += 1
            row["id"] = f"REQ-{number:03d}"
            if original_id:
                aliases[original_id] = row["id"]
        quote = row["source_quote"].strip()
        source_id = row["source_message_id"]
        if row["state"] == "stated":
            if not quote or source_id not in user_messages or quote not in user_messages[source_id]:
                row["state"] = "suggested"
                row["source_message_id"] = ""
                row["source_quote"] = ""
        elif source_id not in user_messages or not quote or quote not in user_messages[source_id]:
            row["source_message_id"] = ""
            row["source_quote"] = ""
        row["source_type"] = "user" if row["source_message_id"] else "ai"
        row["updated_at"] = now()
        known[row["id"]] = row
        changed.append(row["id"])
    for rid in removals:
        del known[rid]
        changed.append(rid)
    from .experience import statistical_note
    classification_only=set();statistical_ids=[]
    for row in known.values():
        if row['category']=='functions' and row.get('implementation','hardware')=='hardware' and statistical_note(row):
            if row['id'] not in changed:classification_only.add(row['id']);changed.append(row['id'])
            row.update(category='architecture',implementation='system');statistical_ids.append(row['id'])
    if statistical_ids:result.changes.append('数量统计与规模参考按系统说明保留，不虚构额外电路模块。')
    valid = set(known)
    def refs(ids: list[str]) -> list[str]:
        return list(dict.fromkeys(aliases.get(r, r) for r in ids if aliases.get(r, r) in valid))
    incoming_questions = [q.model_dump() for q in result.questions]
    if len({q["id"] for q in incoming_questions}) != len(incoming_questions):
        raise ValueError("模型返回了重复问题编号，请重试。")
    questions = {q["id"]: q for q in doc["questions"] if q["id"] not in resolved}
    for q in incoming_questions:
        if q["id"] in resolved:
            continue
        # Keep one stable branch when a model accidentally renames its ID.
        same_key=next((old for old in questions.values() if q.get('decision_key') and old.get('decision_key')==q['decision_key']),None)
        if same_key and same_key['id']!=q['id']:q['id']=same_key['id']
        q["requirement_ids"] = refs(q["requirement_ids"])
        q["source_message_id"] = questions.get(q['id'],{}).get('source_message_id') or message_id
        linked = [known[rid] for rid in q["requirement_ids"]]
        q["branch"] = linked[0]["category"] if linked else "overview"
        q["status"] = questions.get(q["id"], {}).get("status", "pending")
        # A renamed ID must not multiply the same pending question.
        duplicate = next((old for old in questions.values() if set(old["requirement_ids"])==set(q["requirement_ids"]) and re.sub(r"[\W_]+", "", old["question"]) == re.sub(r"[\W_]+", "", q["question"])), None)
        if duplicate and duplicate["id"] != q["id"]:
            continue
        questions[q["id"]] = q
    for q in questions.values():
        q["requirement_ids"] = refs(q["requirement_ids"])
        q.setdefault("branch", next((known[rid]["category"] for rid in q["requirement_ids"]), "overview"))
        if q["id"] in deferred:
            q["status"] = "deferred"
    history = doc.get("question_history", []) + [{**q, "resolved_by":message_id} for q in doc["questions"] if q["id"] in resolved]
    from .experience import ui_review_question
    workflow_questions=[q for q in questions.values() if ui_review_question(q)]
    for q in workflow_questions:
        questions.pop(q['id'])
        history.append({**q,'resolved_by':'','resolution_kind':'ui_action','resolution':'由界面的整体审核按钮承接；没有替用户批准文档。'})
    flow = [s.model_dump() for s in result.flow] if result.flow else copy.deepcopy(doc['flow'])
    kept_flow=[]
    for s in flow:
        prior_refs=s['requirement_ids'];s['requirement_ids']=refs(prior_refs)
        if not prior_refs or s['requirement_ids']:kept_flow.append(s)
    flow=kept_flow
    paused = doc.get("questions_paused", False) if continuation else result.pause_questions
    coverage = {c["topic_id"]: c for c in doc.get("coverage", [])}
    content_fields=("category","name","value","unit","priority","state","implementation")
    semantic_changes={rid for rid in changed if rid not in classification_only and (rid not in known or rid not in original or any(known[rid].get(k,'hardware' if k=='implementation' else None)!=original[rid].get(k,'hardware' if k=='implementation' else None) for k in content_fields))}
    rechecks=set(doc.get("recheck_topics",[]))
    for item in coverage.values():
        if item.get("state")=="tbd" and item.get("conclusion") in ("关联需求已修改，待重新核对。","关联需求已手动修改，待重新核对。"):
            rechecks.add(item["topic_id"])
        if set(item["requirement_ids"]) & semantic_changes:
            item.update(state="tbd",conclusion="关联需求已修改，待重新核对。",source_message_id="",source_quote="")
            rechecks.add(item["topic_id"])
    seen_coverage = set()
    for item in result.coverage:
        row=item.model_dump()
        if row["topic_id"] not in TOPICS or row["topic_id"] in seen_coverage:
            raise ValueError("章节覆盖编号不存在或重复，本轮文档未保存。")
        seen_coverage.add(row["topic_id"])
        row["requirement_ids"] = refs(row["requirement_ids"])
        quote=row["source_quote"].strip()
        evidence=bool(quote and row["source_message_id"] in user_messages and quote in user_messages[row["source_message_id"]])
        if not evidence:
            row.update(source_message_id="",source_quote="")
        if row["state"] in ("confirmed","not_applicable","design_stage") and (not evidence or (row["state"]=="confirmed" and (not row["requirement_ids"] or any(known[rid]["state"]!="stated" for rid in row["requirement_ids"])))):
            row.update(state="tbd",conclusion=row["conclusion"]+"（尚缺用户确认依据）")
        coverage[row["topic_id"]]=row
        rechecks.discard(row["topic_id"])
    doc.update(title=doc["title"] if doc.get("title_locked") else result.title.strip(), summary=result.summary.strip(), requirements=list(known.values()), questions=list(questions.values()), question_history=history[-100:], questions_paused=paused, coverage=list(coverage.values()), recheck_topics=sorted(rechecks), review_status="draft", flow=flow, changed_ids=changed, changes=result.changes, updated_at=now(),next_requirement_number=number+1)
    module_aliases=apply_convergence(doc,result,original_document,messages,aliases,semantic_changes) or {}
    apply_device_updates(doc,result,original_document,messages,aliases,module_aliases)
    from .decisions import reconcile_requirements
    reconcile_requirements(doc,original_document,result,resolved,semantic_changes,aliases,message_id)
    from .intake import apply_checks,fallback_question
    apply_checks(doc,original_document,result,messages,aliases,semantic_changes)
    from .delivery import apply as apply_delivery
    apply_delivery(doc,result,messages,aliases,semantic_changes)
    from .canvas_sync import record as record_canvas_changes, update as update_canvas_changes
    record_canvas_changes(doc,original_document,message_id,result=result)
    update_canvas_changes(doc)
    review = review_document(doc)
    user_gaps=review['decision_gaps'] if doc.get('convergence_version')==2 else [gap for gap in review["gaps"] if gap["id"] not in set(doc.get('recheck_topics',[]))]
    if not doc["questions"] and not paused and doc["requirements"] and (user_gaps or review["unsettled_requirements"]):
        gap=user_gaps[0] if user_gaps else None
        label=gap["title"] if gap else "未确认的需求条目"
        fields="、".join(gap["fields"][:5]) if gap else "、".join(review["unsettled_requirements"][:5])
        doc["questions"]=[{"id":"gap-"+gap["id"] if gap else "gap-requirements", "question":f"关于{label}，请补充{fields}；哪些不适用或交由设计阶段确定，也可以说明。", "reason":"本轮问题已答完，但说明书仍有缺口，需要继续收敛。", "options":[], "requirement_ids":[], "source_message_id":message_id, "branch":{1:"overview",2:"functions",3:"performance",4:"manufacturing",5:"architecture"}.get(int(gap["section"][0]) if gap else 1,"overview"), "status":"pending"}]
        if gap and doc.get('convergence_version')==2:
            from .experience import decision_scope
            from .convergence import field_rows
            doc['questions'][0]['field_ids']=[f['id'] for f in decision_scope(doc,field_rows(doc))['decision_fields'] if f['topic_id']==gap['id'] and f['state'] not in ('confirmed','not_applicable','design_stage')]
        if doc.get('guidance_version')==1:
            from .decisions import next_product_question
            doc['questions']=[next_product_question(doc,gap,message_id)]
    if not doc['questions'] and not paused and doc['requirements']:fallback_question(doc,message_id)
    return doc


def render_markdown(doc: dict, domain: dict | None = None) -> str:
    from .document_export import render_project_document
    return render_project_document(doc,domain)
