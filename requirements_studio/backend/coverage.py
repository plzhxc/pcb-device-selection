"""Versioned V3 requirement coverage and a deterministic review gate."""
import json
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / "resources" / "v3_checklist.json").read_text(encoding="utf-8"))["topics"]
TOPICS = {topic["id"]: topic for topic in CATALOG}
CLOSED = {"confirmed", "not_applicable", "design_stage"}
LABELS = {"confirmed":"已明确", "not_applicable":"明确不适用", "design_stage":"已约定由设计阶段确定", "tbd":"待确认", "conflict":"存在冲突"}


def review_document(doc):
    coverage = {item["topic_id"]: item for item in doc.get("coverage", [])}
    rows = [{**topic, **coverage.get(topic["id"], {}), "state":coverage.get(topic["id"], {}).get("state", "tbd")} for topic in CATALOG]
    if doc.get('convergence_version')==2:
        from .convergence import field_rows, view
        fields=field_rows(doc)
        for row in rows:
            group=[f for f in fields if f['topic_id']==row['id']]
            states={f['state'] for f in group}
            row['state']='conflict' if 'conflict' in states else 'tbd' if not states<=CLOSED else next(iter(states)) if len(states)==1 else 'confirmed'
            row['fields']=[f['label'] for f in group if f['state'] not in CLOSED] or row['fields']
            row['conclusion']='\n'.join(dict.fromkeys(f.get('conclusion','') for f in group if f.get('conclusion')))
        convergence=view(doc)
        scope=convergence['product_review']
    else:
        convergence=None
    gaps = [row for row in rows if row["state"] not in CLOSED]
    unsettled = [r["id"] for r in doc["requirements"] if r["state"] in ("tbd", "suggested", "conflict")]
    ready = bool(doc["requirements"]) and not gaps and not unsettled and not doc["questions"] and not doc.get("recheck_topics") and bool(doc["flow"])
    if convergence:
        ready=bool(doc['requirements']) and not scope['decision_pending'] and not scope['module_rechecks'] and not unsettled and not doc['questions'] and bool(doc['flow']) and not convergence['module_gaps'] and not convergence['module_pending'] and not convergence['issues']
    decision_gaps=[]
    if convergence:
        for topic in rows:
            missing=[f for f in scope['decision_fields'] if f['topic_id']==topic['id'] and f['state'] not in CLOSED and f['id'] not in scope['decision_rechecks']]
            if missing:decision_gaps.append({'id':topic['id'],'title':topic['title'],'section':topic['section'],'fields':[f['label'] for f in missing]})
    return {"status":"approved" if ready and doc.get("review_status")=="approved" else "ready" if ready else "draft", "closed":len(rows)-len(gaps), "total":len(rows), "topics":rows, "gaps":[{"id":r["id"],"title":r["title"],"section":r["section"],"fields":r["fields"]} for r in gaps], "unsettled_requirements":unsettled, "open_questions":len(doc["questions"]), "has_flow":bool(doc["flow"]), "recheck_count":len(doc.get("recheck_topics",[])), 'fields_closed':convergence['fields_closed'] if convergence else None,'fields_total':convergence['fields_total'] if convergence else None,'module_gaps':convergence['module_gaps'] if convergence else [],'module_pending':convergence['module_pending'] if convergence else [],'consistency_issues':convergence['issues'] if convergence else [],'decision_gaps':decision_gaps,'decision_total':scope['decision_total'] if convergence else None,'decision_closed':scope['decision_closed'] if convergence else None,'engineering_pending':scope['engineering_pending'] if convergence else 0,'internal_rechecks':len(scope['decision_rechecks'])+len(scope['module_rechecks']) if convergence else len(doc.get('recheck_topics',[]))}
