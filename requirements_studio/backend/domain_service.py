"""Finite, cancellable knowledge jobs; ordinary chat does not depend on availability."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor

from .device_domain import digest
from .domain_store import identity_input
from .knowledge import catalog, mentions, KnowledgeUnavailable, now


def catalog_evidence(device):
    identity = device['identity']
    value = {'kind': 'catalog', 'device_key': identity['device_key'], 'title': identity['model'] + ' · 目录参数',
             'locator': identity['source_locator'], 'assertions': device['assertions'],
             'version_context': device['version_context'], 'availability': 'available',
             'checked_at': device['checked_at'], 'identity': identity}
    value['id'] = 'EV-' + digest({k: v for k, v in value.items() if k != 'checked_at'})[:24]
    return value


def span_evidence(identity, span):
    content = span.get('content', '')
    value = {'kind': 'datasheet', 'device_key': identity['device_key'], 'title': span.get('title') or identity['model'],
             'locator': f"{span['database']}.{span['collection']}:{span['id']}",
             'primary_key': str(span['id']), 'page_number': span.get('page_number'),
             'section_path': span.get('section_path', ''), 'content': content,
             'excerpt': content[:400], 'content_hash': hashlib.sha256(content.encode()).hexdigest(),
             'assertions': [], 'availability': 'available', 'retrieval_mode': span.get('retrieval_mode'),
             'version_context': {k: span.get(k) for k in ('database', 'collection', 'ingested_at', 'source_run')},
             'checked_at': now()}
    value['id'] = 'EV-' + digest({k: v for k, v in value.items() if k != 'checked_at'})[:24]
    return value


def strengthen_attributes(catalog_snapshot, spans):
    """Promote only explicitly worded operating/feature ranges with matching numbers.

    Most attributes deliberately remain catalog-only. Never infer guaranteed board performance.
    """
    for assertion in catalog_snapshot['assertions']:
        normalized = assertion.get('normalized')
        if not normalized: continue
        for span in spans:
            content = span.get('content', '')
            collapsed = re.sub(r'\s+', '', content)
            if assertion['field_key'] == 'input_voltage_range':
                # Require both bounds next to input-range wording, not a table of absolute stresses.
                a, b = normalized['minimum'], normalized['maximum']
                pattern = rf'(?:输入(?:电源)?电压范围|Input[Vv]oltage[Rr]ange)[^。\n]{{0,30}}?{re.escape(a)}V?(?:至|~|～|to|–|-){re.escape(b)}V'
                match = re.search(pattern, collapsed, re.I)
                if match and not re.search(r'绝对|应力|Absolute', collapsed[max(0, match.start()-50):match.end()], re.I):
                    assertion.update(validation_state='verified_for_claim', rating_kind='recommended_operating',
                                     conditions='数据手册明示输入工作范围；不代表板级电流、散热或其他条件已验证。',
                                     supporting_evidence_ids=[span['id']], supporting_quote=match.group())
                    break
            if assertion['field_key'] == 'switching_frequency' and assertion['validation_state'] == 'source_conflict':
                raw = re.sub(r'\s+', '', assertion['raw_value'])
                if raw and raw in collapsed and re.search(r'开关频率|switchingfrequency', collapsed, re.I):
                    assertion.update(validation_state='verified_for_claim', source_disagreement=assertion['issue'],
                                     issue='原始文本的单位已由手册支持，保留结构字段差异。',
                                     supporting_evidence_ids=[span['id']], supporting_quote=raw)
                    break
    # New enriched payload gets its own immutable identity.
    catalog_snapshot['id'] = 'EV-' + digest({k: v for k, v in catalog_snapshot.items() if k not in ('id', 'checked_at')})[:24]


class KnowledgeManager:
    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider or catalog
        self.tasks = {}
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='studio-knowledge')
        self.limit = asyncio.Semaphore(4)

    async def enqueue(self, project_id, references=None, request_id=None, message_id='', parent_run_id=None, refresh=False,include_resolved=False):
        if references is None:
            references = [r for r in self.store.raw_document(project_id).get('device_references', []) if r.get('lifecycle') == 'active']
        references = copy.deepcopy(references)
        if not references: return None
        if refresh and hasattr(self.provider,'invalidate'):
            await asyncio.get_running_loop().run_in_executor(self.pool,self.provider.invalidate,references)
        if not refresh and not include_resolved:
            phase = self.store.domain_view(project_id)
            complete = {identity_input(r) for r in phase['references'] if r['resolution_status'] not in ('unresolved', 'unavailable')}
            references = [r for r in references if identity_input(r) not in complete]
        if not references: return None
        request_id = request_id or 'lookup:' + digest([identity_input(r) for r in references])
        job, created = self.store.create_domain_job(project_id, references, request_id, message_id, parent_run_id)
        if created:
            task = asyncio.create_task(self._run(job))
            self.tasks[job['id']] = task
            task.add_done_callback(lambda done, key=job['id']: self.tasks.pop(key, None))
        return job['id']

    async def prepare(self, project_id, message, run_id):
        if not self.provider.enabled: return
        doc = self.store.raw_document(project_id)
        existing = [r for r in doc.get('device_references', []) if r.get('lifecycle') == 'active']
        known = {r['mention'].casefold() for r in existing}
        refs = []
        for mention in mentions(message['content']):
            if mention.casefold() not in known:
                refs.append({'id': '', 'mention': mention, 'identity_scope': 'exact_part', 'device_key': None})
                known.add(mention.casefold())
            if len(refs) >= 5: break
        refs+=existing[:max(0,5-len(refs))]
        job_id = await self.enqueue(project_id, refs,request_id='preflight:'+run_id, message_id=message['id'], parent_run_id=run_id)
        if job_id and job_id in self.tasks:
            try: await asyncio.wait_for(asyncio.shield(self.tasks[job_id]), timeout=24)
            except asyncio.TimeoutError: pass

    def preflight_context(self, project_id, text):
        doc = self.store.raw_document(project_id)
        values = {m.casefold() for m in mentions(text)}
        with self.store.connect() as db:
            rows = db.execute("SELECT result FROM domain_jobs WHERE project_id=? AND status='completed' ORDER BY rowid DESC LIMIT 12", (project_id,)).fetchall()
        result = []
        for row in rows:
            for ref in json.loads(row['result']).get('references', []):
                if ref['mention'].casefold() in values and not any(r['mention'].casefold() == ref['mention'].casefold() for r in result):
                    result.append({k: v for k, v in ref.items() if k not in ('items',)})
        return result[:12]

    async def _read_reference(self, job, ref):
        async with self.limit:
            if self.store.domain_job(job['project_id'], job['id'])['status'] not in ('queued', 'running'):
                return None, []
            loop = asyncio.get_running_loop()
            self.store.domain_event(job['id'], '正在查询 ' + ref['mention'] + ' 的真实目录身份。')
            try:
                resolution = await loop.run_in_executor(self.pool, self.provider.resolve, ref)
                output = {'mention': ref['mention'], 'identity_input_hash': identity_input(ref),
                          'status': resolution['status'], 'items': resolution.get('items', []),
                          'match_mode': resolution.get('match_mode'), 'identity': None,
                          'evidence_ids': [], 'document_status': 'not_checked', 'message': ''}
                if not resolution.get('device'):
                    output['message'] = '存在多个或相关型号，未替用户选择代表器件。' if resolution['status'] == 'ambiguous' else '在本次目录范围内未找到唯一身份。'
                    return output, []
                device = resolution['device']; identity = device['identity']; output['identity'] = identity
                self.store.domain_event(job['id'], ref['mention'] + ' 已定位，正在取得参数与手册片段。', 'datasheet')
                topics=ref.get('knowledge_topics') or ['输入电压 开关频率','功能 接口 工作条件']
                documents = await loop.run_in_executor(self.pool, self.provider.spans, identity, topics)
                spans = [span_evidence(identity, r) for r in documents.get('items', [])]
                record = catalog_evidence(device)
                strengthen_attributes(record, spans)
                evidence = [record, *spans]
                output.update(evidence_ids=[e['id'] for e in evidence], document_status=documents['status'],
                              attributes=[a for a in record['assertions'] if a['priority_rank'] <= 8][:8],
                              message='已保存目录原值及实际取得的资料；工程结论按具体条件核对。')
                self.store.domain_event(job['id'], ref['mention'] + f'：已取得 {len(record["assertions"])} 项属性与 {len(spans)} 个片段。', 'checks')
                return output, evidence
            except (KnowledgeUnavailable, ValueError) as exc:
                return {'mention': ref['mention'], 'identity_input_hash': identity_input(ref), 'status': 'unavailable',
                        'items': [], 'identity': None, 'evidence_ids': [], 'document_status': 'unavailable', 'message': str(exc)}, []
            except Exception as exc:
                return {'mention': ref['mention'], 'identity_input_hash': identity_input(ref), 'status': 'unavailable',
                        'items': [], 'identity': None, 'evidence_ids': [], 'document_status': 'unavailable',
                        'message': '资料读取未完成（' + type(exc).__name__ + '），可以重试。'}, []

    async def _run(self, job):
        if not self.store.start_domain_job(job['project_id'], job['id']): return
        refs = json.loads(job['request'])['references']
        tasks = []
        outputs = []; evidence = []
        try:
            # Finite batches retain all references, with a real time budget per batch.
            for start in range(0, len(refs), 5):
                tasks = [asyncio.create_task(self._read_reference(job, r)) for r in refs[start:start+5]]
                done, pending = await asyncio.wait(tasks, timeout=30)
                for task in done:
                    output, pieces = task.result()
                    if output: outputs.append(output); evidence += pieces
                for task in pending: task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if pending:
                    for ref, task in zip(refs[start:start+5], tasks):
                        if task in pending:
                            outputs.append({'mention': ref['mention'], 'identity_input_hash': identity_input(ref),
                                            'status': 'unavailable', 'items': [], 'evidence_ids': [],
                                            'message': '此批检索达到预算，已有需求保留，可以重试。'})
            self.store.finish_domain_job(job['project_id'], job['id'], {'references': outputs}, evidence)
            self.store.persist_domain_checks(job['project_id'])
        except asyncio.CancelledError:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.store.finish_domain_job(job['project_id'], job['id'], {'references': outputs}, evidence,
                                         'cancelled', '资料核对已停止；已完成快照保留。')
            raise
        except Exception as exc:
            self.store.finish_domain_job(job['project_id'], job['id'], {'references': outputs}, evidence,
                                         'failed', '资料作业未完成（' + type(exc).__name__ + '）；已有需求保留。')

    async def cancel_project(self, project_id):
        pending=[]
        for key, task in list(self.tasks.items()):
            try: self.store.domain_job(project_id, key)
            except Exception: continue
            pending.append(task);task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def close(self):
        for task in list(self.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        self.pool.shutdown(wait=False, cancel_futures=True)


def requested_references(result,document):
    """Resolve model temporary IDs against saved objects; never accept arbitrary SQL/tools."""
    references={r['id']:r for r in document.get('device_references',[]) if r.get('lifecycle')=='active'}
    aliases={u.id:next((r['id'] for r in references.values() if r['mention'].casefold()==u.mention.casefold()),'')
             for u in result.device_reference_updates}
    requested={}
    for request in result.knowledge_requests:
        rid=aliases.get(request.reference_id,request.reference_id)
        if rid not in references:raise ValueError('knowledge_requests引用了不存在或已撤销的器件。')
        row=requested.setdefault(rid,copy.deepcopy(references[rid]))
        row['knowledge_topics']=list(dict.fromkeys(row.get('knowledge_topics',[])+request.topics))[:2]
    return list(requested.values())
