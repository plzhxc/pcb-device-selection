"""Semantic guards for explicit parts, current decisions and complete handoff."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from backend.decision_map import decision_map, constraint_context
from backend.domain import empty_document
from backend.handoff import generation_brief
from backend.domain_service import KnowledgeManager
from backend.store import Store


class DecisionMapTests(unittest.TestCase):
    def setUp(self):
        self.doc=empty_document()
        self.doc.update(title='真实型号主控板',version=1,requirements=[dict(id='REQ-001',name='两路CAN',value='提供2路CAN',unit='',state='stated',priority='must',category='functions',implementation='hardware',source_message_id='input',source_quote='采用STM32F407VET6，提供2路CAN',spec_field_ids=['V3-2.1.1:F02'])])
        self.doc['modules']=[dict(id='MOD-001',type_id='P_10_1',name='主控',purpose='控制两路CAN',boundary='引脚由设计落实',quantity=1,state='confirmed',requirement_ids=['REQ-001'],source_message_id='input',source_quote='采用STM32F407VET6')]
        self.doc['device_references']=[dict(id='REF-001',mention='STM32F407VET6',intent='must',quantity=None,lifecycle='active',identity_scope='exact_part',purpose='主控',requirement_ids=['REQ-001'],module_ids=['MOD-001'],source_message_id='input',source_quote='采用STM32F407VET6')]
        self.domain={'references':[dict(**self.doc['device_references'][0],resolution_status='not_found',identity=None,evidence_ids=[])],'checks':[]}

    def test_user_fixed_part_survives_missing_catalog_without_claiming_identity(self):
        m=decision_map(self.doc,self.domain)
        part=next(n for n in m['nodes'] if n['id']=='REF-001')
        self.assertEqual(part['status'],'stated')
        self.assertIn('本版必须使用',part['conclusion'])
        self.assertTrue(any('本次目录未找到' in f['value'] for f in part['facts']))
        self.assertTrue(any('尚未给定' in f['value'] for f in part['facts']))
        self.assertEqual(m['counts']['specified_devices'],1)
        self.assertEqual(m['counts']['located_devices'],0)

    def test_one_current_node_per_decision_after_changes_no_chat_replication(self):
        first=copy.deepcopy(self.doc)
        self.doc['requirements'][0]['value']='改为1路CAN'
        self.doc['version']=2
        m=decision_map(self.doc,self.domain,[first,self.doc])
        nodes=[n for n in m['nodes'] if n['id']=='REQ-001']
        self.assertEqual(len(nodes),1)
        self.assertEqual(nodes[0]['conclusion'],'改为1路CAN')
        self.assertEqual([h['value']['value'] for h in nodes[0]['history']],['提供2路CAN','改为1路CAN'])
        self.assertNotIn('turns',m)
        self.assertNotIn('analyses',json.dumps(m))
        self.assertTrue(any(e['source']=='REF-001' and e['target']=='REQ-001' for e in m['edges']))
        self.assertTrue(any(e['source']=='REQ-001' and e['target']=='MOD-001' for e in m['edges']))

    def test_user_preference_is_not_relabelled_as_ai_advice(self):
        for intent,label in [('prefer','用户偏好'),('reference','仅供参考')]:
            self.domain['references'][0]['intent']=intent
            m=decision_map(self.doc,self.domain)
            node=next(n for n in m['nodes'] if n['id']=='REF-001')
            self.assertEqual(node['status_label'],label)
            self.assertEqual(m['counts']['specified_devices'],0)

    def test_description_is_separate_from_design_constraint(self):
        self.doc['device_constraints']=[dict(id='c1',reference_id='REF-001',requirement_id='REQ-001',field_key='feature',text='168MHz',kind='user_device_description'),dict(id='c2',reference_id='REF-001',requirement_id='REQ-001',field_key='interface',text='2路CAN',kind='design_requirement')]
        c=constraint_context(self.doc,self.domain)
        self.assertEqual(c['unverified_user_device_descriptions'][0]['text'],'168MHz')
        self.assertEqual(c['device_design_constraints'][0]['text'],'2路CAN')
        self.assertEqual(len(c['known_product_decisions']),1)

    def test_pending_answer_retains_node_but_disables_new_decision(self):
        self.doc['questions']=[dict(id='power',question='电源从哪里来？',reason='需要确定外部供电',options=['电池','适配器'],requirement_ids=['REQ-001'])]
        m=decision_map(self.doc,self.domain,submissions=[dict(question_id='power',status='queued')])
        q=next(n for n in m['nodes'] if n['kind']=='question')
        self.assertEqual(q['status'],'processing')
        self.assertEqual(q['question_id'],'power')

    def test_usb_power_only_does_not_add_unused_usb_communication_topic(self):
        from backend.experience import module_topics
        self.doc['intake_policy_version']=1
        self.doc['modules'][0].update(name='USB电源输入',purpose='只用于外部供电')
        self.assertNotIn('V3-5.2.4.5',module_topics(self.doc))
        self.doc['modules'][0]['purpose']='USB转UART串口通信'
        self.assertIn('V3-5.2.4.5',module_topics(self.doc))
        self.doc.pop('intake_policy_version')
        self.assertNotIn('V3-5.2.4.5',module_topics(self.doc),'Legacy approvals do not acquire new checks silently')

    def test_complete_brief_contains_requirements_not_only_summary(self):
        spec=dict(title='主控板',summary='一句摘要',requirements=self.doc['requirements'],constraints=[dict(scope='外部输入',minimum=12,maximum=24,unit='V',conditions='直流适配器')],field_resolutions=[dict(field_id='V3-3.1.1:F01',state='confirmed',conclusion='12～24V直流输入')],modules=self.doc['modules'],module_links=[dict(id='if1',source='a',target='b',protocol='CAN',voltage='3.3V',boundary='板内')],workflow=[dict(label='接收命令')],acceptance_criteria=[dict(requirement_id='REQ-001',criterion='两路分别收发成功')],open_items=[])
        brief=generation_brief(spec)
        for text in ('提供2路CAN','12～24V','直流适配器','3.3V','接收命令','两路分别收发成功','引脚由设计落实'):self.assertIn(text,brief)


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_turn_can_read_catalog_evidence_while_manual_job_is_running(self):
        base=Path(__file__).resolve().parents[1]/'.runtime'
        with tempfile.TemporaryDirectory(dir=base) as directory:
            store=Store(Path(directory)/'studio.sqlite3');p=store.create_project()
            from backend.domain_store import identity_input
            ref=dict(id='',mention='STM32F407VET6',identity_scope='exact_part',device_key=None)
            job,_=store.create_domain_job(p['id'],[ref],'partial-test','input')
            store.start_domain_job(p['id'],job['id'])
            result=dict(mention=ref['mention'],identity_input_hash=identity_input(ref),status='matched',identity={'model':ref['mention'],'device_key':'datas:source-device:8985'},items=[],evidence_ids=['EV-partial'],document_status='pending')
            evidence=dict(id='EV-partial',kind='catalog',assertions=[],title='真实目录快照')
            self.assertTrue(store.record_domain_reference(p['id'],job['id'],result,[evidence]))
            manager=KnowledgeManager(store)
            try:
                records=manager.preflight_context(p['id'],'采用STM32F407VET6')
                self.assertEqual(records[0]['status'],'matched')
                self.assertEqual(records[0]['document_status'],'pending')
                self.assertEqual(records[0]['evidence'][0]['id'],'EV-partial')
                self.assertEqual(store.domain_job(p['id'],job['id'])['status'],'running')
            finally:await manager.close()

    async def test_more_than_five_parts_receive_first_turn_lookup(self):
        base=Path(__file__).resolve().parents[1]/'.runtime'
        with tempfile.TemporaryDirectory(dir=base) as directory:
            store=Store(Path(directory)/'studio.sqlite3')
            p=store.create_project();names=['MODEL100'+str(i) for i in range(9)]
            class Provider:
                enabled=True
                def __init__(self):self.seen=[]
                def resolve(self,ref):self.seen.append(ref['mention']);return {'status':'not_found','items':[]}
            provider=Provider();manager=KnowledgeManager(store,provider)
            try:
                text='采用'+ '、'.join(names)
                await manager.prepare(p['id'],{'id':'input','content':text},'run')
                self.assertEqual(set(provider.seen),set(names))
                preflight=manager.preflight_context(p['id'],text)
                self.assertEqual(len(preflight),9)
                self.assertTrue(all(r['status']=='not_found' for r in preflight))
            finally:await manager.close()


if __name__=='__main__':unittest.main()
