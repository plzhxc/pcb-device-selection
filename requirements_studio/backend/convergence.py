"""Field-level decisions, specification module instances and bounded consistency rules."""
from __future__ import annotations
import copy
import json
import math
import re
from fractions import Fraction
from collections import Counter
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .coverage import CATALOG
from .module_catalog import TYPES, CATEGORIES
from .experience import decision_scope,product_field

FIELDS = {f'{t["id"]}:F{i:02d}': {'id': f'{t["id"]}:F{i:02d}', 'topic_id': t['id'],
          'label': label, 'chapter': t['chapter'], 'optional': '可选' in label}
          for t in CATALOG for i, label in enumerate(t['fields'], 1)}
CLOSED = {'confirmed', 'not_applicable', 'design_stage'}
PRODUCT_CORE={f'V3-1.1:F{i:02d}' for i in range(1,5)}|{f'V3-2.1.1:F{i:02d}' for i in range(1,4)}
for _fid,_field in FIELDS.items():
    _field['delegatable']=_fid not in PRODUCT_CORE


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class FieldResolution(Strict):
    field_ids: list[str] = Field(min_length=1,max_length=len(FIELDS))
    state: Literal['confirmed','not_applicable','design_stage','tbd','conflict']
    conclusion: str = Field(min_length=1,max_length=2500)
    requirement_ids: list[str] = Field(max_length=80)
    source_message_id: str = Field(max_length=64)
    source_quote: str = Field(max_length=3000)


class ModuleUpdate(Strict):
    id: str = Field(max_length=64)
    type_id: str = Field(max_length=64)
    name: str = Field(min_length=1,max_length=160)
    purpose: str = Field(min_length=1,max_length=1000)
    boundary: str = Field(max_length=1500)
    quantity: int = Field(ge=1,le=1000)
    state: Literal['proposed','confirmed','design_stage','tbd','not_applicable']
    requirement_ids: list[str] = Field(max_length=80)
    source_message_id: str = Field(max_length=64)
    source_quote: str = Field(max_length=3000)


class ModuleLink(Strict):
    id: str = Field(min_length=1,max_length=64)
    source: str = Field(max_length=64)
    target: str = Field(max_length=64)
    kind: Literal['power','data','control','clock']
    source_port: str = Field(max_length=100)
    target_port: str = Field(max_length=100)
    description: str = Field(min_length=1,max_length=800)
    state: Literal['proposed','confirmed','design_stage']


class NumericConstraint(Strict):
    # Custom Python validators do not appear in JSON Schema. Constrain the actual
    # CLI output contract too, otherwise a structurally valid all-null row forces
    # another full model turn after several minutes of generation.
    model_config = ConfigDict(json_schema_extra={'anyOf':[
        {'properties':{'minimum':{'type':'number'}}},
        {'properties':{'maximum':{'type':'number'}}}]})
    id: str = Field(min_length=1,max_length=64)
    requirement_id: str = Field(max_length=32)
    metric: Literal['voltage','current','power','temperature','budget','battery_capacity','average_current','runtime','sample_period','retention','record_size','storage_capacity']
    scope: str = Field(min_length=1,max_length=120)
    conditions: str = Field(max_length=200)
    minimum: float | None
    maximum: float | None
    unit: str = Field(max_length=32)
    source_message_id: str = Field(max_length=64)
    source_quote: str = Field(max_length=3000)

    @model_validator(mode='after')
    def bounds(self):
        if self.minimum is None and self.maximum is None:
            raise ValueError('数值约束至少需要一个边界。')
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('数值约束下界不能大于上界。')
        return self


# The multiplier maps to canonical units; quantity and measurement conditions remain separate.
UNITS = {'V':('voltage',1), '伏':('voltage',1), 'mV':('voltage',.001), 'uV':('voltage',1e-6), 'µV':('voltage',1e-6), 'μV':('voltage',1e-6), 'kV':('voltage',1000),
         'A':('current',1), '安':('current',1), 'mA':('current',.001),
         'uA':('current',1e-6), 'μA':('current',1e-6), 'µA':('current',1e-6),
         'W':('power',1), '瓦':('power',1), 'mW':('power',.001), '毫瓦':('power',.001), 'uW':('power',1e-6),
         'kW':('power',1000), 'KW':('power',1000), '千瓦':('power',1000),
         '°C':('temperature',1), '℃':('temperature',1), 'degC':('temperature',1), 'K':('temperature',1),
         '元':('budget',1), '人民币元':('budget',1), 'CNY':('budget',1), 'RMB':('budget',1), 'Ah':('battery_capacity',1), 'mAh':('battery_capacity',.001),
         's':('time',1), '秒':('time',1), '秒钟':('time',1), 'sec':('time',1), 'second':('time',1), 'seconds':('time',1),
         'ms':('time',.001), 'us':('time',.000001), 'µs':('time',.000001), 'μs':('time',.000001),
         'min':('time',60), 'mins':('time',60), 'minute':('time',60), 'minutes':('time',60), '分钟':('time',60),
         'h':('time',3600), 'hour':('time',3600), 'hours':('time',3600), '小时':('time',3600),
         '天':('time',86400), '日':('time',86400), 'd':('time',86400), 'day':('time',86400), 'days':('time',86400),
         '周':('time',604800), 'week':('time',604800), 'weeks':('time',604800),
         'B':('bytes',1), 'KB':('bytes',1000), 'MB':('bytes',1e6), 'GB':('bytes',1e9), 'TB':('bytes',1e12),
         'KiB':('bytes',1024), 'MiB':('bytes',1048576), 'GiB':('bytes',1073741824), 'TiB':('bytes',1099511627776)}
DIMENSIONS = {'average_current':'current','runtime':'time','sample_period':'time','retention':'time',
              'record_size':'bytes','storage_capacity':'bytes'}


def scoped_unit(row):
    unit=UNITS.get(row['unit'])
    if unit is None and row['metric']=='budget':
        price=re.fullmatch(r'(?:人民币)?(?:元|CNY|RMB)/(台|个|件)',row['unit'].replace(' ',''))
        if price and re.search(r'(?:单|每)'+price[1],row['scope']):
            return ('budget',1)
    return unit


def normalize_number(row, bound):
    if row.get(bound) is None:
        return None
    unit = scoped_unit(row)
    if unit is None or unit[0] != DIMENSIONS.get(row['metric'],row['metric']):
        return None
    value=row[bound]*unit[1]
    return value-273.15 if row['unit']=='K' else value


def consistency_issues(doc):
    known = {r['id'] for r in doc['requirements'] if r['state']=='stated'}
    groups = {}
    issues = []
    for row in doc.get('numeric_constraints',[]):
        if row['requirement_id'] not in known:
            continue
        key = (row['metric'],row['scope'],row['conditions'])
        groups.setdefault(key,[]).append(row)
    for (metric,scope,conditions),rows in groups.items():
        # Calendar duration is meaningful in lifetime targets. Compare like units
        # without inventing a fixed number of seconds in a month or a year.
        calendar_units={r['unit'] for r in rows}
        if metric in ('runtime','retention') and len(calendar_units)==1 and calendar_units<={'年','月','year','month'}:
            lows=[r['minimum'] for r in rows if r.get('minimum') is not None]
            highs=[r['maximum'] for r in rows if r.get('maximum') is not None]
            if lows and highs and max(lows)>min(highs):
                issues.append({'id':'calendar-'+rows[0]['id'],'kind':'conflict','title':f'{scope} 的时长要求相互矛盾',
                               'description':'同一日历单位与相同工况的时长上下限没有交集。','requirement_ids':[r['requirement_id'] for r in rows]})
            continue
        if any(scoped_unit(r) is None or scoped_unit(r)[0]!=DIMENSIONS.get(metric,metric) for r in rows):
            issues.append({'id':'unit-'+rows[0]['id'],'kind':'review','title':'单位待核对',
                           'description':f'{scope} 的 {metric} 缺少可比较的同维度单位。', 'requirement_ids':[r['requirement_id'] for r in rows]})
            continue
        lows = [normalize_number(r,'minimum') for r in rows if r.get('minimum') is not None]
        highs = [normalize_number(r,'maximum') for r in rows if r.get('maximum') is not None]
        if lows and highs and max(lows) > min(highs)+1e-12:
            issues.append({'id':'range-'+rows[0]['id'],'kind':'conflict','title':f'{scope} 的要求相互矛盾',
                           'description':f'同一 {metric}、同一条件“{conditions or "未另限定"}”的上下限没有交集。需要修改要求，不能以建议掩盖。',
                           'requirement_ids':list(dict.fromkeys(r['requirement_id'] for r in rows))})
    # Deliberately limited to explicitly co-scoped facts. No device assumptions or typical currents.
    scopes = {(key[1],key[2]) for key in groups}
    for scope,conditions in scopes:
        pick=lambda metric,bound:[(normalize_number(r,bound),r) for r in groups.get((metric,scope,conditions),[]) if normalize_number(r,bound) is not None]
        capacity,current,runtime=pick('battery_capacity','maximum'),pick('average_current','minimum'),pick('runtime','minimum')
        calendar_runtime=[r for r in groups.get(('runtime',scope,conditions),[]) if r['unit'] in ('年','月','year','month')]
        if capacity and current and calendar_runtime:
            issues.append({'id':'calendar-energy-'+calendar_runtime[0]['id'],'kind':'review','title':'续航计算需要明确时长口径',
                           'description':'容量与平均电流需要和同一工况的小时/天数比较；日历年或月不自动折算为固定秒数。设计寿命可单独按年记录。',
                           'requirement_ids':[r['requirement_id'] for _,r in capacity+current]+[r['requirement_id'] for r in calendar_runtime]})
        if capacity and current and runtime:
            cap,cr=min(capacity,key=lambda x:x[0]);amp,ar=max(current,key=lambda x:x[0]);seconds,tr=max(runtime,key=lambda x:x[0])
            if cap>=0 and amp>0 and seconds>cap/amp*3600+1e-9:
                issues.append({'id':'energy-'+cr['id'],'kind':'conflict','title':'续航目标超过理想电量上界',
                               'description':f'同一工况下，容量÷平均电流的理想上界约 {cap/amp:.2f} 小时，低于要求的 {seconds/3600:.2f} 小时；尚未计入损耗。请调整容量、平均电流或续航目标。',
                               'requirement_ids':list(dict.fromkeys(r['requirement_id'] for r in (cr,ar,tr)))})
        # A lower bound, not a complete filesystem capacity estimate. A maximum
        # recording interval and minimum record size/time must share the exact
        # stream and conditions; acquisition/update rate alone is insufficient.
        def amounts(metric,bound):
            result=[]
            for r in groups.get((metric,scope,conditions),[]):
                unit=scoped_unit(r)
                if r.get(bound) is not None and unit and unit[0]==DIMENSIONS.get(metric,metric):
                    result.append((Fraction(str(r[bound]))*Fraction(str(unit[1])),r))
            return result
        sizes,periods,kept,limits=amounts('record_size','minimum'),amounts('sample_period','maximum'),amounts('retention','minimum'),amounts('storage_capacity','maximum')
        if sizes and periods and kept and limits:
            size,sr=max(sizes,key=lambda x:x[0]);period,pr=min(periods,key=lambda x:x[0]);duration,tr=max(kept,key=lambda x:x[0]);limit,lr=min(limits,key=lambda x:x[0])
            if size>0 and period>0 and duration>0 and limit>=0:
                count=duration//period;required=count*size
                if required>limit:
                    issues.append({'id':'storage-'+sr['id'],'kind':'conflict','title':'日志保存目标超过容量上界',
                      'description':f'同一记录链路与工况，至少 floor({duration}/{period})={count} 条记录，每条至少{size} B，原始数据下界{required} B，超过容量上限{limit} B；未计文件头、索引和其他开销，也未假定首样时刻。请调整容量、记录周期、大小或保存期限。',
                      'requirement_ids':list(dict.fromkeys(r['requirement_id'] for r in (sr,pr,tr,lr)))})
    return issues


def field_rows(doc):
    resolutions = {r['field_id']: r for r in doc.get('field_resolutions',[])}
    return [{**field, **resolutions.get(fid,{}), 'state':resolutions.get(fid,{}).get('state','tbd')} for fid,field in FIELDS.items()]


def mark_recheck(row,reason):
    if row.get('source_quote'):
        row['recheck_evidence']={k:row.get(k,'') for k in ('state','source_message_id','source_quote','conclusion')}
    row.update(state='tbd',conclusion=reason,source_message_id='',source_quote='')


def source_message_ids(doc):
    ids={r.get('source_message_id') for key in ('requirements','field_resolutions','modules','device_references','device_constraints') for r in doc.get(key,[])}
    ids.update(r.get('recheck_evidence',{}).get('source_message_id') for r in doc.get('field_resolutions',[]))
    ids.update(r.get('mention_source',{}).get('source_message_id') for r in doc.get('device_references',[]))
    return ids-{None,''}


def prompt_document(doc):
    """Losslessly group repeated field decisions for the model, not for storage."""
    snapshot=copy.deepcopy(doc)
    groups={}
    for resolution in snapshot.pop('field_resolutions',[]):
        fid=resolution.pop('field_id')
        key=json.dumps(resolution,ensure_ascii=False,sort_keys=True)
        groups.setdefault(key,{**resolution,'field_ids':[]})['field_ids'].append(fid)
    snapshot['field_resolution_groups']=list(groups.values())
    snapshot['convergence_history']=snapshot.get('convergence_history',[])[-20:]
    return snapshot


def view(doc):
    fields = field_rows(doc)
    scope=decision_scope(doc,fields)
    counts = Counter(f['state'] for f in fields)
    modules = [{**m,'type_name':TYPES.get(m['type_id'],{}).get('name','待核对类别'),
                'category_name':CATEGORIES.get(TYPES.get(m['type_id'],{}).get('category_id'),{}).get('name','待核对类别'),
                'level':TYPES.get(m['type_id'],{}).get('level','parent'),
                'ports':TYPES.get(m['type_id'],{}).get('ports',[])} for m in doc.get('modules',[])]
    active = [m for m in modules if m['state']!='not_applicable']
    mapped = {rid for m in active if m['state'] in CLOSED for rid in m['requirement_ids']}
    module_gaps = [r['id'] for r in doc['requirements'] if r['category']=='functions' and r['priority']=='must'
                   and r['state']!='not_applicable' and r.get('implementation','hardware')=='hardware' and r['id'] not in mapped]
    issues = consistency_issues(doc)
    issues += [{'id':'field-'+f['id'],'kind':'conflict','title':'需要解决：'+f['label'],'description':f.get('conclusion') or '该项存在尚未解决的冲突。','requirement_ids':f.get('requirement_ids',[])} for f in fields if f['state']=='conflict']
    progress = doc.get('convergence_history',[])
    stalled = bool(scope['decision_pending']) and len(progress)>=2 and all(p.get('delta',0)==0 for p in progress[-2:])
    return {'enabled':doc.get('convergence_version')==2,'fields':fields,'fields_closed':sum(counts[s] for s in CLOSED),
            'fields_total':len(fields),'counts':dict(counts),'modules':modules,'module_links':doc.get('module_links',[]),
            'module_gaps':module_gaps,'module_pending':[m['id'] for m in active if m['state'] not in CLOSED],
            'issues':issues,'progress':progress[-20:],'stalled':stalled,
            'handoff':[f for f in fields if f['state']=='design_stage'],'product_review':scope}


def apply(doc, result, original, messages, aliases, semantic_changes):
    if result.analysis_revision < 2 and not doc.get('convergence_version'):
        return
    doc['convergence_version']=2
    users={m['id']:m['content'] for m in messages if m['role']=='user'}
    reqs={r['id']:r for r in doc['requirements']}
    def refs(ids):
        mapped=list(dict.fromkeys(aliases.get(r,r) for r in ids))
        if any(r not in reqs for r in mapped):
            raise ValueError('收敛结果引用了不存在的需求，本轮未保存。')
        return mapped
    def evidence(row):
        quote=row['source_quote'].strip()
        return bool(quote and quote in users.get(row['source_message_id'],''))
    fields={r['field_id']:copy.deepcopy(r) for r in doc.get('field_resolutions',[])}
    rechecks=set(doc.get('recheck_fields',[]))
    for row in fields.values():
        if set(row['requirement_ids']) & semantic_changes or (semantic_changes and row['state']=='confirmed' and not row['requirement_ids']):
            mark_recheck(row,'关联需求已修改，待内部重新核对。')
            rechecks.add(row['field_id'])
    seen=set()
    for update in result.field_resolutions:
        row=update.model_dump();row['requirement_ids']=refs(row['requirement_ids'])
        if len(set(row['field_ids']))!=len(row['field_ids']) or any(fid not in FIELDS or fid in seen for fid in row['field_ids']):
            raise ValueError('字段核对范围不存在或重复，本轮未保存。')
        proven=evidence(row)
        if row['state'] in CLOSED and not proven:
            row.update(state='tbd',conclusion=row['conclusion']+'（尚缺用户原话依据）')
        elif row['state']=='confirmed' and any(reqs[r]['state']!='stated' for r in row['requirement_ids']):
            row.update(state='tbd',conclusion=row['conclusion']+'（关联需求尚未确认）')
        if not proven:row.update(source_quote='',source_message_id='')
        for fid in row.pop('field_ids'):
            actual={**row,'field_id':fid}
            if fid in PRODUCT_CORE and actual['state'] in ('design_stage','not_applicable'):
                actual.update(state='tbd',conclusion='产品用途、核心功能和目标需要在需求阶段明确，不能整体交给设计阶段或标为不适用。')
            if fid in PRODUCT_CORE and actual['state']=='confirmed' and not actual['requirement_ids']:
                actual.update(state='tbd',conclusion=actual['conclusion']+'（产品核心目标需要关联已明确的需求）')
            fields[fid]=actual;seen.add(fid);rechecks.discard(fid)
    modules={m['id']:copy.deepcopy(m) for m in doc.get('modules',[])}
    module_alias={}
    number=max([int(k[4:]) for k in modules if re.fullmatch(r'MOD-\d+',k)]+[doc.get('next_module_number',1)-1])
    module_seen=set()
    for update in result.module_updates:
        row=update.model_dump();mid=row['id']
        if row['type_id'] not in TYPES or mid in module_seen:
            raise ValueError('模块类别不属于规范目录或模块重复，本轮未保存。')
        module_seen.add(mid)
        if mid not in modules:
            if mid and not mid.startswith('new'):
                raise ValueError('模块实例不存在，本轮未保存。')
            number+=1;row['id']=f'MOD-{number:03d}';module_alias[mid]=row['id']
        row['requirement_ids']=refs(row['requirement_ids'])
        if row['state'] in CLOSED and (not evidence(row) or not row['requirement_ids']):row['state']='proposed'
        if not evidence(row):row.update(source_quote='',source_message_id='')
        modules[row['id']]=row
    for mid in result.remove_module_ids:
        if mid not in modules:raise ValueError('待移除模块不存在，本轮未保存。')
        if mid in module_seen:raise ValueError('同一模块不能同时修改和移除，本轮未保存。')
        del modules[mid]
    # Reuse unchanged mappings. A change in purpose or type must be explicit in a module delta.
    for module in modules.values():
        old_refs=module['requirement_ids'];module['requirement_ids']=[rid for rid in old_refs if rid in reqs]
        if set(old_refs)-set(reqs):module['state']='tbd'
    prior_modules={m['id']:m for m in original.get('modules',[])}
    affected_categories=set()
    # Copyediting the explanation does not change the functional architecture.
    # Changed requirements are invalidated separately; here track concrete type,
    # instance count, functional assignment and inclusion/exclusion changes.
    module_content=('type_id','quantity')
    for mid in set(prior_modules)|set(modules):
        before,after=prior_modules.get(mid),modules.get(mid)
        if (before is None or after is None or any(before.get(k)!=after.get(k) for k in module_content)
                or set(before.get('requirement_ids',[]))!=set(after.get('requirement_ids',[]))
                or (before['state']=='not_applicable')!=(after['state']=='not_applicable')):
            affected_categories.update(TYPES[m['type_id']]['category_id'] for m in (before,after) if m)
    prefixes={'1':['V3-5.2.1','V3-3.1.1'],'2':['V3-5.2.2'],'3':['V3-5.2.3'],
              '4':['V3-5.2.4'],'5':['V3-5.2.7'],'6':['V3-5.2.8'],'7':['V3-5.2.3'],
              '8':['V3-5.2.5'],'9':['V3-5.2.4'],'10':['V3-5.2.2','V3-5.2.6']}
    affected_prefixes=['V3-5.1','V3-5.3']+[p for category in affected_categories for p in prefixes[category]] if affected_categories else []
    for fid,row in fields.items():
        if fid not in seen and any(FIELDS[fid]['topic_id'].startswith(prefix) for prefix in affected_prefixes):
            mark_recheck(row,'模块方案已修改，关联实现边界待重新核对。')
            rechecks.add(fid)
    links={l['id']:l for l in doc.get('module_links',[]) if l['source'] in modules and l['target'] in modules}
    for link in result.module_links:
        row=link.model_dump()
        row['source']=module_alias.get(row['source'],row['source']);row['target']=module_alias.get(row['target'],row['target'])
        if row['source'] not in modules or row['target'] not in modules or row['source']==row['target']:
            raise ValueError('模块关系引用了不存在或相同的端点，本轮未保存。')
        for side in ('source','target'):
            ports={p['role'] for p in TYPES[modules[row[side]]['type_id']].get('ports',[])}
            if row[side+'_port'] and row[side+'_port'] not in ports:
                raise ValueError('模块关系端口不属于该规范类型的抽象角色，请保留待设计。')
        links[row['id']]=row
    constraints={r['id']:r for r in doc.get('numeric_constraints',[]) if r['requirement_id'] not in semantic_changes and r['requirement_id'] in reqs}
    for update in result.numeric_constraints:
        row=update.model_dump();row['requirement_id']=refs([row['requirement_id']])[0]
        if not evidence(row) or reqs[row['requirement_id']]['state']!='stated':continue
        # Source numbers must occur in the quote (optionally expressed in the canonical unit).
        numbers=[float(x) for x in re.findall(r'(?<![\d.])-?\d+(?:\.\d+)?',row['source_quote'])]
        bounds=[row[k] for k in ('minimum','maximum') if row[k] is not None]
        if any(not any(math.isclose(abs(v),abs(n),rel_tol=1e-9,abs_tol=1e-12) for n in numbers) for v in bounds):continue
        constraints[row['id']]=row
    doc.update(field_resolutions=list(fields.values()),recheck_fields=sorted(rechecks),modules=list(modules.values()),
               module_links=list(links.values()),numeric_constraints=list(constraints.values()),next_module_number=number+1)
    # Prevent a renamed question from re-asking a field that has already been resolved.
    questions=[];keys=set()
    decision_ids={f['id'] for f in decision_scope(doc,field_rows(doc))['decision_fields']}
    for q in doc['questions']:
        ids=q.get('field_ids',[])
        if q['id'].startswith('gap-V3-'):
            ids=[fid for fid,f in FIELDS.items() if f['topic_id']==q['id'][4:] and fid in decision_ids]
            if not ids:
                doc.setdefault('question_history',[]).append({**q,'resolved_by':'internal-design-checklist'})
                continue
            q['field_ids']=ids
        if any(fid not in FIELDS for fid in ids):raise ValueError('问题引用了未知的规范字段。')
        # A broad confirmed field does not answer a newly named product decision.
        # Modern keyed questions stay pending until explicitly resolved by a user
        # answer. Keep the old pruning rule only for unkeyed legacy checklist items.
        if ids and not q.get('decision_key') and all(fields.get(fid,{}).get('state') in CLOSED for fid in ids):
            doc.setdefault('question_history',[]).append({**q,'resolved_by':'field-evidence'})
            continue
        key=q.get('decision_key') or '|'.join(sorted(ids)) or q['id']
        if key in keys:continue
        keys.add(key);questions.append(q)
    doc['questions']=sorted(questions,key=lambda q:(q.get('priority',2),q.get('status')=='deferred'))
    doc['recheck_topics']=sorted({FIELDS[f]['topic_id'] for f in rechecks})
    before=sum(r.get('state') in CLOSED for r in original.get('field_resolutions',[]))
    after=sum(r.get('state') in CLOSED for r in fields.values())
    history=copy.deepcopy(original.get('convergence_history',[]))
    history.append({'version':doc['version']+1,'closed':after,'delta':after-before,
                    'changed_requirements':len(semantic_changes),'module_count':len(modules),
                    'open_questions':len(questions)})
    doc['convergence_history']=history[-100:]
    return module_alias


def invalidate(doc, requirement_ids):
    changed=set(requirement_ids)
    rechecks=set(doc.get('recheck_fields',[]))
    for row in doc.get('field_resolutions',[]):
        if changed & set(row['requirement_ids']) or (changed and row['state']=='confirmed' and not row['requirement_ids']):
            mark_recheck(row,'关联需求已修改，待内部重新核对。');rechecks.add(row['field_id'])
    doc['recheck_fields']=sorted(rechecks)
    doc['numeric_constraints']=[r for r in doc.get('numeric_constraints',[]) if r['requirement_id'] not in changed]
    doc['recheck_topics']=sorted(set(doc.get('recheck_topics',[]))|{FIELDS[f]['topic_id'] for f in rechecks})
