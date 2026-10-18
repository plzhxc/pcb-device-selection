import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.domain import AnalysisResult, apply_analysis, empty_document, render_markdown
from backend.store import Conflict, Store


def result(updates=None,remove_ids=None):
    return AnalysisResult.model_validate({"reply":"已整理需求。","title":"测试产品","summary":"需求整理中。","updates":updates or [],"remove_ids":remove_ids or [],"questions":[],"flow":[],"changes":["更新需求。"]})


def requirement(rid='new-1',quote='电池供电',source='m1',state='stated'):
    return {"id":rid,"category":"architecture","name":"供电方式","value":"电池供电","unit":"","priority":"must","state":state,"source_message_id":source,"source_quote":quote}


class CoreTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/'.runtime'/'tests';folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.store=Store(Path(self.temp.name)/'test.sqlite3')
        self.project=self.store.create_project()

    def tearDown(self):self.temp.cleanup()

    def test_source_requires_verbatim_historical_user_message(self):
        messages=[{'id':'m1','role':'user','content':'我需要电池供电。'},{'id':'m2','role':'user','content':'再加蓝牙。'}]
        doc=apply_analysis(empty_document(),result([requirement()]),messages,'m2')
        self.assertEqual(doc['requirements'][0]['state'],'stated')
        fake=apply_analysis(empty_document(),result([requirement(quote='我需要220V交流电')]),messages,'m2')
        self.assertEqual(fake['requirements'][0]['state'],'suggested')
        self.assertEqual(fake['requirements'][0]['source_message_id'],'')

    def test_new_fact_never_removes_unrelated_fact(self):
        messages=[{'id':'m1','role':'user','content':'电池供电'}]
        doc=apply_analysis(empty_document(),result([requirement()]),messages,'m1')
        other=requirement('new-2',state='suggested');other.update(name='通信方式',value='蓝牙',category='functions')
        updated=apply_analysis(doc,result([other]),messages,'m1')
        self.assertEqual(updated['requirements'][0],doc['requirements'][0])
        self.assertEqual(len(updated['requirements']),2)

    def test_ids_not_reused_after_deletion(self):
        messages=[{'id':'m1','role':'user','content':'电池供电'}]
        doc=apply_analysis(empty_document(),result([requirement()]),messages,'m1')
        doc=apply_analysis(doc,result(remove_ids=['REQ-001']),messages,'m1')
        doc=apply_analysis(doc,result([requirement()]),messages,'m1')
        self.assertEqual(doc['requirements'][0]['id'],'REQ-002')

    def test_unknown_existing_id_rejected(self):
        with self.assertRaises(ValueError):apply_analysis(empty_document(),result([requirement('REQ-999')]),[],'m1')

    def test_ai_does_not_rename_a_user_named_project(self):
        project=self.store.rename(self.project['id'],'我的硬件项目',0)
        updated=apply_analysis(project['document'],result(),[],'m1')
        self.assertEqual(updated['title'],'我的硬件项目')

    def test_duplicate_existing_changes_rejected(self):
        with self.assertRaises(ValueError):apply_analysis(empty_document(),result([requirement(),requirement()]),[],'m1')

    def test_request_idempotency_and_project_concurrency(self):
        pid=self.project['id']
        first,created=self.store.begin_run(pid,'需求','request-1')
        same,created_again=self.store.begin_run(pid,'需求','request-1')
        self.assertTrue(created);self.assertFalse(created_again);self.assertEqual(first['id'],same['id'])
        self.assertEqual(len(self.store.get_project(pid)['messages']),1)
        with self.assertRaises(Conflict):self.store.begin_run(pid,'需求2','request-2')

    def test_cancelled_analysis_cannot_overwrite_document(self):
        run,_=self.store.begin_run(self.project['id'],'需求','request-1')
        self.store.finish(run['id'],'cancelled','已停止')
        with self.assertRaises(Conflict):self.store.commit_analysis(run['id'],empty_document(),'回答')
        self.assertEqual(self.store.get_project(self.project['id'])['document']['version'],0)

    def test_restart_retains_previous_document_and_allows_retry(self):
        project=self.store.create_project(example=True)
        self.store.begin_run(project['id'],'续航改为60天','request-1')
        self.store.recover()
        after=self.store.get_project(project['id'])
        self.assertEqual(project['document'],after['document'])
        self.assertEqual(after['latest_run']['status'],'interrupted')
        self.store.begin_run(project['id'],'','retry-1',retry=True)
        self.assertEqual(len(self.store.get_project(project['id'])['messages']),3)

    def test_manual_edit_version_guard_and_source(self):
        project=self.store.create_project(example=True)
        row=copy.deepcopy(project['document']['requirements'][0])
        update={k:row[k] for k in ['id','category','name','value','unit','priority','state']}
        update['value']='室外环境'
        updated=self.store.edit_requirement(project['id'],1,update)
        self.assertEqual(updated['document']['version'],2)
        self.assertIn('室外环境',updated['document']['summary'])
        self.assertEqual(updated['document']['requirements'][0]['source_type'],'manual')
        with self.assertRaises(Conflict):self.store.edit_requirement(project['id'],1,update)

    def test_active_run_blocks_manual_write(self):
        project=self.store.create_project(example=True)
        self.store.begin_run(project['id'],'补充需求','request-1')
        row=project['document']['requirements'][0]
        with self.assertRaises(Conflict):self.store.edit_requirement(project['id'],1,{k:row[k] for k in ['id','category','name','value','unit','priority','state']})

    def test_markdown_escapes_tables_without_mutating_document(self):
        project=self.store.create_project(example=True)
        doc=project['document'];doc['requirements'][0]['value']='A|B\n新行'
        before=copy.deepcopy(doc);markdown=render_markdown(doc)
        self.assertIn('A\\|B<br>新行',markdown)
        self.assertEqual(doc,before)

    def test_api_rejects_cross_origin_and_unknown_api_paths(self):
        from backend.app import create_app
        from fastapi.testclient import TestClient
        app=create_app(Path(self.temp.name)/'api')
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/projects',json={},headers={'Origin':'https://unrelated.example'}).status_code,403)
            self.assertEqual(client.get('/api/not-a-route').status_code,404)
            project=client.post('/api/projects',json={}).json()
            self.assertEqual(client.post(f"/api/projects/{project['id']}/messages",json={'text':'  ','request_id':'x'}).status_code,422)
            self.assertEqual(client.get(f"/api/projects/{project['id']}/export/pdf").status_code,404)
            exported=client.get(f"/api/projects/{project['id']}/export/md")
            self.assertEqual(exported.headers['X-Document-Version'],'0')
            self.assertIn('filename="requirements-v0.md"',exported.headers['Content-Disposition'])
            self.assertIn("filename*=UTF-8''",exported.headers['Content-Disposition'])


if __name__=='__main__':unittest.main(verbosity=2)
