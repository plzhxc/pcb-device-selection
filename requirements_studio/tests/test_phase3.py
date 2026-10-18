from __future__ import annotations
import asyncio
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.store import Store,Conflict,Missing
from backend.domain import AnalysisResult,apply_analysis
from backend.device_domain import DeviceReferenceUpdate,DeviceConstraintUpdate,apply_updates,module_mapping
from backend.domain_service import KnowledgeManager,catalog_evidence,span_evidence,strengthen_attributes
from backend.domain_store import identity_input
from backend.engineering_checks import parse_value,normalize_attribute,quoted_quantity
from backend.handoff import build_spec,generate_archive,verify_archive,generation_adapter,legacy_document
from phase3_fixtures import FixtureCatalog,seed


class DomainUnitTests(unittest.TestCase):
    def test_source_unit_disagreement_is_not_a_pass(self):
        a=normalize_attribute({'field_key':'frequency','field_value_text':'500kHz','field_value_num':500,'field_unit':'Hz'})
        self.assertEqual(a['normalized']['minimum'],'500000');self.assertEqual(a['validation_state'],'source_conflict')
    def test_compatible_units_temperature_and_signed_range(self):
        self.assertEqual(parse_value('500mV')['minimum'],'0.500')
        self.assertEqual(parse_value('-40℃~85℃')['minimum'],'-40')
        self.assertEqual(parse_value('273.15K')['minimum'],'0.00')
        self.assertIsNone(parse_value('NaN V'));self.assertIsNone(parse_value('Infinity A'))
        self.assertIsNone(parse_value('10MHz~3V'));self.assertIsNone(parse_value('30V~5V'))
    def test_quantity_evidence_preserves_prefix_and_does_not_use_part_digits(self):
        self.assertTrue(quoted_quantity(500000,'Hz','开关频率500kHz'))
        self.assertFalse(quoted_quantity(9,'V','VIN直接输入5V'))
        self.assertFalse(quoted_quantity(32,'V','采用ESP32'))
    def test_all_normative_modules_have_reviewable_mapping(self):
        from backend.module_catalog import TYPES,CATALOG
        for key in TYPES:self.assertTrue(module_mapping(key)['leaf_codes'])
        mapping=json.loads((ROOT/'backend/resources/module_device_map.json').read_text())
        self.assertEqual(mapping['module_source_sha256'],CATALOG['source_sha256']);self.assertEqual(len(mapping['mappings']),257)
    def test_manual_identity_key_and_big_document_ids_remain_strings(self):
        p=FixtureCatalog();s=span_evidence(p.identity(),p.spans(p.identity())['items'][0])
        self.assertIsInstance(s['primary_key'],str);self.assertEqual(s['page_number'],1)
    def test_operating_proof_does_not_promote_an_absolute_maximum(self):
        p=FixtureCatalog();e=catalog_evidence(p.device(p.identity()['device_key']));span=span_evidence(p.identity(),{**p.spans(p.identity())['items'][0],'content':'绝对最大应力：输入电压范围5.5V至36V。'})
        strengthen_attributes(e,[span]);self.assertNotEqual(e['assertions'][0]['rating_kind'],'recommended_operating')


class DomainPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        parent=ROOT/'.runtime/tests';parent.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=parent);self.store=Store(Path(self.tmp.name)/'studio.sqlite3')
        self.p=seed(self.store);self.pid=self.p['id'];self.provider=FixtureCatalog();self.manager=KnowledgeManager(self.store,self.provider)
    async def asyncTearDown(self):await self.manager.close();self.tmp.cleanup()
    async def lookup(self):
        job=await self.manager.enqueue(self.pid,refresh=True);await self.manager.tasks[job];return self.store.get_project(self.pid)
    def test_schema_revision_three_retains_normative_module_engine(self):
        d=self.p['document'];source=self.p['messages'][0]
        result=AnalysisResult.model_validate({'reply':'已核对。','title':d['title'],'summary':d['summary'],'updates':[],'remove_ids':[],
          'questions':[],'flow':[],'changes':['保留已有信息'],'analysis_revision':3})
        doc=apply_analysis(d,result,self.p['messages'],source['id'])
        self.assertEqual(doc['convergence_version'],2);self.assertEqual(doc['modules'],d['modules'])
    async def test_ambiguous_identity_does_not_load_representative_attributes(self):
        p=await self.lookup();r=p['domain']['references'][1]
        self.assertEqual(r['resolution_status'],'ambiguous');self.assertIsNone(r['identity']);self.assertEqual(r['evidence_ids'],[])
        self.assertFalse(any('100' in x or '101' in x for x in self.provider.calls))
    async def test_reference_intent_is_independent_of_catalog_match(self):
        p=await self.lookup();self.assertEqual(p['domain']['references'][0]['intent'],'must')
        self.assertEqual(p['domain']['references'][1]['intent'],'reference');self.assertEqual(p['review']['status'],'ready')
    async def test_missing_fields_do_not_open_user_questionnaire(self):
        p=await self.lookup();self.assertFalse(p['document']['questions']);self.assertEqual(p['review']['status'],'ready')
        self.assertTrue(any(a['is_missing'] for e in p['domain']['evidence'] for a in e.get('assertions',[])))
    async def test_failed_refresh_cannot_erase_a_known_voltage_conflict(self):
        d=self.store.raw_document(self.pid);source=self.p['messages'][0]
        d['device_constraints']=[DeviceConstraintUpdate(id='DC-1',reference_id='REF-001',requirement_id='REQ-001',field_key='input_voltage',kind='design_requirement',relation='covers',minimum=5,maximum=5,unit='V',port='VIN',source_message_id=source['id'],source_quote=source['content']).model_dump()]
        with self.store.connect() as db:self.store._save(db,self.pid,d,2)
        before=await self.lookup();self.assertTrue(before['domain']['blockers'])
        # The old evidence must also survive falling out of the bounded visible
        # job timeline; these are deliberately empty controlled history entries.
        with self.store.connect() as db:
            db.executemany("INSERT INTO domain_jobs(id,project_id,request_id,base_version,status,request,result,created_at) VALUES(?,?,?,2,'completed','{}','{}',?)",
                           [(f'past-{i}',self.pid,f'past-{i}',self.p['updated_at']) for i in range(121)])
        self.provider.fail=True
        job=await self.manager.enqueue(self.pid,request_id='offline-refresh',refresh=True);await self.manager.tasks[job]
        after=self.store.get_project(self.pid)
        self.assertTrue(after['domain']['blockers'],'a failed lookup is not evidence that the old conflict disappeared')
        self.assertTrue(after['domain']['references'][0]['evidence_ids'])
        self.assertEqual(after['domain']['references'][0]['lookup_status'],'unavailable')
        with self.assertRaises(Conflict):self.store.approve(self.pid,after['document']['version'],after['domain_revision'])
    async def test_explicit_hard_conflict_blocks_review(self):
        d=self.store.raw_document(self.pid);quote=self.p['messages'][0]
        d['device_constraints']=[DeviceConstraintUpdate(id='DC-1',reference_id='REF-001',requirement_id='REQ-001',field_key='input_voltage',kind='design_requirement',relation='covers',minimum=5,maximum=5,unit='V',port='VIN',conditions='直接供电',source_message_id=quote['id'],source_quote=quote['content']).model_dump()]
        with self.store.connect() as db:self.store._save(db,self.pid,d,2)
        p=await self.lookup();self.assertEqual(p['domain']['checks'][0]['status'],'contradicted');self.assertEqual(p['review']['status'],'draft')
        with self.assertRaises(Conflict):self.store.approve(self.pid,p['document']['version'],p['domain_revision'])
    async def test_unscoped_values_do_not_claim_board_impossibility(self):
        d=self.store.raw_document(self.pid);quote=self.p['messages'][0]
        d['device_constraints']=[DeviceConstraintUpdate(id='DC-1',reference_id='REF-001',requirement_id='REQ-001',field_key='input_voltage',kind='design_requirement',relation='covers',minimum=5,maximum=5,unit='V',port='',source_message_id=quote['id'],source_quote=quote['content']).model_dump()]
        with self.store.connect() as db:self.store._save(db,self.pid,d,2)
        p=await self.lookup();self.assertFalse(p['domain']['blockers']);self.assertEqual(p['domain']['checks'][0]['status'],'unknown')
        self.assertIn('连接边界',''.join(p['domain']['checks'][0]['missing']))
        self.assertTrue(p['domain']['references'][0]['evidence_ids'])
    async def test_immutable_evidence_is_project_scoped(self):
        p=await self.lookup();ev=p['domain']['evidence'][0]['id'];other=self.store.create_project()
        with self.assertRaises(Missing):self.store.evidence(other['id'],ev)
        self.assertEqual(self.store.evidence(self.pid,ev)['id'],ev)
    async def test_async_fact_arrival_does_not_change_user_document_version(self):
        before=copy.deepcopy(self.p['document']);p=await self.lookup()
        self.assertEqual(p['document'],before);self.assertGreater(p['domain_revision'],0)
    async def test_removed_reference_does_not_revive_after_late_job(self):
        self.provider.delay=.15;job=await self.manager.enqueue(self.pid,refresh=True)
        d=self.store.raw_document(self.pid);d['device_references'][0]['lifecycle']='removed'
        with self.store.connect() as db:self.store._save(db,self.pid,d,2)
        await self.manager.tasks[job];p=self.store.get_project(self.pid)
        self.assertFalse(any(r['id']=='REF-001' for r in p['domain']['references']))
        self.assertNotIn('REF-001 · TPS5450DDAR',p['markdown'])
    async def test_revision_guard_detects_evidence_arrival(self):
        old=self.p['domain_revision'];p=await self.lookup()
        with self.assertRaisesRegex(Conflict,'资料核对状态'):self.store.approve(self.pid,p['document']['version'],old)
        p=self.store.approve(self.pid,p['document']['version'],p['domain_revision']);self.assertEqual(p['review']['status'],'approved')
    async def test_archive_roundtrip_and_tampering(self):
        p=await self.lookup();p=self.store.approve(self.pid,p['document']['version'],p['domain_revision'])
        manifest,data=generate_archive(p,self.store);self.assertTrue(verify_archive(data)['ok'])
        buf=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(data)) as original,zipfile.ZipFile(buf,'w') as altered:
            for name in original.namelist():altered.writestr(name,b'changed' if name=='requirements.md' else original.read(name))
        with self.assertRaisesRegex(ValueError,'完整性'):verify_archive(buf.getvalue())
    async def test_contract_keeps_unknown_physical_quantity_and_reference_intent(self):
        p=await self.lookup();spec=build_spec(p);item=spec['selection_items'][0]
        self.assertIsNone(item['component_quantity']);self.assertEqual(item['circuit_quantity'],1)
        self.assertEqual(item['specified_references'],['REF-001']);self.assertEqual(item['reference_only'],['REF-002'])
    async def test_downstream_uses_only_user_specified_models(self):
        p=await self.lookup();p=self.store.approve(self.pid,p['document']['version'],p['domain_revision'])
        result=generation_adapter(build_spec(p));devices=result['generation_input']['modules'][0]['selected_devices']
        self.assertEqual(len(devices),1);self.assertEqual(devices[0]['device']['model'],'TPS5450DDAR')
        self.assertEqual(devices[0]['device']['verification_status'],'unverified')
    def test_source_quote_and_link_validation_rejects_forged_reference(self):
        from types import SimpleNamespace
        d=copy.deepcopy(self.p['document']);before=copy.deepcopy(d)
        r=DeviceReferenceUpdate(id='new-1',mention='LM358',intent='must',identity_scope='exact_part',purpose='x',requirement_ids=['REQ-999'],module_ids=[],source_message_id=self.p['messages'][0]['id'],source_quote=self.p['messages'][0]['content'])
        with self.assertRaisesRegex(ValueError,'不存在'):apply_updates(d,SimpleNamespace(device_reference_updates=[r],remove_device_reference_ids=[],device_constraints=[]),before,self.p['messages'],{}, {})
    def test_forged_device_numeric_limit_is_rejected(self):
        from types import SimpleNamespace
        d=copy.deepcopy(self.p['document']);quote=self.p['messages'][0]
        c=DeviceConstraintUpdate(id='C',reference_id='REF-001',requirement_id='REQ-001',field_key='input_voltage',kind='design_requirement',relation='covers',minimum=9,maximum=9,unit='V',source_message_id=quote['id'],source_quote=quote['content'])
        with self.assertRaisesRegex(ValueError,'未在所引原话'):apply_updates(d,SimpleNamespace(device_reference_updates=[],remove_device_reference_ids=[],device_constraints=[c]),copy.deepcopy(d),self.p['messages'],{}, {})
    async def test_cancelled_job_keeps_previous_requirements(self):
        self.provider.delay=.2;job=await self.manager.enqueue(self.pid,refresh=True);await asyncio.sleep(.03)
        await self.manager.cancel_project(self.pid);self.assertEqual(self.store.domain_job(self.pid,job)['status'],'cancelled')
        self.assertEqual(self.store.raw_document(self.pid)['requirements'],self.p['document']['requirements'])
    def test_legacy_export_excludes_new_domain_keys(self):
        d=legacy_document(self.p['document']);self.assertNotIn('device_references',d);self.assertNotIn('selection_items',d)


if __name__=='__main__':unittest.main()
