import copy
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from backend.store import Store
from backend.domain import AnalysisResult,apply_analysis
from backend.convergence import FIELDS,prompt_document
from backend.model_context import compact,expand_sources,normalize_abstract_ports
from backend.module_catalog import TYPES
from backend.engineering_checks import quoted_quantity,evaluate_constraint
from backend.handoff import generate_archive,verify_archive
from backend.domain_service import requested_references
from phase3_fixtures import seed


class CatalogHealthTests(unittest.TestCase):
 def test_identity_diagnostic_does_not_disable_a_working_catalog_on_union_temp_exhaustion(self):
  from contextlib import contextmanager
  from backend.knowledge import Catalog,KnowledgeUnavailable
  catalog=Catalog()
  @contextmanager
  def connection(source=False):yield 'source' if source else 'publication'
  def query(db,sql,params=()):
   if db=='source':return [{'device_id':i,'device_name':'PART-'+str(i)} for i in (1,2,3)]
   if 'dsearch_v_device_search_published_incremental' in sql:
    raise KnowledgeUnavailable('模拟MySQL联合视图临时表已满，但精确查询仍可用')
   if 'dsearch_v_device_search_active' in sql:return [{'device_id':i,'part_number_raw':'PART-'+str(i)} for i in (1,2)]
   if 'drelease_search_document' in sql:
    self.assertIn("r.release_status='PUBLISHED'",sql);self.assertIn("s.status='PUBLISHED'",sql)
    return [{'device_id':3,'part_number_raw':'PART-3'}]
   if 'dmanual_device_ingest_record' in sql:return [{'count':2}]
   if 'drelease_incremental_release' in sql:return [{'release_id':7,'release_status':'PUBLISHED'}]
   raise AssertionError('unexpected diagnostic query')
  with patch.object(catalog,'connection',connection),patch.object(catalog,'query',query):result=catalog.status(True)
  self.assertTrue(result['available']);self.assertTrue(result['read_only'])
  self.assertEqual(result['published_devices'],3);self.assertEqual(result['manual_records'],2)
  self.assertEqual(result['freshness'],'identity_fresh');self.assertFalse(result['errors'])


class IterationRegressions(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.runtime/phase3');self.store=Store(Path(self.temp.name)/'studio.sqlite3');self.p=seed(self.store)
 def tearDown(self):self.temp.cleanup()
 def result(self,**kwargs):
  values=dict(reply='已核对',title=self.p['title'],summary=self.p['document']['summary'],updates=[],remove_ids=[],questions=[],flow=[],changes=['核对'],analysis_revision=3)
  values.update(kwargs);return AnalysisResult.model_validate(values)
 def test_pending_device_choices_survive_reload_and_deduplicate_new_request_ids(self):
  pid=self.p['id'];action={'operation':'set_intent','reference_id':'REF-001','intent':'prefer'}
  run,created,_=self.store.submit_message(pid,'明确改为优先', 'intent-one',actions=[action])
  p=self.store.get_project(pid)
  self.assertEqual(p['reference_submissions'][0]['reference_id'],'REF-001')
  replay,created,item=self.store.submit_message(pid,'明确改为优先','intent-after-reload',actions=[action])
  self.assertEqual(replay['id'],run['id']);self.assertFalse(created);self.assertIsNone(item)
  self.assertEqual(len(self.store.get_project(pid)['messages']),2)
  other={**action,'intent':'reference'}
  _,_,queued=self.store.submit_message(pid,'仅供参考','queued-one',actions=[other])
  _,created,again=self.store.submit_message(pid,'仅供参考','queued-after-reload',actions=[other])
  self.assertEqual(again['id'],queued['id']);self.assertFalse(created)
  self.assertEqual([a['status'] for a in self.store.get_project(pid)['reference_submissions']],['processing','queued'])
  self.store.remove_queued(pid,queued['id']);self.store.finish(run['id'],'cancelled','停止分析，明确选择仍已保存')
  p=self.store.get_project(pid);self.assertFalse(p['reference_submissions'])
  self.assertEqual(p['document']['device_references'][0]['intent'],'prefer')

 def test_new_input_recovers_a_storage_failure_without_stranding_the_queue(self):
  from backend.app import create_app
  from fastapi.testclient import TestClient
  for operation in ('message','reference','resume'):
   app=create_app(Path(self.temp.name)/('storage-'+operation))
   with TestClient(app,raise_server_exceptions=False) as client:
    store=app.state.store;runner=app.state.runner;p=seed(store);pid=p['id'];calls=[]
    old,_=store.begin_run(pid,'此前输入','old');store.mark_running(old['id'])
    runner.failed_runs[old['id']]='模拟存储故障，现已恢复'
    async def complete(run_id):
     calls.append(run_id);store.mark_running(run_id);store.finish(run_id,'cancelled','受控恢复验证')
    runner._run=complete
    if operation=='message':
     response=client.post(f'/api/projects/{pid}/messages',json={'text':'恢复后新输入','request_id':'new'})
    elif operation=='reference':
     response=client.post(f'/api/projects/{pid}/device-references',json={'base_version':p['document']['version'],'request_id':'new',
       'action':{'operation':'set_intent','reference_id':'REF-001','intent':'prefer'}})
    else:
     store.submit_message(pid,'已经排队的补充','queued')
     response=client.post(f'/api/projects/{pid}/queue/resume')
    self.assertEqual(response.status_code,202,response.text)
    result=client.get(f'/api/projects/{pid}').json()
    self.assertEqual(store.get_run(old['id'])['status'],'failed')
    self.assertEqual(len(calls),1);self.assertFalse(result['input_queue'])
    self.assertEqual(result['latest_run']['status'],'cancelled')

 def test_storage_failure_overlay_also_releases_device_and_question_receipts(self):
  from backend.codex import Runner
  pid=self.p['id'];doc=self.store.raw_document(pid)
  doc['questions']=[{'id':'controlled-question','question':'优先还是参考？','reason':'受控问题','options':['优先','参考'],
    'requirement_ids':['REQ-001'],'decision_key':'controlled_intent','status':'pending','priority':1}]
  with self.store.connect() as db:self.store._save(db,pid,doc,doc['version']+1)
  run,_,_=self.store.submit_message(pid,'明确优先考虑', 'receipt-overlay',
    answers=[{'question_id':'controlled-question','answer':'优先'}],
    actions=[{'operation':'set_intent','reference_id':'REF-001','intent':'prefer'}])
  self.store.mark_running(run['id'])
  runner=Runner(self.store,Path(self.temp.name)/'overlay');runner.failed_runs[run['id']]='受控写入故障'
  project=runner.project(pid)
  self.assertEqual(project['latest_run']['status'],'failed');self.assertFalse(project['reference_submissions'])
  self.assertEqual(project['question_submissions'][0]['status'],'failed')
  # GET is a truthful view and performs no status mutation until explicit input.
  self.assertEqual(self.store.get_run(run['id'])['status'],'running')
 def test_failed_latest_input_cannot_approve_or_handoff_the_previous_document(self):
  from backend.store import Conflict
  from backend.handoff import readiness
  for status in ('failed','cancelled','interrupted'):
   p=seed(self.store);p=self.store.approve(p['id'],p['document']['version'],p['domain_revision'])
   run,_=self.store.begin_run(p['id'],'请结合新增使用条件重新整理','pending-'+status)
   self.store.finish(run['id'],status,'受控未完成')
   current=self.store.get_project(p['id'])
   self.assertEqual(current['review']['status'],'draft');self.assertTrue(current['analysis_pending'])
   self.assertIn('最新补充尚未完成整理',current['markdown'])
   self.assertEqual(readiness(current)['status'],'blocked')
   with self.assertRaisesRegex(Conflict,'最新补充'):self.store.approve(p['id'],current['document']['version'],current['domain_revision'])
   retry,_=self.store.begin_run(p['id'],'','retry-'+status,retry=True)
   self.store.commit_analysis(retry['id'],copy.deepcopy(current['document']),'已结合补充完成受控整理')
   current=self.store.get_project(p['id']);self.assertFalse(current['analysis_pending']);self.assertEqual(current['review']['status'],'ready')
 def test_handoff_cannot_miss_new_input_that_has_not_yet_changed_document_version(self):
  import backend.app as module
  from fastapi.testclient import TestClient
  app=module.create_app(Path(self.temp.name)/'input-handoff-race')
  with TestClient(app) as client:
   store=app.state.store;p=seed(store);p=store.approve(p['id'],p['document']['version'],p['domain_revision']);original=module.generate_archive
   def add_input_after_snapshot(project,db):
    output=original(project,db);store.begin_run(p['id'],'新增加一个产品要求','racing-user-input');return output
   with patch.object(module,'generate_archive',side_effect=add_input_after_snapshot):
    response=client.post(f'/api/projects/{p["id"]}/handoff',json={'base_version':p['document']['version'],'base_domain_revision':p['domain_revision'],'request_id':'handoff-input-race'})
   self.assertEqual(response.status_code,409,response.text)
   with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM handoff_artifacts').fetchone()[0],0)
 def test_full_field_and_module_catalog_survives_compact_context(self):
  source={'current_document':prompt_document(self.p['document']),'coverage_review':{'status':'draft'},'messages':self.p['messages']}
  before=copy.deepcopy(source);result=compact(source)
  self.assertEqual({r[0] for t in result['field_catalog'] for r in t['fields']},set(FIELDS))
  self.assertEqual({r['id'] for r in result['module_catalog']['parents']}|{c[0] for p in result['module_catalog']['parents'] for c in p['children']},set(TYPES))
  self.assertEqual([m['content'] for m in result['messages']],[m['content'] for m in source['messages']]);self.assertEqual(source,before)
  self.assertEqual(result['message_aliases'][result['messages'][0]['id']],source['messages'][0]['id'])
  quotes=result['evidence_quote_table'];ref=result['current_document']['device_references'][0]
  self.assertEqual(quotes[ref['source_quote_ref']],self.p['document']['device_references'][0]['source_quote'])
 def test_new_product_question_is_not_answered_by_a_broad_field_state(self):
  d=self.p['document'];q={'id':'content-policy','question':'两段声音同时触发时怎么处理？','reason':'决定实际播放行为','options':[],
                         'requirement_ids':['REQ-001'],'field_ids':['V3-2.1.1:F03'],'decision_key':'voice_overlap'}
  doc=apply_analysis(d,self.result(questions=[q]),self.p['messages'],self.p['messages'][0]['id'])
  self.assertIn('content-policy',[q['id'] for q in doc['questions']])
 def test_metadata_rename_keeps_approval_and_electrical_evidence(self):
  approved=self.store.approve(self.p['id'],self.p['document']['version'],self.p['domain_revision'])
  renamed=self.store.rename(self.p['id'],'新显示名称',approved['document']['version'])
  self.assertEqual(renamed['review']['status'],'approved');self.assertEqual(renamed['domain_revision'],approved['domain_revision'])
  self.assertEqual(renamed['document']['device_references'],approved['document']['device_references'])
 def test_gpfs_rollback_mode_and_concurrent_reads(self):
  with self.store.connect() as db:self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
  with ThreadPoolExecutor(max_workers=8) as pool:
   results=list(pool.map(lambda _:self.store.get_project(self.p['id'])['document']['version'],range(64)))
  self.assertEqual(set(results),{self.p['document']['version']})
 def test_part_counts_and_model_digits_cannot_become_current_limits(self):
  for quote in ('采用ESP32-C3，9路继电器。','采用9片驱动芯片，封装32脚。'):
   self.assertFalse(quoted_quantity(9,'A',quote));self.assertFalse(quoted_quantity(32,'V',quote))
  self.assertTrue(quoted_quantity(.9,'V','输入0.9~1.5V。'))
  self.assertTrue(quoted_quantity(24,'bit','采用24位ADC。'))
 def test_board_supply_and_output_pin_cannot_be_treated_as_chip_vin(self):
  ref=self.p['document']['device_references'][0]
  evidence=[{'id':'EV-controlled-port','assertions':[{'field_key':'input_voltage_range','validation_state':'verified_for_claim',
    'normalized':{'minimum':'5.5','maximum':'36','dimension':'voltage','unit':'V'},'conditions':'受控工作范围证据',
    'rating_kind':'recommended_operating','is_missing':False}]}]
  c={'id':'DC-1','requirement_id':'REQ-001','field_key':'input_voltage','minimum':5,'maximum':5,'unit':'V',
     'relation':'covers','kind':'design_requirement','port':'VIN','source_quote':'TPS5450DDAR VIN直接输入5V'}
  self.assertTrue(evaluate_constraint(c,ref,evidence,'test')['blocking'])
  for port,quote in [('VOUT','TPS5450DDAR VOUT输出5V'),('整板USB','整板USB输入5V，使用TPS5450DDAR'),('VIN','整板输入5V，使用TPS5450DDAR，连接尚未确定')]:
   result=evaluate_constraint({**c,'port':port,'source_quote':quote},ref,evidence,'test')
   self.assertEqual(result['status'],'unknown');self.assertFalse(result['blocking']);self.assertTrue(result['missing'])
 def test_minutes_cannot_leave_a_complete_motor_case_stuck_in_unit_review(self):
  from backend.convergence import consistency_issues,normalize_number
  row={'id':'NC-time','requirement_id':'REQ-001','metric':'runtime','scope':'台架连续运行时长','conditions':'25℃受控散热',
       'minimum':10,'maximum':10,'unit':'min'}
  doc={'requirements':[{'id':'REQ-001','state':'stated'}],'numeric_constraints':[row]}
  self.assertEqual(normalize_number(row,'minimum'),600);self.assertFalse(consistency_issues(doc))
  for unit in ('分钟','minute','minutes'):self.assertEqual(normalize_number({**row,'unit':unit},'minimum'),600)
  doc['numeric_constraints'].append({**row,'id':'NC-upper','minimum':None,'maximum':599,'unit':'s'})
  self.assertTrue(any(i['kind']=='conflict' for i in consistency_issues(doc)))
  doc['numeric_constraints']=[{**row,'unit':'m'}]
  self.assertTrue(any(i['kind']=='review' for i in consistency_issues(doc)))
 def test_ups_kilowatts_and_kelvin_are_compared_in_the_correct_dimension(self):
  from backend.convergence import consistency_issues,normalize_number
  from backend.engineering_checks import parse_value
  row={'id':'NC-power','requirement_id':'REQ-001','metric':'power','scope':'UPS连续输出','conditions':'同一负载',
       'minimum':2,'maximum':None,'unit':'kW'}
  doc={'requirements':[{'id':'REQ-001','state':'stated'}],'numeric_constraints':[row]}
  self.assertFalse(consistency_issues(doc));self.assertEqual(normalize_number(row,'minimum'),2000)
  self.assertEqual(parse_value('2kW')['minimum'],'2000')
  doc['numeric_constraints'].append({**row,'id':'NC-limit','minimum':None,'maximum':1999,'unit':'W'})
  self.assertTrue(any(i['kind']=='conflict' for i in consistency_issues(doc)))
  self.assertAlmostEqual(normalize_number({**row,'metric':'temperature','unit':'K','minimum':273.15},'minimum'),0)
  self.assertIsNone(parse_value('10Vpp')) # Amplitude conventions cannot be silently collapsed.
 def test_recording_capacity_uses_only_explicit_shared_stream_bounds(self):
  from backend.convergence import consistency_issues
  base={'requirement_id':'REQ-001','scope':'日志记录链路','conditions':'每次存一条、无压缩','minimum':None,'maximum':None}
  rows=[{**base,'id':'size','metric':'record_size','minimum':32,'unit':'B'},
        {**base,'id':'period','metric':'sample_period','maximum':1,'unit':'s'},
        {**base,'id':'retention','metric':'retention','minimum':30,'unit':'d'},
        {**base,'id':'capacity','metric':'storage_capacity','maximum':64,'unit':'MiB'}]
  doc={'requirements':[{'id':'REQ-001','state':'stated'}],'numeric_constraints':rows}
  issues=consistency_issues(doc);self.assertTrue(any(i['id']=='storage-size' and '82944000' in i['description'] for i in issues))
  for replacement in ({**rows[0],'minimum':16},{**rows[0],'scope':'另一条记录链路'}):
   self.assertFalse(consistency_issues({**doc,'numeric_constraints':[replacement,*rows[1:]]}))
  self.assertFalse(consistency_issues({**doc,'numeric_constraints':rows[1:]}))
  self.assertFalse(consistency_issues({**doc,'numeric_constraints':[*rows[:-1],{**rows[-1],'minimum':64,'maximum':None}]}))
 def test_whole_archive_manifest_cannot_drop_a_hashed_file(self):
  _,data=generate_archive(self.p,self.store);target=io.BytesIO()
  with zipfile.ZipFile(io.BytesIO(data)) as a,zipfile.ZipFile(target,'w') as b:
   for name in a.namelist():
    payload=a.read(name)
    if name=='manifest.json':
     m=json.loads(payload);m['files'].pop('open-items.md');payload=json.dumps(m).encode()
    b.writestr(name,payload)
  with self.assertRaisesRegex(ValueError,'清单'):verify_archive(target.getvalue())
 def test_model_knowledge_request_must_target_a_real_reference(self):
  result=self.result(knowledge_requests=[{'reference_id':'REF-001','operation':'find_spans','topics':['VIN工作范围']}])
  rows=requested_references(result,self.p['document']);self.assertEqual(rows[0]['knowledge_topics'],['VIN工作范围'])
  bad=self.result(knowledge_requests=[{'reference_id':'REF-invented','operation':'lookup_identity','topics':[]}])
  with self.assertRaisesRegex(ValueError,'不存在'):requested_references(bad,self.p['document'])
 def test_short_model_sources_expand_to_exact_historical_user_text(self):
  mid=self.p['messages'][0]['id'];text=self.p['messages'][0]['content']
  result=self.result(updates=[{'id':'new-1','category':'overview','name':'使用场景','value':'室内实验室','unit':'','priority':'must','state':'stated','source_message_id':'M-001','source_quote':'@message'}])
  expand_sources(result,{'message_aliases':{'M-001':mid}}, {mid:text})
  self.assertEqual(result.updates[0].source_message_id,mid);self.assertEqual(result.updates[0].source_quote,text)
  doc=apply_analysis(self.p['document'],result,self.p['messages'],mid)
  self.assertTrue(any(r['name']=='使用场景' and r['source_quote']==text for r in doc['requirements']))
 def test_unknown_abstract_port_is_deferred_without_losing_the_functional_link(self):
  d=copy.deepcopy(self.p['document']);second={**d['modules'][0],'id':'MOD-002','name':'第二电源功能'};d['modules'].append(second)
  result=self.result(module_links=[{'id':'L1','source':'MOD-001','target':'MOD-002','kind':'power','source_port':'UNSUPPORTED_PHYSICAL_PIN',
   'target_port':'','description':'两个功能的供电关系','state':'proposed'}])
  notes=normalize_abstract_ports(result,d);self.assertEqual(len(notes),1);self.assertEqual(result.module_links[0].source_port,'')
  self.assertEqual(result.module_links[0].state,'design_stage');self.assertIn('供电关系',result.module_links[0].description)
 def test_shared_part_is_not_duplicated_in_downstream_physical_input(self):
  from backend.handoff import build_spec,generation_adapter
  p=copy.deepcopy(self.p);d=p['document'];d['modules'].append({**d['modules'][0],'id':'MOD-002','name':'共享功能'})
  d['device_references'][0]['module_ids'].append('MOD-002');p['domain']['references'][0]['module_ids'].append('MOD-002')
  result=generation_adapter(build_spec(p));data=result['generation_input']
  ids=[w['device']['requirement_reference_id'] for m in data['modules'] for w in m['selected_devices']]
  self.assertEqual(ids,['REF-001']);self.assertEqual(len(data['system']['functional_modules']),2)
  self.assertEqual(data['system']['shared_device_references'][0]['reference_id'],'REF-001')

 def test_http_retry_recovers_a_committed_but_unscheduled_run_once(self):
  from backend.app import create_app
  from fastapi.testclient import TestClient
  for operation in ('message','reference'):
   app=create_app(Path(self.temp.name)/operation)
   with TestClient(app,raise_server_exceptions=False) as client:
    p=seed(app.state.store);pid=p['id'];runner=app.state.runner;calls=[]
    async def complete(run_id):
     calls.append(run_id);app.state.store.mark_running(run_id)
     app.state.store.finish(run_id,'cancelled','受控调度验证完成')
    runner._run=complete
    if operation=='message':
     endpoint=f'/api/projects/{pid}/messages';body={'text':'补充新的使用条件','request_id':'recover-request'}
    else:
     endpoint=f'/api/projects/{pid}/device-references';body={'base_version':p['document']['version'],'request_id':'recover-request',
       'action':{'operation':'set_intent','reference_id':'REF-001','intent':'prefer'}}
    with patch.object(runner,'start',side_effect=RuntimeError('模拟提交之后、调度之前连接失败')):
     self.assertEqual(client.post(endpoint,json=body).status_code,500)
    queued=client.get(f'/api/projects/{pid}').json();run_id=queued['latest_run']['id']
    self.assertEqual(queued['latest_run']['status'],'queued')
    self.assertEqual(client.post(endpoint,json=body).status_code,202)
    self.assertEqual(client.post(endpoint,json=body).status_code,202)
    recovered=client.get(f'/api/projects/{pid}').json()
    self.assertEqual(calls,[run_id]);self.assertEqual(len(recovered['messages']),2)

 def test_milvus_lazy_query_rows_are_materialized_before_merging_hits(self):
  from backend.knowledge import Catalog
  class LazyRows(list):
   def __init__(self,values):super().__init__(values);self.bitmap=[True]*len(values)
   def __iter__(self):
    for i in range(len(self)):
     self.bitmap[i] # PyMilvus QueryResult has metadata only for its original rows.
     yield self[i]
  row={'id':469187396438325949,'lichuang_id':'C114425','content':'输入电压范围5.5V至36V','page_idx':0,'title':'手册'}
  class Client:
   def query(self,*a,**k):return LazyRows([dict(row)])
   def search(self,*a,**k):return [[{'id':row['id'],'entity':dict(row)}]]
   def close(self):pass
  with patch('pymilvus.MilvusClient',return_value=Client()):
   result=Catalog()._read_spans({'supplier_part':'C114425','document_locator':'DataSheetBase.DataSheet'},['VIN'])
  self.assertEqual(result['status'],'available');self.assertEqual(result['errors'],[])
  self.assertEqual(result['items'][0]['page_number'],1);self.assertEqual(result['items'][0]['retrieval_mode'],'bm25')

 def test_ui_review_invitation_does_not_self_block_actual_approval(self):
  invitation={'id':'review-invitation','question':'是否进入当前需求草稿的整体审阅？','reason':'开始审阅','options':['现在审阅'],'requirement_ids':[],
              'field_ids':[],'decision_key':'overall_requirements_review'}
  for existing in (False,True):
   d=copy.deepcopy(self.p['document'])
   if existing:d['questions']=[invitation]
   doc=apply_analysis(d,self.result(questions=[] if existing else [invitation]),self.p['messages'],self.p['messages'][0]['id'])
   self.assertFalse(doc['questions']);self.assertNotEqual(doc['review_status'],'approved')
   self.assertEqual(doc['question_history'][-1]['resolution_kind'],'ui_action')
  invitation['requirement_ids']=['REQ-001'];invitation['question']='是否需要对实际电源功能做工程审阅？'
  doc=apply_analysis(self.p['document'],self.result(questions=[invitation]),self.p['messages'],self.p['messages'][0]['id'])
  self.assertEqual(doc['questions'][0]['id'],'review-invitation')

 def test_active_finite_lookup_cannot_be_approved_or_exported_as_ready(self):
  from backend.store import Conflict
  from backend.handoff import readiness
  job,_=self.store.create_domain_job(self.p['id'],self.p['document']['device_references'],'approval-race')
  p=self.store.get_project(self.p['id'])
  with self.assertRaisesRegex(Conflict,'仍在核对'):self.store.approve(p['id'],p['document']['version'],p['domain_revision'])
  self.assertEqual(readiness(p)['status'],'blocked')
  self.store.finish_domain_job(p['id'],job['id'],{'references':[]},[])
  p=self.store.get_project(p['id']);self.assertEqual(self.store.approve(p['id'],p['document']['version'],p['domain_revision'])['review']['status'],'approved')

 def test_handoff_rechecks_a_lookup_queued_during_archive_build(self):
  import backend.app as module
  from fastapi.testclient import TestClient
  for allow in (False,True):
   app=module.create_app(Path(self.temp.name)/('handoff-race-'+str(allow)))
   with TestClient(app) as client:
    store=app.state.store;p=seed(store);p=store.approve(p['id'],p['document']['version'],p['domain_revision'])
    original=module.generate_archive
    def queue_after_snapshot(project,db):
     payload=original(project,db)
     store.create_domain_job(p['id'],p['document']['device_references'],'new-lookup')
     return payload
    with patch.object(module,'generate_archive',side_effect=queue_after_snapshot):
     r=client.post(f'/api/projects/{p["id"]}/handoff',json={'base_version':p['document']['version'],'base_domain_revision':p['domain_revision'],'request_id':'racing-export','allow_draft':allow})
    self.assertEqual(r.status_code,409,r.text)
    with store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM handoff_artifacts').fetchone()[0],0)

 def test_review_invitation_variants_cannot_hide_a_remaining_product_gate(self):
  from backend.experience import review_reply
  d=copy.deepcopy(self.p['document']);d['questions']=[{'id':'q','question':'是否需要额外的输出通道？','reason':'确定通道数','options':[],'requirement_ids':['REQ-001']}]
  for reply in ('已复核，可使用工作台的整体审阅入口检查文档。','问题已解决，你可以通过工作台的整体审阅入口检查本版。'):
   self.assertIn('当前仍为草稿',review_reply(reply,d))

 def test_preflight_separates_internal_ids_and_protocols_from_real_part_suffixes(self):
  from backend.knowledge import mentions
  text='参考REF-001、REQ-012、MOD-002和V3-2.1.1。USB2.0、Cortex-M4和DDR4是规格；采用USB3320、DAC8568、ISO7741、STM32F407VET6，供应商C114425。'
  self.assertEqual(mentions(text),['USB3320','DAC8568','ISO7741','STM32F407VET6','C114425'])

 def test_reference_counting_note_does_not_require_an_invented_hardware_module(self):
  from backend.coverage import review_document
  for value,should_normalize in [('8个外设接口保留为工程规模参考，不作为物理连接器总数或上限。',True),('实际提供8路GPIO输入，数量是明确的硬性要求。',False)]:
   d=copy.deepcopy(self.p['document']);source=self.p['messages'][0]
   messages=copy.deepcopy(self.p['messages']);messages[0]['content']+=' '+value
   d['requirements'].append({**d['requirements'][0],'id':'REQ-002','name':'外设接口数量口径','value':value,'source_quote':value})
   self.assertIn('REQ-002',review_document(d)['module_gaps'])
   doc=apply_analysis(d,self.result(),messages,source['id']);row=next(r for r in doc['requirements'] if r['id']=='REQ-002')
   self.assertEqual(len(doc['modules']),1);self.assertEqual(row['value'],value)
   self.assertEqual(row.get('implementation')=='system',should_normalize)
   self.assertEqual('REQ-002' in review_document(doc)['module_gaps'],not should_normalize)


if __name__=='__main__':unittest.main()
