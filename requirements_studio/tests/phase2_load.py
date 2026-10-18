"""Seeded pressure on queues, optimistic module edits, catalogue and read recovery."""
import asyncio
import json
import random
import time
import uuid
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'.runtime/phase2/verification';OUT.mkdir(parents=True,exist_ok=True)


async def main():
    rounds=[]
    async with httpx.AsyncClient(base_url='http://[::1]:8891',trust_env=False,timeout=45) as c:
        catalog=(await c.get('/api/module-catalog')).json()
        assert len(catalog['categories'])==10 and len(catalog['types'])==257
        for round,seed in enumerate((20261016,20261017),1):
            rng=random.Random(seed);checks=[];http500=[]
            def check(name,value):
                assert value,name;checks.append(name);print(f'PASS LOAD R{round} {name}',flush=True)
            project=(await c.post('/api/projects',json={})).json();pid=project['id']
            r=await c.patch(f'/api/projects/{pid}/requirements',json=dict(base_version=0,id='',category='functions',name='随机压力测试功能',value=f'虚构测试功能-{seed}',unit='',priority='must',state='stated'))
            assert r.status_code==200
            first=await c.post(f'/api/projects/{pid}/messages',json={'text':'[队列容量阻塞] 保持当前任务，收包后由测试显式取消。','request_id':uuid.uuid4().hex});run=first.json()['run']
            async def enqueue(i):
                request_id=f'phase2-load-{seed}-{i}'
                start=time.perf_counter();r=await c.post(f'/api/projects/{pid}/messages',json={'text':f'随机补充 {i}，保留原产品范围。','request_id':request_id})
                if r.status_code>=500:http500.append(r.status_code)
                return {'index':i,'status':r.status_code,'request_id':request_id,'ms':(time.perf_counter()-start)*1000}
            responses=await asyncio.gather(*(enqueue(i) for i in range(32)))
            accepted=[r for r in responses if r['status']==202]
            limited=[r for r in responses if r['status']==409]
            check('concurrent supplements enforce queue capacity',len(accepted)==20 and len(limited)==12)
            p=(await c.get(f'/api/projects/{pid}')).json();check('all accepted inputs are visible',len(p['input_queue'])==20)
            duplicate=await c.post(f'/api/projects/{pid}/messages',json={'text':'不应覆盖原始排队文本','request_id':accepted[0]['request_id']})
            check('idempotent duplicate does not create an extra input',duplicate.status_code==202 and len((await c.get(f'/api/projects/{pid}')).json()['input_queue'])==20)
            await c.post(f'/api/runs/{run["id"]}/cancel')
            p=(await c.get(f'/api/projects/{pid}')).json();check('cancel pauses rather than loses queued input',p['latest_run']['status']=='cancelled' and len(p['input_queue'])==20)
            remove=rng.choice(p['input_queue']);await c.delete(f'/api/projects/{pid}/queue/{remove["id"]}')
            await c.post(f'/api/projects/{pid}/queue/resume')
            start=time.monotonic()
            while time.monotonic()-start<120:
                p=(await c.get(f'/api/projects/{pid}')).json()
                if not p['input_queue'] and p['latest_run']['status']=='completed':break
                await asyncio.sleep(.2)
            check('resume processes every retained input once',not p['input_queue'] and p['latest_run']['status']=='completed' and len([m for m in p['messages'] if m['role']=='user'])==21)
            base=p['document']['version'];types=rng.sample(catalog['types'],12)
            async def edit(kind):
                return await c.post(f'/api/projects/{pid}/modules',json=dict(base_version=base,id='',type_id=kind['id'],name=kind['name'],purpose='承接随机测试功能',boundary='用于测试版本并发，具体实现交给设计阶段',quantity=rng.randint(1,10),state='design_stage',requirement_ids=['REQ-001']))
            results=await asyncio.gather(*(edit(t) for t in types))
            check('simultaneous module edits do not overwrite a new version',sum(r.status_code==200 for r in results)==1 and sum(r.status_code==409 for r in results)==11)
            p=(await c.get(f'/api/projects/{pid}')).json();check('the saved module has a real functional category',len(p['convergence']['modules'])==1 and any(t['id']==p['convergence']['modules'][0]['type_id'] and t['name']==p['convergence']['modules'][0]['type_name'] for t in catalog['types']))
            async def malformed(i):
                return await c.post(f'/api/projects/{pid}/modules',json=dict(base_version=p['document']['version'],id='',type_id=f'不存在的类别-{seed}-{i}',name='未知类型',purpose='不允许凭空增加规范类别',boundary='',quantity=1,state='confirmed',requirement_ids=['REQ-001']))
            malformed_results=await asyncio.gather(*(malformed(i) for i in range(12)))
            check('unknown categories are rejected without corruption',all(r.status_code==409 for r in malformed_results) and (await c.get(f'/api/projects/{pid}')).json()['document']['version']==p['document']['version'])
            sem=asyncio.Semaphore(12);times=[]
            async def read(i):
                async with sem:
                    start=time.perf_counter()
                    r=await c.get('/api/module-catalog/'+rng.choice(catalog['types'])['id'] if i%3==0 else f'/api/projects/{pid}')
                    assert r.status_code==200
                    times.append((time.perf_counter()-start)*1000)
            await asyncio.gather(*(read(i) for i in range(180)))
            check('mixed catalogue and project reads survive pressure',len(times)==180)
            check('no internal server errors',not http500 and all(r.status_code<500 for r in results+malformed_results))
            rounds.append({'round':round,'seed':seed,'provider':'controlled_phase2_fixture','real_inference':False,'checks':checks,'concurrent_followups':32,'accepted':len(accepted),'backpressure':len(limited),'read_requests':len(times),'read_concurrency':12,'read_p95_ms':round_number(sorted(times)[int(len(times)*.95)-1]),'read_max_ms':round_number(max(times)),'enqueue_p95_ms':round_number(sorted(r['ms'] for r in responses)[int(len(responses)*.95)-1]),'http_500':http500})
            (OUT/'load.json').write_text(json.dumps({'ok':len(rounds)==2,'rounds':rounds},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'ok':True,'rounds':[{k:v for k,v in r.items() if k!='checks'} for r in rounds]},ensure_ascii=False))


def round_number(value):return round(value,1)


if __name__=='__main__':asyncio.run(main())
