"""Decision dependencies and a source-backed, current-state view of the journey.

No model-generated second history: steps come from actual saves and answers.
Historical snapshots remain immutable. Presentation always resolves current IDs.
"""
from __future__ import annotations

import copy


def reconcile_requirements(doc, original, result, resolved, changed, aliases, message_id):
    if not result.guidance_revision and not doc.get('guidance_version'):
        return
    doc['guidance_version']=1
    current={r['id']:r for r in doc['requirements']}
    before={r['id']:r for r in original['requirements']}
    reviewed={aliases.get(r,r) for r in result.reviewed_requirement_ids}
    if reviewed-set(current):
        raise ValueError('关联复核引用了不存在的需求，请使用当前 REQ 编号。')
    updated={aliases.get(r.id,r.id) for r in result.updates}
    pending={r['requirement_id']:r for r in original.get('decision_reviews',[]) if r['requirement_id'] in current}
    targets=set(changed)&set(before)
    field_ids=set()
    for q in original['questions']:
        if q['id'] in resolved:
            targets.update(q['requirement_ids']);field_ids.update(q.get('field_ids',[]))
    for f in original.get('field_resolutions',[]):
        if set(f['requirement_ids'])&targets:
            field_ids.add(f['field_id'])
    # Broad indexes are views of many requirements, not semantic dependencies.
    # In particular a sensor answer must not reopen every must-have function.
    def specific(fid):
        return not fid.startswith(('V3-1.1:','V3-1.2:','V3-2.1.','V3-2.2:','V3-4.2.1:','V3-5.1:'))
    field_ids={fid for fid in field_ids if specific(fid)}
    for f in [*original.get('field_resolutions',[]),*doc.get('field_resolutions',[])]:
        if f['field_id'] in field_ids:
            targets.update(f['requirement_ids'])
    for r in before.values():
        if field_ids&set(r.get('spec_field_ids',[])):
            targets.add(r['id'])
    # Explicit interface contracts can carry a real dependency across modules.
    for link in original.get('module_links',[]):
        if targets.intersection(link.get('requirement_ids',[])):
            for module in original.get('modules',[]):
                if module['id'] in (link['source'],link['target']):targets.update(module['requirement_ids'])
    for rid in targets&set(current):
        pending[rid]={'requirement_id':rid,'message_id':message_id,
                      'reason':'对照本轮决定复核关联表述，避免旧的待定说明或旧方案继续留在正文。'}
    for rid in updated|reviewed:
        pending.pop(rid,None)
    doc['decision_reviews']=list(pending.values())


def next_product_question(doc,gap,message_id):
    """Human wording for the legacy final-gap fallback, never an ID questionnaire."""
    core=gap and gap['id']=='V3-1.1'
    if core:
        text='这个产品主要给谁用、在什么场合使用？先说一个最常见的使用场景就可以。'
        reason='先确定使用者和场景，后面的功能与方案才有依据。'
    elif gap and gap['id']=='V3-2.1.1':
        text='在你最常用的场景里，做到什么就算好用？可以用一次实际使用的过程描述，不必先给技术参数。'
        reason='需要把核心功能和可检查的使用结果说清楚；具体实现参数由我继续整理。'
    else:
        rows=[r for r in doc['requirements'] if r['state'] in ('tbd','suggested','conflict')]
        title=rows[0]['name'] if rows else (gap['title'] if gap else '当前产品范围')
        text=f'关于“{title}”，你希望实际使用时达到什么效果？不确定的话，可以告诉我，我会给出建议和取舍。'
        reason='这一项仍有实际产品取舍；其他已经明确的要求继续沿用。'
    return {'id':'gap-'+gap['id'] if gap else 'gap-requirements','question':text,
            'reason':reason,'options':[],'requirement_ids':[],
            'field_ids':[f['field_id'] for f in doc.get('field_resolutions',[]) if gap and f['field_id'].startswith(gap['id']+':') and f['state'] not in ('confirmed','not_applicable','design_stage')],
            'source_message_id':message_id,'branch':'overview' if core else 'functions','status':'pending'}


def _signature(row):
    return tuple(row.get(k) for k in ('name','value','unit','state','type_id','purpose','boundary','quantity'))


def public_change(before,after):
    """An internal check needs no second chat reply if product content is stable."""
    for key in ('title','summary','flow'):
        if before.get(key)!=after.get(key):return True
    for key,fields in (
        ('requirements',('category','name','value','unit','state','priority','implementation')),
        ('modules',('type_id','name','purpose','boundary','quantity','state','requirement_ids')),
        ('module_links',('source','target','kind','description','state','protocol','signals','voltage','rate','boundary','role','conditions','requirement_ids')),
        ('intake_checks',('key','state','conclusion','requirement_ids')),
        ('device_references',('mention','intent','quantity','lifecycle','device_key','identity_scope')),
        ('questions',('question','options','reason','status'))):
        normalize=lambda doc:{r.get('id',r.get('key')):tuple(r.get(k) if not isinstance(r.get(k),list) else tuple(r[k]) for k in fields) for r in doc.get(key,[])}
        if normalize(before)!=normalize(after):return True
    return False


def journey_view(doc,messages,snapshots,runs,submissions):
    """Small vertical decision tree derived from existing versioned objects."""
    users={m['id']:m for m in messages if m['role']=='user'}
    current={r['id']:r for r in doc['requirements']}
    modules={m['id']:m for m in doc.get('modules',[])}
    questions={}
    origins={}
    for snapshot in snapshots:
        for question in snapshot.get('questions',[]):
            origins.setdefault(question['id'],question.get('source_message_id',''))
            questions[question['id']]=copy.deepcopy(question)
    questions.update({q['id']:copy.deepcopy(q) for q in doc.get('question_history',[])})
    questions.update({q['id']:copy.deepcopy(q) for q in doc['questions']})
    live={q['id'] for q in doc['questions']}
    receipts={s['question_id']:s for s in submissions}
    answers={}
    for r in runs:
        for a in r['answers']:
            if not a.get('deferred') and r['status']=='completed':
                answers[a['question_id']]={**a,'message_id':r['message_id']}
    turns={mid:{'id':mid,'input':m['content'],'at':m['created_at'],'versions':[],
                'analyses':[],'results':[],'branches':[],'legacy':True} for mid,m in users.items()}
    previous={};first_closed={};closed_fields={};changed_at={}
    for snapshot in snapshots:
        event=snapshot.get('decision_event',{})
        if event.get('version')!=snapshot['version']:event={}
        mid=event.get('message_id')
        if mid not in turns:
            # Old runs did not store the input→version link. Do not invent one.
            candidates=[m for m in users.values() if m['created_at']<=snapshot['updated_at']]
            mid=candidates[-1]['id'] if candidates else None
        turn=turns.get(mid)
        if turn:
            turn['versions'].append(snapshot['version'])
            if event:
                turn['legacy']=False
                if event.get('reply'):
                    target=turn.setdefault('internal_notes',[]) if event.get('kind')=='continuation' else turn['analyses']
                    target.append(event['reply'])
            elif snapshot['changes']:
                turn['analyses'].append('；'.join(snapshot['changes']))
        rows={r['id']:r for r in snapshot['requirements']}
        for rid,row in rows.items():
            if rid not in previous or _signature(row)!=_signature(previous[rid]):
                changed_at[rid]=mid
                if turn and rid not in turn['results']:turn['results'].append(rid)
        for q in snapshot.get('question_history',[]):
            if q['id'] not in first_closed:
                first_closed[q['id']]=rows
                closed_fields[q['id']]=snapshot.get('field_resolutions',[])
        previous=rows
    for q in questions.values():
        qid=q['id'];origin=origins.get(qid) or q.get('source_message_id');turn=turns.get(origin)
        if turn is None:
            # Missing provenance is visible; never assign an old choice to a new input.
            continue
        evidence=answers.get(qid)
        resolved=q.get('resolved_by')
        answer_mid=resolved if resolved in users else evidence.get('message_id') if evidence else ''
        if answer_mid in turns and qid not in live:
            turns[answer_mid].setdefault('answered',[]).append(qid)
        related=set(q.get('requirement_ids',[]))
        field_links=closed_fields.get(qid,doc.get('field_resolutions',[]))
        for f in field_links:
            if f['field_id'] in q.get('field_ids',[]):related.update(f['requirement_ids'])
        old=first_closed.get(qid,{})
        adjusted=any(rid in old and (rid not in current or _signature(old[rid])!=_signature(current[rid])) for rid in related)
        state='waiting' if qid in live else 'answered' if answer_mid else 'organized'
        receipt=receipts.get(qid)
        if qid in live and receipt and receipt['status'] in ('sending','processing','queued'):state='processing'
        if qid in live and q.get('status')=='deferred':state='deferred'
        turn['branches'].append({'id':qid,'question':q['question'],'reason':q.get('reason',''),
            'options':q.get('options',[]),'option_impacts':q.get('option_impacts',[]),
            'state':state,'adjusted':adjusted,'requirement_ids':sorted(related&set(current)),
            'answer':evidence['answer'] if evidence else receipt['answer'] if receipt else users.get(answer_mid,{}).get('content',''),
            'answer_message_id':answer_mid,'receipt':receipt})
    for mid,turn in turns.items():
        turn['results']=[rid for rid in turn['results'] if rid in current]
        turn['current_results']=[rid for rid in turn['results'] if changed_at.get(rid)==mid]
        turn['module_ids']=[m['id'] for m in modules.values() if m.get('source_message_id')==mid]
        turn['analyses']=list(dict.fromkeys(turn['analyses']))
    return {'turns':list(turns.values()),'pending_reviews':doc.get('decision_reviews',[]),
            'version':doc['version'],'note':'历史内容由已保存版本整理；当前结果以本版需求为准。'}
