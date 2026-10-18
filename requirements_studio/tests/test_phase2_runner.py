import asyncio
import errno
import json
import sys
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch, AsyncMock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.store import Store
from backend.codex import Runner,ProviderFailure

FAKE=r'''import json,sys,time
from pathlib import Path
c=json.loads(sys.stdin.read().split('以下 JSON 为用户数据：\n',1)[1])
out=Path(sys.argv[sys.argv.index('--output-last-message')+1]);mid=c['current_message_id'];text=next(m['content'] for m in c['messages'] if m['id']==mid)
print(json.dumps({'type':'turn.started'}),flush=True)
if 'network-' in text or 'routing-' in text or 'provider-auth' in text:
 counter=out.parent/'connection-count';n=int(counter.read_text())+1 if counter.exists() else 1;counter.write_text(str(n))
 if 'provider-auth' in text:
  print(json.dumps({'type':'turn.failed','error':{'code':'authentication_error','message':'HTTP 401 api_key=fixture-api-secret https://example.invalid/v1?api_key=fixture-url-secret\nAuthorization: Bearer fixture-bearer-secret'}}),flush=True);sys.exit(1)
 if 'network-persistent' in text or 'routing-persistent' in text or n==1:
  out.write_text(json.dumps(dict(reply='不可提交的中断结果',title='未完成',summary='不应保存',updates=[],remove_ids=[],questions=[],flow=[],changes=[],analysis_revision=3)))
  detail={'code':'','message':'workspace routing discovery failed'} if 'routing-' in text else {'code':'network_error','message':'stream disconnected before completion'}
  print(json.dumps({'type':'turn.failed','error':detail}),flush=True);sys.exit(1 if 'routing-' in text else 0)
if 'tool' in text:
 print(json.dumps({'type':'item.started','item':{'type':'command_execution'}}),flush=True);time.sleep(30)
valid=('validation_feedback' in c and 'always_bad' not in text) or bool(c.get('previous_validation_feedback')) or 'network-recoverable' in text or 'routing-recoverable' in text or text=='archive-fault'
if 'cancel-repair' in text and 'validation_feedback' in c:
 (out.parent/'repair-running').write_text('ready');time.sleep(30)
r=dict(reply='已记录。',title='测试产品',summary='仅作协议验收',updates=[dict(id='new-1',category='functions',name='显示',value=text,unit='',priority='must',state='stated',source_message_id=mid,source_quote=text)],remove_ids=[],questions=[],flow=[dict(label='显示',requirement_ids=['new-1'])],changes=['记录需求'],analysis_revision=2)
if text.startswith('owned-anchor'):
 valid=True;r['updates'][0].update(source_message_id='',source_quote='@'+next(k for k,v in c['source_anchor_index'].items() if v['message_id']==mid))
if not valid:
 if 'source-typo' in text:r['updates'][0]['source_message_id']=mid[:-1]
 elif 'quote-typo' in text:r['updates'][0]['source_quote']='用户没有说过这句话'
 else:r['numeric_constraints']=[dict(id='bad',requirement_id='new-1',metric='budget',scope='board',conditions='',minimum=None,maximum=None,unit='元',source_message_id=mid,source_quote=text)]
out.write_text(json.dumps(r,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'type':'turn.completed'}),flush=True)
'''


class RepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT/'.runtime/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.runtime/tests');self.folder=Path(self.temp.name)
        self.binary=self.folder/'codex';self.binary.write_text('#!'+sys.executable+'\n'+FAKE,encoding='utf-8');self.binary.chmod(0o755)
        self.store=Store(self.folder/'studio.sqlite3');self.project=self.store.create_project();self.runner=Runner(self.store,self.folder,timeout_seconds=10)
        self.mock=patch('backend.codex.shutil.which',return_value=str(self.binary));self.mock.start()

    async def asyncTearDown(self):
        await self.runner.close();self.mock.stop();self.temp.cleanup()

    async def begin(self,text):
        run,_=self.store.begin_run(self.project['id'],text,text);self.runner.start(run['id']);return run

    async def done(self,run,timeout=15):
        await asyncio.wait_for(self.runner.tasks[run['id']],timeout)
        return self.store.get_project(self.project['id'])

    async def test_one_bounded_repair_commits_only_one_version(self):
        run=await self.begin('recoverable numeric contract');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        self.assertEqual(len([m for m in p['messages'] if m['role']=='user']),1)
        self.assertEqual(json.loads((self.folder/'runs'/run['id']/'validation.json').read_text())['repair_attempts'],1)

    def test_routing_retry_does_not_mask_auth_quota_or_configuration_errors(self):
        self.assertTrue(ProviderFailure({'message':'workspace routing discovery failed'}).retryable)
        for code,message in [('authentication_error','HTTP 401 workspace routing discovery failed'),
                             ('insufficient_quota','workspace routing discovery failed'),
                             ('invalid_request','workspace routing discovery failed'),
                             ('','workspace does not exist')]:
            with self.subTest(code=code,message=message):
                self.assertFalse(ProviderFailure({'code':code,'message':message}).retryable)

    async def test_routing_failure_recovers_without_reposting_or_committing_partial_output(self):
        self.runner.timeout=60
        run=await self.begin('routing-recoverable');p=await self.done(run,75)
        folder=self.folder/'runs'/run['id']
        self.assertEqual(p['latest_run']['status'],'completed')
        self.assertEqual(p['document']['version'],1)
        self.assertEqual(sum(m['role']=='user' for m in p['messages']),1)
        self.assertEqual((folder/'connection-count').read_text(),'2')
        self.assertTrue((folder/'connection-attempt-1/receipt.json').exists())
        self.assertEqual(json.loads((folder/'receipt.json').read_text())['network_reconnects_used'],1)
        self.assertNotIn('不可提交',p['messages'][-1]['content'])

    async def test_persistent_routing_failure_stops_after_one_reconnection(self):
        self.runner.timeout=60
        run=await self.begin('routing-persistent');p=await self.done(run,75)
        folder=self.folder/'runs'/run['id']
        self.assertEqual((folder/'connection-count').read_text(),'2')
        self.assertEqual(p['latest_run']['status'],'failed')
        self.assertEqual(p['document']['version'],0)
        self.assertEqual(sum(m['role']=='user' for m in p['messages']),1)

    async def test_connection_recovers_once_without_duplicate_input_or_accepting_partial_result(self):
        # Two real filesystem reservations plus durable failure receipts can
        # exceed 10s on GPFS. This tests retry count, not the separate deadline.
        self.runner.timeout=60
        run=await self.begin('network-recoverable');p=await self.done(run,75);folder=self.folder/'runs'/run['id']
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        self.assertEqual(len([m for m in p['messages'] if m['role']=='user']),1)
        self.assertEqual((folder/'connection-count').read_text(),'2')
        first=json.loads((folder/'connection-attempt-1/receipt.json').read_text())
        self.assertTrue(any(e['type']=='turn.failed' for e in first['events']))
        self.assertEqual(json.loads((folder/'receipt.json').read_text())['network_reconnects_used'],1)
        self.assertFalse((folder/'repair-1').exists())

    async def test_persistent_connection_failure_stops_after_one_retry_without_committing_partial_json(self):
        self.runner.timeout=60
        run=await self.begin('network-persistent');p=await self.done(run,75);folder=self.folder/'runs'/run['id']
        self.assertEqual(p['latest_run']['status'],'failed');self.assertEqual(p['document']['version'],0)
        self.assertEqual((folder/'connection-count').read_text(),'2');self.assertIn('连接',p['latest_run']['error'])
        self.assertFalse((folder/'repair-1').exists())

    async def test_login_failure_is_not_retried_and_credentials_are_redacted_in_receipt(self):
        run=await self.begin('provider-auth');p=await self.done(run);folder=self.folder/'runs'/run['id']
        self.assertEqual(p['latest_run']['status'],'failed');self.assertIn('登录',p['latest_run']['error'])
        self.assertEqual((folder/'connection-count').read_text(),'1');self.assertEqual(p['document']['version'],0)
        receipt=(folder/'receipt.json').read_text()
        for secret in ('fixture-api-secret','fixture-url-secret','fixture-bearer-secret'):self.assertNotIn(secret,receipt+p['latest_run']['error'])
        self.assertIn('authentication_error',receipt);self.assertIn('已隐藏',receipt)

    async def test_second_invalid_output_fails_without_modifying_the_document(self):
        run=await self.begin('always_bad');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'failed');self.assertEqual(p['document']['version'],0)
        self.assertIn('复核',p['latest_run']['error']);self.assertFalse((self.folder/'runs'/run['id']/'repair-2').exists())
        retry,_=self.store.begin_run(self.project['id'],'','manual-retry',retry=True);self.runner.start(retry['id']);p=await self.done(retry)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(len([m for m in p['messages'] if m['role']=='user']),1)

    async def test_source_identifier_typo_is_repaired_before_any_commit(self):
        run=await self.begin('source-typo');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        requirement=p['document']['requirements'][0]
        self.assertEqual(requirement['state'],'stated');self.assertEqual(requirement['source_message_id'],run['message_id'])
        folder=self.folder/'runs'/run['id']
        feedback=json.loads((folder/'validation-error.json').read_text())
        self.assertIn('source_message_id',feedback[0]['message'])
        self.assertEqual(json.loads((folder/'validation.json').read_text())['repair_attempts'],1)
        with zipfile.ZipFile(folder/'inputs.zip') as archive:
            schema=json.loads(archive.read('response.schema.json'))
            context=json.loads(archive.read('context.json'))
        self.assertEqual(context['message_aliases'],{'M-001':run['message_id']})
        def resolved(node):
            while '$ref' in node:
                node=schema['$defs'][node['$ref'].rsplit('/',1)[-1]]
            return node
        for name in ('RequirementUpdate','FieldResolution','ModuleUpdate','CoverageUpdate','NumericConstraint'):
            definition=schema['$defs'][name]
            for branch in definition.get('anyOf',[definition]):
                props=branch['properties']
                self.assertEqual(resolved(props['source_message_id'])['enum'],[''])
                self.assertEqual(set(resolved(props['source_quote'])['enum']),{'',f'@S-{run["message_id"]}-0-{len("source-typo")}'})
        self.assertEqual(len([m for m in p['messages'] if m['role']=='user']),1)

    async def test_owned_anchor_restores_source_without_model_copying_user_text(self):
        text='owned-anchor 中文工程要求，保留标点、换行和单位。\nUSB-C 输入5V。'
        run=await self.begin(text);p=await self.done(run);folder=self.folder/'runs'/run['id']
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        row=p['document']['requirements'][0]
        self.assertEqual(row['source_message_id'],run['message_id']);self.assertEqual(row['source_quote'],text)
        answer=json.loads((folder/'answer.json').read_text())
        self.assertEqual(answer['updates'][0]['source_quote'],f'@S-{run["message_id"]}-0-{len(text)}')
        self.assertEqual(answer['updates'][0]['source_message_id'],'')
        validation=json.loads((folder/'validation.json').read_text())
        self.assertEqual(validation['repair_attempts'],0)
        self.assertEqual(validation['normalized_evidence'][0]['end'],len(text))

    async def test_archive_failure_does_not_invalidate_a_saved_document(self):
        with patch('backend.codex.archive_run_inputs',side_effect=OSError(errno.ENOSPC,'fixture storage full')):
            run=await self.begin('archive-fault');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        self.assertTrue((self.folder/'runs'/run['id']/'context.json').exists())

    async def test_local_cli_file_failure_is_reported_as_storage_not_bad_requirements(self):
        with patch('backend.codex.asyncio.create_subprocess_exec',side_effect=OSError(errno.EDQUOT,'fixture disk quota')):
            run=await self.begin('disk-write-failure');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'failed');self.assertEqual(p['document']['version'],0)
        self.assertIn('存储',p['latest_run']['error']);self.assertEqual(len(p['messages']),1)

    async def test_no_model_is_started_when_the_write_budget_cannot_be_reserved(self):
        with patch('backend.codex.WriteBudget.acquire',side_effect=OSError(errno.EDQUOT,'fixture quota')),patch('backend.codex.asyncio.create_subprocess_exec',new_callable=AsyncMock) as invoke:
            run=await self.begin('owned-anchor no-space');p=await self.done(run)
        invoke.assert_not_called();self.assertEqual(p['latest_run']['status'],'failed')
        self.assertEqual(p['document']['version'],0);self.assertIn('存储',p['latest_run']['error'])
        self.assertEqual(sum(m['role']=='user' for m in p['messages']),1)

    async def test_reserved_space_is_released_before_output_metadata_and_commit(self):
        state={'held':True}
        def release():state['held']=False
        budget=SimpleNamespace(requested_bytes=8388608,allocated_bytes=16777216,release=release)
        original=Path.write_text
        def guarded_write(path,*args,**kwargs):
            if path.name in ('stderr.log','receipt.json','validation.json') and state['held']:
                raise OSError(errno.EDQUOT,'fixture output needs the reserved blocks')
            return original(path,*args,**kwargs)
        with patch('backend.codex.WriteBudget.acquire',return_value=budget),patch.object(Path,'write_text',guarded_write):
            run=await self.begin('owned-anchor budget-release');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        self.assertFalse(state['held'])
        receipt=json.loads((self.folder/'runs'/run['id']/'receipt.json').read_text())
        self.assertEqual(receipt['storage_reserve_requested_bytes'],8388608)

    async def test_saved_output_does_not_report_idle_during_final_archival(self):
        entered=threading.Event();release=threading.Event();self.runner.timeout=60
        def archive(folder):
            entered.set()
            if not release.wait(60):raise RuntimeError('fixture archive was not released')
        with patch('backend.codex.archive_run_inputs',side_effect=archive):
            run=await self.begin('owned-anchor finishing lifecycle');task=self.runner.tasks[run['id']]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait,20))
                p=self.runner.project(self.project['id'])
                self.assertEqual(p['document']['version'],1)
                self.assertEqual(p['latest_run']['status'],'running')
                self.assertFalse(any(e['kind']=='completed' for e in self.store.events(run['id'],0)))
            finally:release.set()
            await asyncio.wait_for(task,30)
        self.assertEqual(self.runner.project(self.project['id'])['latest_run']['status'],'completed')
        self.assertEqual(sum(e['kind']=='completed' for e in self.store.events(run['id'],0)),1)

    async def test_cancel_during_repair_terminates_the_child(self):
        run=await self.begin('cancel-repair')
        marker=self.folder/'runs'/run['id']/'repair-1/repair-running'
        for _ in range(300):
            if marker.exists():break
            await asyncio.sleep(.02)
        self.assertTrue(marker.exists());await self.runner.cancel(run['id']);p=self.store.get_project(self.project['id'])
        self.assertEqual(p['latest_run']['status'],'cancelled');self.assertEqual(p['document']['version'],0)
        self.assertNotIn(run['id'],self.runner.processes)

    async def test_paraphrased_source_is_repaired_without_reasking_the_user(self):
        run=await self.begin('quote-typo');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        self.assertEqual(p['document']['requirements'][0]['source_quote'],'quote-typo')
        folder=self.folder/'runs'/run['id']
        self.assertIn('source_quote',json.loads((folder/'validation-error.json').read_text())[0]['message'])
        self.assertEqual(json.loads((folder/'validation.json').read_text())['repair_attempts'],1)
        self.assertEqual(len([m for m in p['messages'] if m['role']=='user']),1)

    async def test_out_of_scope_tool_does_not_trigger_a_repair(self):
        run=await self.begin('tool');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'failed');self.assertIn('工具',p['latest_run']['error'])
        self.assertFalse((self.folder/'runs'/run['id']/'repair-1').exists())

    async def test_storage_failure_retains_and_recovers_the_returned_cli_answer(self):
        original=self.store.finish;calls=[]
        def finish(*args):
            calls.append(args)
            if len(calls)==1:raise sqlite3.OperationalError('disk I/O error')
            return original(*args)
        with patch.object(self.store,'commit_analysis',side_effect=sqlite3.OperationalError('disk I/O error')),patch.object(self.store,'finish',side_effect=finish):
            run=await self.begin('storage recovery');p=await self.done(run)
        self.assertEqual(p['latest_run']['status'],'failed');self.assertEqual(p['document']['version'],0)
        self.assertEqual(len(calls),2);self.assertIn('存储',p['latest_run']['error'])
        retry,_=self.store.begin_run(self.project['id'],'','recover-storage',retry=True)
        with patch.object(self.runner,'_invoke',new=AsyncMock(side_effect=AssertionError('returned result must be reused'))):
            self.runner.start(retry['id']);p=await self.done(retry)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],1)
        self.assertEqual(len([m for m in p['messages'] if m['role']=='user']),1)
        receipt=json.loads((self.folder/'runs'/retry['id']/'receipt.json').read_text())
        self.assertEqual(receipt['restored_from_run'],run['id'])

    async def test_persistent_storage_failure_does_not_leave_a_false_running_state(self):
        with patch.object(self.store,'commit_analysis',side_effect=sqlite3.OperationalError('disk I/O error')),patch.object(self.store,'finish',side_effect=sqlite3.OperationalError('disk I/O error')):
            run=await self.begin('persistent storage');await self.done(run)
        self.assertEqual(self.runner.project(self.project['id'])['latest_run']['status'],'failed')
        self.assertEqual(self.store.get_run(run['id'])['status'],'running')
        await self.runner.reconcile(self.project['id'])
        self.assertEqual(self.store.get_run(run['id'])['status'],'failed');self.assertNotIn(run['id'],self.runner.failed_runs)

    async def test_cached_reply_cannot_overwrite_a_new_document_version(self):
        with patch.object(self.store,'commit_analysis',side_effect=sqlite3.OperationalError('disk I/O error')):
            run=await self.begin('old source');await self.done(run)
        self.store.edit_requirement(self.project['id'],0,dict(id='',category='functions',name='另行确认的功能',value='保留此项新决定',unit='',priority='must',state='stated'))
        retry,_=self.store.begin_run(self.project['id'],'','new-baseline',retry=True)
        with patch.object(self.runner,'_invoke',wraps=self.runner._invoke) as invoke:
            self.runner.start(retry['id']);p=await self.done(retry)
            self.assertGreater(invoke.await_count,0)
        self.assertEqual(p['latest_run']['status'],'completed');self.assertEqual(p['document']['version'],2)
        self.assertTrue(any(r['value']=='保留此项新决定' for r in p['document']['requirements']))

    async def test_api_exposes_storage_failure_and_allows_reconciliation(self):
        from fastapi.testclient import TestClient
        from backend.app import create_app
        app=create_app(self.folder/'api')
        with TestClient(app) as client:
            project=client.post('/api/projects',json={}).json();store=app.state.store;runner=app.state.runner
            run,_=store.begin_run(project['id'],'存储故障测试','storage-api');store.mark_running(run['id'])
            runner.failed_runs[run['id']]='本地存储写入失败，已保存文档保留。'
            self.assertEqual(client.get('/api/projects/'+project['id']).json()['latest_run']['status'],'failed')
            self.assertIn('"kind": "failed"',client.get('/api/runs/'+run['id']+'/events').text)
            with patch.object(runner,'start'):
                response=client.post('/api/projects/'+project['id']+'/retry',json={'request_id':'reconcile'})
            self.assertEqual(response.status_code,202);self.assertEqual(store.get_run(run['id'])['status'],'failed')
            with patch.object(store,'begin_run',side_effect=sqlite3.OperationalError('disk I/O error')):
                response=client.post('/api/projects/'+project['id']+'/retry',json={'request_id':'unavailable'})
            self.assertEqual(response.status_code,503);self.assertIn('存储',response.json()['detail'])
