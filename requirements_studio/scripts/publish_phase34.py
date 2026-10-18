"""Publish the verified frontend with an offline DB backup; keep existing projects byte-for-byte."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.model_context import release_hash
from phase34_backup import backup_bundle

def fingerprints(database):
    with sqlite3.connect(database) as db:return {r[0]:hashlib.sha256(r[1].encode()).hexdigest() for r in db.execute('SELECT id,document FROM projects')}

def main():
    state_path=ROOT/'.runtime/server.json'
    if state_path.exists():
        state=json.loads(state_path.read_text())
        identity=subprocess.run(['ps','-p',str(state['pid']),'-o','lstart=,args='],capture_output=True,text=True).stdout.strip()
        if identity and identity==state.get('identity'):raise SystemExit('请先停止正式服务；发布只在停机备份后进行。')
    source=ROOT/'.runtime/phase3-dist';database=ROOT/'.runtime/studio.sqlite3'
    assert (source/'index.html').exists() and database.exists()
    before=fingerprints(database);stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=ROOT/'.runtime/backups'/('phase34-publish-'+stamp+'.zip')
    destination=ROOT/'frontend/dist'
    backup_bundle(backup,database,destination)
    staged=ROOT/'frontend/dist-phase34-staged'
    if staged.exists():raise SystemExit('存在未完成的暂存目录，请先核对；不会覆盖。')
    shutil.copytree(source,staged)
    if destination.exists():shutil.rmtree(destination)
    staged.rename(destination)
    from backend.store import Store
    Store(database)
    assert before==fingerprints(database)
    baseline=ROOT/'.runtime/backups/phase3-before-20261015T171429Z.sqlite3'
    original=fingerprints(baseline) if baseline.exists() else {}
    original_unchanged=all(before.get(k)==v for k,v in original.items())
    report={'ok':True,'backend_build':release_hash(),'backup':str(backup),'preserved_projects':len(before),
            'original_document_sha256':before,'baseline_project_count':len(original),'baseline_documents_unchanged':original_unchanged,
            'frontend_files':{str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.rglob('*') if p.is_file()},
            'stage4_status':'real_case_acceptance_in_progress'}
    (ROOT/'.runtime/phase34-publication.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('ok','backup','preserved_projects','baseline_project_count','baseline_documents_unchanged','backend_build')},ensure_ascii=False))

if __name__=='__main__':main()
