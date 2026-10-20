"""Read-only verification immediately after publishing the canvas-sync release."""
import gzip,hashlib,json,sqlite3,sys,urllib.request
from pathlib import Path
from zipfile import ZipFile
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from publish_canvas_sync import TABLES,rows_digest
from backend.handoff import build_spec,verify_archive

def main():
 rt=ROOT/'.runtime/canvas-sync-20261020';suite=ROOT/'.runtime/customer-acceptance/canvas-sync-20261020'
 freeze=json.loads((rt/'source-freeze.json').read_text());pub=json.loads((rt/'publication.json').read_text())
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}));base='http://[::1]:8878'
 health=json.loads(op.open(base+'/api/health').read());assert health['backend_build']==freeze['backend_build']
 index=json.loads((suite/'index.json').read_text());cases=[]
 before=json.loads((rt/'old-row-digests.json').read_text())
 with sqlite3.connect((ROOT/'.runtime/studio.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as db:
  retained={}
  for table in TABLES:
   current=rows_digest(db,table)
   for row in before[table]:assert current.get(tuple(row['primary_key']))==row['sha256'],(table,row['primary_key'])
   assert len(current)==len(before[table])+pub['imported'][table]
   retained[table]=len(before[table])
  assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok' and not db.execute('PRAGMA foreign_key_check').fetchall()
  for table in ('runs','domain_jobs'):assert not db.execute(f"SELECT 1 FROM {table} WHERE status IN ('queued','running')").fetchone()
  for entry in index['cases']:
   pid,key=entry['project_id'],entry['key'];p=json.loads(op.open(base+'/api/projects/'+pid).read())
   archived=json.loads(gzip.decompress((suite/(key+'.json.gz')).read_bytes()))
   assert p['document']==archived['document'] and p['domain_revision']==archived['domain_revision']
   assert p['review']['status']=='approved' and not p['canvas_sync']['pending_count']
   content=(suite/(key+'-handoff.zip')).read_bytes();assert verify_archive(content)['ok']
   with ZipFile(suite/(key+'-handoff.zip')) as z:
    assert z.read('requirements.md').decode()==p['markdown']
    assert json.loads(z.read('requirement-spec.json'))==build_spec(p)
   artifact=db.execute('SELECT id,content FROM handoff_artifacts WHERE project_id=?',(pid,)).fetchall();assert len(artifact)==1
   assert artifact[0][1]==content
   assert op.open(base+'/api/projects/'+pid+'/handoff/'+artifact[0][0]).read()==content
   runs=[r[0] for r in db.execute('SELECT id FROM runs WHERE project_id=?',(pid,))]
   for rid in runs:
    origin=rt/'live/runs'/rid;target=ROOT/'.runtime/runs'/rid
    for f in origin.iterdir():
     if f.is_file():assert hashlib.sha256(f.read_bytes()).digest()==hashlib.sha256((target/f.name).read_bytes()).digest(),rid
   cases.append({'key':key,'project_id':pid,'version':p['document']['version'],'review':p['review']['status'],'zip_sha256':hashlib.sha256(content).hexdigest(),'runs_preserved':len(runs)})
 for name,digest in freeze['frontend_files'].items():assert hashlib.sha256((ROOT/'frontend/dist'/name).read_bytes()).hexdigest()==digest,name
 for path in (ROOT.parent/'docs/需求分析工作台_客户使用与流程介绍.html',ROOT/'frontend/dist/reports/requirements-studio-guide.html'):
  assert hashlib.sha256(path.read_bytes()).hexdigest()==freeze['independent_html_sha256']
 result={'ok':True,'base':base,'health':health,'existing_projects':pub['existing_projects'],'project_count':pub['project_count'],'retained_rows':retained,'cases':cases,'independent_html_unchanged':True,'current_assets_verified':True,'all_tasks_idle':True}
 (rt/'publication-verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
