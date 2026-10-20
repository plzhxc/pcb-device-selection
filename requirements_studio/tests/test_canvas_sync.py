import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from backend.store import Store
from backend.canvas_sync import record, update, pending
from backend.reference_design import bom_view
from tests.phase3_fixtures import seed

ROOT=Path(__file__).resolve().parents[1]


class ChangeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.runtime/canvas-sync-20261020')
        self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'studio.sqlite3')
        self.project=seed(self.store,ambiguous=False)
        self.before=copy.deepcopy(self.project['document'])
        self.before['modules'][0]['name']='TPS5450DDAR 电源转换'
        self.before['delivery_policy_version']=1
        self.before['delivery_checks']=[dict(key=k,state='design_task',conclusion='TPS5450DDAR 方案待工程核验',
            requirement_ids=['REQ-001'],next_action='按新方案核对') for k in
            ('scope_acceptance','capability_chain','interfaces_power','core_parts')]
        self.doc=copy.deepcopy(self.before)
        ref=self.doc['device_references'][0]
        ref.update(mention='ME6211C33M5G-N',device_key=None,source_message_id='edit')
        record(self.doc,self.before,'edit',origin='canvas')

    def clear_reviews(self):
        self.doc['decision_reviews']=[]
        self.doc['recheck_fields']=[]
        for f in self.doc['field_resolutions']:
            f.update(state='confirmed',conclusion='ME6211C33M5G-N 方案，功能目标保留')
        for c in self.doc['delivery_checks']:
            c.update(state='design_task',conclusion='ME6211C33M5G-N 待工程核验')
        self.doc['summary']='ME6211C33M5G-N 方案'
        self.doc['requirements'][0]['value']='保留电源功能，优先使用 ME6211C33M5G-N，适用性另行核验'

    def test_edit_blocks_review_and_records_actual_dependants(self):
        from backend.coverage import review_document
        self.assertEqual(len(pending(self.doc)),1)
        change=pending(self.doc)[0]
        self.assertEqual(change['requirement_ids'],['REQ-001'])
        self.assertEqual(change['module_ids'],['MOD-001'])
        self.assertEqual(review_document(self.doc)['status'],'draft')
        self.assertTrue(self.doc['recheck_fields'])
        self.assertEqual(self.before['device_references'][0]['mention'],'TPS5450DDAR')

    def test_stale_module_or_workflow_cannot_close_a_reported_sync(self):
        self.clear_reviews()
        update(self.doc)
        self.assertEqual(len(pending(self.doc)),1)
        self.assertTrue(any(r['kind']=='module' for r in pending(self.doc)[0]['remaining']))
        self.doc['modules'][0]['name']='ME6211C33M5G-N 电源方案'
        self.doc['flow'][0]['label']='TPS5450DDAR 上电'
        update(self.doc)
        self.assertTrue(any(r['kind']=='flow' for r in pending(self.doc)[0]['remaining']))
        self.doc['flow'][0]['label']='接入电源并提供输出'
        update(self.doc)
        self.assertFalse(pending(self.doc))
        self.assertEqual(self.doc['canvas_changes'][0]['status'],'synchronized')

    def test_explicit_old_to_new_comparison_is_not_a_stale_choice(self):
        self.clear_reviews()
        self.doc['modules'][0]['name']='电源转换'
        self.doc['modules'][0]['boundary']='原 TPS5450DDAR 已替换为 ME6211C33M5G-N；检查适配'
        update(self.doc)
        self.assertFalse(pending(self.doc))

    def test_same_category_is_not_a_dependency(self):
        before=copy.deepcopy(self.before)
        before['requirements'].append({**before['requirements'][0],'id':'REQ-002','name':'独立电路'})
        before['modules'].append({**before['modules'][0],'id':'MOD-002','requirement_ids':['REQ-002']})
        before['field_resolutions'].append(dict(field_id='V3-1.3:F01',state='confirmed',
            conclusion='另一模块预算要求',requirement_ids=['REQ-002'],source_message_id='x',source_quote='独立预算'))
        doc=copy.deepcopy(before);doc['device_references'][0]['mention']='ME6211C33M5G-N'
        record(doc,before,'edit',origin='canvas')
        self.assertNotIn('REQ-002',doc['canvas_changes'][0]['requirement_ids'])
        self.assertNotIn('V3-1.3:F01',doc['recheck_fields'])

    def test_followup_review_reopens_the_latest_synced_edit(self):
        self.clear_reviews()
        self.doc['modules'][0]['name']='新电源方案'
        update(self.doc)
        self.assertFalse(pending(self.doc))
        self.doc['decision_reviews']=[dict(requirement_id='REQ-001',message_id='followup',reason='讨论后核对负载')]
        update(self.doc)
        self.assertEqual(len(pending(self.doc)),1)
        self.assertTrue(any(r['kind']=='requirement' for r in pending(self.doc)[0]['remaining']))
        self.doc['decision_reviews']=[]
        update(self.doc)
        self.assertFalse(pending(self.doc))

    def test_older_synced_edit_is_not_reopened_after_a_newer_choice(self):
        self.clear_reviews()
        self.doc['modules'][0]['name']='新电源方案'
        update(self.doc)
        old=copy.deepcopy(self.doc)
        self.doc['device_references'][0]['mention']='第三个器件'
        record(self.doc,old,'newer',origin='canvas')
        self.assertEqual(self.doc['canvas_changes'][0]['status'],'synchronized')
        self.assertEqual(len(pending(self.doc)),1)

    def test_old_device_description_is_retired_but_design_target_survives(self):
        before=copy.deepcopy(self.before)
        base=dict(reference_id='REF-001',requirement_id='REQ-001')
        before['device_constraints']=[dict(base,id='old-rating',kind='user_device_description'),
                                      dict(base,id='target',kind='design_requirement')]
        doc=copy.deepcopy(before);doc['device_references'][0]['mention']='ME6211C33M5G-N'
        record(doc,before,'edit',origin='canvas')
        self.assertEqual([c['id'] for c in doc['device_constraints']],['target'])
        self.assertEqual(doc['canvas_changes'][0]['retired_descriptions'][0]['id'],'old-rating')

    def test_association_wording_does_not_start_an_endless_change_loop(self):
        doc=copy.deepcopy(self.before);doc['device_references'][0]['purpose']='重新说明同一用途'
        record(doc,self.before,'later')
        self.assertNotIn('canvas_changes',doc)

    def test_later_explicit_choice_cannot_be_rolled_back_by_older_quote(self):
        from backend.device_domain import DeviceReferenceUpdate,apply_updates
        doc=copy.deepcopy(self.before);ref=doc['device_references'][0]
        first=self.project['messages'][0]
        ref.update(mention='ME6211C33M5G-N',source_message_id='edit',source_quote='改用 ME6211C33M5G-N',
                   choice_source={'source_message_id':'edit','operation':'revise_reference'})
        row={k:v for k,v in self.before['device_references'][0].items() if k in DeviceReferenceUpdate.model_fields}
        result=SimpleNamespace(device_reference_updates=[DeviceReferenceUpdate(**row)],remove_device_reference_ids=[],device_constraints=[])
        with self.assertRaisesRegex(ValueError,'不能用更早'):
            apply_updates(doc,result,copy.deepcopy(doc),[first,{'id':'edit','role':'user','content':'改用 ME6211C33M5G-N'}],{}, {})


class CanvasActionTests(unittest.TestCase):
    def setUp(self):
        from backend.app import create_app
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.runtime/canvas-sync-20261020')
        self.addCleanup(self.temp.cleanup)
        self.app=create_app(Path(self.temp.name))
        self.app.state.runner.start=lambda _:None
        self.client=TestClient(self.app);self.client.__enter__()
        self.addCleanup(self.client.__exit__,None,None,None)
        self.project=seed(self.app.state.store,ambiguous=False)
        self.pid=self.project['id'];self.url='/api/projects/'+self.pid+'/device-references'

    def action(self,action,request='edit'):
        p=self.app.state.store.get_project(self.pid)
        return self.client.post(self.url,json=dict(base_version=p['document']['version'],request_id=request,action=action))

    def test_model_edit_is_immediate_traceable_and_idempotent(self):
        response=self.action(dict(operation='revise_reference',reference_id='REF-001',mention='ME6211C33M5G-N'))
        self.assertEqual(response.status_code,202,response.text)
        p=response.json()['project'];ref=p['document']['device_references'][0]
        self.assertEqual(ref['mention'],'ME6211C33M5G-N')
        self.assertIsNone(ref['device_key'])
        self.assertEqual(p['canvas_sync']['pending_count'],1)
        again=self.action(dict(operation='revise_reference',reference_id='REF-001',mention='ME6211C33M5G-N'))
        self.assertTrue(again.json()['replayed'])
        self.assertEqual(len(again.json()['project']['document']['canvas_changes']),1)
        self.assertEqual(bom_view(p['document'])['current_rows'][0]['model'],'ME6211C33M5G-N')
        csv=self.client.get('/api/projects/'+self.pid+'/current-bom.csv')
        self.assertIn('ME6211C33M5G-N',csv.text)

    def test_clear_quantity_and_reject_empty_model_without_overwriting(self):
        bad=self.action(dict(operation='revise_reference',reference_id='REF-001',mention=' '))
        self.assertEqual(bad.status_code,422)
        self.assertEqual(self.app.state.store.raw_document(self.pid)['version'],self.project['document']['version'])
        response=self.action(dict(operation='revise_reference',reference_id='REF-001',quantity=None),request='clear')
        # The initial fixture already has an unknown quantity: a no-op is rejected.
        self.assertEqual(response.status_code,409)
        set_qty=self.action(dict(operation='revise_reference',reference_id='REF-001',quantity=2),request='quantity')
        self.assertEqual(set_qty.status_code,202,set_qty.text)
        s=self.app.state.store;run=set_qty.json()['run'];s.finish(run['id'],'completed')
        cleared=self.action(dict(operation='revise_reference',reference_id='REF-001',quantity=None),request='clear-again')
        self.assertEqual(cleared.status_code,202,cleared.text)
        self.assertIsNone(cleared.json()['project']['document']['device_references'][0]['quantity'])

    def test_queued_edit_does_not_pretend_to_be_applied(self):
        s=self.app.state.store;run,_=s.begin_run(self.pid,'先比较当前电源方案','comparison');s.mark_running(run['id'])
        result=self.action(dict(operation='revise_reference',reference_id='REF-001',mention='ME6211C33M5G-N'))
        self.assertTrue(result.json()['queued'])
        self.assertEqual(result.json()['project']['document']['device_references'][0]['mention'],'TPS5450DDAR')
        s.finish(run['id'],'completed');following=s.advance(run['id'])
        self.assertIsNotNone(following)
        self.assertEqual(s.raw_document(self.pid)['device_references'][0]['mention'],'ME6211C33M5G-N')

    def test_customer_explicit_reference_is_not_an_adoption_question(self):
        from backend.delivery import review
        p=self.action(dict(operation='set_intent',reference_id='REF-001',intent='reference')).json()['project']
        self.assertEqual(bom_view(p['document'])['current_rows'],[])
        self.assertEqual(review(p['document'])['reference_adoption_pending'],0)
