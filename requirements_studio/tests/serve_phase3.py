"""Explicitly isolated fault fixtures for the phase 3 browser acceptance."""
import json
import os
from pathlib import Path
import copy
import random
import shlex
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
RUNTIME=ROOT/'.runtime/phase3/controlled'
os.environ['REQUIREMENTS_STUDIO_DATA']=str(RUNTIME)
os.environ['REQUIREMENTS_STUDIO_DIST']=os.environ.get('REQUIREMENTS_STUDIO_FIXTURE_DIST',str(ROOT/'.runtime/phase3-dist'))
os.environ['REQUIREMENTS_STUDIO_KNOWLEDGE']='0'
from backend.app import create_app
from phase3_fixtures import FixtureCatalog,seed
from backend.domain import now
from backend.device_domain import reconcile_selection_items

provider=FixtureCatalog(delay=.25)
app=create_app(RUNTIME,knowledge_provider=provider)
out=ROOT/'.runtime/phase3/browser';out.mkdir(parents=True,exist_ok=True)

def reset_fixtures():
    provider.fail=False
    fixtures=[]
    for round_number,seed_value in enumerate((202610161,202610162),1):
        rng=random.Random(seed_value);title=rng.choice(['实验电源核对','桌面电源资料','低压输入原型']);projects={}
        for kind in ('normal','conflict','late','heavy','cancel','storage'):
            p=seed(app.state.store,f'受控测试 R{round_number} {title} {kind}',include_constraint=kind=='conflict')
            if kind=='storage':
                d=p['document'];message=p['messages'][0]
                text='同一日志记录链路，每1秒记录一条，每条至少32B，要求保存30天，但最多只提供64MiB存储空间；不压缩。'
                with app.state.store.connect() as db:
                    db.execute('UPDATE messages SET content=? WHERE id=?',(message['content']+'\n'+text,message['id']))
                    d['requirements'].append({**d['requirements'][0],'id':'REQ-002','category':'performance','name':'日志记录与保存','value':text,'source_quote':text})
                    common={'requirement_id':'REQ-002','scope':'日志记录链路','conditions':'每次存一条、无压缩','minimum':None,'maximum':None,'source_message_id':message['id'],'source_quote':text}
                    d['numeric_constraints']=[{**common,'id':'record-size','metric':'record_size','minimum':32,'unit':'B'},
                      {**common,'id':'record-period','metric':'sample_period','maximum':1,'unit':'s'},
                      {**common,'id':'record-time','metric':'retention','minimum':30,'unit':'d'},
                      {**common,'id':'record-capacity','metric':'storage_capacity','maximum':64,'unit':'MiB'}]
                    app.state.store._save(db,p['id'],d,2)
            if kind=='heavy':
                d=p['document'];r=d['requirements'][0];m=d['modules'][0];reference=d['device_references'][0]
                d['requirements']=[{**r,'id':f'REQ-{i:03d}','name':f'受控功能{i}','value':f'受控性能记录{i}：长会话仍可查询、浏览和导出。'} for i in range(1,301)]
                d['modules']=[{**m,'id':f'MOD-{i:03d}','name':f'受控电源功能{i}','quantity':rng.randint(1,3),'requirement_ids':[f'REQ-{j:03d}' for j in range(i,301,30)]} for i in range(1,31)]
                d['device_references']=[{**reference,'id':f'REF-{i:03d}','mention':f'FIXTURE{i:03d}','intent':rng.choice(['must','prefer','reference']),
                   'module_ids':[f'MOD-{1+(i%30):03d}'],'requirement_ids':[f'REQ-{i:03d}']} for i in range(1,81)]
                d['questions']=[{'id':f'branch-{i}','question':f'受控分支{i}：是否保留这一产品取舍？','reason':'受控并发测试问题，不代表真实产品','options':['保留','调整'],'requirement_ids':[f'REQ-{i:03d}'],'priority':2,'decision_key':f'branch-{i}','status':'pending'} for i in range(1,21)]
                reconcile_selection_items(d)
                with app.state.store.connect() as db:app.state.store._save(db,p['id'],d,2)
            projects[kind]=p['id']
        fixtures.append({'round':round_number,'seed':seed_value,**projects})
    value={'provider':'controlled_fixture','real_inference':False,'rounds':fixtures}
    (out/'fixtures.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    return value

@app.post('/__controlled/reset')
async def reset():return reset_fixtures()

@app.post('/__controlled/source/{offline}')
async def source_outage(offline: bool):
    provider.fail=offline
    return {'provider':'controlled_fixture','offline':provider.fail,'real_sources_modified':False}

reset_fixtures()
binary=RUNTIME/'fixture-bin/codex';binary.parent.mkdir(parents=True,exist_ok=True)
binary.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -X utf8 '+shlex.quote(str(ROOT/'tests/phase3_provider.py'))+' "$@"\n',encoding='utf-8')
binary.chmod(0o755)
os.environ['PATH']=str(binary.parent)+os.pathsep+os.environ.get('PATH','')

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='::1',port=8895,log_level='warning')
