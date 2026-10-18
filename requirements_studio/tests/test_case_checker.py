"""An empty retained project is an incomplete case, not a broken audit."""
import sys
import unittest
import copy
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from backend.domain import empty_document
from check_case import check_case


class CaseCheckerTests(unittest.TestCase):
    def power_role_project(self):
        original='原方案支持USB-C PD供电和DC 5.5×2.1备用电源输入。'
        choice='优先评估一个直流适配器接入板上DC接口，原有USB-C PD功能保留。'
        doc=empty_document()
        doc['requirements']=[
            {'id':'REQ-001','name':'DC适配器输入','value':'提供DC 5.5×2.1输入，优先评估由DC适配器供电，USB-C PD保留。',
             'state':'stated','source_message_id':'choice','source_quote':choice},
            {'id':'REQ-002','name':'USB-C PD供电','value':'支持USB-C PD受电协商。',
             'state':'stated','source_message_id':'original','source_quote':original},
        ]
        project={'id':'power-role-case','document':doc,'messages':[
            {'id':'original','role':'user','content':original},{'id':'choice','role':'user','content':choice}],
            'review':{'status':'draft'},'domain':{'blockers':[],'active_jobs':[]},'input_queue':[],'latest_run':None}
        decisions={'dc_input_role':{'decision':'preferred_with_pd_retained','source_message_id':'choice',
                    'source_quote':choice,'review_basis':'客户明确选择DC优先评估，并保留原USB-C PD输入；物理供电能力不能因旧角色标签而被误报为删除。'}}
        return project,decisions

    def customer_project(self):
        text='这两个候选型号暂不锁定，由设计补齐资料比较，但整机频段和接口目标不变。'
        doc=empty_document()
        doc['device_references']=[{'id':'REF-001','mention':'AD9363','intent':'undecided',
                                  'lifecycle':'active','purpose':'待设计比较',
                                  'source_message_id':'customer','source_quote':text}]
        p={'id':'customer-case','document':doc,'messages':[{'id':'customer','role':'user','content':text}],
           'review':{'status':'draft'},'domain':{'blockers':[],'active_jobs':[]},
           'input_queue':[],'latest_run':None}
        decision={'AD9363':{'intent':'undecided','source_message_id':'customer','source_quote':text,
                           'review_basis':'实际客户明确选择两个候选不锁定，委托设计补充资料比较，旧预设优先方案不再适用。'}}
        return p,decision

    def test_project_without_a_run_fails_semantics_without_crashing(self):
        project={'id':'empty-history','document':empty_document(),'messages':[],
                 'review':{'status':'draft'},'domain':{'blockers':[],'active_jobs':[]},
                 'input_queue':[],'latest_run':None}
        result=check_case('CASE-09',project)
        self.assertFalse(result['ok'])
        self.assertIn('尚未达到文档审阅门槛',result['failures'])
        self.assertTrue(any('缺少独立器件引用' in row for row in result['failures']))

    def test_actual_customer_intent_does_not_require_a_legacy_persona_choice(self):
        project,decision=self.customer_project()
        legacy=check_case('CASE-03',project)
        actual=check_case('CASE-03',project,intent_decisions=decision)
        self.assertTrue(any('最新器件意图不符' in f for f in legacy['failures']))
        self.assertFalse(any('最新器件意图不符' in f for f in actual['failures']))
        self.assertFalse(actual['ok'])
        self.assertTrue(any('实际需求缺少目标事实' in f for f in actual['failures']))

    def test_fabricated_customer_decision_cannot_change_the_expected_intent(self):
        project,decision=self.customer_project()
        decision['AD9363']['source_quote']='这里没有真实用户说过的话。'
        result=check_case('CASE-03',project,intent_decisions=decision)
        self.assertTrue(any('缺少真实客户决定' in f for f in result['failures']))
        self.assertTrue(any('最新器件意图不符' in f for f in result['failures']))

    def test_decision_must_have_an_independent_human_review_explanation(self):
        project,decision=self.customer_project()
        decision['AD9363']['review_basis']=''
        result=check_case('CASE-03',project,intent_decisions=decision)
        self.assertTrue(any('缺少真实客户决定' in f for f in result['failures']))

    def test_customer_power_role_can_change_without_removing_either_input(self):
        project,decisions=self.power_role_project()
        legacy=check_case('CASE-12',project)
        actual=check_case('CASE-12',project,role_decisions=decisions)
        self.assertIn('实际需求缺少目标事实：备用',legacy['failures'])
        self.assertNotIn('实际需求缺少目标事实：备用',actual['failures'])
        self.assertEqual(actual['reviewed_customer_roles'],['dc_input_role'])
        self.assertFalse(actual['ok'])
        self.assertTrue(any('缺少独立器件引用' in f for f in actual['failures']))

    def test_power_role_requires_a_real_quote_and_explicit_human_review(self):
        project,decisions=self.power_role_project()
        for field,value in [('source_quote','没有客户说过这句话。'),('source_message_id','unknown'),('review_basis','')]:
            with self.subTest(field=field):
                changed=copy.deepcopy(decisions);changed['dc_input_role'][field]=value
                result=check_case('CASE-12',project,role_decisions=changed)
                self.assertIn('实际需求缺少目标事实：备用',result['failures'])
                self.assertTrue(any('供电角色验收口径' in f for f in result['failures']))
        other=check_case('CASE-12',project,role_decisions={'unrelated_role':decisions['dc_input_role']})
        self.assertIn('实际需求缺少目标事实：备用',other['failures'])

    def test_power_role_cannot_hide_a_missing_dc_or_pd_function(self):
        project,decisions=self.power_role_project()
        for missing in ('REQ-001','REQ-002'):
            with self.subTest(missing=missing):
                changed=copy.deepcopy(project)
                changed['document']['requirements']=[r for r in changed['document']['requirements'] if r['id']!=missing]
                result=check_case('CASE-12',changed,role_decisions=decisions)
                self.assertFalse(result['reviewed_customer_roles'])
                self.assertIn('实际需求缺少目标事实：备用',result['failures'])
                self.assertTrue(any('两种输入' in f for f in result['failures']))


if __name__=='__main__':unittest.main()
