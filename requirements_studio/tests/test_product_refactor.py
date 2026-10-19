"""Focused guards for V3 reading, decision provenance and stale dependencies."""
import copy
import tempfile
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.domain import AnalysisResult,apply_analysis,empty_document,render_markdown
from backend.convergence import view
from backend.coverage import review_document
from backend.specification import specification_view
from backend.decisions import journey_view
from backend.experience import CORE


def requirement(rid,name,value,source='idea',fields=None,state='stated'):
    return dict(id=rid,category='functions',name=name,value=value,unit='',priority='must',state=state,
                source_message_id=source,source_quote=value,source_type='user',updated_at='2026-10-19T01:00:00+00:00',
                implementation='hardware',spec_field_ids=fields or [])


def response(**kwargs):
    payload=dict(reply='已保存选择。',title='桌面温度显示器',summary='室内桌面显示温度',
        updates=[],remove_ids=[],questions=[],flow=[],changes=['整理需求'],analysis_revision=3,guidance_revision=1)
    payload.update(kwargs)
    return AnalysisResult.model_validate(payload)



class ProductRefactorTests(unittest.TestCase):
    def setUp(self):
        self.doc=empty_document();self.doc.update(title='桌面温度显示器',version=1,convergence_version=2)
        self.doc['requirements']=[requirement('REQ-001','供电','先用电池','idea',['V3-3.1.1:F01']),
                                  requirement('REQ-002','显示温度','室内桌面显示温度',fields=['V3-2.1.1:F02'])]
        self.doc['field_resolutions']=[dict(field_id=fid,state='confirmed',conclusion='室内桌面显示温度',
            requirement_ids=['REQ-002'],source_message_id='idea',source_quote='室内桌面显示温度') for fid in CORE]
        self.doc['field_resolutions'].append(dict(field_id='V3-3.1.1:F01',state='confirmed',conclusion='先用电池',
            requirement_ids=['REQ-001','REQ-002'],source_message_id='idea',source_quote='先用电池'))
        self.doc['modules']=[dict(id='MOD-001',type_id='P_10_1',name='控制与显示',purpose='显示温度',boundary='实现待设计',quantity=1,state='confirmed',
            requirement_ids=['REQ-001','REQ-002'],source_message_id='idea',source_quote='室内桌面显示温度')]
        self.doc['flow']=[dict(label='显示温度',requirement_ids=['REQ-002'])]
        self.q=dict(id='power',question='怎样供电？',reason='影响是否需要充电',options=['用USB电源','用电池'],
            requirement_ids=['REQ-001'],field_ids=['V3-3.1.1:F01'],decision_key='power_mode',source_message_id='idea',status='pending')
        self.doc['questions']=[copy.deepcopy(self.q)]
        self.messages=[dict(id='idea',role='user',content='室内桌面显示温度，先用电池',created_at='2026-10-19T01:00:00+00:00'),
                       dict(id='choice',role='user',content='用USB电源',created_at='2026-10-19T02:00:00+00:00')]

    def test_normative_order_and_metadata_without_unused_modules(self):
        md=render_markdown(self.doc)
        positions=[md.index(f'## 第 {i} 章') for i in range(1,7)]
        self.assertEqual(positions,sorted(positions));self.assertIn('| 生成日期 |',md)
        self.assertIn('### 1.1 产品简介',md);self.assertIn('**核心功能一句话描述**',md)
        self.assertIn('### 4.2.1 功能测试',md)
        self.assertNotIn('Wi-Fi 子模块',md)
        before=copy.deepcopy(self.doc);spec=specification_view(self.doc,view(self.doc))
        self.assertEqual([c['number'] for c in spec['chapters']],[1,2,3,4]);self.assertEqual(self.doc,before)

    def test_performance_summary_reused_without_repeating_full_function(self):
        for field in self.doc['field_resolutions']:
            if field['field_id']=='V3-2.1.1:F03':field['conclusion']='与参考温度计相差约1°C以内。'
        spec=specification_view(self.doc,view(self.doc))
        fields={f['id']:f for c in spec['chapters'] for t in c['topics'] for f in t['fields']}
        self.assertEqual(fields['V3-2.1.1:F03']['display'],'summary')
        self.assertEqual(fields['V3-1.2:F02']['content'],'与参考温度计相差约1°C以内。')
        self.assertEqual(fields['V3-1.2:F02']['derived_from'],'V3-2.1.1:F03')
        self.assertEqual(fields['V3-1.3:F01']['state'],'tbd','A performance goal is not a confirmed cost bound')
        self.assertFalse(any(r['field_id']=='V3-1.2:F02' for r in self.doc['field_resolutions']))

    def test_old_summary_not_reused_while_related_requirement_is_rechecked(self):
        from backend.convergence import invalidate
        for field in self.doc['field_resolutions']:
            if field['field_id']=='V3-2.1.1:F03':field['conclusion']='旧目标相差1°C以内。'
        self.doc['requirements'][1]['value']='改为与参考温度计相差0.5°C以内'
        invalidate(self.doc,['REQ-002'])
        spec=specification_view(self.doc,view(self.doc))
        fields={f['id']:f for c in spec['chapters'] for t in c['topics'] for f in t['fields']}
        self.assertTrue(fields['V3-2.1.1:F03']['review_pending'])
        self.assertNotIn('旧目标',fields['V3-2.1.1:F03']['content'])
        self.assertNotEqual(fields['V3-1.2:F02']['state'],'confirmed')
        self.assertIn('0.5°C',render_markdown(self.doc))

    def test_current_values_replace_old_field_copy_in_download(self):
        self.doc['requirements'][0].update(value='使用USB 5V电源')
        self.doc['field_resolutions'][-1].update(conclusion='过时的电池结论，尚未确定')
        md=render_markdown(self.doc)
        self.assertIn('使用USB 5V电源',md);self.assertNotIn('过时的电池结论',md)

    def test_answer_reopens_related_requirement_until_inspected(self):
        update=requirement('REQ-001','供电','用USB电源','choice',['V3-3.1.1:F01'])
        update.pop('source_type');update.pop('updated_at')
        doc=apply_analysis(self.doc,response(updates=[update],resolved_question_ids=['power']),self.messages,'choice')
        self.assertEqual([r['requirement_id'] for r in doc['decision_reviews']],['REQ-002'])
        self.assertNotIn('power',[q['id'] for q in doc['questions']])
        self.assertEqual(review_document(doc)['status'],'draft')
        continued=apply_analysis(doc,response(reviewed_requirement_ids=['REQ-002']),self.messages,'choice',continuation=True)
        self.assertEqual(continued['decision_reviews'],[])
        self.assertEqual(continued['requirements'][1]['value'],'室内桌面显示温度')

    def test_internal_review_cannot_confirm_an_unsettled_requirement(self):
        self.doc['requirements'][1]['state']='tbd'
        self.doc['decision_reviews']=[dict(requirement_id='REQ-002',message_id='choice',reason='待内部检查')]
        self.doc['guidance_version']=1
        doc=apply_analysis(self.doc,response(reviewed_requirement_ids=['REQ-002']),self.messages,'choice',continuation=True)
        self.assertEqual(doc['requirements'][1]['state'],'tbd');self.assertEqual(review_document(doc)['status'],'draft')

    def test_invalid_normative_reference_rejected(self):
        update=requirement('REQ-001','供电','用USB电源','choice',['V3-imaginary'])
        update.pop('source_type');update.pop('updated_at')
        with self.assertRaisesRegex(ValueError,'规范条款'):
            apply_analysis(self.doc,response(updates=[update]),self.messages,'choice')
        with self.assertRaisesRegex(ValueError,'不存在的需求'):
            apply_analysis(self.doc,response(reviewed_requirement_ids=['REQ-999']),self.messages,'choice')

    def test_path_preserves_real_answer_and_shows_current_revision(self):
        first=copy.deepcopy(self.doc);first.update(updated_at='2026-10-19T01:10:00+00:00')
        second=copy.deepcopy(first);second.update(version=2,updated_at='2026-10-19T02:10:00+00:00',questions=[])
        second['question_history']=[{**self.q,'resolved_by':'choice'}];second['requirements'][0]['value']='用USB电源'
        second['decision_event']=dict(version=2,message_id='choice',reply='已改为USB供电。')
        third=copy.deepcopy(second);third.update(version=3,updated_at='2026-10-19T03:10:00+00:00')
        third['requirements'][0].update(value='改成可充电电池',source_message_id='change')
        third['decision_event']=dict(version=3,message_id='change',reply='按你的修改改为可充电电池。')
        messages=self.messages+[dict(id='change',role='user',content='改成可充电电池',created_at='2026-10-19T03:00:00+00:00')]
        runs=[dict(message_id='choice',status='completed',answers=[dict(question_id='power',answer='用USB电源',deferred=False)])]
        path=journey_view(third,messages,[first,second,third],runs,[])
        b=path['turns'][0]['branches'][0]
        self.assertEqual(b['state'],'answered');self.assertEqual(b['answer'],'用USB电源');self.assertTrue(b['adjusted'])
        self.assertEqual(path['turns'][-1]['current_results'],['REQ-001'])
        self.assertEqual(path['turns'][1]['analyses'],['已改为USB供电。'])

    def test_manual_edit_retains_normative_binding_and_rechecks_dependencies(self):
        from backend.store import Store
        root=Path(__file__).resolve().parents[1]/'.runtime/tests';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store=Store(Path(directory)/'studio.sqlite3');project=store.create_project()
            doc=copy.deepcopy(self.doc);doc['guidance_version']=1
            with store.connect() as db:store._save(db,project['id'],doc,1)
            update={k:self.doc['requirements'][0][k] for k in ('id','category','name','value','unit','priority','state','implementation')}
            update['value']='改用USB电源'
            edited=store.edit_requirement(project['id'],1,update)
            self.assertEqual(edited['document']['requirements'][0]['spec_field_ids'],['V3-3.1.1:F01'])
            self.assertEqual([r['requirement_id'] for r in edited['document']['decision_reviews']],['REQ-002'])
            self.assertEqual(edited['review']['status'],'draft')

    def test_silent_internal_check_keeps_trace_without_duplicate_chat(self):
        from backend.store import Store
        root=Path(__file__).resolve().parents[1]/'.runtime/tests';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store=Store(Path(directory)/'studio.sqlite3');project=store.create_project()
            run,_=store.begin_run(project['id'],'室内桌面显示温度，先用电池','first')
            doc=copy.deepcopy(self.doc);doc['guidance_version']=1
            store.commit_analysis(run['id'],doc,'先确定供电方式。')
            following=store.advance(run['id'],'核对已有用途，不替用户选择')
            self.assertIsNotNone(following)
            current=store.get_project(project['id'])['document']
            store.commit_analysis(following['id'],current,'已完成关联复核。')
            final=store.get_project(project['id'])
            self.assertEqual([m['content'] for m in final['messages'] if m['role']=='assistant'],['先确定供电方式。'])
            self.assertTrue(any(e.get('phase')=='review' for r in final['run_activity'] for e in r['events']))

    def test_internal_readiness_transition_notifies_the_customer(self):
        from backend.store import Store
        root=Path(__file__).resolve().parents[1]/'.runtime/tests';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store=Store(Path(directory)/'studio.sqlite3');project=store.create_project()
            run,_=store.begin_run(project['id'],'室内桌面显示温度，先用电池','first')
            doc=copy.deepcopy(self.doc);doc.update(guidance_version=1,questions=[],decision_reviews=[dict(requirement_id='REQ-001',message_id=run['message_id'],reason='复核关联内容')])
            store.commit_analysis(run['id'],doc,'已记录，正在完成关联复核。')
            following=store.advance(run['id'],'核对已有范围')
            current=store.get_project(project['id'])['document'];current['decision_reviews']=[]
            store.commit_analysis(following['id'],current,'已有决定保持有效。')
            final=store.get_project(project['id'])
            self.assertEqual(final['review']['status'],'ready')
            replies=[m['content'] for m in final['messages'] if m['role']=='assistant']
            self.assertEqual(len(replies),2);self.assertIn('整体审阅本版内容',replies[-1])
            self.assertEqual(final['document']['decision_event']['reply'],replies[-1])

    def test_renamed_branch_keeps_its_first_source_and_id(self):
        incoming={**self.q,'id':'accidental-new-id','question':'这版采用哪种供电？'}
        incoming.pop('source_message_id');incoming.pop('status')
        doc=apply_analysis(self.doc,response(questions=[incoming]),self.messages,'choice')
        self.assertEqual(len(doc['questions']),1)
        self.assertEqual(doc['questions'][0]['id'],'power')
        self.assertEqual(doc['questions'][0]['source_message_id'],'idea')

    def test_internal_source_refresh_is_not_a_new_product_reply(self):
        from backend.decisions import public_change
        after=copy.deepcopy(self.doc)
        after['requirements'][0].update(source_message_id='choice',updated_at='later')
        after['field_resolutions'][0]['conclusion']='依据原话完成了内部复核'
        self.assertFalse(public_change(self.doc,after))
        after['requirements'][0]['value']='用USB电源'
        self.assertTrue(public_change(self.doc,after))

    def test_submitted_answer_is_processing_not_completed(self):
        receipt=dict(question_id='power',answer='用USB电源',deferred=False,status='processing')
        path=journey_view(self.doc,self.messages,[self.doc],[],[receipt])
        b=path['turns'][0]['branches'][0]
        self.assertEqual(b['state'],'processing');self.assertFalse(b['answer_message_id'])
        self.assertEqual(b['options'],self.q['options'])

    def test_old_internal_closure_never_becomes_customer_choice(self):
        self.doc['questions']=[];self.doc['question_history']=[{**self.q,'resolved_by':'field-evidence'}]
        path=journey_view(self.doc,self.messages,[self.doc],[],[])
        b=path['turns'][0]['branches'][0]
        self.assertEqual(b['state'],'organized');self.assertEqual(b['answer'],'')

if __name__=='__main__':unittest.main(verbosity=2)
