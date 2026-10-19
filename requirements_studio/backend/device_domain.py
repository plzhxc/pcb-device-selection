"""User device intent, functional selection needs and versioned dependencies."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from functools import lru_cache
from typing import Literal,Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .module_catalog import TYPES, CATALOG

INTENTS = {'must': '必须使用', 'prefer': '优先考虑', 'reference': '仅作参考', 'undecided': '意图待明确'}
METRICS = ('input_voltage', 'output_voltage', 'output_current', 'operating_temperature',
           'switching_frequency', 'rf_frequency', 'clock_frequency', 'resolution', 'interface', 'feature', 'price')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class DeviceReferenceUpdate(Strict):
    id: str = Field(max_length=64)
    mention: str = Field(min_length=1, max_length=160)
    intent: Literal['must', 'prefer', 'reference', 'undecided']
    identity_scope: Literal['exact_part', 'family', 'assembly', 'unresolved']
    purpose: str = Field(max_length=1000)
    requirement_ids: list[str] = Field(max_length=80)
    module_ids: list[str] = Field(max_length=80)
    quantity: int | None = Field(default=None, ge=1, le=100000)
    source_message_id: str = Field(max_length=64)
    source_quote: str = Field(min_length=1, max_length=3000)


class DeviceConstraintUpdate(Strict):
    id: str = Field(min_length=1, max_length=64)
    reference_id: str = Field(max_length=64)
    requirement_id: str = Field(max_length=32)
    field_key: Literal['input_voltage', 'output_voltage', 'output_current', 'operating_temperature',
                       'switching_frequency', 'rf_frequency', 'clock_frequency', 'resolution', 'interface', 'feature', 'price']
    kind: Literal['design_requirement', 'user_device_description']
    relation: Literal['covers', 'at_least', 'at_most', 'equals', 'contains']
    minimum: float | None = None
    maximum: float | None = None
    text: str = Field(default='', max_length=500)
    unit: str = Field(default='', max_length=32)
    port: str = Field(default='', max_length=100)
    conditions: str = Field(default='', max_length=500)
    source_message_id: str = Field(max_length=64)
    source_quote: str = Field(min_length=1, max_length=3000)

    @model_validator(mode='after')
    def valid_value(self):
        if self.minimum is None and self.maximum is None and not self.text:
            raise ValueError('器件约束需要数值或文本。')
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('器件约束下界不能大于上界。')
        return self


class KnowledgeRequest(Strict):
    reference_id: str = Field(max_length=64)
    operation: Literal['lookup_identity', 'fetch_fields', 'find_spans']
    topics: list[Annotated[str,Field(min_length=1,max_length=160)]] = Field(default_factory=list, max_length=5)


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def reference_fingerprint(ref: dict) -> str:
    return digest({k: ref.get(k) for k in ('id', 'mention', 'intent', 'identity_scope',
                 'device_key', 'lifecycle', 'requirement_ids', 'module_ids', 'quantity')})


def input_fingerprint(doc: dict) -> str:
    return digest({'requirements': [{k: r.get(k) for k in ('id', 'name', 'value', 'unit', 'state', 'priority')}
                                   for r in doc['requirements']],
                   'modules': [{k: m.get(k) for k in ('id', 'type_id', 'quantity', 'state', 'requirement_ids')}
                               for m in doc.get('modules', [])],
                   'references': [reference_fingerprint(r) for r in doc.get('device_references', [])],
                   'constraints': doc.get('device_constraints', [])})


def _bounded_delegation_targets(original, quote, source_text):
    """Resolve a finite 'these two models' answer within its saved question.

    This permits an intent change, never a new identity or new associations.
    Ambiguous groups stay unresolved instead of applying a project-wide change.
    """
    count_pattern = r'这([2-9二两三四五六七八九])(?:个|种|款|颗)?(?:型号|器件|芯片|候选)'

    def count(text):
        found = re.findall(count_pattern, text)
        if len(found) != 1 or not re.search(r'(?:暂|先)?不(?:再)?(?:锁定|指定|限定|确定)', text):
            return None
        if re.search(r'没说|并非|不是|不要|不得|不许|必须|仍需|仍要', text):
            return None
        return int(found[0]) if found[0].isdigit() else {'二': 2, '两': 2, '三': 3, '四': 4,
                                                       '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}[found[0]]

    if count(quote.split('」：', 1)[-1]) is None:
        return set()
    questions = [q for q in original.get('questions', []) if q.get('status', 'pending') == 'pending']
    groups = []
    for question in questions:
        marker = '关于「' + question['question'] + '」：'
        if marker in source_text:
            segment = source_text.split(marker, 1)[1].split('\n关于「', 1)[0]
            if not (segment in quote or quote in marker + segment):
                continue
        elif '关于「' not in source_text and len(questions) == 1:
            segment = source_text
        else:
            continue
        size = count(segment)
        if size is None:
            continue
        requirements = set(question.get('requirement_ids', []))
        targets = {r['id'] for r in original.get('device_references', [])
                   if r.get('lifecycle', 'active') == 'active'
                   and requirements.intersection(r.get('requirement_ids', []))}
        if len(targets) == size:
            groups.append(targets)
    return groups[0] if len(groups) == 1 else set()


def _module_plan_adoption(text):
    """Recognize our existing button command, not arbitrary design delegation."""
    text = text.strip()
    return (text.startswith('确认本版功能模块方案：\n')
            and '\n我采用上述功能类别、用途和数量。' in text
            and text.endswith('这不是电路已验证。'))


def apply_updates(doc, result, original, messages, req_aliases, module_aliases):
    updates = getattr(result, 'device_reference_updates', [])
    removals = getattr(result, 'remove_device_reference_ids', [])
    constraints = getattr(result, 'device_constraints', [])
    if not updates and not removals and not constraints and not doc.get('device_references'):
        return
    doc['domain_schema_version'] = 1
    user_messages = [m for m in messages if m['role'] == 'user']
    users = {m['id']: m['content'] for m in user_messages}
    positions = {m['id']: i for i, m in enumerate(user_messages)}
    adopting_modules = bool(user_messages and _module_plan_adoption(user_messages[-1]['content']))
    if adopting_modules and removals:
        raise ValueError('模块方案采纳不撤销既有器件引用；撤销器件需要独立的用户决定。')
    reqs = {r['id'] for r in doc['requirements'] if r['state'] != 'not_applicable'}
    modules = {m['id'] for m in doc.get('modules', []) if m['state'] != 'not_applicable'}
    refs = {r['id']: copy.deepcopy(r) for r in doc.get('device_references', [])}
    aliases = {}
    number = max([int(x[4:]) for x in refs if re.fullmatch(r'REF-\d+', x)] +
                 [doc.get('next_reference_number', 1) - 1])
    prepared = []
    unsupported_parts = []
    for item in updates:
        row = item.model_dump()
        key = row['id']
        quote = row['source_quote'].strip()
        if quote not in users.get(row['source_message_id'], ''):
            raise ValueError('器件意图必须有真实用户原话，不能来自数据库或AI建议。')
        prior_part = refs.get(key) or next(
            (r for r in refs.values() if r.get('lifecycle') == 'active'
             and r['mention'].casefold() == row['mention'].casefold()), None)
        module_source = _module_plan_adoption(users.get(row['source_message_id'], ''))
        if prior_part:
            changed_part = (prior_part['mention'].casefold() != row['mention'].casefold()
                            or any(prior_part.get(field) != row.get(field)
                                   for field in ('intent', 'identity_scope', 'quantity')))
            prior_source = prior_part.get('source_message_id')
            independent_decision = (not module_source and prior_source in positions
                                    and positions.get(row['source_message_id'], -1) > positions[prior_source])
            if changed_part and (module_source or adopting_modules and not independent_decision):
                unsupported_parts.append(prior_part['mention'] + '（intent=' + prior_part['intent'] +
                                         '，identity_scope=' + prior_part['identity_scope'] +
                                         '，quantity=' + str(prior_part.get('quantity')) + '）')
            if (key in refs and not changed_part and not module_source and row['mention'].casefold() not in quote.casefold()
                    and not re.search(r'不(?:再)?(?:锁定|指定|限定|确定)|取消|撤销|仅作参考|只作参考|改为参考|必须|强制|优先(?:采用|考虑)|可(?:以)?(?:更换|替换|换)',quote)):
                # A power/usage decision can update the functional association
                # of an already sourced part without re-authoring its identity.
                # Preserve both provenances; this path cannot alter the part,
                # its adoption intent, identity scope, or physical quantity.
                row['association_source']={'source_message_id':row['source_message_id'],'source_quote':quote}
                row.update(mention=prior_part['mention'],source_message_id=prior_part['source_message_id'],source_quote=prior_part['source_quote'])
            if module_source:
                # Functional associations may change; a button adopting circuits
                # does not replace the original evidence for a part constraint.
                row.update(mention=prior_part['mention'],
                           source_message_id=prior_part['source_message_id'],
                           source_quote=prior_part['source_quote'])
        prepared.append(row)
    if unsupported_parts:
        # Report the whole invalid group so the existing single repair attempt
        # can preserve every part, rather than discovering them one at a time.
        raise ValueError('模块方案采纳仅确认功能类别、用途和独立电路数量，不能改变既定器件的采用意图、'
                         '身份或器件数量。请保留原约束：' + '；'.join(dict.fromkeys(unsupported_parts)) +
                         '。另有独立的用户决定时，应引用该决定。')
    seen = set()
    for row in prepared:
        key = row['id']
        if key in seen:
            raise ValueError('器件引用编号重复。')
        seen.add(key)
        quote = row['source_quote'].strip()
        if row['mention'].casefold() not in quote.casefold():
            # Later intent changes may name the saved reference rather than repeating a suffix.
            prior = refs.get(key)
            collective=bool(prior and prior['mention'].casefold()==row['mention'].casefold() and
                            re.search(r'(?:全部|所有|各|这些|上述|原文).{0,8}(?:器件|芯片|型号|方案)',quote) and
                            re.search(r'参考|优先|必须|指定|可换|替换|reference|prefer|must',quote,re.I))
            bounded = bool(prior and prior['mention'].casefold() == row['mention'].casefold()
                           and row['intent'] == 'undecided'
                           and key in _bounded_delegation_targets(original, quote, users.get(row['source_message_id'], '')))
            retained = bool(prior and prior['mention'].casefold() == row['mention'].casefold()
                            and prior['intent'] == row['intent']
                            and prior['source_message_id'] == row['source_message_id']
                            and prior['source_quote'] == quote)
            if prior and row.get('association_source'):
                # The identity-preserving path above already retained its
                # original source; keep the newly justified associations too.
                pass
            elif collective or bounded or retained:
                # A collective intent change cannot silently change part identity,
                # known physical quantity or functional associations.
                row={**prior,'intent':row['intent'],'source_message_id':row['source_message_id'],'source_quote':quote,
                     'mention_source':prior.get('mention_source') or {'source_message_id':prior['source_message_id'],'source_quote':prior['source_quote']}}
            elif not prior or (key not in quote and prior['mention'].casefold() not in quote.casefold()):
                raise ValueError('器件型号必须逐字来自所引原话；修改意图请引用已有器件。')
        rid = list(dict.fromkeys(req_aliases.get(r, r) for r in row['requirement_ids']))
        mids = list(dict.fromkeys(module_aliases.get(m, m) for m in row['module_ids']))
        if set(rid) - reqs or set(mids) - modules:
            raise ValueError('器件引用关联了不存在或已取消的需求/模块。')
        if key not in refs:
            if key and not key.startswith('new'):
                raise ValueError('器件引用编号不存在。')
            # Same original part in the same project is one reference with many associations.
            existing = next((r for r in refs.values() if r.get('lifecycle') == 'active' and
                             r['mention'].casefold() == row['mention'].casefold()), None)
            if existing:
                aliases[key] = existing['id']; row['id'] = existing['id']
                rid = list(dict.fromkeys(existing['requirement_ids'] + rid))
                mids = list(dict.fromkeys(existing['module_ids'] + mids))
            else:
                number += 1; row['id'] = f'REF-{number:03d}'; aliases[key] = row['id']
        prior = refs.get(row['id'], {})
        if prior.get('mention_source') and prior.get('mention')==row['mention']:
            row.setdefault('mention_source',prior['mention_source'])
        if prior.get('reference_quantity_source') and prior.get('quantity')==row.get('quantity'):
            row['reference_quantity_source']=copy.deepcopy(prior['reference_quantity_source'])
        if prior.get('reference_supplier_part') and prior.get('mention')==row['mention']:
            row['reference_supplier_part']=prior['reference_supplier_part']
        row.update(requirement_ids=rid, module_ids=mids, lifecycle='active',
                   device_key=prior.get('device_key'), identity_decision=prior.get('identity_decision'))
        if prior and prior.get('mention') != row['mention']:
            row.update(device_key=None, identity_decision=None)
        refs[row['id']] = row
    for key in removals:
        if key in seen or key not in refs:
            raise ValueError('待撤销器件引用不存在或与本轮更新冲突。')
        refs[key]['lifecycle'] = 'removed'
    for row in refs.values():
        had_links = bool(row['requirement_ids'] or row['module_ids'])
        row['requirement_ids'] = [r for r in row['requirement_ids'] if r in reqs]
        row['module_ids'] = [m for m in row['module_ids'] if m in modules]
        if had_links and not row['requirement_ids'] and not row['module_ids']:
            row['lifecycle'] = 'removed'
    known = {c['id']: copy.deepcopy(c) for c in doc.get('device_constraints', [])}
    for item in constraints:
        row = item.model_dump()
        row['reference_id'] = aliases.get(row['reference_id'], row['reference_id'])
        row['requirement_id'] = req_aliases.get(row['requirement_id'], row['requirement_id'])
        if row['reference_id'] not in refs or row['requirement_id'] not in reqs:
            missing=[]
            if row['reference_id'] not in refs:missing.append('reference_id='+row['reference_id'])
            if row['requirement_id'] not in reqs:missing.append('requirement_id='+row['requirement_id'])
            raise ValueError('器件约束关联不存在：'+', '.join(missing)+'。请使用当前文档或本轮已定义的真实引用/需求ID，不得猜测ID。')
        if row['source_quote'].strip() not in users.get(row['source_message_id'], ''):
            raise ValueError('器件约束必须引用真实用户原话。')
        from .engineering_checks import quoted_quantity
        if any(value is not None and not quoted_quantity(value,row['unit'],row['source_quote'])
               for value in (row['minimum'],row['maximum'])):
            raise ValueError('器件约束的数值和单位未在所引原话中找到；推导值不能冒充用户硬要求。')
        known[row['id']] = row
    doc['device_references'] = list(refs.values())
    doc['next_reference_number'] = number + 1
    doc['device_constraints'] = [c for c in known.values() if c['requirement_id'] in reqs
                                 and refs.get(c['reference_id'], {}).get('lifecycle') == 'active']
    reconcile_selection_items(doc)
    impacts = []
    old = {r['id']: r for r in original.get('device_references', [])}
    for r in doc['device_references']:
        if r['id'] in old and reference_fingerprint(r) != reference_fingerprint(old[r['id']]):
            impacts.append({'reference_id': r['id'], 'reason': '器件意图、身份或关联改变，相关核对重新计算。'})
    changed_reqs = set(doc.get('changed_ids', []))
    for r in doc['device_references']:
        if changed_reqs.intersection(r['requirement_ids']):
            impacts.append({'reference_id': r['id'], 'reason': '关联需求本轮更新，按当前约束复核。'})
    doc['domain_impacts'] = impacts


def reconcile_selection_items(doc):
    """Each functional circuit is a selection need, not an assumed one-chip BOM."""
    prior = {i['module_id']: i for i in doc.get('selection_items', [])}
    refs = [r for r in doc.get('device_references', []) if r.get('lifecycle') == 'active']
    items = []
    for module in doc.get('modules', []):
        if module['state'] == 'not_applicable':
            continue
        old = prior.get(module['id'], {})
        kind = TYPES.get(module['type_id'], {})
        associated = [r['id'] for r in refs if module['id'] in r['module_ids']]
        items.append({'id': old.get('id', 'SEL-' + module['id'].removeprefix('MOD-')),
                      'module_id': module['id'], 'name': module['name'], 'type_id': module['type_id'],
                      'type_name': kind.get('name', ''), 'purpose': module['purpose'],
                      'boundary': module.get('boundary', ''), 'requirement_ids': module['requirement_ids'],
                      'circuit_quantity': module['quantity'], 'component_quantity': None,
                      'quantity_state': 'design_stage', 'reference_ids': associated,
                      'object_kind': 'circuit_need', 'catalog_mapping': module_mapping(module['type_id'])})
    doc['selection_items'] = items


@lru_cache(maxsize=1)
def _mapping_data():
    from pathlib import Path
    path = Path(__file__).parent / 'resources/module_device_map.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'mappings':{}}


def module_mapping(type_id):
    return _mapping_data()['mappings'].get(type_id, {'status':'deferred','leaf_codes':[],'reason':'类别映射待建立'})


def context_assertions(assertions, limit=16):
    """Requirement analysis needs capabilities, not hundred-pin JSON arrays."""
    excluded={'pin_functions','pin_definitions','pin_mapping','datasheet','domain_code','family_code','leaf_code','taxonomy_path','part_number','package'}
    selected=[]
    for raw in assertions:
        if raw.get('is_missing') or raw.get('field_key') in excluded:continue
        row={k:copy.deepcopy(raw.get(k)) for k in ('field_key','label','raw_value','normalized','validation_state','rating_kind',
             'conditions','issue','source_disagreement','supporting_evidence_ids','supporting_quote','is_missing','source_evidence_status','locator')}
        value=str(row.get('raw_value') or '')
        if len(value)>1000:row.update(raw_value=value[:1000],context_truncated=True)
        selected.append(row)
    return selected[:limit]


def device_context(doc, domain):
    references=[]
    for raw in domain.get('references',[]):
        row={k:raw.get(k) for k in ('id','mention','intent','identity_scope','purpose','requirement_ids','module_ids','quantity',
                                  'device_key','resolution_status','identity','evidence_ids','lookup_message','lookup_status','evidence_freshness','document_status')}
        row['candidates']=[{k:c.get(k) for k in ('device_key','model','manufacturer','package','leaf_name')}
                           for c in raw.get('candidates',[])[:5]]
        references.append(row)
    evidence=[]
    # Retain evidence fairly across devices instead of dropping the early parts
    # of a long input when only the final 30 snapshots fit the old projection.
    by_ref={}
    for item in domain.get('evidence',[]):by_ref.setdefault(item.get('reference_id',item.get('device_key','')),[]).append(item)
    selected=[e for items in by_ref.values() for e in items[:4]]
    for raw in selected[:240]:
        row={k:raw.get(k) for k in ('id','device_key','kind','title','locator','version_context','availability','checked_at','excerpt')}
        row['assertions']=context_assertions(raw.get('assertions',[]))
        evidence.append(row)
    return {'references':references, 'checks': domain.get('checks', []),
            'evidence':evidence,
            'source_status': domain.get('source_status', {}),
            'policy': '目录事实不是用户需求；仅参考不强制选型；缺资料由内部补证，已证实硬冲突才请求取舍。'}
