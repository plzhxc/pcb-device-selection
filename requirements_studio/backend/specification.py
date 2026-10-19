"""The V3 reading structure shared by the UI and the downloaded document."""
from __future__ import annotations
from .coverage import CATALOG
from .experience import product_field

CHAPTERS={1:'项目概述',2:'功能需求详解',3:'性能指标要求',4:'生产与测试考虑'}
PARENTS={'2.1':'核心功能列表','3.1':'电气性能','3.2':'环境适应性（可选）','3.3':'可靠性要求',
         '4.1':'可制造性','4.2':'可测试性'}
RESOLUTION_LABELS={'confirmed':'已明确','not_applicable':'明确不纳入本版','design_stage':'用户委托设计',
                   'tbd':'尚未明确','conflict':'存在冲突'}
REQUIREMENT_LABELS={'stated':'用户已明确','suggested':'AI 建议，待选择','tbd':'待明确','conflict':'存在冲突','not_applicable':'明确不纳入本版'}


def specification_view(doc,phase):
    requirements={r['id']:r for r in doc['requirements']}
    rechecks=set(doc.get('recheck_fields',[]))
    scope=phase.get('product_review',{})
    live_rechecks=set(scope.get('decision_rechecks',[])+scope.get('module_rechecks',[]))
    pending={r['requirement_id'] for r in doc.get('decision_reviews',[])}
    fields={f['id']:f for f in phase['fields']}
    explicit={fid:[] for fid in fields}
    for r in requirements.values():
        for fid in r.get('spec_field_ids',[]):
            if fid in explicit:explicit[fid].append(r['id'])
    chapters=[];module_topics=[];assigned=set()
    selected=set(phase['product_review']['module_topics'])
    for chapter,title in {**CHAPTERS,5:'电路模块分析'}.items():
        topics=[]
        for topic in (t for t in CATALOG if t['chapter']==chapter and (chapter<5 or (t['id'] in selected and t['id']!='V3-5.1'))):
            items=[]
            for original_field in (f for f in fields.values() if f['topic_id']==topic['id']):
                field=dict(original_field)
                # These two clauses describe the same product performance target.
                # Reuse an explicitly checked target; never infer new numbers or
                # close the underlying ledger on the user's behalf.
                core=fields.get('V3-2.1.1:F03',{})
                if (field['id']=='V3-1.2:F02' and field['state']=='tbd' and not field.get('conclusion')
                        and core.get('state')=='confirmed' and core.get('conclusion')
                        and core['id'] not in rechecks and not (set(core.get('requirement_ids',[]))&pending)):
                    field.update({k:core.get(k,'') for k in ('conclusion','source_message_id','source_quote')})
                    field.update(state='confirmed',requirement_ids=core.get('requirement_ids',[]),derived_from=core['id'])
                fid=field['id'];ids=explicit[fid] or field.get('requirement_ids',[])
                ids=[rid for rid in ids if rid in requirements]
                # Old ledgers sometimes attach the full function list to all
                # three fields. Render names/descriptions as references, not
                # a copied historical paragraph that can become stale.
                display='requirements' if ids else 'summary'
                if fid=='V3-2.1.1:F01':display='names'
                if fid=='V3-2.1.1:F03' and not explicit[fid]:display='summary'
                if fid=='V3-1.1:F01':display='title'
                if fid=='V3-1.1:F02':display='summary'
                if fid=='V3-2.1.2:F01':
                    ids=[r['id'] for r in requirements.values() if r['category']=='functions' and r['priority']=='should']
                if fid=='V3-2.1.3:F01':
                    ids=[r['id'] for r in requirements.values() if r['category']=='functions' and r['priority']=='could']
                assigned.update(ids)
                reviewing=fid in live_rechecks or bool(set(ids)&pending)
                conclusion=field.get('conclusion','').strip()
                if reviewing:conclusion=''
                # A sentence-sized clause should not repeat the whole function
                # paragraph. Only use a checked, current field summary; during
                # rechecks continue to expose the actual current REQ values.
                if (fid in {'V3-1.1:F03','V3-1.2:F02','V3-2.1.1:F03','V3-3.1.3:F01'}
                        and field['state']=='confirmed' and conclusion and not reviewing):
                    display='summary'
                engineering=not product_field(field)
                label=RESOLUTION_LABELS[field['state']]
                if field.get('derived_from'):label='已明确 · 关联核心目标'
                if reviewing:label='内容已记录 · AI 关联复核中'
                elif fid in rechecks:label='已记录边界 · 后续设计核定'
                elif field['state']=='tbd':
                    label='待设计评估' if engineering else '未提出明确要求'
                    if any(fid in q.get('field_ids',[]) for q in doc['questions']):label='待你决定'
                    if any(requirements[r]['state']=='suggested' for r in ids):label='AI 建议，待选择'
                    elif ids and display in ('requirements','names'):label='要求已记录 · 条款待整理'
                # A working title is editable and never a forced decision.
                if display=='title':
                    conclusion=doc['title'];label='产品工作名称' if field['state']=='tbd' else label
                fallback=('实现阶段结合产品目标评估；当前未给出额外指标。' if engineering else
                          '本项目尚未提出该项的明确要求；如影响使用或验收，助手会继续引导。')
                if fid in ('V3-2.1.2:F01','V3-2.1.3:F01') and not ids:
                    fallback='当前未单独提出此优先级的功能。'
                items.append({**field,'requirement_ids':ids,'display':display,'content':conclusion or fallback,
                    'status_label':label,'review_pending':reviewing,'engineering':engineering,
                    'source_message_id':field.get('source_message_id') or (field.get('recheck_evidence') or {}).get('source_message_id','')})
            prefix='.'.join(topic['section'].split('.')[:2])
            parent={'section':prefix,'title':PARENTS[prefix]} if prefix in PARENTS else None
            topics.append({'id':topic['id'],'section':topic['section'],'title':topic['title'],'fields':items,'parent':parent,
                           'engineering':all(f['engineering'] for f in items)})
        if chapter<5:chapters.append({'number':chapter,'title':title,'topics':topics})
        else:module_topics=topics
    return {'revision':'V3.0','chapters':chapters,'module_topics':module_topics,
            'unassigned_requirement_ids':[rid for rid in requirements if rid not in assigned and requirements[rid]['category']!='architecture'],
            'field_count':sum(len(t['fields']) for c in chapters for t in c['topics']),
            'confirmed_count':sum(f['state']=='confirmed' and not f['review_pending'] for c in chapters for t in c['topics'] for f in t['fields'])}


def render_normative_topics(topics,doc,source):
    reqs={r['id']:r for r in doc['requirements']};lines=[];last_parent=None
    for topic in topics:
        parent=topic.get('parent')
        if parent and parent['section']!=last_parent:
            lines += [f"### {parent['section']} {parent['title']}",'']
        last_parent=parent['section'] if parent else None
        lines += [f"{'####' if parent else '###'} {topic['section']} {topic['title']}",'']
        for f in topic['fields']:
            lines += [f"- **{f['label']}** · {f['status_label']}"]
            rows=[reqs[rid] for rid in f['requirement_ids'] if rid in reqs]
            if rows and f['display'] in ('requirements','names'):
                for row in rows:
                    value=row['name'] if f['display']=='names' else row['name']+'：'+row['value']+((' '+row['unit']) if row['unit'] and row['unit'] not in row['value'] else '')
                    lines += ['  - '+value.replace('\n','\n    ')+f"（{row['id']}；{REQUIREMENT_LABELS[row['state']]}；{source(row)}）"]
            else:
                lines += ['  - '+f['content'].replace('\n','\n    ')]
                if f.get('source_quote'):lines += ['  - 依据：'+source(f)]
        lines += ['']
    return lines


def render_normative_chapters(spec,doc,source):
    reqs={r['id']:r for r in doc['requirements']}
    lines=[]
    for chapter in spec['chapters']:
        lines += [f"## 第 {chapter['number']} 章 {chapter['title']}",'']
        lines += render_normative_topics(chapter['topics'],doc,source)
    extra=[reqs[rid] for rid in spec['unassigned_requirement_ids'] if reqs[rid]['category']!='architecture']
    if extra:
        lines += ['### 已记录的补充要求','', '以下内容继续有效，尚未细分到具体规范条款；不因归档位置丢失。','']
        lines+=['| 需求 | 当前要求 | 状态 | 依据 |','| --- | --- | --- | --- |']
        cell=lambda value:str(value).replace('|','\\|').replace('\r','').replace('\n','<br>')
        for r in extra:
            value=r['value']+((' '+r['unit']) if r['unit'] and r['unit'] not in r['value'] else '')
            lines+=['| '+' | '.join(cell(v) for v in [r['id']+' · '+r['name'],value,REQUIREMENT_LABELS[r['state']],source(r)])+' |']
        lines += ['']
    return lines
