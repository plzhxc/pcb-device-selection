import copy
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.domain import AnalysisResult,apply_analysis,empty_document,analysis_schema,render_markdown
from backend.coverage import CATALOG,review_document
from backend.convergence import FIELDS,PRODUCT_CORE,view,consistency_issues,prompt_document,source_message_ids
from backend.module_catalog import CATALOG as MODULE_CATALOG,TYPES,detail
from backend.store import Store,Conflict


def response(**values):
    data=dict(reply='按已有决定继续整理。',title='桌面温度显示器',summary='室内显示温度。',updates=[],remove_ids=[],questions=[],flow=[{'label':'显示温度','requirement_ids':['REQ-001']}],changes=['继续核对。'],analysis_revision=2)
    data.update(values)
    return AnalysisResult.model_validate(data)


def resolution(ids,state='confirmed',quote='室内桌面显示温度，精度±0.5°C；未列出的实现细节由设计阶段确定。'):
    return {'field_ids':list(ids),'state':state,'conclusion':'室内桌面显示温度，精度±0.5°C；其余实现细节满足上述目标，由设计阶段确定。','requirement_ids':['REQ-001'],'source_message_id':'owner','source_quote':quote}


class Phase2Tests(unittest.TestCase):
    def setUp(self):
        (ROOT/'.runtime/tests').mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'.runtime/tests')
        self.store=Store(Path(self.temp.name)/'studio.sqlite3');self.project=self.store.create_project()
        self.doc=empty_document();self.doc['version']=1
        self.text='室内桌面显示温度，精度±0.5°C；未列出的实现细节由设计阶段确定。'
        self.messages=[{'id':'owner','role':'user','content':self.text}]
        self.doc['requirements']=[dict(id='REQ-001',category='functions',name='温度显示',value='室内显示温度，精度±0.5°C',unit='',priority='must',state='stated',source_message_id='owner',source_quote=self.text,source_type='user',updated_at=self.doc['updated_at'])]
        self.doc['flow']=[{'label':'显示温度','requirement_ids':['REQ-001']}]

    def tearDown(self):self.temp.cleanup()

    def module(self,**kwargs):
        return dict(id='new-module-1',type_id='P_8_1',name='温度显示模块',purpose='显示当前温度',boundary='显示器件、引脚和驱动留到设计阶段',quantity=1,state='design_stage',requirement_ids=['REQ-001'],source_message_id='owner',source_quote=self.text,**kwargs)

    def complete(self):
        return apply_analysis(self.doc,response(field_resolutions=[resolution(PRODUCT_CORE),resolution(set(FIELDS)-PRODUCT_CORE,'design_stage')],module_updates=[self.module()]),self.messages,'owner')

    def test_authoritative_catalogue_is_complete_and_unique(self):
        self.assertEqual((len(MODULE_CATALOG['categories']),len(MODULE_CATALOG['parents']),len(MODULE_CATALOG['modules'])),(10,60,197))
        self.assertEqual(len(TYPES),257)
        self.assertEqual(sum(len(m['parameters']) for m in MODULE_CATALOG['modules']),1260)
        self.assertEqual(sum(len(g['parameters']) for g in MODULE_CATALOG['parameter_groups'].values()),82)
        buck=detail('M_1_1_2');self.assertEqual(buck['category_name'],'电源类模块');self.assertIn('VIN',[p['role'] for p in buck['ports']]);self.assertTrue(buck['inherited_parameters'])
        self.assertTrue(all(p['description'] and p['ports'] and p['engineering_card'] for p in MODULE_CATALOG['parents']))

    def test_section_flags_cannot_bypass_field_gate(self):
        self.doc['coverage']=[{'topic_id':t['id'],'state':'confirmed','conclusion':'已齐全','requirement_ids':['REQ-001'],'source_message_id':'owner','source_quote':self.text} for t in CATALOG]
        after=apply_analysis(self.doc,response(),self.messages,'owner')
        self.assertEqual(review_document(after)['closed'],0);self.assertEqual(view(after)['fields_closed'],0)

    def test_blanket_design_delegation_cannot_skip_product_goals(self):
        after=apply_analysis(self.doc,response(field_resolutions=[resolution(FIELDS,'design_stage')]),self.messages,'owner')
        self.assertEqual(view(after)['fields_closed'],len(FIELDS)-len(PRODUCT_CORE));self.assertEqual(review_document(after)['status'],'draft')

    def test_realistic_complete_boundary_can_be_reviewed_only_after_module_mapping(self):
        fields=[resolution(PRODUCT_CORE),resolution(set(FIELDS)-PRODUCT_CORE,'design_stage')]
        after=apply_analysis(self.doc,response(field_resolutions=fields),self.messages,'owner')
        self.assertEqual(view(after)['fields_closed'],247);self.assertEqual(review_document(after)['status'],'draft')
        complete=self.complete();self.assertEqual(review_document(complete)['status'],'ready')
        complete['review_status']='approved';self.assertEqual(review_document(complete)['status'],'approved')

    def test_fabricated_field_evidence_is_not_confirmed(self):
        after=apply_analysis(self.doc,response(field_resolutions=[resolution([next(iter(FIELDS))],quote='不存在的用户表述')]),self.messages,'owner')
        self.assertEqual(after['field_resolutions'][0]['state'],'tbd')

    def test_unknown_or_duplicate_fields_fail_without_mutation(self):
        before=copy.deepcopy(self.doc);fid=next(iter(FIELDS))
        for batches in [[resolution(['invented'])],[resolution([fid]),resolution([fid])]]:
            with self.assertRaises(ValueError):apply_analysis(self.doc,response(field_resolutions=batches),self.messages,'owner')
        self.assertEqual(self.doc,before)

    def test_module_category_must_come_from_spec(self):
        data=self.module();data['type_id']='new-unregistered-type'
        with self.assertRaisesRegex(ValueError,'类别'):apply_analysis(self.doc,response(module_updates=[data]),self.messages,'owner')

    def test_random_spec_category_selection_and_quantity_survives_two_seeds(self):
        for seed in (20261015,20261016):
            rng=random.Random(seed)
            for kind in rng.sample(MODULE_CATALOG['modules'],20):
                data=self.module();data.update(type_id=kind['id'],quantity=rng.randint(1,12),name=kind['name'])
                after=apply_analysis(self.doc,response(module_updates=[data]),self.messages,'owner')
                row=view(after)['modules'][0]
                self.assertEqual(row['type_name'],kind['name']);self.assertEqual(row['quantity'],data['quantity']);self.assertEqual(row['level'],'submodule')

    def test_unproven_module_mapping_remains_a_proposal(self):
        data=self.module();data.update(state='confirmed',source_quote='未说过')
        after=apply_analysis(self.doc,response(module_updates=[data]),self.messages,'owner')
        self.assertEqual(after['modules'][0]['state'],'proposed');self.assertEqual(view(after)['module_gaps'],['REQ-001'])

    def test_submodule_ports_are_roles_and_unknown_pin_is_rejected(self):
        a=self.module();a.update(type_id='M_1_1_2')
        b=self.module();b.update(id='new-module-2',type_id='M_1_1_1')
        edge=dict(id='power-1',source='new-module-1',target='new-module-2',kind='power',source_port='VOUT',target_port='VIN',description='电源目标连接，待设计验证',state='design_stage')
        after=apply_analysis(self.doc,response(module_updates=[a,b],module_links=[edge]),self.messages,'owner');self.assertEqual(after['module_links'][0]['source'],'MOD-001')
        edge['source_port']='PIN42'
        with self.assertRaisesRegex(ValueError,'角色'):apply_analysis(self.doc,response(module_updates=[a,b],module_links=[edge]),self.messages,'owner')

    def test_software_function_does_not_force_fictitious_hardware(self):
        self.doc['requirements'][0]['implementation']='software'
        after=apply_analysis(self.doc,response(field_resolutions=[resolution(PRODUCT_CORE),resolution(set(FIELDS)-PRODUCT_CORE,'design_stage')]),self.messages,'owner')
        self.assertEqual(view(after)['module_gaps'],[]);self.assertEqual(review_document(after)['status'],'ready')

    def test_closed_fields_prune_renamed_repeat_questions(self):
        fid=next(iter(FIELDS));self.doc['questions']=[dict(id='old',question='产品名称？',reason='',options=[],requirement_ids=['REQ-001'],field_ids=[fid])]
        after=apply_analysis(self.doc,response(field_resolutions=[resolution([fid])],questions=[dict(id='renamed',question='产品叫什么？',reason='',options=[],requirement_ids=['REQ-001'],field_ids=[fid])]),self.messages,'owner')
        self.assertFalse(any(q['id'] in ('old','renamed') for q in after['questions']))

    def test_decision_key_dedup_and_critical_question_sort(self):
        q=lambda id,key,priority:dict(id=id,question=id,reason='',options=[],requirement_ids=['REQ-001'],decision_key=key,priority=priority)
        self.doc['questions']=[q('old','power-choice',2)]
        after=apply_analysis(self.doc,response(questions=[q('copy','power-choice',2),q('urgent','risk',1)]),self.messages,'owner')
        self.assertEqual([q['id'] for q in after['questions']],['urgent','old'])

    def test_changed_fact_reopens_only_linked_field_and_invalidates_numeric_cache(self):
        after=self.complete();self.doc=after
        update={k:self.doc['requirements'][0][k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']}
        update['value']='精度±0.1°C'
        after=apply_analysis(self.doc,response(updates=[update]),self.messages,'owner')
        self.assertTrue(after['recheck_fields']);self.assertEqual(view(after)['fields_closed'],0);self.assertEqual(view(after)['modules'][0]['id'],'MOD-001')
        self.assertEqual(after['questions'],[],'internal rechecks must not reask already supplied product facts')

    def test_internal_recheck_clears_without_repeating_known_question(self):
        after=self.complete();after['recheck_fields']=list(FIELDS);after['recheck_topics']=[t['id'] for t in CATALOG]
        after['questions']=[dict(id='gap-V3-1.1',question='产品名称？',reason='旧版自动缺口',options=[],requirement_ids=[],source_message_id='owner')]
        after=apply_analysis(after,response(field_resolutions=[resolution(PRODUCT_CORE),resolution(set(FIELDS)-PRODUCT_CORE,'design_stage')]),self.messages,'owner',continuation=True)
        self.assertEqual(after['recheck_fields'],[]);self.assertEqual(after['recheck_topics'],[]);self.assertEqual(after['questions'],[])

    def test_manual_module_edits_are_versioned_and_removal_preserves_requirements(self):
        with self.store.connect() as db:self.store._save(db,self.project['id'],self.doc,1)
        data={k:v for k,v in self.module().items() if k not in ('source_message_id','source_quote')};data['id']=''
        added=self.store.edit_module(self.project['id'],1,data);self.assertEqual(added['document']['version'],2)
        with self.assertRaises(Conflict):self.store.edit_module(self.project['id'],1,data)
        mid=added['document']['modules'][0]['id'];removed=self.store.edit_module(self.project['id'],2,{'id':mid},remove=True)
        self.assertEqual(len(removed['document']['requirements']),1);self.assertEqual(removed['document']['modules'],[])

    def test_typed_range_conflicts_convert_units_and_respect_scope(self):
        self.doc['numeric_constraints']=[dict(id='a',requirement_id='REQ-001',metric='voltage',scope='rail',conditions='normal',minimum=4.8,maximum=None,unit='V'),dict(id='b',requirement_id='REQ-001',metric='voltage',scope='rail',conditions='normal',minimum=None,maximum=3300,unit='mV')]
        self.assertEqual(len(consistency_issues(self.doc)),1)
        self.doc['numeric_constraints'][1]['scope']='other-rail';self.assertEqual(consistency_issues(self.doc),[])
        self.doc['numeric_constraints'][1]['scope']='rail';self.doc['numeric_constraints'][1]['maximum']=5000;self.assertEqual(consistency_issues(self.doc),[])

    def test_battery_runtime_uses_only_same_scope_and_conditions(self):
        self.doc['numeric_constraints']=[dict(id='cap',requirement_id='REQ-001',metric='battery_capacity',scope='battery',conditions='continuous',minimum=None,maximum=500,unit='mAh'),dict(id='amp',requirement_id='REQ-001',metric='average_current',scope='battery',conditions='continuous',minimum=20,maximum=None,unit='mA'),dict(id='time',requirement_id='REQ-001',metric='runtime',scope='battery',conditions='continuous',minimum=30,maximum=None,unit='天')]
        self.assertTrue(any(i['id'].startswith('energy-') for i in consistency_issues(self.doc)))
        self.doc['numeric_constraints'][1]['conditions']='transmitting';self.assertEqual(consistency_issues(self.doc),[])

    def test_numeric_evidence_cannot_be_invented(self):
        numeric=dict(id='budget',requirement_id='REQ-001',metric='budget',scope='board',conditions='',minimum=None,maximum=900,unit='元',source_message_id='owner',source_quote=self.text)
        after=apply_analysis(self.doc,response(numeric_constraints=[numeric]),self.messages,'owner');self.assertEqual(after['numeric_constraints'],[])

    def test_unit_price_requires_an_explicit_single_item_scope(self):
        row=dict(id='budget',requirement_id='REQ-001',metric='budget',scope='单台硬件 BOM 总成本',conditions='100台批量估算',minimum=None,maximum=69,unit='人民币元/台')
        self.doc['numeric_constraints']=[row]
        self.assertEqual(consistency_issues(self.doc),[])
        self.doc['numeric_constraints'].append({**row,'id':'minimum','unit':'元','minimum':70,'maximum':None})
        self.assertEqual(consistency_issues(self.doc)[0]['kind'],'conflict')
        for item in self.doc['numeric_constraints']:item['scope']='整批 BOM 总成本'
        self.assertEqual(consistency_issues(self.doc)[0]['kind'],'review')
        self.doc['numeric_constraints']=[{**row,'unit':'人民币元'}]
        self.assertEqual(consistency_issues(self.doc),[])

    def test_final_export_includes_field_decisions_modules_and_handoff(self):
        after=self.complete();md=render_markdown(after)
        self.assertIn('温度显示模块',md);self.assertIn('需求来源',md);self.assertIn('```mermaid',md)
        self.assertNotIn('Wi-Fi 子模块',md);self.assertNotIn('充电管理子模块',md)
        schema=analysis_schema(self.doc);self.assertEqual(len(schema['$defs']['ModuleUpdate']['properties']['type_id']['enum']),257)

    def test_module_category_change_reopens_associated_scope_only(self):
        complete=self.complete();module={k:v for k,v in complete['modules'][0].items()}
        module.update(type_id='P_7_1',name='温度采集模块')
        after=apply_analysis(complete,response(module_updates=[module]),self.messages,'owner')
        self.assertTrue(after['recheck_fields']);self.assertTrue(all(FIELDS[f]['chapter'] in (3,5) for f in after['recheck_fields']))
        self.assertTrue(all(f['state']=='confirmed' for f in after['field_resolutions'] if f['field_id'] in PRODUCT_CORE))
        self.assertEqual(review_document(after)['status'],'draft')

    def test_calendar_lifetime_does_not_create_a_false_unknown_unit_blocker(self):
        self.doc['numeric_constraints']=[dict(id='life',requirement_id='REQ-001',metric='runtime',scope='产品设计寿命',conditions='',minimum=3,maximum=None,unit='年')]
        self.assertEqual(consistency_issues(self.doc),[])
        self.doc['numeric_constraints'].append(dict(id='life2',requirement_id='REQ-001',metric='runtime',scope='产品设计寿命',conditions='',minimum=None,maximum=2,unit='年'))
        self.assertEqual(consistency_issues(self.doc)[0]['kind'],'conflict')

    def test_module_explanation_refresh_does_not_reopen_unchanged_design(self):
        complete=self.complete();module=copy.deepcopy(complete['modules'][0]);module.update(name='本地温度显示',purpose='更清楚地说明：在本地显示已经采集到的温度。')
        after=apply_analysis(complete,response(module_updates=[module]),self.messages,'owner')
        self.assertEqual(view(after)['fields_closed'],247);self.assertEqual(after['recheck_fields'],[])
        self.assertEqual(review_document(after)['status'],'ready')

    def test_prompt_compaction_is_lossless_and_preserves_source_evidence(self):
        complete=self.complete();before=copy.deepcopy(complete);compact=prompt_document(complete)
        restored={fid:{**{k:v for k,v in group.items() if k!='field_ids'},'field_id':fid} for group in compact['field_resolution_groups'] for fid in group['field_ids']}
        self.assertEqual(restored,{row['field_id']:row for row in complete['field_resolutions']})
        self.assertLess(len(json.dumps(compact)),len(json.dumps(complete))*.3)
        self.assertEqual(complete,before)

    def test_explicit_optional_boundary_can_close_without_a_duplicate_flat_requirement(self):
        text='本版无明确交付日期约束。';messages=self.messages+[dict(id='schedule',role='user',content=text)]
        row=resolution(['V3-1.3:F07']);row.update(requirement_ids=[],source_message_id='schedule',source_quote=text,conclusion=text)
        after=apply_analysis(self.doc,response(field_resolutions=[row]),messages,'schedule')
        self.assertEqual(after['field_resolutions'][0]['state'],'confirmed')
        self.assertEqual(len(after['requirements']),1)

    def test_unlinked_confirmed_boundary_is_rechecked_after_a_requirement_change(self):
        fid='V3-1.3:F07';self.doc['convergence_version']=2
        self.doc['field_resolutions']=[dict(field_id=fid,state='confirmed',conclusion='无日期约束',requirement_ids=[],source_message_id='owner',source_quote=self.text)]
        update={k:self.doc['requirements'][0][k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']};update['value']='新目标'
        after=apply_analysis(self.doc,response(updates=[update]),self.messages,'owner')
        self.assertIn(fid,after['recheck_fields']);self.assertEqual(after['field_resolutions'][0]['state'],'tbd')
        self.assertEqual(after['field_resolutions'][0]['recheck_evidence']['source_message_id'],'owner')
        self.assertIn('owner',source_message_ids(after))

    def test_empty_workflow_delta_does_not_erase_the_existing_product_flow(self):
        after=apply_analysis(self.doc,response(flow=[]),self.messages,'owner')
        self.assertEqual(after['flow'],self.doc['flow'])

    def test_cancelled_function_does_not_leave_an_orphan_workflow_step(self):
        after=apply_analysis(self.doc,response(remove_ids=['REQ-001'],flow=[]),self.messages,'owner')
        self.assertEqual(after['flow'],[])


if __name__=='__main__':unittest.main()
