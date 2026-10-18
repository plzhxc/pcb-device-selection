import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.coverage import CATALOG, review_document
from backend.domain import AnalysisResult, apply_analysis
from backend.store import Conflict,Store,MAX_INTERNAL_CONTINUATIONS


def response(**fields):
    return AnalysisResult.model_validate({"reply":"继续整理。","title":"测试产品","summary":"按用户需求整理。","updates":[],"remove_ids":[],"questions":[],"flow":[],"changes":["补充需求。"],**fields})


def question(qid):
    return {"id":qid,"question":f"请明确 {qid} 的要求？","reason":"补齐需求。","options":[],"requirement_ids":[]}


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        folder=ROOT/'.runtime/tests';folder.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=folder)
        self.store=Store(Path(self.temp.name)/'studio.sqlite3')
        self.project=self.store.create_project(example=True)
        self.doc=self.project['document']
        self.doc['questions']=[question('accuracy'),question('platform'),question('range')]
        self.messages=self.project['messages']+[{'id':'answer','role':'user','content':'Android；未列出的实现细节由设计阶段决定。'}]

    def tearDown(self):self.temp.cleanup()

    def test_partial_answer_keeps_other_branches_and_accepts_more_than_three_total(self):
        after=apply_analysis(self.doc,response(resolved_question_ids=['platform'],questions=[question('size'),question('battery'),question('test')]),self.messages,'answer')
        self.assertEqual({q['id'] for q in after['questions']},{'accuracy','range','size','battery','test'})
        self.assertEqual([q['id'] for q in self.doc['questions']],['accuracy','platform','range'])

    def test_deferred_answer_stays_open_even_if_model_resolves_it(self):
        after=apply_analysis(self.doc,response(resolved_question_ids=['accuracy']),self.messages,'answer',[{'question_id':'accuracy','answer':'不确定','deferred':True}])
        self.assertEqual(next(q for q in after['questions'] if q['id']=='accuracy')['status'],'deferred')

    def test_incomplete_card_answer_is_not_automatically_treated_as_resolution(self):
        after=apply_analysis(self.doc,response(),self.messages,'answer',[{'question_id':'accuracy','answer':'还没想好，先聊其他部分','deferred':False}])
        self.assertIn('accuracy',[q['id'] for q in after['questions']])

    def test_background_analysis_cannot_resolve_user_questions(self):
        with self.assertRaisesRegex(ValueError,'代替用户回答'):
            apply_analysis(self.doc,response(resolved_question_ids=['accuracy']),self.messages,'answer',continuation=True)
        after=apply_analysis(self.doc,response(questions=[question('new_branch')]),self.messages,'answer',continuation=True)
        self.assertEqual(len(after['questions']),4)

    def test_pause_preserves_pending_questions_and_not_reviewed(self):
        after=apply_analysis(self.doc,response(pause_questions=True),self.messages,'answer')
        self.assertTrue(after['questions_paused']);self.assertEqual(len(after['questions']),3)
        self.assertEqual(review_document(after)['status'],'draft')

    def test_new_ids_do_not_duplicate_identical_question_text(self):
        duplicate={**question('new-id'),'question':self.doc['questions'][0]['question']}
        after=apply_analysis(self.doc,response(questions=[duplicate]),self.messages,'answer')
        self.assertEqual(len(after['questions']),3)

    def test_identical_wording_for_different_requirement_branches_is_preserved(self):
        self.doc['questions']=[{**question('temperature_accuracy'),'question':'需要多高的精度？','requirement_ids':['REQ-002']}]
        incoming={**question('other_accuracy'),'question':'需要多高的精度？','requirement_ids':['REQ-003']}
        after=apply_analysis(self.doc,response(questions=[incoming]),self.messages,'answer')
        self.assertEqual({q['id'] for q in after['questions']},{'temperature_accuracy','other_accuracy'})

    def test_zero_questions_does_not_mean_complete_and_gets_next_gap(self):
        after=apply_analysis(self.doc,response(resolved_question_ids=['accuracy','platform','range']),self.messages,'answer')
        self.assertEqual(review_document(after)['closed'],0)
        self.assertEqual(review_document(after)['status'],'draft')
        self.assertEqual(len(after['questions']),1)
        self.assertTrue(after['questions'][0]['id'].startswith('gap-V3-'))

    def test_fabricated_coverage_evidence_does_not_close_a_section(self):
        item={'topic_id':CATALOG[0]['id'],'state':'not_applicable','conclusion':'不适用。','requirement_ids':[],'source_message_id':'answer','source_quote':'用户从未说过的话'}
        after=apply_analysis(self.doc,response(coverage=[item]),self.messages,'answer')
        self.assertEqual(after['coverage'][0]['state'],'tbd');self.assertEqual(review_document(after)['closed'],0)

    def test_complete_review_requires_coverage_facts_questions_and_user_click(self):
        self.doc['questions']=[]
        self.doc['requirements']=[r for r in self.doc['requirements'] if r['state']=='stated']
        self.doc['coverage']=[{'topic_id':t['id'],'state':'design_stage','conclusion':'用户约定由设计阶段决定。','requirement_ids':[],'source_message_id':'answer','source_quote':'未列出的实现细节由设计阶段决定。'} for t in CATALOG]
        with self.store.connect() as db:self.store._save(db,self.project['id'],self.doc,2)
        self.assertEqual(self.store.get_project(self.project['id'])['review']['status'],'ready')
        approved=self.store.approve(self.project['id'],2)
        self.assertEqual(approved['review']['status'],'approved');self.assertEqual(approved['document']['version'],3)
        with self.assertRaises(Conflict):self.store.approve(self.project['id'],2)
        row=approved['document']['requirements'][0]
        change={k:row[k] for k in ['id','category','name','value','unit','priority','state']};change['value']='室外'
        modified=self.store.edit_requirement(self.project['id'],3,change)
        self.assertNotEqual(modified['review']['status'],'approved')

    def test_incomplete_document_cannot_be_approved(self):
        with self.assertRaisesRegex(Conflict,'章节缺口'):self.store.approve(self.project['id'],1)

    def test_changed_requirement_reopens_linked_coverage(self):
        self.doc['coverage']=[{'topic_id':'V3-1.1','state':'confirmed','conclusion':'室内。','requirement_ids':['REQ-001'],'source_message_id':self.doc['requirements'][0]['source_message_id'],'source_quote':'室内'}]
        update={k:self.doc['requirements'][0][k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']}
        update['value']='室内办公环境'
        after=apply_analysis(self.doc,response(updates=[update]),self.messages,'answer')
        self.assertEqual(after['coverage'][0]['state'],'tbd')
        self.assertEqual(after['recheck_topics'],['V3-1.1'])

    def test_source_refresh_does_not_reopen_semantically_identical_coverage(self):
        self.doc['coverage']=[{'topic_id':'V3-1.1','state':'confirmed','conclusion':'室内。','requirement_ids':['REQ-001'],'source_message_id':self.doc['requirements'][0]['source_message_id'],'source_quote':'室内'}]
        update={k:self.doc['requirements'][0][k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']}
        update.update(source_message_id='again',source_quote='室内环境')
        after=apply_analysis(self.doc,response(updates=[update]),self.messages+[{'id':'again','role':'user','content':'再次确认室内环境。'}],'again')
        self.assertEqual(after['coverage'][0]['state'],'confirmed');self.assertEqual(after['recheck_topics'],[])

    def test_internal_recheck_does_not_ask_the_user_to_repeat_known_facts(self):
        self.doc['questions']=[];self.doc['requirements']=[r for r in self.doc['requirements'] if r['state']=='stated']
        self.doc['coverage']=[{'topic_id':t['id'],'state':'design_stage','conclusion':'由设计阶段确定。','requirement_ids':['REQ-001'],'source_message_id':'answer','source_quote':'未列出的实现细节由设计阶段决定。'} for t in CATALOG]
        update={k:self.doc['requirements'][0][k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']};update['value']='办公室室内环境'
        after=apply_analysis(self.doc,response(updates=[update]),self.messages,'answer')
        self.assertEqual(len(after['recheck_topics']),50)
        self.assertEqual(after['questions'],[])
        self.assertEqual(review_document(after)['status'],'draft')

    def test_internal_recheck_can_continue_without_asking_a_new_question(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'补充验收细节','q0')
        doc=self.store.get_project(pid)['document'];doc['questions']=[];doc['recheck_topics']=['V3-1.1']
        self.store.commit_analysis(run['id'],doc,'已补充，继续复核关联章节。')
        following=self.store.advance(run['id'],'重新核对关联章节')
        self.assertEqual(following['kind'],'continuation')

    def test_legacy_document_generation_date_is_recovered_from_first_revision(self):
        self.doc.pop('created_at')
        with self.store.connect() as db:
            earliest=db.execute('SELECT created_at FROM revisions WHERE project_id=? ORDER BY version LIMIT 1',(self.project['id'],)).fetchone()['created_at']
            self.store._save(db,self.project['id'],self.doc,2)
        saved=self.store.get_project(self.project['id'])
        self.assertEqual(saved['document']['created_at'],earliest)
        self.assertIn('生成日期',saved['markdown'])

    def test_queued_inputs_are_fifo_idempotent_and_not_in_current_context(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'第一条','q0')
        _,_,queued=self.store.submit_message(pid,'第二条','q1')
        _,created,same=self.store.submit_message(pid,'第二条','q1')
        self.assertFalse(created);self.assertEqual(same['id'],queued['id'])
        self.store.submit_message(pid,'第三条','q2')
        p=self.store.get_project(pid);self.assertEqual(len(p['input_queue']),2)
        self.assertNotIn('第二条',[m['content'] for m in p['messages']])
        self.store.commit_analysis(run['id'],p['document'],'第一条已处理')
        following=self.store.advance(run['id'],'继续分析预算')
        self.assertEqual(following['request_id'],'q1');self.assertEqual(following['kind'],'user')
        self.assertEqual(self.store.get_project(pid)['messages'][-1]['content'],'第二条')

    def test_stop_does_not_autostart_queue_and_queue_can_resume(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'第一条','q0')
        self.store.submit_message(pid,'第二条','q1');self.store.finish(run['id'],'cancelled','停止')
        self.assertIsNone(self.store.advance(run['id'],'继续分析'))
        resumed=self.store.resume_queue(pid)
        self.assertEqual(resumed['request_id'],'q1')

    def test_stop_winning_a_race_with_followup_keeps_followup_paused(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'正在分析','q0')
        self.store.finish(run['id'],'cancelled','已停止')
        following,created,queued=self.store.submit_message(pid,'发送时仍在分析的补充','q1',after_run_id=run['id'])
        self.assertIsNone(following);self.assertFalse(created);self.assertEqual(queued['status'],'pending')
        self.assertEqual(self.store.get_project(pid)['latest_run']['status'],'cancelled')
        resumed=self.store.resume_queue(pid);self.assertEqual(resumed['request_id'],'q1')

    def test_queue_cancel_and_limit_preserve_unprocessed_data(self):
        pid=self.project['id'];self.store.submit_message(pid,'第一条','q0')
        for i in range(20):self.store.submit_message(pid,f'补充{i}',f'q{i+1}')
        with self.assertRaisesRegex(Conflict,'20 条'):self.store.submit_message(pid,'超过限制','overflow')
        before=self.store.get_project(pid);self.store.remove_queued(pid,before['input_queue'][0]['id'])
        self.assertEqual(len(self.store.get_project(pid)['input_queue']),19)
        self.store.submit_message(pid,'替代补充','replacement')
        self.assertEqual(len(self.store.get_project(pid)['input_queue']),20)

    def test_restart_retains_pending_inputs_and_question_backlog(self):
        pid=self.project['id']
        with self.store.connect() as db:self.store._save(db,pid,self.doc,2)
        self.store.submit_message(pid,'第一条','q0');self.store.submit_message(pid,'第二条','q1')
        self.store.recover();p=self.store.get_project(pid)
        self.assertEqual(p['latest_run']['status'],'interrupted');self.assertEqual(len(p['input_queue']),1)
        self.assertEqual(p['document']['questions'],self.project['document']['questions'])

    def test_waiting_for_user_does_not_repeat_independent_analysis(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'先梳理','q0')
        p=self.store.get_project(pid);self.store.commit_analysis(run['id'],p['document'],'已有问题，先分析其他部分')
        following=self.store.advance(run['id'],'根据每分钟采样推导记录数量')
        self.assertEqual(following['kind'],'continuation')
        self.assertEqual(len(self.store.get_project(pid)['messages']),4)
        doc=self.store.get_project(pid)['document'];self.store.commit_analysis(following['id'],doc,'独立部分已整理')
        self.assertIsNone(self.store.advance(following['id'],'不能无限继续'))

    def test_known_numeric_cleanup_continues_without_an_extra_user_prompt(self):
        pid=self.project['id'];text='在0.9V、1.2V、1.5V分别验收。'
        run,_,_=self.store.submit_message(pid,text,'numeric-cleanup')
        doc=self.store.get_project(pid)['document'];doc.update(convergence_version=2,questions=[])
        doc['requirements'].append(dict(id='REQ-010',category='performance',name='输入电压验收点',value=text,unit='V',priority='must',state='stated',source_message_id=run['message_id'],source_quote=text,source_type='user',updated_at=doc['updated_at']))
        doc['numeric_constraints']=[dict(id=f'NC-test-{index}',requirement_id='REQ-010',metric='voltage',scope='台架输入电压',conditions='离散测试点',minimum=value,maximum=value,unit='V',source_message_id=run['message_id'],source_quote=text) for index,value in enumerate((0.9,1.2,1.5))]
        self.store.commit_analysis(run['id'],doc,'已记录，继续核对实际关联。')
        first=self.store.advance(run['id'],'核对已有模块与验收边界')
        self.assertEqual(first['kind'],'continuation')
        current=self.store.get_project(pid)['document']
        self.store.commit_analysis(first['id'],current,'模块已核对，还需整理离散验收点。')
        second=self.store.advance(first['id'],'将不同验收点明确为各自独立工况')
        self.assertEqual(second['kind'],'continuation')
        current=self.store.get_project(pid)['document']
        for row in current['numeric_constraints']:row['conditions']=f"在{row['minimum']}V测试点分别验收"
        self.store.commit_analysis(second['id'],current,'离散工况表达已整理，保留用户数值。')
        self.assertIsNone(self.store.advance(second['id'],'不得为未知产品决定继续生成'))
        p=self.store.get_project(pid)
        self.assertFalse(p['review']['consistency_issues'])
        self.assertEqual([row['minimum'] for row in p['document']['numeric_constraints']],[0.9,1.2,1.5])
        self.assertEqual([m['content'] for m in p['messages'] if m['role']=='user'],[self.project['messages'][0]['content'],text])

    def test_internal_work_is_bounded_to_three_continuations(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'整理已有决定','bounded-cleanup')
        for index in range(MAX_INTERNAL_CONTINUATIONS+1):
            doc=self.store.get_project(pid)['document'];doc.update(questions=[],recheck_topics=['V3-1.1'])
            self.store.commit_analysis(run['id'],doc,'保持待复核事项，不伪造完成。')
            following=self.store.advance(run['id'],f'尚待核对的独立事项{index}')
            if index==MAX_INTERNAL_CONTINUATIONS:self.assertIsNone(following)
            else:self.assertEqual(following['kind'],'continuation');run=following
        self.assertEqual(sum(m['role']=='user' for m in self.store.get_project(pid)['messages']),2)

    def test_unchanged_internal_task_is_not_repeated(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'整理已有决定','no-repeat')
        doc=self.store.get_project(pid)['document'];doc.update(questions=[],recheck_topics=['V3-1.1'])
        self.store.commit_analysis(run['id'],doc,'已有内部事项。')
        following=self.store.advance(run['id'],'核对 已有事实')
        self.store.commit_analysis(following['id'],self.store.get_project(pid)['document'],'没有新增进展。')
        self.assertIsNone(self.store.advance(following['id'],'  核对  已有事实  '))

    def test_new_user_input_preempts_a_second_internal_step(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'先整理','preempt-0')
        doc=self.store.get_project(pid)['document'];doc['recheck_topics']=['V3-1.1']
        self.store.commit_analysis(run['id'],doc,'继续内部核对。')
        first=self.store.advance(run['id'],'复核已有边界')
        _,_,queued=self.store.submit_message(pid,'补充了新的目标','preempt-1')
        self.assertIsNotNone(queued)
        self.store.commit_analysis(first['id'],self.store.get_project(pid)['document'],'尚有内部事项。')
        following=self.store.advance(first['id'],'继续核对剩余事项')
        self.assertEqual(following['kind'],'user');self.assertEqual(following['request_id'],'preempt-1')

    def test_pause_stops_further_internal_steps_without_resolving_questions(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'先整理','pause-tail')
        doc=self.store.get_project(pid)['document'];doc['recheck_topics']=['V3-1.1']
        self.store.commit_analysis(run['id'],doc,'先分析独立部分。')
        first=self.store.advance(run['id'],'复核已有目标')
        doc=self.store.get_project(pid)['document'];questions=copy.deepcopy(doc['questions']);doc['questions_paused']=True
        self.store.commit_analysis(first['id'],doc,'暂停，保留未答事项。')
        self.assertIsNone(self.store.advance(first['id'],'不能继续触发自动整理'))
        self.assertEqual(self.store.get_project(pid)['document']['questions'],questions)

    def test_saved_analysis_queues_new_input_until_final_handoff(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'整理当前需求','finish-0')
        self.store.mark_running(run['id']);doc=self.store.get_project(pid)['document']
        self.store.commit_analysis(run['id'],doc,'本轮修改已保存。',finalize=False)
        p=self.store.get_project(pid)
        self.assertEqual(p['document']['version'],doc['version'])
        self.assertEqual(p['latest_run']['status'],'running')
        with self.assertRaisesRegex(Conflict,'正在处理'):self.store.approve(pid,p['document']['version'])
        _,_,queued=self.store.submit_message(pid,'新补充优先处理','finish-1')
        self.assertIsNotNone(queued)
        following=self.store.advance(run['id'],'内部核对',finalize=True)
        self.assertEqual(self.store.get_run(run['id'])['status'],'completed')
        self.assertEqual(following['kind'],'user');self.assertEqual(following['request_id'],'finish-1')

    def test_completion_and_internal_followup_have_no_visible_idle_gap(self):
        from unittest.mock import patch
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'整理已有内容','atomic-tail')
        self.store.mark_running(run['id']);doc=self.store.get_project(pid)['document'];doc['recheck_topics']=['V3-1.1']
        self.store.commit_analysis(run['id'],doc,'已保存，仍需内部核对。',finalize=False)
        entered=threading.Event();release=threading.Event();results=[];errors=[];original=self.store._begin_run
        def delayed_begin(*args,**kwargs):
            entered.set()
            if not release.wait(20):raise RuntimeError('fixture handoff was not released')
            return original(*args,**kwargs)
        def advance():
            try:results.append(self.store.advance(run['id'],'完成已有内容的内部核对',finalize=True))
            except BaseException as exc:errors.append(exc)
        with patch.object(self.store,'_begin_run',side_effect=delayed_begin):
            worker=threading.Thread(target=advance);worker.start()
            try:
                self.assertTrue(entered.wait(20))
                # Completion has been updated inside the writer transaction,
                # but another connection still sees active work until both
                # completion and the next task are committed together.
                self.assertEqual(self.store.get_run(run['id'])['status'],'running')
            finally:release.set();worker.join(20)
        self.assertFalse(worker.is_alive());self.assertFalse(errors,errors)
        p=self.store.get_project(pid)
        self.assertEqual(self.store.get_run(run['id'])['status'],'completed')
        self.assertEqual(p['latest_run']['id'],results[0]['id']);self.assertEqual(p['latest_run']['status'],'queued')

    def test_restart_after_document_save_keeps_document_and_interrupts_finishing(self):
        pid=self.project['id'];run,_,_=self.store.submit_message(pid,'整理本轮补充','restart-finishing')
        self.store.mark_running(run['id']);doc=self.store.get_project(pid)['document'];doc['summary']='本轮已经安全保存的摘要。'
        self.store.commit_analysis(run['id'],doc,'已保存，正在完成收尾。',finalize=False)
        self.store.recover();p=self.store.get_project(pid)
        self.assertEqual(p['latest_run']['status'],'interrupted')
        self.assertEqual(p['document']['summary'],'本轮已经安全保存的摘要。')
        self.assertTrue(p['analysis_pending'])


if __name__=='__main__':unittest.main(verbosity=2)
