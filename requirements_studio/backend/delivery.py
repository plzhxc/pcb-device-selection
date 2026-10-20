"""Product handoff checks, external contracts, and concise next-owner summaries."""
import copy
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from .reference_design import bom_view

LABELS={'scope_acceptance':'功能与验收完整','capability_chain':'系统能力链','interfaces_power':'供电和外部接口','core_parts':'核心器件方案'}
class Strict(BaseModel):model_config=ConfigDict(extra='forbid')
class DeliveryCheck(Strict):
    key:Literal['scope_acceptance','capability_chain','interfaces_power','core_parts']
    state:Literal['complete','needs_user','design_task','conflict']
    conclusion:str=Field(min_length=1,max_length=1800)
    next_action:str=Field(default='',max_length=1000)
    requirement_ids:list[str]=Field(default_factory=list,max_length=80)
    source_message_id:str=Field(default='',max_length=64)
    source_quote:str=Field(default='',max_length=3000)

class InterfaceContract(Strict):
    id:str=Field(min_length=1,max_length=64)
    name:str=Field(min_length=1,max_length=120)
    purpose:str=Field(max_length=600)
    external_object:str=Field(default='',max_length=200)
    protocol:str=Field(default='',max_length=120)
    signals:list[str]=Field(default_factory=list,max_length=40)
    voltage:str=Field(default='',max_length=120)
    shared_link:str=Field(default='',max_length=120)
    state:Literal['confirmed','proposed','design_stage']
    requirement_ids:list[str]=Field(default_factory=list,max_length=80)
    source_message_id:str=Field(default='',max_length=64)
    source_quote:str=Field(default='',max_length=3000)

def apply(doc,result,messages,aliases,changed):
    if not result.delivery_revision and not doc.get('delivery_policy_version'):return
    doc['delivery_policy_version']=1
    users={m['id']:m['content'] for m in messages if m['role']=='user'};reqs={r['id'] for r in doc['requirements']}
    checks={r['key']:copy.deepcopy(r) for r in doc.get('delivery_checks',[])}
    for row in checks.values():
        if set(row['requirement_ids'])&set(changed):row['state']='review'
    for update in result.delivery_checks:
        row=update.model_dump();row['requirement_ids']=list(dict.fromkeys(aliases.get(k,k) for k in row['requirement_ids']))
        if set(row['requirement_ids'])-reqs:raise ValueError('交接检查关联了不存在的需求。')
        if row['state'] in ('complete','design_task') and (not row['requirement_ids'] or not row['source_quote'] or row['source_quote'] not in users.get(row['source_message_id'],'')):
            row['state']='review'
        checks[row['key']]=row
    doc['delivery_checks']=list(checks.values())
    interfaces={r['id']:copy.deepcopy(r) for r in doc.get('external_interfaces',[])}
    for update in result.external_interfaces:
        row=update.model_dump();row['requirement_ids']=list(dict.fromkeys(aliases.get(k,k) for k in row['requirement_ids']))
        if set(row['requirement_ids'])-reqs:raise ValueError('外部接口关联了不存在的需求。')
        if row['state']=='confirmed' and (not row['source_quote'] or row['source_quote'] not in users.get(row['source_message_id'],'')):row['state']='proposed'
        interfaces[row['id']]=row
    doc['external_interfaces']=list(interfaces.values())

def review(doc):
    enabled=bool(doc.get('delivery_policy_version'));saved={r['key']:r for r in doc.get('delivery_checks',[])}
    checks=[{**saved.get(key,{'key':key,'state':'missing','conclusion':'助手尚需整理这部分，不要求客户填写规范。','next_action':'','requirement_ids':[]}), 'label':label} for key,label in LABELS.items()] if enabled else []
    internal=[c for c in checks if c['state'] in ('missing','review')]
    gaps=[c for c in checks if c['state'] in ('needs_user','conflict')]
    bom=bom_view(doc)
    proposed=[p for p in bom['current_rows'] if p['status']=='proposed']
    if enabled and proposed:gaps.append({'key':'reference_adoption','label':'参考核心器件方案','state':'needs_user','conclusion':f'{len(proposed)} 项参考核心器件已关联功能，等待你采用或调整。','next_action':'在模块方案中查看核心器件方案，采用适用部分或说明需要修改的地方。'})
    return {'enabled':enabled,'checks':checks,'rechecks':internal,'gaps':gaps,'reference_adoption_pending':len(proposed)}

def view(project):
    doc=project['document'];r=review(doc);bom=bom_view(doc,project.get('domain'))
    submitted={s['question_id'] for s in project.get('question_submissions',[]) if s['status'] in ('sending','processing','queued')}
    run=project.get('latest_run') or {};questions=sum(q['id'] not in submitted for q in doc['questions']);internal=project['review'].get('internal_rechecks',0)
    modules_pending=len(project.get('convergence',{}).get('module_pending',[]))
    if run.get('status') in ('running','queued'):owner='ai';label='助手正在处理本轮输入' if run['status']=='running' else '等待 AI 执行槽位'
    elif run.get('status')=='failed':owner='user';label='本轮整理未完成，请重试；已保存文档仍保留'
    elif questions:owner='user';label=f'还有 {questions} 个使用取舍需要你决定'
    elif r['reference_adoption_pending']:owner='user';label='查看并采用核心器件方案'
    elif modules_pending:owner='user';label=f'有 {modules_pending} 项功能模块方案，等待你采用或调整'
    elif internal:owner='ai';label='还有关联内容需要助手整理'
    elif project['review']['status'] in ('ready','approved'):owner='user' if project['review']['status']=='ready' else 'design';label='本版内容完整，请整体审阅' if owner=='user' else '需求已审核，进入实现与选型'
    else:owner='ai';label='需求尚有缺口，继续完善'
    from .handoff import readiness
    # Only derive the open-items list; do not invoke a model or generation here.
    return {**r,'next_owner':owner,'next_label':label,'user_decisions':questions+int(bool(r['reference_adoption_pending'] or modules_pending)),
            'internal_tasks':internal,'bom':bom,'engineering_tasks':readiness(project)['open_items']}
