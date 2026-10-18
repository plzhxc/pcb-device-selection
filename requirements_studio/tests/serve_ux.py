"""Isolated UX fixtures. Never substitutes a provider in the normal workbench."""
import json
import os
import random
import shlex
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.store import Store
from backend.domain import now
from backend.experience import CORE

runtime=ROOT/'.runtime/ux/controlled';out=ROOT/'.runtime/ux/verification';out.mkdir(parents=True,exist_ok=True)
store=Store(runtime/'studio.sqlite3');rounds=[]
for round,seed in enumerate((20261025,20261026),1):
    rng=random.Random(seed);product=rng.choice(['桌面温度显示器','植物照度显示器','房间湿度显示器'])
    ids={}
    for variant in ('answers','ready','proposal'):
        p=store.create_project();doc=p['document'];mid='owner-'+p['id']
        text=f'受控验收案例：{product}，室内桌面使用，测量并显示当前数值，量程和精度按本版目标。'
        doc.update(title=f'受控体验 R{round} {product} {variant}',summary=text,convergence_version=2,flow=[dict(label='测量并显示',requirement_ids=['REQ-001'])])
        doc['requirements']=[dict(id='REQ-001',category='functions',name='本地显示',value=text,unit='',priority='must',state='stated',source_message_id=mid,source_quote=text,source_type='user',updated_at=now())]
        doc['field_resolutions']=[dict(field_id=fid,state='confirmed',conclusion=text,requirement_ids=['REQ-001'],source_message_id=mid,source_quote=text) for fid in CORE]
        doc['modules']=[dict(id='MOD-001',type_id='P_8_2',name='本地显示',purpose='显示测量值',boundary='具体器件和接口待设计',quantity=1,state='confirmed',requirement_ids=['REQ-001'],source_message_id=mid,source_quote=text)]
        doc['next_module_number']=2;doc['next_requirement_number']=2
        if variant=='proposal':doc['modules'][0]['state']='proposed'
        doc['questions']=[] if variant!='answers' else [dict(id=qid,question=question,reason='影响产品体验与范围',options=options,requirement_ids=['REQ-001'],branch=branch,decision_key=qid,status='pending',priority=2) for qid,question,options,branch in [
            ('budget','本版单台预算上限希望是多少？',['[慢分析] 60元','80元'],'selection'),
            ('size','对外形有硬性限制吗？',['90×60×30mm以内','无硬性限制'],'manufacturing'),
            ('refresh','希望多久刷新一次读数？',['每1秒刷新','每5秒刷新'],'performance')]]
        with store.connect() as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(mid,p['id'],'user',text,now()))
            store._save(db,p['id'],doc,1)
        ids[variant]=p['id']
    rounds.append(dict(round=round,seed=seed,product=product,**ids))
(out/'fixtures.json').write_text(json.dumps(dict(rounds=rounds),ensure_ascii=False,indent=2),encoding='utf-8')
folder=ROOT/'.runtime/ux/fixture-bin';folder.mkdir(parents=True,exist_ok=True)
binary=folder/'codex';binary.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -X utf8 '+shlex.quote(str(ROOT/'tests/ux_provider.py'))+' "$@"\n',encoding='utf-8');binary.chmod(0o755)
os.environ['PATH']=str(folder)+os.pathsep+os.environ.get('PATH','')
os.environ['REQUIREMENTS_STUDIO_DATA']=str(runtime)
os.environ['REQUIREMENTS_STUDIO_DIST']=str(ROOT/'.runtime/ux-dist')
os.chdir(ROOT)
os.execv(sys.executable,[sys.executable,'-X','utf8','-m','uvicorn','backend.app:app','--host','::1','--port','8893','--timeout-graceful-shutdown','3'])
