"""Controlled delays/failures for the browser regression, not real AI output."""
import json
import sys
import time
from pathlib import Path

context=json.loads(sys.stdin.read().split('以下 JSON 为用户数据：\n',1)[1])
from provider_context import decode_quotes
context=decode_quotes(context)
doc=context['current_document'];mid=context['current_message_id']
text=next(m['content'] for m in context['messages'] if m['id']==mid)
answer=Path(sys.argv[sys.argv.index('--output-last-message')+1]);runtime=next(parent.parent for parent in answer.parents if parent.name=='runs')
(answer.parent/'test-provider.json').write_text(json.dumps(dict(provider='controlled_ux_fixture',real_inference=False)),encoding='utf-8')
print(json.dumps(dict(type='turn.started')),flush=True)
print(json.dumps(dict(type='item.completed',item=dict(type='agent_message',text='已接收补充，正在核对这次选择与已有需求。'))),flush=True)
time.sleep(8 if '[慢分析]' in text else .15)
marker=runtime/('failed-'+mid);failures=int(marker.read_text()) if marker.exists() else 0
if '[故障重试]' in text and failures<2:
    marker.write_text(str(failures+1),encoding='utf-8');answer.write_text('invalid-json',encoding='utf-8');raise SystemExit(0)
result=dict(reply='已整理本轮补充，其他未回答的分支仍保留。',title=doc['title'],summary=doc['summary'],updates=[],remove_ids=[],questions=[],flow=doc['flow'],changes=['核对本轮补充'],analysis_revision=2,
    resolved_question_ids=[a['question_id'] for a in context.get('question_answers',[]) if not a.get('deferred')],field_resolutions=[],module_updates=[],module_links=[],numeric_constraints=[])
if text.startswith('确认本版功能模块方案：'):
    result['module_updates']=[{**m,'state':'confirmed','source_message_id':mid,'source_quote':text} for m in doc['modules']]
answer.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
print(json.dumps(dict(type='turn.completed')),flush=True)
