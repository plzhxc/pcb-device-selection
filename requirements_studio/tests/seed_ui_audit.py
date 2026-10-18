"""Synthetic data for the isolated controlled-provider UI acceptance server."""
import copy
import json
import uuid
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.store import Store
from backend.domain import now
from backend.coverage import CATALOG

store=Store(ROOT/'.runtime/ui-audit-controlled/studio.sqlite3')
fixtures={'provider':'controlled_cli_fixture','rounds':[]}
for round_number in (1,2):
    example=store.create_project(example=True);doc=example['document']
    doc['questions'][0].update(branch='performance',status='pending')
    doc['questions'] += [
        {'id':'phone_platform','question':'手机端需要兼容哪些平台？','reason':'明确兼容范围。','options':['Android','iOS'],'requirement_ids':['REQ-003'],'branch':'functions','status':'pending'},
        {'id':'temperature_range','question':'温度测量范围需要多少？','reason':'定义测试范围。','options':[],'requirement_ids':['REQ-007'],'branch':'performance','status':'pending'}]
    with store.connect() as db:store._save(db,example['id'],doc,2)
    ready=store.create_project(example=True);ready_doc=ready['document'];ready_doc['questions']=[]
    mid=uuid.uuid4().hex;text='测量精度±0.5°C。此验收样例未列出的实现细节由后续设计阶段决定，必须满足已有功能、续航和预算约束。'
    ready_doc['requirements'][6].update(value='±0.5',unit='°C',state='stated',source_message_id=mid,source_quote='测量精度±0.5°C')
    ready_doc['coverage']=[{'topic_id':t['id'],'state':'design_stage','conclusion':'已约定在功能、续航和成本边界内由后续设计决定。','requirement_ids':['REQ-001'],'source_message_id':mid,'source_quote':'未列出的实现细节由后续设计阶段决定'} for t in CATALOG]
    with store.connect() as db:
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(mid,ready['id'],'user',text,now()));store._save(db,ready['id'],ready_doc,2)
    large=store.create_project(example=True);large_doc=large['document'];large_doc['title']=f'长文压力样例 {round_number}'
    base=large_doc['requirements'][0]
    large_doc['requirements']=[{**copy.deepcopy(base),'id':f'REQ-{i:03d}','category':list(['overview','functions','performance','manufacturing','architecture','selection'])[i%6],'name':f'压力需求 {i}','value':'中文长内容 / <img src=x onerror=alert(1)> / A|B\n'+('需要保持可读并支持滚动。'*15)} for i in range(1,301)]
    large_doc['questions']=[{'id':f'large-q-{i}','question':f'分支问题 {i} 需要怎样确定？','reason':'验证问题积累后的可用性。','options':['方案一','方案二'],'requirement_ids':[f'REQ-{i+1:03d}'],'branch':['overview','functions','performance','manufacturing','architecture','selection'][i%6],'status':'pending'} for i in range(20)]
    large_doc['next_requirement_number']=301
    with store.connect() as db:
        for i in range(100):db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(uuid.uuid4().hex,large['id'],'assistant' if i%2 else 'user',f'压力历史消息{i}\n'+('持续补全需求。'*15),now()))
        store._save(db,large['id'],large_doc,2)
    fixtures['rounds'].append({'round':round_number,'example':example['id'],'ready':ready['id'],'large':large['id']})
output=ROOT/'.runtime/verification/ui-audit';output.mkdir(parents=True,exist_ok=True)
(output/'fixtures.json').write_text(json.dumps(fixtures,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(fixtures,ensure_ascii=False))
