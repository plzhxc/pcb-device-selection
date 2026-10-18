"""Two reproducible random test populations, isolated from the user's workspace."""
import copy
import json
import os
import random
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.store import Store
from backend.domain import now
from backend.convergence import FIELDS,PRODUCT_CORE
from backend.module_catalog import CATALOG,TYPES

runtime=ROOT/'.runtime/phase2/controlled';out=ROOT/'.runtime/phase2/verification';out.mkdir(parents=True,exist_ok=True)
store=Store(runtime/'studio.sqlite3')
rounds=[]
products=[('室内温度监测板','P_7_1','温度采集'),('工作台光照显示器','P_7_3','光照采集'),('房间空气质量面板','P_7_11','气体采集'),('实验用压力显示器','P_7_8','压力采集')]
for round,seed in enumerate((20261016,20261017),1):
    rng=random.Random(seed);name,sensor_type,function=rng.choice(products)
    row={'round':round,'seed':seed,'product':name,'sensor_type':sensor_type}
    for variant in ('progress','ready','large','workflow_gap'):
        p=store.create_project();doc=p['document'];doc.update(title=f'{name} · {variant} · {seed}',summary=f'虚构测试产品：{function}并显示；用户可继续明确边界。',convergence_version=2)
        text=f'这是虚构验收产品{name}，室内桌面使用，核心功能{function}和本地显示，量程及精度按本版目标确认。未列出的实现细节明确由设计阶段确定。'
        mid=f'owner-{p["id"]}'
        requirements=[]
        for i,(category,n,value) in enumerate([('overview','产品目标',name),('functions',function,'采集并输出测量值'),('functions','本地显示','显示当前数值'),('selection','BOM预算',f'不超过{rng.randint(50,95)}元'),('performance','采样周期',f'{rng.choice([1,2,5])}秒')],1):
            requirements.append(dict(id=f'REQ-{i:03d}',category=category,name=n,value=value,unit='',priority='must',state='stated',source_message_id=mid,source_quote=text,source_type='user',updated_at=now()))
        if variant=='large':
            for i in range(6,306):
                requirements.append(dict(id=f'REQ-{i:03d}',category=rng.choice(['performance','manufacturing','architecture','selection']),name=f'随机核对内容 {i}',value=f'测试值 {rng.randrange(1000)}，仅作长内容呈现验收',unit='',priority='should',state='stated',source_message_id=mid,source_quote=text,source_type='user',updated_at=now()))
        doc.update(requirements=requirements,next_requirement_number=len(requirements)+1,flow=[dict(label='采集测量值',requirement_ids=['REQ-002']),dict(label='显示当前结果',requirement_ids=['REQ-003'])])
        selected=set(FIELDS) if variant in ('ready','workflow_gap') else PRODUCT_CORE|set(rng.sample(list(FIELDS),75))
        doc['field_resolutions']=[dict(field_id=fid,state='confirmed' if fid in PRODUCT_CORE else 'design_stage',conclusion='本版产品目标已明确；实现须满足既定指标，由设计阶段确定并验证。',requirement_ids=['REQ-001'],source_message_id=mid,source_quote=text) for fid in FIELDS if fid in selected]
        doc['modules']=[]
        choices=[(sensor_type,function,'REQ-002'),('P_8_2','本地显示','REQ-003')]
        if variant=='large':choices+=[(t['id'],t['name'],'REQ-002') for t in rng.sample(CATALOG['modules'],18)]
        for i,(type_id,n,rid) in enumerate(choices,1):
            doc['modules'].append(dict(id=f'MOD-{i:03d}',type_id=type_id,name=n,purpose=f'承接{n}的产品目标',boundary='具体器件、外围与接口由设计阶段确定',quantity=rng.randint(1,4),state='design_stage' if variant in ('ready','workflow_gap') else 'proposed',requirement_ids=[rid],source_message_id=mid,source_quote=text))
        doc['next_module_number']=len(doc['modules'])+1
        if variant=='workflow_gap':doc['flow']=[]
        pending=[fid for fid in FIELDS if fid not in selected]
        doc['questions']=[] if variant in ('ready','workflow_gap') else [dict(id=f'question-{i}',question=f'关于{FIELDS[fid]["label"]}，你希望怎样使用？',reason='该决定影响产品范围和后续实现。',options=['先按室内方案','需要进一步约束'],option_impacts=['结构简单，保留既定范围','需要补充目标与验收条件'],impact='使用范围与验收边界',priority=1 if i==0 else 2,decision_key=f'field:{fid}',field_ids=[fid],requirement_ids=['REQ-001'],branch='overview',status='pending') for i,fid in enumerate(rng.sample(pending,20 if variant=='large' else 5))]
        doc['convergence_history']=[dict(version=1,closed=len(selected),delta=len(selected),changed_requirements=len(requirements),module_count=len(doc['modules']),open_questions=len(doc['questions']))]
        doc['module_links']=[dict(id='sensor-to-display',source='MOD-001',target='MOD-002',kind='data',source_port='',target_port='',description='采集结果送往本地显示，具体接口待设计',state='design_stage')]
        with store.connect() as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(mid,p['id'],'user',text,now()))
            for i in range(160 if variant=='large' else 2):
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(f'message-{p["id"]}-{i}',p['id'],'assistant',f'测试历史 {i}：已经整理产品用途和对应的功能范围，未答分支继续保留。',now()))
            store._save(db,p['id'],doc,1)
        row[variant]=p['id']
    rounds.append(row)
(out/'fixtures.json').write_text(json.dumps({'rounds':rounds},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(rounds,ensure_ascii=False))
