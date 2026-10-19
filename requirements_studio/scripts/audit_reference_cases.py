"""Read-only netlist/core-CSV comparison after human-reviewed UI journeys."""
import argparse,base64,csv,hashlib,io,json,sys,urllib.request
from pathlib import Path
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.reference_design import model_name
from backend.handoff import build_spec

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--base',default='http://[::1]:8896');args=parser.parse_args()
 journey=ROOT/'.runtime/customer-acceptance/incremental-ref-20261019';rt=ROOT/'.runtime/ux-optimization-20261019'
 index=json.loads((journey/'index.json').read_text());baseline=json.loads((ROOT/'.runtime/phase4/ux-netlist-20261019-review1/reference-baseline.json').read_text())
 op=urllib.request.build_opener(urllib.request.ProxyHandler({}));results=[]
 for entry in index['cases']:
  original=next(c for c in baseline if c['id']==entry['case_id']);prefix=args.base+'/api/projects/'+entry['project_id']
  p=json.load(op.open(prefix));assert p['review']['status'] in ('ready','approved');assert not p['analysis_pending'] and not p['input_queue'] and not p['domain']['active_jobs']
  assert p['review']['internal_rechecks']==0 and not p['document']['questions'];assert (p['latest_run'] or {}).get('status')=='completed'
  spec=build_spec(p);assert spec['acceptance_criteria'],'Functional acceptance must survive the downstream contract'
  verified=[]
  for file in original['reference_files']+[{'path':original['input_path'],'sha256':original['input_sha256']}]:
   assert hashlib.sha256(Path(file['path']).read_bytes()).hexdigest()==file['sha256'];verified.append(file['path'])
  core_file=next(f for f in original['reference_files'] if '核心元件清单' in f['path']);expected=list(csv.DictReader(io.StringIO(Path(core_file['path']).read_text(encoding='utf-8-sig'))))
  assert len(p['document']['reference_designs'])==1;source=p['document']['reference_designs'][0];raw=op.open(prefix+'/reference-designs/'+source['id']).read()
  assert hashlib.sha256(raw).hexdigest()==source['sha256'];assert raw==base64.b64decode(source['original_content_base64'])
  actual=list(csv.DictReader(io.StringIO(op.open(prefix+'/reference-bom.csv').read().decode('utf-8-sig'))));assert len(actual)==len(source['bom'])
  (journey/(entry['key']+'-reference-and-current-bom.csv')).write_text('\ufeff'+op.open(prefix+'/reference-bom.csv').read().decode('utf-8-sig'),encoding='utf-8')
  rows=[]
  for e in expected:
   normalized=model_name(e['名称']).casefold();found=[r for r in p['delivery']['bom']['rows'] if r['model'].casefold()==normalized];assert found,'Lost reference model '+e['名称']
   assert sum(r['observed_quantity'] for r in found)==int(e['数量'])
   rows.append({'reference_model':e['名称'],'reference_quantity':int(e['数量']),'items':[{k:r.get(k) for k in ('model','designators','supplier_part','package','observed_quantity','quantity','status','module_ids','purpose','quantity_basis','resolution_status')} for r in found]})
  typed=[]
  for a in entry['actions']:
   if a['kind']=='customer_message':typed.append(a['text'])
   if a['kind']=='question_answers':typed.extend(v for k,v in a['answers'].items() if k not in a.get('choice_indexes',{}))
  assert typed and max(map(len,typed))<=180,'Acceptance must use small customer inputs'
  statements=[m for m in p['messages'] if m['role']=='user'];assert statements[0]['content']==typed[0]
  replies=[m for m in p['messages'] if m['role']=='assistant'];start=datetime.fromisoformat(statements[0]['created_at']);first=datetime.fromisoformat(replies[0]['created_at'])
  result={'key':entry['key'],'project_id':p['id'],'version':p['document']['version'],'review':p['review']['status'],
   'initial_input':typed[0],'typed_inputs':typed,'max_typed_characters':max(map(len,typed)),'user_turns':len(statements),'first_answer_seconds':round((first-start).total_seconds(),2),
   'reference_instances':source['instance_count'],'full_reference_bom_rows':len(actual),'extracted_core_rows':p['delivery']['bom']['core_count'],'adopted_core_rows':p['delivery']['bom']['core_adopted'],
   'core_csv_rows':len(expected),'core_csv_extracted':len(rows),'core_csv_adopted':sum(any(i['status']=='adopted' for i in row['items']) for row in rows),'comparison':rows,
   'requirements':len(p['document']['requirements']),'modules':len(spec['modules']),'interfaces':p['document']['external_interfaces'],'delivery_checks':p['delivery']['checks'],
   'acceptance_criteria':spec['acceptance_criteria'],'source_files_unchanged':verified,'reference_download_sha256':source['sha256'],
   'catalog_statuses':[{k:r.get(k) for k in ('mention','resolution_status','document_status','identity')} for r in p['domain']['references']],
   'scope':'Requirement analysis and actual reference extraction; no netlist generation, electrical simulation or production-BOM validation.'}
  results.append(result)
 output={'ok':True,'base':args.base,'checked_at':datetime.now(timezone.utc).isoformat(),'cases':results}
 (rt/('reference-final-published.json' if '8878' in args.base else 'reference-final.json')).write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({**{k:v for k,v in output.items() if k!='cases'},'cases':[{k:c[k] for k in ('key','version','review','user_turns','max_typed_characters','reference_instances','full_reference_bom_rows','core_csv_rows','core_csv_extracted','core_csv_adopted')} for c in results]},ensure_ascii=False))

if __name__=='__main__':main()
