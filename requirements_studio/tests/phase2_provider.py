"""Clearly marked deterministic provider for random UI/failure/load tests only."""
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.convergence import FIELDS,PRODUCT_CORE

context=json.loads(sys.stdin.read().split('以下 JSON 为用户数据：\n',1)[1])
from provider_context import decode_quotes
context=decode_quotes(context)
doc=context['current_document'];mid=context['current_message_id'];messages=context['messages']
doc['field_resolutions']=[{**{k:v for k,v in row.items() if k!='field_ids'},'field_id':fid} for row in doc.get('field_resolution_groups',[]) for fid in row['field_ids']] or doc.get('field_resolutions',[])
text=next(m['content'] for m in messages if m['id']==mid)
answer=Path(sys.argv[sys.argv.index('--output-last-message')+1])
(answer.parent/'test-provider.json').write_text(json.dumps({'provider':'controlled_phase2_fixture','real_inference':False}),encoding='utf-8')
print(json.dumps({'type':'turn.started'}),flush=True)
time.sleep(90 if '[队列容量阻塞]' in text else 12 if '[压力阻塞]' in text else 2 if '[慢分析]' in text else .08)
runtime=next(parent.parent for parent in answer.parents if parent.name=='runs')
marker=runtime/('injected-'+mid)
failures=int(marker.read_text()) if marker.exists() else 0
if ('[故障重试]' in text and failures<2) or ('[自动复核]' in text and failures<1):
    marker.write_text(str(failures+1),encoding='utf-8');answer.write_text('invalid-json',encoding='utf-8');raise SystemExit(0)
result=dict(reply='已沿用已有事实核对需求，未答分支会持续保留。',title=doc['title'],summary=doc['summary'],updates=[],remove_ids=[],questions=[],resolved_question_ids=[],next_analysis='',pause_questions=False,coverage=[],flow=doc['flow'],changes=['按本轮补充继续整理。'],analysis_revision=2,field_resolutions=[],module_updates=[],remove_module_ids=[],module_links=[],numeric_constraints=[])
for a in context.get('question_answers',[]):
    if not a.get('deferred'):result['resolved_question_ids'].append(a['question_id'])
if '[完成核对]' in text or '[复核已知]' in text:
    rid=doc['requirements'][0]['id']
    for state,ids in [('confirmed',list(PRODUCT_CORE)),('design_stage',[f for f in FIELDS if f not in PRODUCT_CORE])]:
        result['field_resolutions'].append(dict(field_ids=ids,state=state,conclusion='采用当前已确认的产品目标；其余已明确授权的实现细节由设计阶段确定，须满足产品指标并交由后续方案验证。',requirement_ids=[rid],source_message_id=mid,source_quote=text))
    result['resolved_question_ids']=[q['id'] for q in doc['questions']]
    for m in doc.get('modules',[]):
        result['module_updates'].append({k:v for k,v in m.items() if k in ['id','type_id','name','purpose','boundary','quantity','requirement_ids']})
        result['module_updates'][-1].update(state='design_stage',source_message_id=mid,source_quote=text)
elif '[增加分支]' in text:
    closed={r['field_id'] for r in doc.get('field_resolutions',[]) if r['state'] in ['confirmed','not_applicable','design_stage']}
    occupied={fid for q in doc['questions'] for fid in q.get('field_ids',[])}
    fid=next((f for f in FIELDS if f not in closed|occupied),None)
    if fid:result['questions']=[dict(id='branch-'+mid[:7],question=f'关于{FIELDS[fid]["label"]}，希望采用哪种边界？',reason='影响适用范围与设计取舍。',options=['保持现有范围','需要进一步约束'],option_impacts=['实现更简洁','需要补充目标和验收方法'],impact='产品范围与验收',priority=1,decision_key='field:'+fid,field_ids=[fid],requirement_ids=[doc['requirements'][0]['id']])]
if '[数值冲突]' in text:
    rid=doc['requirements'][0]['id']
    for id,minimum,maximum in [('budget-low',100,None),('budget-high',None,50)]:
        result['numeric_constraints'].append(dict(id=id,requirement_id=rid,metric='budget',scope='同一块板',conditions='100台批量',minimum=minimum,maximum=maximum,unit='元',source_message_id=mid,source_quote=text))
if '[更新预算]' in text:
    r=next((r for r in doc['requirements'] if r['category']=='selection'),doc['requirements'][0])
    update={k:r[k] for k in ['id','category','name','value','unit','priority','state','source_message_id','source_quote']}
    update.update(value='单台预算不超过80元',source_message_id=mid,source_quote=text)
    result['updates']=[update]
if '[继续独立分析]' in text and context['analysis_mode']=='user':result['next_analysis']='依据现有资料整理其他环境分支。'
if context['analysis_mode']=='continuation':
    time.sleep(1)
    if doc.get('recheck_fields'):
        proof=next(m for m in messages if m['role']=='user' and '未列出的实现细节' in m['content'])
        result['field_resolutions']=[dict(field_ids=doc['recheck_fields'],state='design_stage',conclusion='依据已有明确委托重新核对模块边界；具体实现满足现有产品目标，由后续设计承担。',requirement_ids=[doc['requirements'][0]['id']],source_message_id=proof['id'],source_quote=proof['content'])]
    else:result['questions']=[dict(id='background-size',question='外形尺寸有硬性上限吗？',reason='独立核对结构边界。',options=['没有硬性上限','需要限定尺寸'],requirement_ids=[doc['requirements'][0]['id']],field_ids=['V3-1.3:F02'],decision_key='size',priority=2)]
    result['resolved_question_ids']=[]
answer.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'type':'turn.completed'}),flush=True)
