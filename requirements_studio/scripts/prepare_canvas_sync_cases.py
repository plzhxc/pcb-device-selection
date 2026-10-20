"""Fork two saved cases for explicit device-edit tests; never alter originals."""
import copy,json,sys,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.store import Store,now
RUNTIME=ROOT/'.runtime/canvas-sync-20261020'
SUITE=ROOT/'.runtime/customer-acceptance/canvas-sync-20261020'

def main():
 index_file=SUITE/'index.json'
 if index_file.exists():raise SystemExit('Test copies already exist; refusing to replace their history.')
 store=Store(RUNTIME/'live/studio.sqlite3')
 if store.list_projects():raise SystemExit('Isolated DB is not empty; refusing to mix histories.')
 index={'suite':'canvas-sync-20261020','base':'http://[::1]:8896','input_mode':'existing_case_copy','cases':[],'created_at':now()}
 for key,label in [('toy','发声玩具'),('voice','语音板')]:
  source=json.loads((RUNTIME/f'{key}-baseline.json').read_text())
  project=store.create_project()
  mapping={source['id']:project['id'],**{m['id']:uuid.uuid4().hex for m in source['messages']}}
  def remap(value):
   if isinstance(value,dict):return {k:remap(v) for k,v in value.items()}
   if isinstance(value,list):return [remap(v) for v in value]
   if isinstance(value,str):
    for old,new in mapping.items():value=value.replace(old,new)
   return value
  doc=remap(copy.deepcopy(source['document']))
  doc.update(title='器件联动验收 · '+label+'副本',title_locked=True,created_at=now(),reference_audit={},changes=['从已完成案例建立独立测试副本，保留原始对话与参考资料；接下来只测试器件修改和联动。'],forked_from={'project_id':source['id'],'version':source['document']['version'],'purpose':'device_canvas_sync_acceptance'})
  for k in ('canvas_changes','decision_event'):doc.pop(k,None)
  with store.connect() as db:
   for m in source['messages']:
    db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(mapping[m['id']],project['id'],m['role'],m['content'],m['created_at']))
   store._save(db,project['id'],doc,1)
  index['cases'].append({'key':key,'case_id':key,'project_id':project['id'],'title':doc['title'],'actions':[],'status':'baseline_copied','origin':doc['forked_from'],'service_base':index['base']})
 SUITE.mkdir(parents=True,exist_ok=True)
 index_file.write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n')
 (RUNTIME/'case-clones.json').write_text(json.dumps({c['key']:c['project_id'] for c in index['cases']},indent=2)+'\n')
 print(json.dumps(index,ensure_ascii=False))

if __name__=='__main__':main()
