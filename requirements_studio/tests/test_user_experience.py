"""Regression checks for answer receipts and project-scoped requirements review."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.domain import AnalysisResult,apply_analysis,empty_document,render_markdown
from backend.convergence import FIELDS,view
from backend.experience import CORE,review_reply
from backend.coverage import review_document
from backend.store import Store,Conflict
from backend.evidence import literal_span


class UserExperienceTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/'.runtime/tests';folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.store=Store(Path(self.temp.name)/'studio.sqlite3');self.p=self.store.create_project();self.pid=self.p['id']
        self.doc=empty_document();self.doc.update(version=1,convergence_version=2,title='桌面温度显示器')
        self.quote='在室内桌面查看温度，0到50度，精度正负0.5度；用本地屏幕显示。'
        self.messages=[dict(id='owner',role='user',content=self.quote)]
        self.doc['requirements']=[dict(id='REQ-001',category='functions',name='显示温度',value=self.quote,unit='',priority='must',state='stated',source_message_id='owner',source_quote=self.quote,source_type='user',updated_at=self.doc['updated_at'])]
        self.doc['flow']=[dict(label='采集并显示温度',requirement_ids=['REQ-001'])]

    def tearDown(self):self.temp.cleanup()

    def save(self,doc):
        with self.store.connect() as db:self.store._save(db,self.pid,doc,doc['version'])

    def questions(self):
        self.doc['questions']=[dict(id=q,question=q+'?',reason='影响使用',options=['A','B'],requirement_ids=['REQ-001']) for q in ('q1','q2')]
        self.save(self.doc)

    @staticmethod
    def answer(qid):return [dict(question_id=qid,answer='A',deferred=False)]

    def complete(self):
        result=AnalysisResult.model_validate(dict(reply='已整理。',title=self.doc['title'],summary=self.quote,updates=[],remove_ids=[],questions=[],flow=self.doc['flow'],changes=['整理产品要求'],analysis_revision=2,
            field_resolutions=[dict(field_ids=sorted(CORE),state='confirmed',conclusion=self.quote,requirement_ids=['REQ-001'],source_message_id='owner',source_quote=self.quote)],
            module_updates=[dict(id='new-module-display',type_id='P_8_2',name='温度显示',purpose='在本地显示温度',boundary='器件与接口待设计，不影响本地显示的目标',quantity=1,state='confirmed',requirement_ids=['REQ-001'],source_message_id='owner',source_quote=self.quote)]))
        return apply_analysis(self.doc,result,self.messages,'owner')

    def test_active_answer_is_locked_persisted_and_request_retry_is_idempotent(self):
        self.questions();run,created,_=self.store.submit_message(self.pid,'答q1','one',self.answer('q1'))
        self.assertTrue(created)
        same,created,_=self.store.submit_message(self.pid,'答q1','one',self.answer('q1'))
        self.assertFalse(created);self.assertEqual(same['id'],run['id'])
        with self.assertRaisesRegex(Conflict,'已经收到'):self.store.submit_message(self.pid,'重复q1','two',self.answer('q1'))
        fresh=Store(self.store.path).get_project(self.pid)
        self.assertEqual(fresh['question_submissions'][0]['status'],'processing')
        self.assertEqual(len(fresh['messages']),1)

    def test_different_branch_can_queue_and_duplicate_is_rejected(self):
        self.questions();self.store.submit_message(self.pid,'答q1','one',self.answer('q1'))
        _,created,item=self.store.submit_message(self.pid,'答q2','two',self.answer('q2'))
        self.assertFalse(created);self.assertIsNotNone(item)
        self.assertEqual({s['question_id']:s['status'] for s in self.store.get_project(self.pid)['question_submissions']},{'q1':'processing','q2':'queued'})
        with self.assertRaises(Conflict):self.store.submit_message(self.pid,'重复q2','three',self.answer('q2'))
        self.store.submit_message(self.pid,'更正：上一条选择需要补充条件。','free')
        self.assertEqual(len(self.store.get_project(self.pid)['input_queue']),2)

    def test_removing_queued_answer_restores_that_question_only(self):
        self.questions();self.store.submit_message(self.pid,'答q1','one',self.answer('q1'))
        _,_,item=self.store.submit_message(self.pid,'答q2','two',self.answer('q2'))
        self.store.remove_queued(self.pid,item['id'])
        self.assertEqual([s['question_id'] for s in self.store.get_project(self.pid)['question_submissions']],['q1'])

    def test_failed_answer_is_saved_and_explicit_retry_reuses_original_message(self):
        self.questions();run,_,_=self.store.submit_message(self.pid,'答q1','one',self.answer('q1'))
        self.store.finish(run['id'],'failed','测试故障')
        self.assertEqual(self.store.get_project(self.pid)['question_submissions'][0]['status'],'failed')
        retry,_=self.store.begin_run(self.pid,'','retry',retry=True)
        self.assertEqual(json.loads(retry['answers']),self.answer('q1'))
        self.assertEqual(len(self.store.get_project(self.pid)['messages']),1)

    def test_resolved_question_rejects_stale_new_request_but_accepts_old_retry(self):
        self.questions();run,_,_=self.store.submit_message(self.pid,'答q1','one',self.answer('q1'))
        done=copy.deepcopy(self.doc);done['questions']=done['questions'][1:]
        self.store.commit_analysis(run['id'],done,'已整理')
        self.assertEqual(self.store.get_project(self.pid)['question_submissions'],[])
        self.assertFalse(self.store.submit_message(self.pid,'答q1','one',self.answer('q1'))[1])
        with self.assertRaisesRegex(Conflict,'已完成处理'):self.store.submit_message(self.pid,'重复','new',self.answer('q1'))

    def test_unknown_engineering_parameters_do_not_become_user_confirmations(self):
        doc=self.complete();review=review_document(doc);scope=view(doc)['product_review']
        self.assertEqual(review['status'],'ready');self.assertEqual(scope['decision_total'],5)
        self.assertGreater(scope['engineering_pending'],0)
        self.assertEqual(view(doc)['fields_closed'],5)
        self.assertTrue(all(f['state']=='tbd' for f in view(doc)['fields'] if f['id'] not in CORE))
        self.assertNotIn('V3-1.1:F01',scope['decision_pending'],'working name is not a separate product decision')

    def test_actual_product_gap_still_blocks_review(self):
        doc=self.complete();doc['field_resolutions']=[f for f in doc['field_resolutions'] if f['field_id']!='V3-2.1.1:F03']
        self.assertEqual(review_document(doc)['status'],'draft')
        self.assertIn('V3-2.1.1:F03',view(doc)['product_review']['decision_pending'])

    def test_real_conflict_question_or_workflow_gap_still_blocks_review(self):
        for mutate in (lambda d:d.update(flow=[]),lambda d:d['requirements'][0].update(state='conflict'),lambda d:d['requirements'][0].update(state='tbd'),lambda d:d.update(questions=[dict(id='risk',question='用于医疗吗？',reason='影响安全边界',options=[],requirement_ids=['REQ-001'])])):
            doc=self.complete();mutate(doc);self.assertEqual(review_document(doc)['status'],'draft')

    def test_project_document_excludes_unselected_catalogue_and_removed_graph_nodes(self):
        doc=self.complete();removed={**doc['modules'][0],'id':'MOD-999','name':'不再使用的蜂鸣器','type_id':'P_8_3','state':'not_applicable'}
        doc['modules'].append(removed);doc['module_links']=[dict(id='old',source=doc['modules'][0]['id'],target='MOD-999',kind='control',description='旧方案',source_port='',target_port='',state='design_stage')]
        before=copy.deepcopy(doc);md=render_markdown(doc)
        self.assertIn('温度显示',md);self.assertIn('LCD与OLED显示模块',md)
        for unused in ('不再使用的蜂鸣器','MOD_999','Wi-Fi 子模块','充电管理子模块','全部 247','字段核对与来源'):self.assertNotIn(unused,md)
        self.assertEqual(doc,before)

    def test_activity_survives_reload_with_timestamps_without_private_reasoning(self):
        run,_=self.store.begin_run(self.pid,'需求','one')
        self.store.event(run['id'],'progress',dict(message='正在整理产品决定',phase='context'))
        self.store.event(run['id'],'reasoning',dict(message='不应进入界面的私有推理'))
        for n in range(3):self.store.event(run['id'],'progress',dict(message=f'已等待{n}',phase='waiting'))
        self.store.finish(run['id'],'completed')
        events=Store(self.store.path).get_project(self.pid)['run_activity'][0]['events']
        self.assertTrue(all('at' in e for e in events))
        self.assertEqual(sum(e.get('phase')=='waiting' for e in events),1)
        self.assertFalse(any(e['kind']=='reasoning' for e in events))

    def test_field_only_project_decision_is_exported_without_unused_module_fields(self):
        doc=self.complete()
        doc['field_resolutions'] += [dict(field_id='V3-1.3:F07',state='confirmed',conclusion='本版没有硬性交期要求。',requirement_ids=[],source_message_id='owner',source_quote=self.quote),
            dict(field_id='V3-5.2.4.1:F01',state='confirmed',conclusion='未采用的WiFi模板不应进入正文。',requirement_ids=[],source_message_id='owner',source_quote=self.quote)]
        md=render_markdown(doc)
        self.assertIn('本版没有硬性交期要求',md)
        self.assertNotIn('未采用的WiFi模板不应进入正文',md)

    def test_model_cannot_claim_ready_while_module_plan_is_still_a_proposal(self):
        doc=self.complete();doc['modules'][0]['state']='proposed'
        for claim in ('需求已整理好，现可整体审核。','核心需求和模块草案可供整体审核。'):
            reply=review_reply(claim,doc)
            self.assertNotEqual(reply,claim);self.assertIn('模块类别、用途和数量',reply)
            self.assertIn('草稿',reply)
        ready=self.complete();self.assertEqual(review_reply('可以整体审核。',ready),'可以整体审核。')
        self.assertEqual(review_reply('确认方案后可以整体审核。',doc),'确认方案后可以整体审核。')

    def test_joined_whole_clauses_recover_a_real_contiguous_span(self):
        source='倒计时到零后闪烁结束，按一下回到25分钟。开机先显示25:00，不要自动开始。'
        span=literal_span('倒计时到零后闪烁结束。开机先显示25:00，不要自动开始。',source)
        self.assertIn('按一下回到25分钟',span);self.assertIn(span,source)

    def test_quote_recovery_never_changes_negation_numbers_or_fabricated_words(self):
        source='USB供电，不需要电池。预算不超过60元，不需要联网。'
        for quote in ('USB供电，需要电池。','预算不超过80元，不需要联网。','USB供电，采用从未提出的芯片。','不需要联网，USB供电。'):
            self.assertIsNone(literal_span(quote,source))
        self.assertIsNone(literal_span('第一句。最后一句。','第一句。'+('很长的原始内容'*500)+'。最后一句。'))

    def test_known_usb_use_does_not_force_unrequested_power_regulatory_or_tolerance_limits(self):
        doc=self.complete();extra=dict(id='usb',role='user',content='USB-C 5V供电，不用于医疗或安全控制。')
        doc['questions']=[dict(id='gap-V3-1.3',question='请补充功耗与法规约束',reason='旧自动模板缺口',options=[],requirement_ids=[],field_ids=['V3-1.3:F04','V3-1.3:F06'])]
        fields=[dict(field_ids=[fid],state='tbd',conclusion='已知USB-C 5V和普通室内用途，未提出额外硬性限值，实现待设计核对。',requirement_ids=['REQ-001'],source_message_id='usb',source_quote=extra['content']) for fid in ('V3-1.3:F04','V3-1.3:F06','V3-3.1.1:F01')]
        result=AnalysisResult.model_validate(dict(reply='已核对已知用途。',title=doc['title'],summary=doc['summary'],updates=[],remove_ids=[],questions=[],flow=[],changes=['保留未知实现边界'],analysis_revision=2,field_resolutions=fields))
        after=apply_analysis(doc,result,self.messages+[extra],'usb')
        self.assertEqual(review_document(after)['status'],'ready');self.assertEqual(after['questions'],[])
        self.assertTrue(all(f['state']=='tbd' for f in view(after)['fields'] if f['id'] in ('V3-1.3:F04','V3-1.3:F06','V3-3.1.1:F01')))
        from backend.convergence import invalidate
        invalidate(after,['REQ-001'])
        pending=view(after)['product_review']['decision_pending']
        self.assertTrue(CORE<=set(pending))
        self.assertFalse(set(pending)&{'V3-1.3:F04','V3-1.3:F06','V3-3.1.1:F01'})


if __name__=='__main__':unittest.main(verbosity=2)
