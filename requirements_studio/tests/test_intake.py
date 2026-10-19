import copy
import unittest
from types import SimpleNamespace
from backend.intake import LABELS,IntakeCheck,apply_checks,review,fallback_question
from backend.coverage import review_document
from backend.domain import empty_document
from backend.convergence import invalidate
from backend.experience import CORE


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.doc=empty_document();self.doc.update(version=1,convergence_version=2,intake_policy_version=1)
        self.req=dict(id='REQ-001',category='functions',name='通信板',value='USB供电的CAN通信板',unit='',state='stated',priority='must',implementation='hardware',source_message_id='user',source_quote='USB供电的CAN通信板')
        self.doc['requirements']=[self.req]
        self.doc['flow']=[dict(label='收发数据',requirement_ids=['REQ-001'])]
        self.doc['modules']=[dict(id='MOD-001',type_id='P_10_1',name='通信控制',purpose='收发CAN',boundary='实现待设计',quantity=1,state='confirmed',requirement_ids=['REQ-001'])]
        self.doc['field_resolutions']=[dict(field_id=fid,state='confirmed',conclusion='USB供电的CAN通信板',requirement_ids=['REQ-001'],source_message_id='user',source_quote='USB供电的CAN通信板') for fid in CORE]
        self.doc['intake_checks']=[dict(key=key,state='confirmed',conclusion='依据已明确功能整理，具体工程条件由设计落实',requirement_ids=['REQ-001'],source_message_id='user',source_quote='USB供电的CAN通信板') for key in LABELS]
        self.messages=[dict(id='user',role='user',content='USB供电的CAN通信板')]

    def test_open_external_power_blocks_old_core_only_readiness(self):
        self.assertEqual(review_document(self.doc)['status'],'ready')
        self.doc['intake_checks'][1].update(state='open',conclusion='只有XT30连接器，外部电源来源仍未确定')
        result=review_document(self.doc)
        self.assertEqual(result['status'],'draft')
        self.assertEqual(result['intake']['gaps'][0]['key'],'external_power')
        fallback_question(self.doc,'user')
        self.assertEqual(len(self.doc['questions']),1)
        self.assertIn('供电',self.doc['questions'][0]['question'])

    def test_missing_check_is_internal_review_not_five_customer_questions(self):
        self.doc['intake_checks']=[]
        result=review_document(self.doc)
        self.assertEqual(result['status'],'draft')
        self.assertEqual(len(result['intake']['rechecks']),5)
        self.assertEqual(result['intake']['gaps'],[])
        fallback_question(self.doc,'user');self.assertEqual(self.doc['questions'],[])

    def test_supplied_power_closes_without_reasking(self):
        changed=copy.deepcopy(self.doc);changed['intake_checks'][1]['state']='open'
        update=IntakeCheck(**self.doc['intake_checks'][1])
        result=SimpleNamespace(intake_policy_revision=1,intake_checks=[update])
        apply_checks(changed,changed,result,self.messages,{},set())
        self.assertEqual(review(changed)['gaps'],[])
        fallback_question(changed,'user');self.assertEqual(changed['questions'],[])

    def test_closure_without_actual_user_basis_rejected(self):
        update=IntakeCheck(**{**self.doc['intake_checks'][1],'source_message_id':'invented'})
        with self.assertRaisesRegex(ValueError,'真实原话'):
            apply_checks(self.doc,self.doc,SimpleNamespace(intake_policy_revision=1,intake_checks=[update]),self.messages,{},set())

    def test_requirement_change_rechecks_boundary_instead_of_reusing_old_value(self):
        changed=copy.deepcopy(self.doc)
        apply_checks(changed,self.doc,SimpleNamespace(intake_policy_revision=1,intake_checks=[]),self.messages,{}, {'REQ-001'})
        self.assertEqual(len(review(changed)['rechecks']),5)
        self.assertEqual(review(changed)['gaps'],[])
        invalidate(self.doc,['REQ-001'])
        self.assertTrue(all(c['state']=='review' for c in self.doc['intake_checks']))

    def test_legacy_approved_documents_not_silently_migrated(self):
        self.doc.pop('intake_policy_version');self.doc.pop('intake_checks')
        self.doc['review_status']='approved'
        self.assertEqual(review_document(self.doc)['status'],'approved')
        apply_checks(self.doc,self.doc,SimpleNamespace(intake_policy_revision=0,intake_checks=[]),self.messages,{},set())
        self.assertNotIn('intake_policy_version',self.doc)


if __name__=='__main__':unittest.main()
