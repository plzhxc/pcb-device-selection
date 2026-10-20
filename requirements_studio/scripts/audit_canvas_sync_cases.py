"""Read-only audit of device edits, linked content, original references and export."""
import argparse,copy,hashlib,json,sys,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.canvas_sync import update,pending
RUNTIME=ROOT/'.runtime/canvas-sync-20261020'

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://[::1]:8896');parser.add_argument('--phase',default='final');args=parser.parse_args()
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}));results=[]
 for key,pid in json.loads((RUNTIME/'case-clones.json').read_text()).items():
  p=json.loads(op.open(args.base+'/api/projects/'+pid).read());doc=p['document']
  assert p['review']['status'] in ('ready','approved'),(key,p['review']['status'])
  assert p['latest_run']['status']=='completed' and not p['input_queue'] and not p['domain']['active_jobs']
  assert not p['canvas_sync']['pending_count']
  probe=copy.deepcopy(doc);update(probe);assert not pending(probe),(key,pending(probe))
  original=json.loads((RUNTIME/(key+'-baseline.json')).read_text())
  oldref=original['document']['device_references'][1]
  ref=next(r for r in doc['device_references'] if r['id']=='REF-002')
  expected='STM8S003F3P6' if key=='toy' else 'ME6211C33M5G-N'
  assert ref['mention']==expected and ref['intent']==oldref['intent'] and ref['quantity']==oldref['quantity']
  assert ref.get('reference_supplier_part') is None and not ref.get('reference_quantity_source')
  row=next(r for r in p['delivery']['bom']['current_rows'] if r['reference_id']==ref['id'])
  assert row['model']==expected and row['status']=='adopted'
  assert not any(r['model']==oldref['mention'] for r in p['delivery']['bom']['current_rows'])
  assert any(r['model']==oldref['mention'] for r in p['delivery']['bom']['rows'])
  current_sources={s['id']:s for s in doc['reference_designs']}
  hashes=[]
  for source in original['document']['reference_designs']:
   current=current_sources[source['id']]
   for field in ('sha256','original_content_base64','bom'):assert current[field]==source[field],(key,field)
   hashes.append(source['sha256'])
  assert '本版器件方案' in p['markdown'] and expected in p['markdown']
  for rid in ref['requirement_ids']:assert any(r['id']==rid and r['state']!='not_applicable' for r in doc['requirements'])
  for mid in ref['module_ids']:assert any(m['id']==mid and m['state']!='not_applicable' for m in p['convergence']['modules'])
  csv=op.open(args.base+'/api/projects/'+pid+'/current-bom.csv').read().decode('utf-8-sig');assert expected in csv
  result={'key':key,'project_id':pid,'version':doc['version'],'review':p['review']['status'],'before':oldref['mention'],'after':ref['mention'],'current_rows':len(p['delivery']['bom']['current_rows']),'original_source_hashes':hashes,'canvas_changes':p['canvas_sync']['history'],'fields_rechecking':len(doc.get('recheck_fields',[])),'current_csv_sha256':hashlib.sha256(csv.encode()).hexdigest(),'ok':True}
  results.append(result)
 (RUNTIME/('audit-'+args.phase+'.json')).write_text(json.dumps({'base':args.base,'results':results},ensure_ascii=False,indent=2)+'\n')
 print(json.dumps([{k:r[k] for k in ('key','version','review','before','after','current_rows','ok')} for r in results],ensure_ascii=False))

if __name__=='__main__':main()
