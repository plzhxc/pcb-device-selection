"""Read-only integration evidence from the user's real dataS publication and Milvus."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.knowledge import Catalog
from backend.model_context import release_hash

def main():
    c=Catalog();started=time.monotonic();status=c.status(True)
    assert status['available'] and status['read_only'],status
    lm=c.search('LM358',mode='exact',limit=1);page2=c.search('LM358',mode='exact',limit=1,offset=1)
    assert lm['status']=='ambiguous' and page2['status']=='ambiguous'
    assert lm['items'][0]['device_key']!=page2['items'][0]['device_key']
    supplier=c.search('C114425',mode='exact');assert supplier['status']=='matched'
    detail=c.device(supplier['items'][0]['device_key'])
    frequency=next(a for a in detail['assertions'] if a['field_key']=='switching_frequency')
    assert frequency['raw_value']=='500kHz' and frequency['validation_state']=='source_conflict'
    assert frequency['normalized']['minimum']=='500000'
    identity=detail['identity']
    def parallel(i):
        result=c._spans(identity,['input voltage VIN','switching frequency '+str(i)])
        assert result['status']=='available',result
        assert all(s['lichuang_id']==identity['supplier_part'] and isinstance(s['id'],str) for s in result['items'])
        assert all(s['page_number']==s['page_idx']+1 for s in result['items'] if s.get('page_idx') is not None)
        return {'count':len(result['items']),'errors':result['errors'],
                'locators':[s['database']+'.'+s['collection']+':'+s['id'] for s in result['items']],
                'modes':sorted(set(s['retrieval_mode'] for s in result['items']))}
    with ThreadPoolExecutor(max_workers=4) as pool:spans=list(pool.map(parallel,range(8)))
    with c.connection() as db:
        manual=c.query(db,"SELECT record_status,COUNT(*) AS count FROM dmanual_device_ingest_record GROUP BY record_status")
        published=c.query(db,"SELECT record_id,part_number FROM dmanual_device_ingest_record WHERE record_status='PUBLISHED' ORDER BY record_id DESC LIMIT 1")[0]
        rolled=c.query(db,"SELECT record_id,part_number FROM dmanual_device_ingest_record WHERE record_status<>'PUBLISHED' ORDER BY record_id LIMIT 1")[0]
    manual_detail=c.device('datas:manual-record:'+str(published['record_id']))
    assert manual_detail['identity']['origin']=='manual_published'
    rejected=False
    try:c.device('datas:manual-record:'+str(rolled['record_id']))
    except ValueError:rejected=True
    assert rejected,'rolled-back manual record was exposed'
    result={'ok':True,'real_sources':True,'backend_build':release_hash(),'mutations':0,'elapsed_seconds':round(time.monotonic()-started,3),
      'status':status,'ambiguous_page_1':lm,'ambiguous_page_2':page2,'supplier_identity':identity,
      'frequency_raw_anomaly':frequency,'concurrent_milvus_reads':spans,'milvus_concurrency':4,
      'manual_counts':manual,'published_manual_identity':manual_detail['identity'],'rolled_back_rejected':rejected,
      'taxonomy_versions':detail['version_context']}
    out=ROOT/'.runtime/phase3/sources.json';out.parent.mkdir(parents=True,exist_ok=True)
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('ok','real_sources','mutations','elapsed_seconds','milvus_concurrency','rolled_back_rejected')},ensure_ascii=False))

if __name__=='__main__':main()
