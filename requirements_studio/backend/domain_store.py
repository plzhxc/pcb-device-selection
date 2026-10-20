"""SQLite persistence/projections for domain jobs and immutable evidence."""
from __future__ import annotations

import copy
import json
import uuid

from .device_domain import digest, INTENTS, reconcile_selection_items
from .engineering_checks import checks_for


def timestamp():
    from .domain import now
    return now()


def identity_input(ref):
    values={k: ref.get(k) for k in ('mention', 'identity_scope', 'device_key')}
    if ref.get('reference_supplier_part'):values['reference_supplier_part']=ref['reference_supplier_part']
    return digest(values)


class DomainStore:
    def init_domain(self, db):
        schema='''
            CREATE TABLE IF NOT EXISTS domain_jobs(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                message_id TEXT NOT NULL DEFAULT '', parent_run_id TEXT,
                request_id TEXT NOT NULL, base_version INTEGER NOT NULL,
                status TEXT NOT NULL, request TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, finished_at TEXT,
                UNIQUE(project_id,request_id));
            CREATE TABLE IF NOT EXISTS domain_job_events(
                job_id TEXT NOT NULL REFERENCES domain_jobs(id), seq INTEGER NOT NULL,
                kind TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(job_id,seq));
            CREATE TABLE IF NOT EXISTS evidence_snapshots(
                project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,
                job_id TEXT NOT NULL REFERENCES domain_jobs(id),payload TEXT NOT NULL,
                created_at TEXT NOT NULL,PRIMARY KEY(project_id,id));
            CREATE TABLE IF NOT EXISTS domain_checks(
                project_id TEXT NOT NULL REFERENCES projects(id),id TEXT NOT NULL,
                dependency_hash TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(project_id,id,dependency_hash));
            CREATE TABLE IF NOT EXISTS handoff_artifacts(
                id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),
                request_id TEXT NOT NULL,version INTEGER NOT NULL,domain_revision INTEGER NOT NULL,
                manifest TEXT NOT NULL,content BLOB NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(project_id,request_id));
            CREATE INDEX IF NOT EXISTS domain_jobs_project ON domain_jobs(project_id,created_at);
            CREATE INDEX IF NOT EXISTS evidence_project ON evidence_snapshots(project_id,created_at);
        '''
        # executescript would commit the caller's migration transaction first.
        # This fixed DDL contains no semicolons inside literals; execute each
        # statement in the same transaction as the core tables and ALTERs.
        for statement in schema.split(';'):
            if statement.strip():db.execute(statement)

    def raw_document(self, project_id):
        with self.connect() as db:
            return self._document(db, project_id)[1]

    def recover_domain(self, db):
        for row in db.execute("SELECT id FROM domain_jobs WHERE status IN ('queued','running')").fetchall():
            db.execute("UPDATE domain_jobs SET status='interrupted',error=?,finished_at=? WHERE id=?",
                       ('资料核对因服务重启中断，可重新检索；已有证据保留。', timestamp(), row['id']))
            self._domain_event(db, row['id'], 'interrupted', {'message': '资料核对已中断，可重新检索。'})

    @staticmethod
    def _domain_event(db, job_id, kind, payload):
        seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM domain_job_events WHERE job_id=?', (job_id,)).fetchone()[0]
        db.execute('INSERT INTO domain_job_events VALUES(?,?,?,?)',
                   (job_id, seq, kind, json.dumps({'at': timestamp(), **payload}, ensure_ascii=False)))

    def domain_event(self, job_id, message, phase='catalog'):
        with self.connect() as db:
            self._domain_event(db, job_id, 'progress', {'message': message, 'phase': phase})

    def create_domain_job(self, project_id, references, request_id, message_id='', parent_run_id=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            _, doc = self._document(db, project_id)
            old = db.execute('SELECT * FROM domain_jobs WHERE project_id=? AND request_id=?', (project_id, request_id)).fetchone()
            if old: return dict(old), False
            count = db.execute("SELECT COUNT(*) FROM domain_jobs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone()[0]
            if count >= 8:
                raise ValueError('资料任务已排队，请等待当前批次完成；器件引用已保留。')
            key = uuid.uuid4().hex
            payload = json.dumps({'references': references}, ensure_ascii=False)
            db.execute("INSERT INTO domain_jobs(id,project_id,message_id,parent_run_id,request_id,base_version,status,request,created_at) VALUES(?,?,?,?,?,?,'queued',?,?)",
                       (key, project_id, message_id, parent_run_id, request_id, doc['version'], payload, timestamp()))
            self._domain_event(db, key, 'stage', {'message': f'已排队核对 {len(references)} 条器件引用。'})
            return dict(db.execute('SELECT * FROM domain_jobs WHERE id=?', (key,)).fetchone()), True

    def domain_job(self, project_id, job_id):
        from .store import Missing
        with self.connect() as db:
            row = db.execute('SELECT * FROM domain_jobs WHERE project_id=? AND id=?', (project_id, job_id)).fetchone()
            if not row: raise Missing('资料任务不存在。')
            return dict(row)

    def domain_events(self, project_id, job_id, after=0):
        self.domain_job(project_id, job_id)
        with self.connect() as db:
            return [{'seq': r['seq'], 'kind': r['kind'], **json.loads(r['payload'])}
                    for r in db.execute('SELECT * FROM domain_job_events WHERE job_id=? AND seq>? ORDER BY seq', (job_id, after))]

    def start_domain_job(self, project_id, job_id):
        with self.connect() as db:
            return bool(db.execute("UPDATE domain_jobs SET status='running' WHERE project_id=? AND id=? AND status='queued'", (project_id, job_id)).rowcount)

    def finish_domain_job(self, project_id, job_id, result, evidence, status='completed', error=''):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM domain_jobs WHERE project_id=? AND id=?', (project_id, job_id)).fetchone()
            if not row or row['status'] not in ('queued', 'running'): return False
            for item in evidence:
                payload = json.dumps(item, ensure_ascii=False, allow_nan=False)
                db.execute('INSERT OR IGNORE INTO evidence_snapshots VALUES(?,?,?,?,?)',
                           (project_id, item['id'], job_id, payload, timestamp()))
            db.execute('UPDATE domain_jobs SET status=?,result=?,error=?,finished_at=? WHERE id=?',
                       (status, json.dumps(result, ensure_ascii=False), error, timestamp(), job_id))
            self._domain_event(db, job_id, status, {'phase': 'evidence', 'message': error or
                               f'资料核对已保存：{len(result.get("references", []))} 条引用、{len(evidence)} 份证据。'})
        return True

    def record_domain_reference(self, project_id, job_id, result, evidence):
        """Publish an obtained identity before other devices/manual reads finish."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT status,result FROM domain_jobs WHERE project_id=? AND id=?',(project_id,job_id)).fetchone()
            if not row or row['status']!='running':return False
            saved=json.loads(row['result'])
            refs=[r for r in saved.get('references',[]) if r.get('identity_input_hash')!=result['identity_input_hash']]
            refs.append(copy.deepcopy(result));saved['references']=refs
            for item in evidence:
                db.execute('INSERT OR IGNORE INTO evidence_snapshots VALUES(?,?,?,?,?)',
                           (project_id,item['id'],job_id,json.dumps(item,ensure_ascii=False,allow_nan=False),timestamp()))
            db.execute('UPDATE domain_jobs SET result=? WHERE id=?',(json.dumps(saved,ensure_ascii=False),job_id))
        return True

    def saved_resolution(self, project_id, fingerprint, matched=False):
        # The visible job timeline is bounded. Facts remain available after a
        # long session, even when their original job is outside that window.
        with self.connect() as db:
            row=db.execute("SELECT e.value AS resolution FROM domain_jobs j,json_each(j.result,'$.references') e WHERE j.project_id=? AND j.status='completed' AND json_extract(e.value,'$.identity_input_hash')=? "+
                           ("AND json_extract(e.value,'$.status')='matched' " if matched else '')+
                           'ORDER BY j.rowid DESC LIMIT 1',(project_id,fingerprint)).fetchone()
        return json.loads(row['resolution']) if row else None

    def domain_view(self, project_id, doc=None, include_content=False):
        with self.connect() as db:
            if doc is None: _, doc = self._document(db, project_id)
            jobs = [dict(r) for r in db.execute('SELECT * FROM domain_jobs WHERE project_id=? ORDER BY rowid DESC LIMIT 120', (project_id,))]
            evidence = [json.loads(r['payload']) for r in db.execute('SELECT payload FROM evidence_snapshots WHERE project_id=? ORDER BY rowid', (project_id,))]
            revision = db.execute('SELECT COUNT(*) FROM evidence_snapshots WHERE project_id=?', (project_id,)).fetchone()[0]
            revision += db.execute("SELECT COUNT(*) FROM domain_jobs WHERE project_id=? AND status NOT IN ('queued','running')", (project_id,)).fetchone()[0]
            activity = []
            for job in reversed(jobs[:30]):
                events = [{'seq': e['seq'], 'kind': e['kind'], **json.loads(e['payload'])}
                          for e in db.execute('SELECT * FROM domain_job_events WHERE job_id=? ORDER BY seq', (job['id'],))]
                activity.append({'id': job['id'], 'message_id': job['message_id'], 'kind': 'knowledge',
                                 'status': job['status'], 'created_at': job['created_at'], 'events': events[-20:]})
        refs = []
        resolutions = []
        for job in jobs:
            if job['status'] in ('completed','running'): resolutions += json.loads(job['result']).get('references', [])
        linked_evidence = []
        for ref in doc.get('device_references', []):
            if ref.get('lifecycle') != 'active': continue
            current = identity_input(ref)
            matching=[r for r in resolutions if r.get('identity_input_hash')==current]
            found=matching[0] if matching else self.saved_resolution(project_id,current) if len(jobs)>=120 else None
            lookup_status=found['status'] if found else 'unresolved';retained=False
            if found and lookup_status in ('unavailable','not_found') and not found.get('evidence_ids'):
                prior=next((r for r in matching if r.get('status')=='matched' and r.get('evidence_ids')),None)
                if prior is None and len(jobs)>=120:prior=self.saved_resolution(project_id,current,matched=True)
                if prior and prior.get('evidence_ids') and prior.get('identity'):
                    found=prior;retained=True
            # Preflight facts can be reused only for exactly the same mention/identity scope.
            record = {**ref, 'resolution_status': found['status'] if found else 'unresolved',
                      'identity': found.get('identity') if found else None,
                      'candidates': found.get('items', []) if found else [],
                      'document_status': found.get('document_status', 'not_checked') if found else 'not_checked',
                      'lookup_message': found.get('message', '') if found else '尚未核对资料。',
                      'evidence_ids': found.get('evidence_ids', []) if found else []}
            record['lookup_status']=lookup_status
            record['evidence_freshness']='retained_after_lookup_failure' if retained else 'saved_snapshot'
            if retained:record['lookup_message']='本次查询未取得可用更新；保留此前同一身份的已保存资料与核对结论，来源恢复后可重查。'
            if ref['identity_scope'] == 'family' and not ref.get('device_key'):
                record.update(identity=None, evidence_ids=[], resolution_status='family' if found else 'unresolved')
            refs.append(record)
            for item in evidence:
                if item['id'] in record['evidence_ids']:
                    linked_evidence.append({**item, 'reference_id': ref['id']})
        checks = checks_for(doc, refs, linked_evidence)
        blockers = [c for c in checks if c.get('blocking')]
        statuses = {'references': len(refs), 'must': sum(r['intent'] == 'must' for r in refs),
                    'resolved': sum(r['resolution_status'] == 'matched' for r in refs),
                    'needs_identity': sum(r['resolution_status'] in ('ambiguous', 'unresolved', 'not_found') for r in refs),
                    'evidence': len({e['id'] for e in linked_evidence}), 'blocking': len(blockers),
                    'pending_checks': sum(c['status'] in ('unknown', 'conditional', 'stale') for c in checks)}
        if not include_content:
            linked_evidence = [{k: v for k, v in e.items() if k != 'content'} for e in linked_evidence]
            for e in linked_evidence:
                e['assertions'] = e.get('assertions', [])[:30]
        return {'revision': revision, 'references': refs, 'evidence': linked_evidence, 'checks': checks,
                'blockers': blockers, 'summary': statuses,
                'jobs': [{k: j[k] for k in ('id', 'status', 'error', 'created_at', 'finished_at')} for j in jobs[:30]],
                'active_jobs': sum(j['status'] in ('queued', 'running') for j in jobs), 'activity': activity}

    def evidence(self, project_id, evidence_id):
        from .store import Missing
        with self.connect() as db:
            self._document(db, project_id)
            row = db.execute('SELECT payload FROM evidence_snapshots WHERE project_id=? AND id=?', (project_id, evidence_id)).fetchone()
            if not row: raise Missing('本项目中没有这份证据。')
            return json.loads(row['payload'])

    def persist_domain_checks(self, project_id):
        phase = self.domain_view(project_id)
        with self.connect() as db:
            for item in phase['checks']:
                db.execute('INSERT OR IGNORE INTO domain_checks VALUES(?,?,?,?,?)',
                           (project_id, item['id'], item['dependency_hash'], json.dumps(item, ensure_ascii=False), timestamp()))

    def apply_actions(self, db, project_id, doc, message_id, text, actions):
        from .store import Conflict
        if not actions: return
        original = copy.deepcopy(doc)
        refs = {r['id']: r for r in doc.get('device_references', []) if r.get('lifecycle') == 'active'}
        changes = []
        for action in actions:
            if action['operation']=='attach_reference':
                from .reference_design import reference_message
                from .device_domain import DeviceReferenceUpdate,apply_updates
                from types import SimpleNamespace
                source=copy.deepcopy(action['source']);source['source_message_id']=message_id
                if any(s['id']==source['id'] for s in doc.get('reference_designs',[])):continue
                doc.setdefault('reference_designs',[]).append(source)
                lines=text.splitlines()
                for part in [p for p in source['bom'] if p['core']][:80]:
                    if any(r.get('lifecycle')=='active' and r['mention'].casefold()==part['model'].casefold() for r in doc.get('device_references',[])):continue
                    quote=next((line for line in lines if '：'+part['model']+'；' in line),'')
                    if not quote:continue
                    update=DeviceReferenceUpdate(id='new-reference',mention=part['model'],intent='reference',identity_scope='exact_part',purpose='来自上传参考，待结合实际功能分析适用范围。',requirement_ids=[],module_ids=[],quantity=None,source_message_id=message_id,source_quote=quote)
                    apply_updates(doc,SimpleNamespace(device_reference_updates=[update],remove_device_reference_ids=[],device_constraints=[]),copy.deepcopy(doc),[{'id':message_id,'role':'user','content':text}],{}, {})
                    ref=next(r for r in doc['device_references'] if r['mention']==part['model'] and r['lifecycle']=='active')
                    if part['supplier_part']:ref['reference_supplier_part']=part['supplier_part']
                changes.append('读取参考资料：'+source['filename'])
                continue
            if action['operation']=='adopt_reference_core':
                if action.get('base_version',doc['version'])!=doc['version']:raise Conflict('核心器件方案已更新，请查看当前版本后重新采用。')
                from .reference_design import bom_view
                rows=[p for p in bom_view(doc)['rows'] if p['source_id']==action['source_id'] and p['core'] and p['status']=='proposed']
                source=next((s for s in doc.get('reference_designs',[]) if s['id']==action['source_id']),None)
                if not source or not rows:raise Conflict('当前没有这份参考中可采用的核心器件方案。')
                for part in rows:
                    ref=next(r for r in doc['device_references'] if r['id']==part['reference_id'])
                    if ref['intent'] in ('reference','undecided'):ref['intent']='prefer'
                    if not source['warnings'] and ref.get('quantity') is None:
                        ref['quantity']=part['observed_quantity'];ref['reference_quantity_source']={'source_id':source['id'],'sha256':source['sha256'],'part_key':part['key'],'basis':'用户采用已展示的参考实例数量；工程用量仍需核验'}
                    ref['mention_source']=ref.get('mention_source') or {'source_message_id':ref['source_message_id'],'source_quote':ref['source_quote']}
                    ref.update(source_message_id=message_id,source_quote=next(line for line in text.splitlines() if '：'+part['model']+'；' in line))
                changes.append('采用已关联本版功能的参考核心器件方案，保留可替换和工程核验边界。')
                continue
            if action['operation']=='add_reference':
                from types import SimpleNamespace
                from .device_domain import DeviceReferenceUpdate, apply_updates
                original=copy.deepcopy(doc)
                existing=next((r for r in doc.get('device_references',[]) if r.get('lifecycle')=='active' and r['mention'].casefold()==action['mention'].casefold()),{})
                update=DeviceReferenceUpdate(id='new-action',mention=action['mention'],intent=action['intent'],
                    identity_scope='exact_part',purpose=action.get('purpose','参与本项目设计'),
                    requirement_ids=list(dict.fromkeys(existing.get('requirement_ids',[])+action.get('requirement_ids',[]))),
                    module_ids=list(dict.fromkeys(existing.get('module_ids',[])+action.get('module_ids',[]))),
                    quantity=action.get('quantity',existing.get('quantity')),source_message_id=message_id,source_quote=text)
                apply_updates(doc,SimpleNamespace(device_reference_updates=[update],remove_device_reference_ids=[],device_constraints=[]),
                              original,[{'id':message_id,'role':'user','content':text}],{}, {})
                created=next(r for r in doc['device_references'] if r['mention']==action['mention'] and r['lifecycle']=='active')
                created.update(device_key=action['device_key'],identity_decision=message_id)
                if created['intent'] in ('must','prefer'):
                    created['adoption_source']={'source_message_id':message_id,'operation':'add_reference'}
                created['choice_source']={'source_message_id':message_id,'operation':'add_reference'}
                changes.append(action['mention']+'：新增'+INTENTS[action['intent']]+'引用。')
                continue
            ref = refs.get(action['reference_id'])
            if not ref: raise Conflict('器件引用已改变或已撤销，请重新查看后操作。')
            if action.get('expected_identity_hash') and action['expected_identity_hash'] != identity_input(ref):
                raise Conflict('器件身份在排队期间已变化，未覆盖新内容。请重新选择。')
            operation = action['operation']
            if operation == 'set_intent':
                if action.get('intent') not in INTENTS: raise Conflict('器件意图无效。')
                ref['intent'] = action['intent']; changes.append(ref['mention'] + '：' + INTENTS[ref['intent']])
                ref['intent_source']={'source_message_id':message_id,'intent':ref['intent']}
                if ref['intent'] in ('must','prefer'):
                    ref['adoption_source']={'source_message_id':message_id,'operation':'set_intent'}
            elif operation == 'revise_reference':
                edited=set(action['edited_fields'])
                old_name=ref['mention']
                if 'mention' in edited and action['mention']!=old_name:
                    ref.update(mention=action['mention'],identity_scope='exact_part',device_key=None,identity_decision=None)
                    for key in ('reference_supplier_part','reference_quantity_source','mention_source'):
                        ref.pop(key,None)
                for key in ('intent','quantity','purpose'):
                    if key in edited:ref[key]=action.get(key)
                if 'intent' in edited:ref['intent_source']={'source_message_id':message_id,'intent':ref['intent']}
                if 'quantity' in edited:ref.pop('reference_quantity_source',None)
                if ref['intent'] in ('must','prefer'):
                    ref['adoption_source']={'source_message_id':message_id,'operation':'revise_reference'}
                changes.append('修改器件方案：'+old_name+(' → '+ref['mention'] if old_name!=ref['mention'] else '（采用条件或用量更新）'))
            elif operation == 'select_identity':
                if not re_key(action.get('device_key', '')): raise Conflict('器件身份格式无效。')
                ref.update(device_key=action['device_key'], identity_scope='exact_part', identity_decision=message_id)
                changes.append(ref['mention'] + '：用户明确选定目录身份；使用意图独立保留。')
            elif operation == 'remove_reference':
                ref['lifecycle'] = 'removed'; changes.append('撤销器件引用：' + ref['mention'])
            else: raise Conflict('不支持的器件操作。')
            ref.update(source_message_id=message_id, source_quote=text)
            ref['choice_source']={'source_message_id':message_id,'operation':operation}
        reconcile_selection_items(doc)
        from .canvas_sync import record
        record(doc, original, message_id, origin='canvas')
        doc.update(changes=changes, changed_ids=[], domain_impacts=[{'reference_id': a.get('reference_id',''), 'reason': '用户修改器件意图、参考方案或身份，相关核对按当前版本重算。'} for a in actions])
        self._save(db, project_id, doc, doc['version'] + 1)


def re_key(value):
    import re
    return bool(re.fullmatch(r'datas:(source-device|manual-record):[1-9][0-9]{0,18}', value))
