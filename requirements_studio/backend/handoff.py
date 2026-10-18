"""Versioned requirements contract, reproducible archive and explicit downstream adapter."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import uuid
import zipfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .device_domain import reconcile_selection_items
from .module_catalog import CATALOG, TYPES


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Requirement(Contract):
    id: str
    category: str
    name: str
    value: str
    unit: str
    priority: Literal['must', 'should', 'could']
    state: Literal['stated', 'suggested', 'tbd', 'conflict', 'not_applicable']
    implementation: str
    source_message_id: str
    source_quote: str


class SelectionItem(Contract):
    id: str
    module_id: str
    name: str
    type_id: str
    type_name: str
    purpose: str
    boundary: str
    requirement_ids: list[str]
    circuit_quantity: int = Field(ge=1)
    component_quantity: int | None
    quantity_state: Literal['known', 'design_stage']
    reference_ids: list[str]
    object_kind: Literal['circuit_need']
    catalog_mapping: dict
    constraints: list[dict]
    specified_references: list[str]
    preferred_references: list[str]
    reference_only: list[str]


class RequirementSpec(Contract):
    schema_version: Literal['pcbagent.requirement-spec/1.0']
    spec_id: str
    project_id: str
    requirement_version: int = Field(ge=0)
    domain_revision: int = Field(ge=0)
    review_status: Literal['draft', 'ready', 'approved']
    title: str
    summary: str
    template: dict
    requirements: list[Requirement]
    constraints: list[dict]
    modules: list[dict]
    module_links: list[dict]
    workflow: list[dict]
    selection_items: list[SelectionItem]
    device_references: list[dict]
    evidence_refs: list[dict]
    source_messages: list[dict]
    acceptance_criteria: list[dict]
    open_items: list[dict]
    handoff_status: Literal['ready', 'ready_with_open_items', 'blocked']

    @model_validator(mode='after')
    def references_valid(self):
        try:json.dumps(self.model_dump(),allow_nan=False)
        except ValueError as exc:raise ValueError('交接数据包含非有限数值。') from exc
        reqs = {r.id for r in self.requirements}
        modules = {m['id'] for m in self.modules}
        refs = {r['id'] for r in self.device_references}
        sources = {s['id']: s['content'] for s in self.source_messages}
        if len(reqs) != len(self.requirements) or len(modules) != len(self.modules) or len(refs) != len(self.device_references):
            raise ValueError('交接包有重复对象编号。')
        if len({s.id for s in self.selection_items}) != len(self.selection_items):
            raise ValueError('交接包选型项重复。')
        for r in self.requirements:
            if r.state == 'stated' and (not r.source_quote or r.source_quote.strip() not in sources.get(r.source_message_id, '')):
                raise ValueError('交接需求缺少可核查的用户来源。')
        for item in self.selection_items:
            if item.module_id not in modules or set(item.requirement_ids) - reqs or set(item.reference_ids) - refs:
                raise ValueError('选型项包含断开的对象引用。')
            if item.component_quantity is not None and (type(item.component_quantity) is not int or item.component_quantity<1):
                raise ValueError('物理用量未知应为null，已知用量必须为正整数。')
        for module in self.modules:
            if module.get('type_id') not in TYPES or set(module.get('requirement_ids',[]))-reqs:
                raise ValueError('模块类型或需求关联不属于当前有效对象。')
            if type(module.get('quantity')) is not int or module['quantity']<1:
                raise ValueError('模块独立电路数量必须为正整数。')
            if module.get('state') in ('confirmed','design_stage') and (not module.get('source_quote') or
                    module['source_quote'].strip() not in sources.get(module.get('source_message_id'),'')):
                raise ValueError('模块决定缺少真实用户来源。')
        for r in self.device_references:
            if set(r['requirement_ids']) - reqs or set(r['module_ids']) - modules:
                raise ValueError('器件引用包含无效需求或模块。')
            if not r.get('source_quote') or r['source_quote'].strip() not in sources.get(r['source_message_id'], ''):
                raise ValueError('器件意图缺少用户来源。')
        for link in self.module_links:
            if link['source'] not in modules or link['target'] not in modules:
                raise ValueError('模块关系有断开的端点。')
        for constraint in self.constraints:
            if constraint.get('requirement_id') not in reqs or (constraint.get('reference_id') and constraint['reference_id'] not in refs):
                raise ValueError('约束有断开的作用对象。')
        for step in self.workflow:
            if set(step.get('requirement_ids',[]))-reqs:raise ValueError('工作流程包含不存在的需求。')
        return self


def readiness(project):
    doc = project['document']; domain = project.get('domain', {})
    blockers = []
    if project['review']['status'] != 'approved':
        blockers.append('当前需求尚未由用户完成整体审核。')
    if project.get('input_queue') or (project.get('latest_run') or {}).get('status') in ('queued', 'running'):
        blockers.append('还有正在处理或排队的需求补充。')
    if project.get('analysis_pending'):
        blockers.append('最新用户补充尚未完成需求整理，不能将上个版本当成该补充的完成结果。')
    if domain.get('active_jobs'):
        blockers.append('本批器件资料仍在核对，结果保存后可重新检查交接。')
    blockers += [c['title'] + '：' + c['description'] for c in domain.get('blockers', [])]
    opened = []
    if project.get('analysis_pending'):
        mid=project['analysis_pending']['message_id']
        opened.append({'id':'OPEN-INPUT-'+mid,'scope':mid,'kind':'pending_input',
                       'description':'这条最新用户补充尚未完成需求整理，当前说明书仍为先前已保存版本。',
                       'owner':'需求分析','completion_condition':'完成该输入的分析，再由用户整体审核更新后的需求。'})
    for m in doc.get('modules', []):
        if m['state'] != 'not_applicable':
            opened.append({'id': 'OPEN-' + m['id'], 'scope': m['id'], 'kind': 'implementation',
                           'description': m.get('boundary') or '具体器件、物理用量与实现条件待设计。',
                           'owner': '后续设计与选型', 'completion_condition': '完成具体器件、用量和与需求对应的工程核对。'})
    for r in domain.get('references', []):
        if r.get('resolution_status') != 'matched' or r.get('document_status') != 'available' or r.get('evidence_freshness')=='retained_after_lookup_failure':
            opened.append({'id': 'OPEN-' + r['id'], 'scope': r['id'], 'kind': 'evidence',
                           'description': r['mention'] + ' 的身份或资料仍待核验；保留 ' + r['intent'] + ' 意图。',
                           'owner': '器件选型', 'completion_condition': '取得对应身份的资料和适用条件；未核验型号不能当已验证器件。'})
    for c in domain.get('checks', []):
        if c['status'] in ('conditional', 'unknown', 'stale'):
            opened.append({'id': 'OPEN-' + c['id'], 'scope': c['reference_id'], 'kind': 'check',
                           'description': c['description'], 'owner': '后续设计与资料核对',
                           'completion_condition': '补充同对象、同工况的证据，重算这项核对。'})
    return {'status': 'blocked' if blockers else 'ready_with_open_items' if opened else 'ready',
            'blockers': list(dict.fromkeys(blockers)), 'open_items': opened,
            'requirement_version': doc['version'], 'domain_revision': project.get('domain_revision', 0),
            'active_knowledge_jobs': domain.get('active_jobs', 0)}


def build_spec(project):
    doc = copy.deepcopy(project['document']); reconcile_selection_items(doc)
    domain = project.get('domain', {}); ready = readiness(project)
    refs = [copy.deepcopy(r) for r in domain.get('references', [])]
    for r in refs:
        r.pop('candidates', None)
        r.pop('attributes', None)
    refmap = {r['id']: r for r in refs}
    items = []
    for raw in doc.get('selection_items', []):
        item = {k: v for k, v in raw.items() if k in SelectionItem.model_fields}
        item['constraints'] = [c for c in doc.get('numeric_constraints', []) if c['requirement_id'] in raw['requirement_ids']]
        item['constraints'] += [c for c in doc.get('device_constraints', []) if c['requirement_id'] in raw['requirement_ids']]
        for intent, field in [('must', 'specified_references'), ('prefer', 'preferred_references'), ('reference', 'reference_only')]:
            item[field] = [rid for rid in raw['reference_ids'] if refmap.get(rid, {}).get('intent') == intent]
        items.append(item)
    requirements = []
    for r in doc['requirements']:
        if r['state'] == 'not_applicable': continue
        row = {k: r.get(k, '') for k in Requirement.model_fields}
        row['implementation'] = r.get('implementation', 'hardware')
        requirements.append(row)
    valid_reqs = {r['id'] for r in requirements}
    modules = [{**m, 'type_name': TYPES[m['type_id']]['name'], 'category_id': TYPES[m['type_id']]['category_id']}
               for m in doc.get('modules', []) if m['state'] != 'not_applicable']
    evidence = []
    seen = set()
    for e in domain.get('evidence', []):
        if e['id'] in seen: continue
        seen.add(e['id']); evidence.append({k: e.get(k) for k in ('id', 'kind', 'device_key', 'locator', 'title', 'primary_key',
                                                               'page_number', 'content_hash', 'version_context', 'availability')})
    data = {'schema_version': 'pcbagent.requirement-spec/1.0',
            'spec_id': f"SPEC-{project['id']}-v{doc['version']}-d{project.get('domain_revision',0)}",
            'project_id': project['id'], 'requirement_version': doc['version'],
            'domain_revision': project.get('domain_revision', 0), 'review_status': project['review']['status'],
            'title': doc['title'], 'summary': doc['summary'],
            'template': {'requirements': '硬件需求说明书 V3.0', 'module_catalog_revision': CATALOG['revision'],
                         'module_catalog_sha256': CATALOG['source_sha256']},
            'requirements': requirements, 'constraints': doc.get('numeric_constraints', []) + doc.get('device_constraints', []),
            'modules': modules, 'module_links': doc.get('module_links', []), 'workflow': doc['flow'],
            'selection_items': items, 'device_references': refs, 'evidence_refs': evidence,
            'source_messages': [{k: m[k] for k in ('id', 'content', 'created_at')} for m in project['messages'] if m['role'] == 'user'],
            'acceptance_criteria': [{'requirement_id': r['id'], 'criterion': r['value'], 'source_message_id': r['source_message_id']}
                                    for r in requirements if r['category'] in ('performance', 'manufacturing')],
            'open_items': ready['open_items'], 'handoff_status': ready['status']}
    return RequirementSpec.model_validate(data).model_dump(mode='json')


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def generate_archive(project, store):
    spec = build_spec(project)
    readiness_result = readiness(project)
    files = {'requirements.md': project['markdown'].encode(), 'requirement-spec.json': encode(spec),
             'evidence-index.json': encode({'schema_version': 'pcbagent.evidence-index/1.0',
                                            'items': [store.evidence(project['id'], e['id']) for e in spec['evidence_refs']]}),
             'open-items.md': ('# 设计与资料待办\n\n' + '\n'.join('- ' + i['description'] + '（' + i['owner'] + '；完成条件：' + i['completion_condition'] + '）'
                                                    for i in readiness_result['open_items']) + '\n').encode(),
             'conversation.json': encode({'messages': project['messages'], 'activity': project.get('run_activity', [])})}
    manifest = {'schema_version': 'pcbagent.handoff-manifest/1.0', 'project_id': project['id'], 'spec_id': spec['spec_id'],
                'requirement_version': spec['requirement_version'], 'domain_revision': spec['domain_revision'],
                'status': spec['handoff_status'], 'files': {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}
    files['manifest.json'] = encode(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items(): archive.writestr(name, content)
    return manifest, output.getvalue()


def verify_archive(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        expected={'requirements.md','requirement-spec.json','evidence-index.json','open-items.md','conversation.json','manifest.json'}
        names=archive.namelist()
        if len(names)!=len(expected) or set(names)!=expected:raise ValueError('交接包文件清单不完整或包含重复文件。')
        if sum(i.file_size for i in archive.infolist())>64*1024*1024:raise ValueError('交接包解压后超出本地校验预算。')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('schema_version')!='pcbagent.handoff-manifest/1.0' or set(manifest.get('files',{}))!=expected-{'manifest.json'}:
            raise ValueError('交接文件校验清单不完整或版本不支持。')
        for name, expected in manifest['files'].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError('交接文件完整性检查失败：' + name)
        spec = RequirementSpec.model_validate_json(archive.read('requirement-spec.json'))
        evidence = json.loads(archive.read('evidence-index.json'))
        available = {e['id'] for e in evidence['items']}
        if {e['id'] for e in spec.evidence_refs} - available:
            raise ValueError('交接证据有缺失引用。')
        if manifest['spec_id'] != spec.spec_id or manifest['requirement_version'] != spec.requirement_version or manifest['domain_revision'] != spec.domain_revision:
            raise ValueError('交接版本不一致。')
        return {'ok': True, 'spec_id': spec.spec_id, 'selection_items': len(spec.selection_items),
                'requirements': len(spec.requirements), 'evidence': len(available), 'status': spec.handoff_status}


def generation_adapter(spec):
    """Project explicit user-specified devices; never choose a fuzzy catalog hit.

    Unselected functional needs remain in the intake/open-items metadata.
    """
    spec = RequirementSpec.model_validate(spec).model_dump()
    refs = {r['id']: r for r in spec['device_references']}
    modulemap = {m['id']: m for m in spec['modules']}
    modules = []; pending = [];owners={};shared=[]
    for item in spec['selection_items']:
        devices = []
        for rid in item['specified_references']:
            r = refs[rid]
            if r['identity_scope'] not in ('exact_part', 'assembly'):
                continue
            if rid in owners:
                shared.append({'reference_id':rid,'functional_module_id':item['module_id'],'owner_module_id':owners[rid],
                               'quantity_scope':'project','quantity':r.get('quantity')})
                continue
            identity = r.get('identity') or {}
            # Only use catalog model if the user selected that identity or an exact lookup resolved it.
            model = identity.get('model') if r.get('device_key') or r.get('resolution_status') == 'matched' else r['mention']
            device = {'name': r['mention'], 'model': model or r['mention'],'requirement_reference_id':rid,'quantity_scope':'project', 'manufacturer': identity.get('manufacturer', ''),
                      'supplier_part': identity.get('supplier_part', ''), 'role': r.get('purpose', ''),
                      'description': '用户指定；资料与工程适用性分别核对。', 'verification_status': 'unverified',
                      'evidence': 'RequirementSpec ' + spec['spec_id'] + ' / ' + rid}
            wrapper = {'device': device}
            if r.get('quantity') is not None: wrapper['quantity'] = r['quantity']
            devices.append(wrapper);owners[rid]=item['module_id']
        if devices:
            m = modulemap[item['module_id']]
            modules.append({'module': {'id': m['id'], 'name': m['name'], 'description': m['purpose'],
                                        'type': item['type_name'], 'requirement_ids': item['requirement_ids']},
                            'selected_devices': devices})
        elif not any(s['functional_module_id']==item['module_id'] for s in shared):
            pending.append({'selection_item_id': item['id'], 'module_id': item['module_id'], 'reason': '尚无用户明确指定的具体器件，需后续选型。'})
    if not modules:
        return {'accepted': True, 'can_start_generation': False, 'state': 'selection_required',
                'selection_needed': pending, 'generation_input': None,
                'message': '需求输入已接收；这些选型项需要先确定具体器件。'}
    import sys
    from pathlib import Path
    root=str(Path(__file__).resolve().parents[2])
    if root not in sys.path:sys.path.insert(0,root)
    from netlist_end_to_end.generation_input import build_generation_input, generation_decomposition
    from netlist_end_to_end.netlist.stage_results import read_inputs
    ids = {m['module']['id'] for m in modules}
    links = [e for e in spec['module_links'] if e['source'] in ids and e['target'] in ids]
    data = build_generation_input(modules, description=spec['summary'],
                                 system={'requirement_spec_id': spec['spec_id'], 'requirements': spec['requirements'],
                                         'open_items': spec['open_items'], 'selection_needed': pending,
                                         'functional_modules':spec['modules'],'functional_module_links':spec['module_links'],
                                         'shared_device_references':shared,
                                         'input_scope': 'explicit_user_specified_devices', 'hardware_validation': 'pending'}, links=links)
    decomposition = generation_decomposition(data)
    intake=read_inputs(data,spec['summary'])
    return {'accepted': True, 'can_start_generation': spec['handoff_status'] != 'blocked',
            'state': 'input_validated' if spec['handoff_status'] != 'blocked' else 'requirements_blocked',
            'generation_input': data, 'selection_needed': pending, 'prepared_modules': len(decomposition['modules']),
            'intake':{'entrypoint':'netlist_end_to_end.netlist.stage_results.read_inputs','accepted_modules':intake['total'],
                      'requirement_spec_id':spec['spec_id'],'scope':'输入适配和真实入口读取'},
            'message': '现有流程的输入入口已读取指定器件与需求；开放选型项和工程待办已随输入保存。'}


def legacy_document(doc):
    result = copy.deepcopy(doc)
    for key in ('domain_schema_version', 'device_references', 'device_constraints', 'selection_items',
                'domain_impacts', 'next_reference_number', 'knowledge_requests'):
        result.pop(key, None)
    return result
