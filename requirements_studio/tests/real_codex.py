"""Explicit real-model acceptance: two turns, retained facts, export and SSE."""
import json
import os
import time
from pathlib import Path

import httpx

root=Path(__file__).resolve().parents[1]
base=os.environ.get("STUDIO_TEST_URL","http://[::1]:8880")
receipt={"base_url":base,"model":"gpt-6-luna","effort":"xhigh","turns":[]}
with httpx.Client(base_url=base,trust_env=False,timeout=20) as client:
    project=client.post('/api/projects',json={}).raise_for_status().json()
    receipt['project_id']=project['id']
    inputs=[
        '我要做一个室内蓝牙温度记录器。电池供电，每分钟采样一次，续航至少30天，手机能读取历史数据，单台硬件预算80元以内。测量精度暂时不确定。',
        '补充：测温精度需要±0.5°C，温度记录保存7天。不需要显示屏。续航改为至少60天，其他要求不变。'
    ]
    for index,text in enumerate(inputs,1):
        response=client.post(f"/api/projects/{project['id']}/messages",json={"text":text,"request_id":f"real-acceptance-{index}"}).raise_for_status().json()
        run_id=response['run']['id']; print(f"Turn {index} started: {run_id}",flush=True)
        deadline=time.monotonic()+620
        while time.monotonic()<deadline:
            time.sleep(2)
            project=client.get(f"/api/projects/{project['id']}").raise_for_status().json()
            if project['latest_run']['status'] not in ('queued','running'):break
        assert project['latest_run']['status']=='completed',project['latest_run']
        doc=project['document']
        assert doc['version']==index
        assert doc['requirements']
        assert len(doc['questions'])<=3
        users={m['id']:m['content'] for m in project['messages'] if m['role']=='user'}
        for requirement in doc['requirements']:
            if requirement['state']=='stated':
                assert requirement['source_quote'] in users[requirement['source_message_id']]
        events=client.get(f'/api/runs/{run_id}/events').raise_for_status().text
        assert '"kind": "completed"' in events and '"kind": "document"' in events
        receipt['turns'].append({'run_id':run_id,'document':doc,'assistant_reply':project['messages'][-1]['content']})
        print(f"Turn {index} complete: v{doc['version']}, {len(doc['requirements'])} requirements",flush=True)
    facts='\n'.join(r['name']+' '+r['value']+' '+r['unit'] for r in doc['requirements'] if r['state']=='stated')
    assert '60' in facts and '80' in facts and ('蓝牙' in facts or 'BLE' in facts),facts
    assert '.5' in facts and '7' in facts,facts
    first_ids={r['id'] for r in receipt['turns'][0]['document']['requirements']}
    assert first_ids.intersection(r['id'] for r in doc['requirements'])
    markdown=client.get(f"/api/projects/{project['id']}/export/md").raise_for_status().text
    data=client.get(f"/api/projects/{project['id']}/export/json").raise_for_status().json()
    assert data['document']==doc and all(r['id'] in markdown for r in doc['requirements'])
    receipt.update(ok=True,requirements=len(doc['requirements']),version=doc['version'])
folder=root/'.runtime'/'verification';folder.mkdir(parents=True,exist_ok=True)
(folder/'real-codex.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'ok':True,'project_id':receipt['project_id'],'version':doc['version'],'requirements':len(doc['requirements'])},ensure_ascii=False),flush=True)
