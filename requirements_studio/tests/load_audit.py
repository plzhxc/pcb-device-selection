"""Two controlled-provider load rounds; no inference or production data."""
import asyncio
import json
import sqlite3
import time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'.runtime/verification/ui-audit/load.json'


async def main():
    rounds=[]
    async with httpx.AsyncClient(base_url='http://[::1]:8881',trust_env=False,timeout=30,limits=httpx.Limits(max_connections=24)) as client:
        for round_number in (1,2):
            p=(await client.post('/api/projects',json={})).raise_for_status().json();pid=p['id']
            r=(await client.post(f'/api/projects/{pid}/messages',json={'text':'[压力阻塞] 本轮用于队列压力验收。','request_id':'initial'})).raise_for_status().json()['run']
            times=[]
            async def enqueue(i):
                start=time.monotonic()
                response=await client.post(f'/api/projects/{pid}/messages',json={'text':f'[压力补充] 并发补充 {i}','request_id':f'concurrent-{i}','after_run_id':r['id']})
                times.append((time.monotonic()-start)*1000)
                return response.status_code
            codes=await asyncio.gather(*(enqueue(i) for i in range(40)))
            assert set(codes)<={202,409},codes
            assert codes.count(202)==20 and codes.count(409)==20,codes
            current=(await client.get(f'/api/projects/{pid}')).raise_for_status().json()
            assert len(current['input_queue'])==20
            queued_request=next(i for i,code in enumerate(codes) if code==202)
            duplicate=await client.post(f'/api/projects/{pid}/messages',json={'text':f'[压力补充] 并发补充 {queued_request}','request_id':f'concurrent-{queued_request}','after_run_id':r['id']})
            assert duplicate.status_code==202
            assert len((await client.get(f'/api/projects/{pid}')).json()['input_queue'])==20
            await client.post(f"/api/runs/{r['id']}/cancel")
            paused=(await client.get(f'/api/projects/{pid}')).json();assert paused['latest_run']['status']=='cancelled' and len(paused['input_queue'])==20
            (await client.post(f'/api/projects/{pid}/queue/resume')).raise_for_status()
            semaphore=asyncio.Semaphore(12)
            read_times=[]
            async def read(_):
                async with semaphore:
                    start=time.monotonic();response=await client.get(f'/api/projects/{pid}');response.raise_for_status()
                    read_times.append((time.monotonic()-start)*1000)
            await asyncio.gather(*(read(i) for i in range(240)))
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                current=(await client.get(f'/api/projects/{pid}')).json()
                if not current['input_queue'] and current['latest_run']['status'] not in ('queued','running'):break
                await asyncio.sleep(.2)
            assert current['latest_run']['status']=='completed',current['latest_run']
            assert current['document']['version']==20 and len(current['revisions'])==20
            assert sum(m['role']=='user' for m in current['messages'])==21
            others=[(await client.post('/api/projects',json={})).json()['id'] for _ in range(12)]
            await asyncio.gather(*(client.post(f'/api/projects/{other}/messages',json={'text':'[压力补充] 独立项目并发验收。','request_id':'parallel'}) for other in others))
            peaks=[];deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                with sqlite3.connect(ROOT/'.runtime/ui-audit-controlled/studio.sqlite3') as db:
                    statuses=db.execute('SELECT status FROM runs WHERE project_id IN ('+','.join('?' for _ in others)+')',others).fetchall()
                    peaks.append(sum(row[0]=='running' for row in statuses))
                if all(row[0]=='completed' for row in statuses):break
                await asyncio.sleep(.05)
            assert max(peaks)<=2 and all(row[0]=='completed' for row in statuses),(peaks,statuses)
            read_times.sort();times.sort()
            item={'round':round_number,'provider':'controlled_cli_fixture','concurrent_followups':40,'accepted':20,'backpressure_409':20,'duplicate_did_not_reenqueue':True,'read_requests':240,'read_concurrency':12,'read_p95_ms':round(read_times[int(len(read_times)*.95)],1),'read_max_ms':round(max(read_times),1),'enqueue_p95_ms':round(times[int(len(times)*.95)],1),'processed_versions':current['document']['version'],'parallel_projects':12,'max_running_cli':max(peaks),'errors_500':0}
            rounds.append(item);REPORT.write_text(json.dumps({'ok':True,'rounds':rounds},ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(item,ensure_ascii=False),flush=True)


asyncio.run(main())
