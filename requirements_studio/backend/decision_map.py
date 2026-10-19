"""Current decisions and their real dependencies, never a second chat transcript.

This is a read-only projection of saved objects. Catalog identities prove lookup
identity only; they never promote a chip rating to a board requirement.
"""
from __future__ import annotations

import copy

from .convergence import FIELDS
from .module_catalog import TYPES
from .device_domain import context_assertions
from .formatting import constraint_value,metric_key

GROUPS = [('purpose', '用途与范围', '做什么，给谁用，本版做到哪里'),
          ('functions', '功能与验收', '必须完成的动作和可检查的结果'),
          ('interfaces', '供电、性能与接口', '已给定的边界、参数和连接要求'),
          ('devices', '器件与固定条件', '指定型号、使用意图和资料核实'),
          ('implementation', '模块承接与设计任务', '谁实现这些功能，还需设计什么')]
STATUS = {'stated': '用户已明确', 'suggested': 'AI 建议', 'tbd': '待明确',
          'conflict': '存在冲突', 'not_applicable': '不纳入本版', 'proposed': '待采用方案',
          'confirmed': '已确认方案', 'design_stage': '实现待设计', 'review': '关联复核中',
          'waiting': '待你决定', 'processing': '回答已收到', 'deferred': '暂缓决定'}


def requirement_group(row):
    category = row.get('category')
    if category == 'overview': return 'purpose'
    if category in ('selection',): return 'devices'
    if category in ('performance', 'architecture'): return 'interfaces'
    return 'functions'


def _source(row):
    return [{'message_id': row['source_message_id'], 'quote': row.get('source_quote', '')}] if row.get('source_message_id') else []


def _versions(snapshots, collection, key, fields):
    versions = []
    for snapshot in snapshots:
        row = next((r for r in snapshot.get(collection, []) if r.get('id') == key), None)
        if not row: continue
        value = {k: copy.deepcopy(row.get(k)) for k in fields}
        if not versions or versions[-1]['value'] != value:
            versions.append({'version': snapshot['version'], 'value': value,
                             'source_message_id': row.get('source_message_id', '')})
    return versions


def constraint_context(doc, domain):
    """Explicit, differently scoped facts the model must reuse before asking."""
    return {
        'fixed_device_choices': [{k: r.get(k) for k in ('id', 'mention', 'intent', 'identity_scope', 'purpose', 'quantity', 'requirement_ids', 'module_ids')}
                                 for r in doc.get('device_references', []) if r.get('lifecycle', 'active') == 'active'],
        'product_constraints': copy.deepcopy(doc.get('numeric_constraints', [])),
        'device_design_constraints': [c for c in doc.get('device_constraints', []) if c['kind'] == 'design_requirement'],
        'unverified_user_device_descriptions': [c for c in doc.get('device_constraints', []) if c['kind'] == 'user_device_description'],
        'known_product_decisions': [{'id': r['id'], 'name': r['name'], 'value': r['value'], 'unit': r.get('unit', ''),
                                    'spec_field_ids': r.get('spec_field_ids', [])}
                                   for r in doc['requirements'] if r['state'] == 'stated' and r.get('source_message_id')],
        'identity_results': [{k: r.get(k) for k in ('id', 'mention', 'resolution_status', 'identity', 'document_status')}
                             for r in domain.get('references', [])],
        'rule': '这些当前决定沿用至用户明确修改。器件介绍不是整机目标，目录定位不是电气通过；先复用已有值，再问真正缺少的产品决定。'}


def decision_map(doc, domain, snapshots=(), submissions=()):
    nodes = []; edges = []
    reqs = {r['id']: r for r in doc['requirements']}
    modules = {m['id']: m for m in doc.get('modules', []) if m['state'] != 'not_applicable'}
    references = {r['id']: r for r in domain.get('references', [])}
    # Plain document callers, e.g. contract consumers, can still show user intent.
    for r in doc.get('device_references', []):
        if r.get('lifecycle', 'active') == 'active': references.setdefault(r['id'], r)
    pending = {r['requirement_id'] for r in doc.get('decision_reviews', [])}
    locked = {s['question_id'] for s in submissions if s['status'] in ('sending', 'processing', 'queued')}

    def add(key, kind, group, title, conclusion, status, **extra):
        node = dict(id=key, kind=kind, group=group, title=title, conclusion=conclusion,
                    status=status, status_label=STATUS.get(status, status), sources=[], facts=[],
                    evidence_ids=[], requirement_ids=[], module_ids=[], spec_fields=[], history=[], notes=[], **extra)
        nodes.append(node)
        return node

    for rid, r in reqs.items():
        node = add(rid, 'requirement', requirement_group(r), r['name'], r['value'] + (' '+r['unit'] if r.get('unit') else ''),
                   'review' if rid in pending else r['state'])
        node.update(sources=_source(r), requirement_ids=[rid], module_ids=[mid for mid,m in modules.items() if rid in m['requirement_ids']],
                    basis='用户要求' if r.get('source_message_id') else 'AI 建议或待整理',
                    spec_fields=[{'id': f, 'label': FIELDS[f]['label'], 'section': FIELDS[f]['topic_id'].removeprefix('V3-')}
                                 for f in r.get('spec_field_ids', []) if f in FIELDS],
                    history=_versions(snapshots, 'requirements', rid, ('name', 'value', 'unit', 'state')))
        for constraint in doc.get('numeric_constraints', []):
            if constraint['requirement_id'] == rid:
                node['facts'].append({'label': constraint['scope'], 'value': _number(constraint), 'basis': '用户给定参数', 'conditions': constraint.get('conditions', '')})
        if r['state'] == 'not_applicable': node['notes'].append('已排除；不会作为本版待实现功能交给后续。')

    from .intake import review as intake_review
    boundary_groups={'product_scope':'purpose','external_power':'interfaces','external_interfaces':'interfaces','critical_performance':'interfaces','acceptance':'functions'}
    for check in intake_review(doc)['checks']:
        status={'confirmed':'stated','open':'waiting'}.get(check['state'],check['state'])
        node=add('boundary:'+check['key'],'boundary',boundary_groups[check['key']],check['label'],check['conclusion'],status)
        node.update(basis='需求交接必要边界',sources=_source(check),requirement_ids=check['requirement_ids'])
        questions=[q for q in doc['questions'] if q.get('decision_key')=='boundary:'+check['key'] or (check['state']=='open' and set(q.get('requirement_ids',[]))&set(check['requirement_ids']))]
        if len(questions)==1:
            node['question_id']=questions[0]['id']
            if questions[0]['id'] in locked:node.update(status='processing',status_label=STATUS['processing'])
        for rid in check['requirement_ids']:
            if rid in reqs:edges.append({'source':rid,'target':node['id'],'kind':'supports','label':'明确交接边界'})

    for rid, r in references.items():
        state = 'stated' if r['intent'] == 'must' else 'suggested' if r['intent'] in ('prefer', 'reference') else 'tbd'
        intent = {'must':'本版必须使用', 'prefer':'优先考虑，可替换', 'reference':'仅作参考', 'undecided':'使用意图待明确'}[r['intent']]
        identity = r.get('identity') or {}; resolution = r.get('resolution_status', 'unresolved')
        status = {'matched':'已定位目录身份', 'ambiguous':'多个目录候选', 'not_found':'本次目录未找到', 'unavailable':'资料来源暂不可用', 'family':'系列约束，尚未定具体型号', 'unresolved':'身份待检索'}.get(resolution, resolution)
        node = add(rid, 'device', 'devices', r['mention'], intent+'；'+r.get('purpose', ''), state)
        node.update(basis='用户器件选择', sources=_source(r), requirement_ids=r.get('requirement_ids', []),
                    status_label={'must':'用户已指定','prefer':'用户偏好','reference':'仅供参考','undecided':'意图待明确'}[r['intent']],
                    module_ids=[mid for mid in r.get('module_ids', []) if mid in modules],
                    evidence_ids=r.get('evidence_ids', []),
                    history=_versions(snapshots, 'device_references', rid, ('mention', 'intent', 'quantity', 'identity_scope', 'lifecycle')))
        node['facts'].append({'label':'资料核实', 'value':status, 'basis':'数据库检索', 'conditions':'定位到目录不等于器件适用性已验证。'})
        if identity:
            node['facts'].append({'label':'当前目录身份', 'value':' / '.join(str(identity[k]) for k in ('model','supplier_part','package') if identity.get(k)), 'basis':'已保存目录证据', 'conditions':''})
        catalog=next((e for e in domain.get('evidence',[]) if e.get('kind')=='catalog' and e.get('reference_id')==rid),None)
        if catalog:
            for assertion in context_assertions(catalog.get('assertions',[]),6):
                if not assertion.get('raw_value'):continue
                node['facts'].append({'label':assertion.get('label') or assertion['field_key'], 'value':str(assertion['raw_value'])[:500],
                                      'basis':'手册支持的器件条件' if assertion.get('validation_state')=='verified_for_claim' else '目录参数，适用性待核验',
                                      'conditions':assertion.get('conditions') or '这是器件资料条件，不是整机保证指标或用户选择的工作模式。'})
        node['facts'].append({'label':'实际器件数量', 'value':str(r['quantity'])+' 颗/件' if r.get('quantity') else '尚未给定，由实现与选型确定', 'basis':'用户给定' if r.get('quantity') else '后续设计', 'conditions':'功能路数不能直接当成芯片颗数。'})
        for c in doc.get('device_constraints', []):
            if c['reference_id'] != rid: continue
            node['facts'].append({'label':metric_key(c), 'value':constraint_value(c),
                                  'basis':'用户设计要求' if c['kind']=='design_requirement' else '用户器件描述，待资料核验', 'conditions':c.get('conditions','')})
        checks = [c for c in domain.get('checks', []) if c.get('reference_id') == rid and c.get('status') in ('conflict','unknown','conditional','stale')]
        node['notes'] += [c['description'] for c in checks]
        if any(c.get('blocking') for c in checks): node.update(status='conflict',status_label=STATUS['conflict'])
        if resolution != 'matched': node['notes'].append('指定/偏好意图继续保留；不以未命中或候选第一条替代用户决定。')
        for target in r.get('requirement_ids', []):
            if target in reqs: edges.append({'source':rid,'target':target,'kind':'constrains','label':'器件条件关联'})
        for target in r.get('module_ids', []):
            if target in modules: edges.append({'source':rid,'target':target,'kind':'implements','label':'用于模块'})

    for mid, m in modules.items():
        node = add(mid,'module','implementation',m['name'],m['purpose'],m['state'])
        node.update(basis='功能模块方案',sources=_source(m),requirement_ids=m['requirement_ids'],module_ids=[mid])
        node['facts'] += [{'label':'规范类别','value':TYPES.get(m['type_id'],{}).get('name',m['type_id']),'basis':'专业模块规范','conditions':''},
                          {'label':'独立电路路数','value':str(m['quantity']),'basis':'功能分解','conditions':'不是芯片颗数'}]
        if m.get('boundary'): node['notes'].append(m['boundary'])
        for target in m['requirement_ids']:
            if target in reqs: edges.append({'source':target,'target':mid,'kind':'implemented_by','label':'由模块承接'})

    for link in doc.get('module_links', []):
        if link['source'] not in modules or link['target'] not in modules: continue
        key='interface:'+link['id']
        node=add(key,'interface','interfaces',modules[link['source']]['name']+' → '+modules[link['target']]['name'],link['description'],link['state'])
        node.update(basis='接口需求与设计边界',sources=_source(link),module_ids=[link['source'],link['target']],requirement_ids=link.get('requirement_ids',[]))
        for field,label in (('protocol','协议'),('signals','信号'),('voltage','电平/电源域'),('rate','速率'),('boundary','板内外边界'),('role','角色'),('conditions','适用条件')):
            value=link.get(field)
            if value: node['facts'].append({'label':label,'value':'、'.join(value) if isinstance(value,list) else value,'basis':'接口约束','conditions':''})
        missing=[label for field,label in (('protocol','协议'),('voltage','电平/电源域'),('boundary','板内外边界')) if not link.get(field)]
        if missing: node['notes'].append('尚未结构化给定：'+'、'.join(missing)+'；说明中的已有约束继续有效，实现阶段须落实。')
        for mid in (link['source'],link['target']): edges.append({'source':key,'target':mid,'kind':'interface','label':'接口端点'})

    for q in doc['questions']:
        group=next((requirement_group(reqs[rid]) for rid in q.get('requirement_ids',[]) if rid in reqs),'purpose')
        state='processing' if q['id'] in locked else 'deferred' if q.get('status')=='deferred' else 'waiting'
        node=add('question:'+q['id'],'question',group,q['question'],q.get('reason',''),state)
        node.update(basis='需要产品决定',question_id=q['id'],options=q.get('options',[]),option_impacts=q.get('option_impacts',[]),
                    requirement_ids=[rid for rid in q.get('requirement_ids',[]) if rid in reqs],impact=q.get('impact',''))
        for rid in node['requirement_ids']: edges.append({'source':node['id'],'target':rid,'kind':'decides','label':'回答后明确'})

    groups=[]
    for key,label,description in GROUPS:
        group_nodes=[n for n in nodes if n['group']==key and n['status']!='not_applicable']
        groups.append({'id':key,'label':label,'description':description,'total':len(group_nodes),
                       'settled':sum(n['status'] in ('stated','confirmed') for n in group_nodes),
                       'pending':sum(n['status'] in ('waiting','deferred','processing','conflict','review','tbd') for n in group_nodes)})
    return {'revision':'decision-map/1','version':doc['version'],'root':{'title':doc['title'],'summary':doc['summary']},
            'groups':groups,'nodes':nodes,'edges':edges,
            'counts':{'settled':sum(g['settled'] for g in groups),'questions':len(doc['questions']),
                      'specified_devices':sum(r['intent']=='must' for r in references.values()),
                      'located_devices':sum(r.get('resolution_status')=='matched' for r in references.values())},
            'policy':'按当前决定组织，原话与历史仅作展开依据；关系来自实际条款、器件和模块引用。'}


def _number(row):
    low,high=row.get('minimum'),row.get('maximum');unit=row.get('unit','')
    if low is not None and high is not None: return (str(low) if low==high else f'{low}～{high}')+' '+unit
    return ('≥ '+str(low) if low is not None else '≤ '+str(high))+' '+unit
