import base64,copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from backend.store import Store
from backend.source_claims import source_regions,exclusive_claims
from backend.formatting import constraint_value,metric_key
from backend.decisions import reconcile_requirements
from backend.reference_design import parse_reference,reference_message,bom_view,prompt_reference
from backend.delivery import review as delivery_review
from backend.domain import empty_document

class PriorityTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019');self.addCleanup(self.temp.cleanup);self.s=Store(Path(self.temp.name)/'s.sqlite3');self.p=self.s.create_project()['id']
 def internal(self):
  with self.s.connect() as db:return self.s._begin_run(db,self.p,'','internal',kind='continuation',message_id='seed',task='check')[0]
 def test_customer_input_replaces_only_unstarted_internal_task(self):
  old=self.internal();run,created,q=self.s.submit_message(self.p,'只需要一个按键','reply')
  self.assertTrue(created);self.assertIsNone(q);self.assertEqual(self.s.get_run(old['id'])['status'],'cancelled');self.assertFalse(self.s.mark_running(old['id']))
  self.assertEqual(self.s.get_run(run['id'])['kind'],'user');self.assertEqual(self.s.get_project(self.p)['messages'][-1]['content'],'只需要一个按键')
 def test_started_internal_result_is_preserved_then_customer_runs_once(self):
  old=self.internal();self.assertTrue(self.s.mark_running(old['id']))
  _,_,q=self.s.submit_message(self.p,'用一节电池','reply');self.assertIsNotNone(q)
  self.s.finish(old['id'],'completed');next_run=self.s.advance(old['id'],'more internal')
  self.assertEqual(next_run['kind'],'user');self.assertIsNone(self.s.advance(old['id'],'more internal'))
  self.assertEqual(sum(m['content']=='用一节电池' for m in self.s.get_project(self.p)['messages']),1)

class ScopeAndDependencies(unittest.TestCase):
 def test_board_rails_remain_goals_and_chip_rating_does_not(self):
  text='由两路TPS5450芯片（5A额定输出）和两路ME6211C33 LDO组成多级电源管理网络（提供24V/12V/5V/3.3V多电压轨）。'
  result=source_regions(text);self.assertIn('提供24V/12V/5V/3.3V多电压轨',result['main_text']);self.assertNotIn('5A额定输出',result['main_text'])
  self.assertEqual([r['mention'] for r in result['device_descriptions']],['TPS5450'])
 def test_internal_chip_rail_is_still_description(self):
  self.assertNotIn('1.2V',source_regions('采用RT9013 LDO（1.2V内部电压轨）。')['main_text'])
 def test_shared_must_have_index_does_not_reopen_unrelated_functions(self):
  before=empty_document();before.update(guidance_version=1,requirements=[{'id':'REQ-001','spec_field_ids':['V3-2.1.1:F02']},{'id':'REQ-002','spec_field_ids':['V3-2.1.1:F02']}],questions=[{'id':'q','requirement_ids':['REQ-001'],'field_ids':['V3-2.1.1:F02']}],field_resolutions=[{'field_id':'V3-2.1.1:F02','requirement_ids':['REQ-001','REQ-002']}])
  doc=copy.deepcopy(before);result=SimpleNamespace(guidance_revision=1,reviewed_requirement_ids=[],updates=[])
  reconcile_requirements(doc,before,result,{'q'},set(),{},'reply');self.assertEqual([r['requirement_id'] for r in doc['decision_reviews']],['REQ-001'])
 def test_specific_power_contract_still_rechecks_related_requirement(self):
  before=empty_document();before.update(guidance_version=1,requirements=[{'id':'REQ-001','spec_field_ids':['V3-3.1.1:F01']},{'id':'REQ-002','spec_field_ids':['V3-3.1.1:F01']}],questions=[{'id':'q','requirement_ids':['REQ-001'],'field_ids':['V3-3.1.1:F01']}])
  doc=copy.deepcopy(before);result=SimpleNamespace(guidance_revision=1,reviewed_requirement_ids=['REQ-001'],updates=[])
  reconcile_requirements(doc,before,result,{'q'},set(),{},'reply');self.assertEqual([r['requirement_id'] for r in doc['decision_reviews']],['REQ-002'])
 def test_rf_frequency_has_correct_quantity_and_unit_once(self):
  r={'field_key':'switching_frequency','text':'RFID/NFC卡片读写工作频率为13.56。','minimum':13.56,'maximum':13.56,'unit':'MHz'}
  self.assertEqual(metric_key(r),'rf_frequency');self.assertIn('13.56 MHz',constraint_value(r));r['text']='NFC工作频率13.56MHz';self.assertEqual(constraint_value(r),r['text'])

class References(unittest.TestCase):
 def fixture(self):return {'version':'2','components':{'a':{'props':{'Designator':'U1','DeviceName':'CX588'},'pinInfoMap':{'1':{'name':'GND','net':'GND'}}},'b':{'props':{'Designator':'R1','DeviceName':'10k'}},'c':{'props':{'Designator':'J1','DeviceName':'M2螺丝'}}}}
 def source(self):return parse_reference('reference.enet',base64.b64encode(json.dumps(self.fixture()).encode()).decode())
 def test_actual_components_are_grouped_without_turning_screws_into_core_ics(self):
  s=self.source();self.assertEqual(s['instance_count'],3);self.assertEqual([p['model'] for p in s['bom'] if p['core']],['CX588']);self.assertEqual(s['bom'][0]['pins'][0]['net'],'GND')
  self.assertNotIn('original_content_base64',prompt_reference(s))
 def test_csv_quoted_designators_and_real_counts(self):
  s=parse_reference('bom.csv',base64.b64encode('位号,名称,封装,功能,数量\n"BAT1,SPK1",CONN,2P,连接器,2\n'.encode()).decode());self.assertEqual(s['instance_count'],2);self.assertEqual(s['bom'][0]['observed_quantity'],2)
 def test_same_model_connectors_keep_independent_pin_owners_and_real_signal_links(self):
  from backend.reference_design import qualified_reference
  data={'components':{'u':{'props':{'Designator':'U1','DeviceName':'MCU1'},'pinInfoMap':{'1':{'name':'RX','net':'RX'},'2':{'name':'GND','net':'GND'}}},'h1':{'props':{'Designator':'H1','DeviceName':'CONN2'},'pinInfoMap':{'1':{'name':'1','net':'RX'},'2':{'name':'2','net':'GND'}}},'h2':{'props':{'Designator':'H2','DeviceName':'CONN2'},'pinInfoMap':{'1':{'name':'1','net':'SPK'},'2':{'name':'2','net':'GND'}}}}}
  s=parse_reference('ref.enet',base64.b64encode(json.dumps(data).encode()).decode());connector=next(p for p in s['bom'] if p['model']=='CONN2');self.assertEqual(connector['observed_quantity'],2)
  self.assertEqual({p['designator'] for p in connector['pins']},{'H1','H2'})
  for part in s['bom']:
   for p in part['pins']:p.pop('designator');p.pop('instance')
  old=copy.deepcopy(s);projected=prompt_reference(s);rx=next(r for r in projected['signal_connections'] if r['net']=='RX');self.assertEqual({n['designator'] for n in rx['endpoints']},{'U1','H1'});self.assertFalse(any(n['net']=='GND' for n in projected['signal_connections']));self.assertEqual(s,old)
 def test_capacitor_coupled_paths_are_not_lost_by_core_only_same_net_summary(self):
  data={'components':{ref:{'props':{'Designator':ref,'DeviceName':model},'pinInfoMap':{str(i):{'name':str(i),'net':net} for i,net in enumerate(nets,1)}} for ref,model,nets in [('U1','AUDIO1',['MIC_OUT','GND']),('U2','CODEC2',['ADC_IN','GND']),('C1','1uF',['MIC_OUT','ADC_IN']),('C2','1uF',['OTHER','GND'])]}}
  source=parse_reference('coupled.enet',base64.b64encode(json.dumps(data).encode()).decode());p=prompt_reference(source)
  self.assertFalse(p['signal_connections']);caps={r['designator']:r for r in p['passive_connections']};self.assertEqual(set(caps),{'C1','C2'});self.assertEqual({t['net'] for t in caps['C1']['terminals']},{'MIC_OUT','ADC_IN'});self.assertFalse(p['passive_connections_truncated']);self.assertIn('不等于直流导通',p['topology_note'])
 def test_duplicate_designator_does_not_hide_quantity_ambiguity(self):
  d=self.fixture();d['components']['b']['props']['Designator']='U1';s=parse_reference('ref.enet',base64.b64encode(json.dumps(d).encode()).decode());self.assertTrue(s['warnings'])
 def test_pcb_layout_json_is_not_a_netlist(self):
  with self.assertRaises(ValueError):parse_reference('ref.json',base64.b64encode(b'{"boardOutline":{}}').decode())
 def test_malformed_component_properties_is_an_input_error(self):
  with self.assertRaisesRegex(ValueError,'属性'):
   parse_reference('ref.enet',base64.b64encode(b'{"components":{"U1":{"props":[]}}}').decode())
 def test_attachment_is_real_user_reference_not_a_silent_must_choice(self):
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   store=Store(Path(d)/'s.sqlite3');pid=store.create_project()['id'];s=self.source()
   run,_,_=store.submit_message(pid,reference_message(s),'file',actions=[{'operation':'attach_reference','source':s}]);p=store.get_project(pid);r=p['document']['device_references'][0]
   self.assertEqual(r['intent'],'reference');self.assertIsNone(r['quantity']);self.assertIn(r['source_quote'],p['messages'][0]['content']);self.assertEqual(p['document']['reference_designs'][0]['sha256'],s['sha256'])
   self.assertNotIn('original_content_base64',json.dumps(prompt_reference(s)));self.assertEqual(bom_view(p['document'])['core_adopted'],0)
 def test_missing_new_handoff_checks_cannot_be_called_complete(self):
  doc=empty_document();self.assertFalse(delivery_review(doc)['enabled']);doc['delivery_policy_version']=1;self.assertEqual(len(delivery_review(doc)['rechecks']),4)

class CompactDocument(unittest.TestCase):
 def test_compact_reading_retains_diagrams_and_full_export_keeps_unknown_fields(self):
  from tests.test_product_refactor import ProductRefactorTests
  from backend.document_export import render_project_document
  f=ProductRefactorTests();f.setUp();before=copy.deepcopy(f.doc)
  spec=render_project_document(f.doc,structured=True)
  self.assertIn('系统框图草案',spec['compact_tail_markdown']);self.assertIn('```mermaid',spec['compact_tail_markdown'])
  self.assertLess(len(spec['compact_tail_markdown']),len(spec['tail_markdown']))
  self.assertIn('待设计评估',spec['tail_markdown']);self.assertEqual(spec['markdown'],render_project_document(f.doc));self.assertEqual(f.doc,before)
 def test_functional_acceptance_is_not_lost_by_category_in_handoff(self):
  from tests.phase3_fixtures import seed
  from backend.handoff import build_spec
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   s=Store(Path(d)/'s.sqlite3');p=seed(s);doc=p['document'];doc['field_resolutions'].append({**doc['field_resolutions'][0],'field_id':'V3-4.2.1:F04'})
   spec=build_spec(p);self.assertEqual(spec['acceptance_criteria'][0]['requirement_id'],'REQ-001');self.assertEqual(spec['acceptance_criteria'][0]['criterion'],doc['requirements'][0]['value'])

class ReferenceApi(unittest.TestCase):
 def test_invalid_attachment_is_422_and_repeat_upload_is_idempotent(self):
  from fastapi.testclient import TestClient
  from backend.app import create_app
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   app=create_app(Path(d));app.state.runner.start=lambda _:None
   with TestClient(app) as client:
    pid=app.state.store.create_project()['id'];url='/api/projects/'+pid+'/reference-designs'
    malformed=base64.b64encode(b'{"components":{"U1":{"props":[]}}}').decode()
    response=client.post(url,json={'filename':'broken.enet','content_base64':malformed,'request_id':'bad'})
    self.assertEqual(response.status_code,422);self.assertIn('属性',response.json()['detail']);self.assertEqual(app.state.store.get_project(pid)['messages'],[])
    content=base64.b64encode(json.dumps(References().fixture()).encode()).decode()
    for request in ('first','second'):
     response=client.post(url,json={'filename':'reference.enet','content_base64':content,'request_id':request});self.assertEqual(response.status_code,202)
    p=app.state.store.get_project(pid);self.assertEqual(len(p['messages']),1);self.assertEqual(len(p['document']['reference_designs']),1)
    source=p['document']['reference_designs'][0];original=client.get(url+'/'+source['id']);self.assertEqual(original.content,base64.b64decode(content))

 def test_stale_plan_cannot_be_adopted_inside_the_transaction(self):
  from backend.store import Conflict
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   s=Store(Path(d)/'s.sqlite3');pid=s.create_project()['id'];doc=s.raw_document(pid)
   with s.connect() as db:
    with self.assertRaisesRegex(Conflict,'已更新'):s.apply_actions(db,pid,doc,'reply','采用',[{'operation':'adopt_reference_core','source_id':'x','base_version':doc['version']-1}])

class IncrementalOutput(unittest.TestCase):
 def test_duplicate_interface_field_feedback_names_the_field_and_preserves_rejection(self):
  from backend.convergence import apply,FieldResolution
  doc=empty_document();row=FieldResolution(field_ids=['V3-5.3:F01'],state='tbd',conclusion='接口分工尚待整理',requirement_ids=[],source_message_id='',source_quote='')
  result=SimpleNamespace(analysis_revision=2,field_resolutions=[row,row])
  with self.assertRaisesRegex(ValueError,'重复：V3-5.3:F01'):
   apply(doc,result,copy.deepcopy(doc),[],{},set())
 def fields(self,requirements=('REQ-001','REQ-002')):
  from backend.convergence import FieldResolution
  return [FieldResolution(field_ids=['V3-5.3:F01'],state='confirmed',conclusion=text,requirement_ids=[rid],source_message_id=mid,source_quote=quote) for text,rid,mid,quote in [('通过Wi-Fi联网',requirements[0],'wifi','直接连Wi-Fi'),('USB-C用于充电',requirements[1],'power','USB-C充电')]]
 def test_independent_interface_summary_entries_merge_with_both_requirements(self):
  from backend.incremental_output import normalize_interface_summaries
  r=SimpleNamespace(field_resolutions=self.fields());notes=normalize_interface_summaries(r)
  self.assertEqual(len(r.field_resolutions),1);self.assertEqual(r.field_resolutions[0].requirement_ids,['REQ-001','REQ-002']);self.assertIn('Wi-Fi',r.field_resolutions[0].conclusion);self.assertIn('USB-C',r.field_resolutions[0].conclusion);self.assertEqual(len(notes[0]['sources']),2)
 def test_competing_same_endpoint_and_product_fields_are_not_merged(self):
  from backend.incremental_output import normalize_interface_summaries
  r=SimpleNamespace(field_resolutions=self.fields(('REQ-001','REQ-001')));self.assertEqual(normalize_interface_summaries(r),[]);self.assertEqual(len(r.field_resolutions),2)
  r=SimpleNamespace(field_resolutions=self.fields())
  for f in r.field_resolutions:f.field_ids=['V3-3.1.1:F01']
  self.assertEqual(normalize_interface_summaries(r),[])
 def update(self,quote='用一节干电池',**changes):
  from backend.device_domain import DeviceReferenceUpdate,apply_updates
  ref=dict(id='REF-001',mention='CX588',intent='reference',identity_scope='exact_part',purpose='播放候选',requirement_ids=['REQ-001'],module_ids=['MOD-001'],quantity=1,source_message_id='file',source_quote='参考CX588',lifecycle='active')
  doc=empty_document();doc.update(requirements=[{'id':'REQ-001','state':'stated'}],modules=[{'id':'MOD-001','type_id':'P_1_1','name':'供电转换','purpose':'为电路供电','boundary':'参数待设计','quantity':1,'requirement_ids':['REQ-001'],'state':'proposed'}],device_references=[copy.deepcopy(ref)])
  update={k:v for k,v in ref.items() if k in DeviceReferenceUpdate.model_fields};update.update(source_message_id='battery',source_quote=quote,purpose='结合干电池方案核对供电',**changes)
  result=SimpleNamespace(device_reference_updates=[DeviceReferenceUpdate(**update)],remove_device_reference_ids=[],device_constraints=[])
  apply_updates(doc,result,copy.deepcopy(doc),[{'id':'file','role':'user','content':'参考CX588'},{'id':'battery','role':'user','content':quote}],{},{});return doc['device_references'][0]
 def test_functional_update_keeps_part_identity_and_new_association_basis(self):
  r=self.update();self.assertEqual(r['source_quote'],'参考CX588');self.assertEqual(r['quantity'],1);self.assertEqual(r['intent'],'reference');self.assertEqual(r['association_source']['source_quote'],'用一节干电池');self.assertIn('干电池',r['purpose'])
 def test_association_path_cannot_change_part_intent_or_quantity(self):
  for change in ({'intent':'must'},{'quantity':2}):
   with self.assertRaisesRegex(ValueError,'型号必须逐字'):self.update(**change)
 def test_not_asking_customer_to_lookup_models_is_not_a_part_intent_change(self):
  r=self.update(quote='用外接小喇叭，开发时配好，我不想自己查型号。');self.assertEqual(r['intent'],'reference');self.assertIn('不想自己查型号',r['association_source']['source_quote'])

class ReadingWindow(unittest.IsolatedAsyncioTestCase):
 async def test_customer_replaces_internal_run_before_any_inference_slot_is_used(self):
  import asyncio
  from unittest.mock import patch
  from backend.codex import Runner
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   s=Store(Path(d)/'s.sqlite3');p=s.create_project()['id'];doc=s.raw_document(p);doc['questions']=[{'id':'q','question':'怎样供电？'}]
   with s.connect() as db:
    db.execute('UPDATE projects SET document=? WHERE id=?',(json.dumps(doc),p));old=s._begin_run(db,p,'','internal',kind='continuation',message_id='seed',task='check')[0]
   runner=Runner(s,Path(d));entered=asyncio.Event();release=asyncio.Event()
   async def window(_):entered.set();await release.wait()
   with patch('backend.codex.asyncio.sleep',side_effect=window):
    task=asyncio.create_task(runner._run(old['id']));await asyncio.wait_for(entered.wait(),5)
    self.assertEqual(runner.limit._value,2);run,_,q=s.submit_message(p,'用USB供电','customer');self.assertIsNone(q);release.set();await asyncio.wait_for(task,5)
   self.assertEqual(s.get_run(old['id'])['status'],'cancelled');self.assertEqual(s.get_run(run['id'])['kind'],'user');self.assertFalse(runner.processes)
   await runner.close()

class FinalReferenceReview(unittest.TestCase):
 def test_ready_reference_product_waits_for_real_review_and_changes_invalidate_it(self):
  from tests.phase3_fixtures import seed
  from backend.coverage import review_document
  from backend.reference_audit import record,pending,TASK
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   s=Store(Path(d)/'s.sqlite3');doc=seed(s)['document'];doc['delivery_policy_version']=1;doc['reference_designs']=[References().source()]
   keys=['scope_acceptance','capability_chain','interfaces_power','core_parts'];doc['delivery_checks']=[dict(key=k,state='complete',conclusion='职责已整理',next_action='',requirement_ids=['REQ-001']) for k in keys]
   review=review_document(doc);self.assertTrue(review['reference_audit_pending']);self.assertEqual(review['status'],'draft')
   run={'id':'real-review-run','kind':'continuation','continuation_task':TASK+'核对实际参考'}
   with self.assertRaises(ValueError):record(doc,SimpleNamespace(delivery_checks=[]),run)
   self.assertNotIn('reference_audit',doc)
   record(doc,SimpleNamespace(delivery_checks=[SimpleNamespace(key=k) for k in keys]),run);self.assertFalse(pending(doc));self.assertEqual(review_document(doc)['status'],'ready')
   doc['updated_at']='different saved time';self.assertFalse(pending(doc));doc['requirements'][0]['value']='更换为电池供电';self.assertTrue(pending(doc));self.assertEqual(review_document(doc)['status'],'draft')

class StableIntakeLineage(unittest.TestCase):
 def test_incremental_engineering_update_retains_valid_checkpoint_lineage(self):
  from backend.intake import IntakeCheck,apply_checks
  doc=empty_document();doc['requirements']=[{'id':'REQ-001','state':'stated'}];doc['intake_checks']=[{'key':'external_interfaces','state':'confirmed','requirement_ids':['REQ-001'],'conclusion':'无用户数据接口','source_message_id':'initial','source_quote':'做个发声玩具'}]
  original=copy.deepcopy(doc);result=SimpleNamespace(intake_policy_revision=1,intake_checks=[IntakeCheck(key='external_interfaces',state='design_stage',conclusion='维护接口参数留设计核定',requirement_ids=[],source_message_id='adoption',source_quote='采用参考维护接口')]);messages=[{'id':'adoption','role':'user','content':'采用参考维护接口'}]
  apply_checks(doc,original,result,messages,{},set());self.assertEqual(doc['intake_checks'][0]['requirement_ids'],['REQ-001']);self.assertEqual(doc['intake_checks'][0]['state'],'design_stage')
  original['intake_checks']=[]
  with self.assertRaisesRegex(ValueError,'requirement_ids为空'):apply_checks(copy.deepcopy(doc),original,result,messages,{},set())
  original=copy.deepcopy(doc);removed=copy.deepcopy(doc);removed['requirements']=[]
  with self.assertRaises(ValueError):apply_checks(removed,original,result,messages,{},set())
 def test_waiting_module_plan_is_a_customer_decision_not_idle_ai_work(self):
  from tests.phase3_fixtures import seed
  from backend.delivery import view
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   p=seed(Store(Path(d)/'s.sqlite3'));p['convergence']['module_pending']=['MOD-001'];p['review']['status']='draft';p['review']['internal_rechecks']=0
   state=view(p);self.assertEqual(state['next_owner'],'user');self.assertEqual(state['user_decisions'],1);self.assertIn('模块方案',state['next_label'])
 def test_failed_internal_run_never_looks_like_executing_ai_work(self):
  from tests.phase3_fixtures import seed
  from backend.delivery import view
  with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.runtime/ux-optimization-20261019') as d:
   p=seed(Store(Path(d)/'s.sqlite3'));p['latest_run']={'status':'failed'};p['review']['internal_rechecks']=4
   state=view(p);self.assertEqual(state['next_owner'],'user');self.assertIn('重试',state['next_label']);self.assertEqual(state['internal_tasks'],4)
