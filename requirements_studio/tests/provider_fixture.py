"""Controlled CLI fixture for UI load/failure tests; never used in production.

Real model acceptance runs separately with the installed Codex binary. Each
fixture receipt is explicitly marked so the evidence cannot be confused.
"""
import json
import sys
import time
from pathlib import Path

prompt=sys.stdin.read()
context=json.loads(prompt.split('以下 JSON 为用户数据：\n',1)[1])
from provider_context import decode_quotes
context=decode_quotes(context)
doc=context['current_document'];messages=context['messages'];mid=context['current_message_id']
text=next(m['content'] for m in messages if m['id']==mid)
answer_path=Path(sys.argv[sys.argv.index('--output-last-message')+1])
(answer_path.parent/'test-provider.json').write_text(json.dumps({'provider':'controlled_cli_fixture','real_inference':False}),encoding='utf-8')
print(json.dumps({'type':'turn.started'}),flush=True)
time.sleep(30 if '[压力阻塞]' in text else .05 if '[压力补充]' in text else 8 if '[慢分析]' in text or context['analysis_mode']=='continuation' else 1.5)
marker=answer_path.parents[2]/'fixture-failures'/mid
first_failure=not marker.exists()
if any(flag in text for flag in ['[测试工具越界]','[测试调用失败]','[测试损坏JSON]','[测试无效编号]']) and first_failure:
    marker.parent.mkdir(exist_ok=True);marker.write_text('first failure injected',encoding='utf-8')
if '[测试工具越界]' in text and first_failure:
    print(json.dumps({'type':'item.started','item':{'type':'command_execution'}}),flush=True)
    time.sleep(20)
if '[测试调用失败]' in text and first_failure:
    raise SystemExit(1)
if '[测试损坏JSON]' in text and first_failure:
    answer_path.write_text('bad json',encoding='utf-8');raise SystemExit(0)

updates=[];questions=[];resolved=[];coverage=[]
if not doc['requirements']:
    updates=[{'id':'new-1','category':'functions','name':'核心功能','value':text,'unit':'','priority':'must','state':'stated','source_message_id':mid,'source_quote':text}]
    questions=[{'id':'usage','question':'设备会用在什么场景？','reason':'明确环境与使用边界。','options':['室内','室外'],'requirement_ids':['new-1']}]
for answer in context.get('question_answers',[]):
    q=next((q for q in doc['questions'] if q['id']==answer['question_id']),None)
    if not q or answer.get('deferred'):continue
    resolved.append(q['id'])
    row=next((r for r in doc['requirements'] if r['id'] in q['requirement_ids']),None)
    if row:
        update={k:row[k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']}
        update.update(value=answer['answer'],state='stated',source_message_id=mid,source_quote=answer['answer']);updates.append(update)
if '增加报警' in text and context['analysis_mode']=='user':
    updates.append({'id':'new-alarm','category':'functions','name':'报警功能','value':'蜂鸣器报警','unit':'','priority':'must','state':'stated','source_message_id':mid,'source_quote':'增加报警'})
    questions.append({'id':'alarm_threshold','question':'报警阈值需要多少？','reason':'定义报警触发条件。','options':['高于30°C','高于40°C'],'requirement_ids':['new-alarm']})
if '修改场景为室内办公区' in text and context['analysis_mode']=='user':
    row=next(r for r in doc['requirements'] if r['id']=='REQ-001')
    update={k:row[k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']}
    update.update(value='室内办公区',state='stated',source_message_id=mid,source_quote='修改场景为室内办公区');updates.append(update)
if context['analysis_mode']=='continuation':
    if doc.get('recheck_topics'):
        proof=next(m for m in messages if m['role']=='user' and '未列出的实现细节由后续设计阶段决定' in m['content'])
        coverage=[{'topic_id':tid,'state':'design_stage','conclusion':'沿用用户已经约定的设计边界，已针对场景修改重新核对。','requirement_ids':['REQ-001'],'source_message_id':proof['id'],'source_quote':'未列出的实现细节由后续设计阶段决定'} for tid in doc['recheck_topics']]
    else:questions=[{'id':'size_branch','question':'外形尺寸有上限吗？','reason':'检查独立的结构约束分支。','options':[],'requirement_ids':['REQ-001'] if any(r['id']=='REQ-001' for r in doc['requirements']) else []}]
    reply='已继续整理功能与供电约束，未答问题仍保留。请按自己的节奏补充。'
else:reply='已记录本轮补充，其他分支和未答问题继续保留。'
result={'reply':reply,'title':doc['title'] if doc['requirements'] else '验收测试产品','summary':doc['summary'] or text,'updates':updates,'remove_ids':['REQ-999'] if '[测试无效编号]' in text and first_failure else [],'questions':questions,'resolved_question_ids':resolved,'next_analysis':'整理可独立推进的功能与供电约束' if '自动续分析' in text and context['analysis_mode']=='user' else '', 'pause_questions':'暂停提问' in text,'coverage':coverage, 'flow':doc['flow'] or [{'label':'执行核心功能','requirement_ids':['new-1']}],'changes':['记录本轮补充，保留其他需求。']}
answer_path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'type':'turn.completed'}),flush=True)
