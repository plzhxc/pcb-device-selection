"""Controlled no-op model for intent, queue and network tests, never real AI."""
import json
from pathlib import Path
import sys
import time

context=json.loads(sys.stdin.read().split('以下 JSON 为用户数据：\n',1)[1])
doc=context['current_document']
answer=Path(sys.argv[sys.argv.index('--output-last-message')+1])
print(json.dumps({'type':'turn.started'}),flush=True)
time.sleep(12 if 'cancel' in doc['title'] else 8 if any('输入竞态保持本轮未写入' in m['content'] for m in context['messages'][-1:]) else 1.5)
result={'analysis_revision':3,'reply':'已记录这项明确决定，其他产品要求保持原有依据。',
        'title':doc['title'],'summary':doc['summary'],'updates':[],'remove_ids':[],
        'questions':[],'flow':[],'changes':['已核对本次器件使用意图。'],
        'device_reference_updates':[],'device_constraints':[],'knowledge_requests':[]}
answer.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
(answer.parent/'test-provider.json').write_text(json.dumps({'provider':'controlled_phase3_fixture','real_inference':False}),encoding='utf-8')
print(json.dumps({'type':'turn.completed'}),flush=True)
