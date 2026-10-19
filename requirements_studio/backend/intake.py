"""Stage-one product boundaries: five checks, not five compulsory questions."""
from __future__ import annotations
import copy
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field

LABELS={'product_scope':'用途与范围','external_power':'外部供电','external_interfaces':'外部接口',
        'critical_performance':'关键性能边界','acceptance':'核心验收结果'}
QUESTIONS={
 'product_scope':'这块板主要用在什么实际场景？先说最常见的一种用法就可以。',
 'external_power':'你准备怎样给这块板供电？可以说电池、适配器或电脑供电，以及手头电源上标的电压；不确定时我会给建议。',
 'external_interfaces':'这些接口主要接什么外部设备？先说实际用途，我会继续整理连接边界。',
 'critical_performance':'在实际使用时，你最在意哪项效果？如果没有额外数值要求，也可以告诉我，我会保留明确的功能验收。',
 'acceptance':'拿到成品后，你最先会怎样试用，看到什么结果就算满足需要？'}


class IntakeCheck(BaseModel):
    model_config=ConfigDict(extra='forbid')
    key:Literal['product_scope','external_power','external_interfaces','critical_performance','acceptance']
    state:Literal['confirmed','open','design_stage','not_applicable']
    conclusion:str=Field(min_length=1,max_length=1800)
    requirement_ids:list[str]=Field(max_length=80)
    source_message_id:str=Field(default='',max_length=64)
    source_quote:str=Field(default='',max_length=3000)


def apply_checks(doc,original,result,messages,aliases,changes):
    if not result.intake_policy_revision and not doc.get('intake_policy_version'):return
    doc['intake_policy_version']=1
    reqs={r['id']:r for r in doc['requirements']}
    checks={c['key']:copy.deepcopy(c) for c in original.get('intake_checks',[])}
    for check in checks.values():
        if set(check['requirement_ids'])&set(changes):check['state']='review'
        check['requirement_ids']=[rid for rid in check['requirement_ids'] if rid in reqs]
    users={m['id']:m['content'] for m in messages if m['role']=='user'}
    seen=set()
    for update in result.intake_checks:
        row=update.model_dump()
        if row['key'] in seen:raise ValueError('产品边界检查重复，请按五个稳定主题增量更新。')
        seen.add(row['key'])
        row['requirement_ids']=list(dict.fromkeys(aliases.get(rid,rid) for rid in row['requirement_ids']))
        if set(row['requirement_ids'])-set(reqs):raise ValueError('产品边界检查引用了不存在的需求。')
        proven=bool(row['source_quote'] and row['source_quote'] in users.get(row['source_message_id'],''))
        if proven and not row['requirement_ids']:
            # These are five stable checkpoints. An incremental engineering
            # update need not recreate their already valid product lineage.
            prior=checks.get(row['key'],{})
            retained=[rid for rid in prior.get('requirement_ids',[]) if rid in reqs]
            if retained:row.update(requirement_ids=retained,requirement_link_basis='沿用本检查点已有的有效需求关联')
        if row['state'] in ('confirmed','design_stage','not_applicable') and (not proven or not row['requirement_ids']):
            missing='source_message_id/source_quote缺失' if not proven else 'requirement_ids为空，需关联当前真实REQ'
            raise ValueError('关闭产品边界需要真实原话和关联需求：intake_checks.'+row['key']+' 的 '+missing+'；资料未知保留 open，不替用户作决定。')
        if row['state']=='confirmed' and any(reqs[rid]['state']!='stated' for rid in row['requirement_ids']):
            raise ValueError('已明确的产品边界不能依赖待定或建议需求。')
        checks[row['key']]=row
    doc['intake_checks']=list(checks.values())


def review(doc):
    if not doc.get('intake_policy_version'):return {'enabled':False,'gaps':[],'rechecks':[],'checks':[]}
    by_key={c['key']:c for c in doc.get('intake_checks',[])}
    checks=[{**by_key.get(key,{'key':key,'state':'review','conclusion':'助手需要依据已有内容整理这一必要边界。','requirement_ids':[]}), 'label':label} for key,label in LABELS.items()]
    return {'enabled':True,'checks':checks,'gaps':[c for c in checks if c['state']=='open'],
            'rechecks':[c for c in checks if c['state']=='review']}


def fallback_question(doc,message_id):
    gaps=review(doc)['gaps']
    if not gaps:return
    first=gaps[0];key=first['key']
    doc['questions'].append({'id':'boundary-'+key,'decision_key':'boundary:'+key,
        'question':QUESTIONS[key],'reason':first['conclusion'],'options':[],
        'requirement_ids':first['requirement_ids'],'field_ids':[],
        'source_message_id':message_id,'branch':'overview' if key=='product_scope' else 'performance' if key in ('external_power','critical_performance') else 'functions',
        'priority':2,'status':'pending'})
