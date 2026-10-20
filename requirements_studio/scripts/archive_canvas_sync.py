"""Archive delivered source/config/docs with a verified per-file manifest."""
import hashlib,json,sys,zipfile
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];PROJECT=ROOT.parent
sys.path.insert(0,str(ROOT))
from backend.model_context import release_hash

def main():
 rt=ROOT/'.runtime/canvas-sync-20261020';freeze=json.loads((rt/'source-freeze.json').read_text())
 assert release_hash()==freeze['backend_build']
 assert json.loads((rt/'publication-verification.json').read_text())['ok']
 files=set()
 for folder in ('backend','scripts','tests','schemas','frontend/src','frontend/public'):
  files.update(p for p in (ROOT/folder).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc')
 for name in ('README.md','start.sh','requirements.txt','requirements-dev.txt','frontend/package.json','frontend/package-lock.json','frontend/tsconfig.json','frontend/vite.config.ts','frontend/index.html'):
  p=ROOT/name;assert p.is_file(),name;files.add(p)
 for name,digest in freeze['frontend_files'].items():
  p=ROOT/'frontend/dist'/name;assert hashlib.sha256(p.read_bytes()).hexdigest()==digest;files.add(p)
 files.add(ROOT/'frontend/dist/reports/requirements-studio-guide.html')
 files.update((PROJECT/'docs').glob('*.md'))
 for name in ('硬件需求说明书章节规范（带+Agent+Prompt）+V3.0.md','需求分析工作台_对接PCBAgent必要输入与章节优先级.md','AgenticPCB_电路模块与子模块专业定义规范_60父模块197子模块_1.0.docx'):
  p=PROJECT/name;assert p.is_file(),name;files.add(p)
 for name in ('source-freeze.json','unit-full-final.log','publication-verification.json','browser-final2.json','browser-published.json','real-run-stats.json','case-clones.json'):
  p=rt/name;assert p.is_file(),name;files.add(p)
 manifest={'format':'delivered-source/1','created_at':datetime.now(timezone.utc).isoformat(),'backend_build':freeze['backend_build'],'files':{str(p.relative_to(PROJECT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)}}
 raw=json.dumps(manifest,ensure_ascii=False,indent=2)+'\n';out=rt/'source-delivered.zip'
 with zipfile.ZipFile(out,'x',zipfile.ZIP_DEFLATED,compresslevel=9) as z:
  for p in sorted(files):z.write(p,str(p.relative_to(PROJECT)))
  z.writestr('MANIFEST.json',raw)
 with zipfile.ZipFile(out) as z:
  assert z.testzip() is None
  for name,digest in manifest['files'].items():assert hashlib.sha256(z.read(name)).hexdigest()==digest,name
 (rt/'source-delivered-manifest.json').write_text(raw)
 result={'ok':True,'path':str(out),'files':len(files),'bytes':out.stat().st_size,'sha256':hashlib.sha256(out.read_bytes()).hexdigest(),'backend_build':manifest['backend_build']}
 (rt/'source-delivered.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
