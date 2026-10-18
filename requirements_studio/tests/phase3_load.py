"""Two controlled mixed-load rounds on 300 REQ / 30 MOD / 80 REF / 20 branches."""
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time
import httpx

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.runtime/phase3/browser'

def distribution(samples):
    values=sorted(samples)
    return {'count':len(values),'p50_ms':round(statistics.median(values),2),
            'p95_ms':round(values[min(len(values)-1,int(len(values)*.95))],2),'max_ms':round(max(values),2)}

async def main():
    fixtures=json.loads((OUT/'fixtures.json').read_text(encoding='utf-8'));rounds=[]
    async with httpx.AsyncClient(base_url='http://[::1]:8895',trust_env=False,timeout=60,
                                 limits=httpx.Limits(max_connections=8)) as client:
        for fixture in fixtures['rounds']:
            pid=fixture['heavy'];latencies={'project':[],'search':[],'status':[]};failures=[];slots=asyncio.Semaphore(6)
            started_at=datetime.now(timezone.utc).isoformat()
            backend_build=(await client.get('/api/health')).raise_for_status().json()['backend_build']
            async def read(index):
                kind=('project','search','status')[index%3]
                route=f'/api/projects/{pid}' if kind=='project' else '/api/catalog/search?q=LM358&mode=exact&limit=8' if kind=='search' else '/api/knowledge/status'
                async with slots:
                    start=time.monotonic();r=await client.get(route);latencies[kind].append((time.monotonic()-start)*1000)
                    if r.status_code!=200:failures.append({'kind':kind,'status':r.status_code})
            await asyncio.gather(*(read(i) for i in range(120)))
            assert not failures,failures
            p=(await client.get(f'/api/projects/{pid}')).raise_for_status().json()
            assert (len(p['document']['requirements']),len(p['convergence']['modules']),len(p['domain']['references']),len(p['document']['questions']))==(300,30,80,20)
            v=p['document']['version'];old=p['domain']['references'][0]['intent'];new='reference' if old!='reference' else 'prefer'
            action={'base_version':v,'request_id':f'load-once-{fixture["seed"]}',
                    'action':{'operation':'set_intent','reference_id':'REF-001','intent':new}}
            responses=await asyncio.gather(*(client.post(f'/api/projects/{pid}/device-references',json=action) for _ in range(8)))
            assert all(r.status_code==202 for r in responses),[r.status_code for r in responses]
            stale={**action,'request_id':'stale-'+str(fixture['seed'])}
            assert (await client.post(f'/api/projects/{pid}/device-references',json=stale)).status_code==409
            deadline=time.monotonic()+180
            while time.monotonic()<deadline:
                p=(await client.get(f'/api/projects/{pid}')).raise_for_status().json()
                if not p['domain']['active_jobs'] and p['latest_run']['status'] not in ('queued','running'):break
                await asyncio.sleep(.2)
            assert p['latest_run']['status']=='completed',p['latest_run']
            assert p['domain']['references'][0]['intent']==new
            assert len(p['messages'])==3 and len(p['document']['requirements'])==300
            assert (await client.get('/api/health')).raise_for_status().json()['backend_build']==backend_build
            result={'round':fixture['round'],'seed':fixture['seed'],'provider':'controlled_phase3_fixture','real_inference':False,
                    'backend_build':backend_build,'fixture_project_id':pid,'started_at':started_at,
                    'completed_at':datetime.now(timezone.utc).isoformat(),
                    'scale':{'requirements':300,'modules':30,'references':80,'questions':20},'mixed_requests':120,'concurrency':6,
                    'latencies':{k:distribution(v) for k,v in latencies.items()},'errors':failures,'duplicate_posts':8,
                    'effective_intent_decisions':1,'stale_version_status':409,'final_version':p['document']['version']}
            rounds.append(result);(OUT/'load.json').write_text(json.dumps({'ok':len(rounds)==2,'rounds':rounds},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False),flush=True)

if __name__=='__main__':asyncio.run(main())
