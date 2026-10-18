"""Append retained real projects to the stopped production DB without replacing old projects.

Both services must be stopped. This is intentionally explicit and auditable;
controlled fixture projects are never imported.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from backend.store import Store
from phase34_backup import backup_bundle

def digest(value):return hashlib.sha256(value).hexdigest()

def main():
    p=argparse.ArgumentParser();p.add_argument('--apply',action='store_true');args=p.parse_args()
    source=ROOT/'.runtime/phase3/live/studio.sqlite3';target=ROOT/'.runtime/studio.sqlite3'
    # Do not silently stop a process or copy a changing WAL database.
    for state in (ROOT/'.runtime/server.json',ROOT/'.runtime/phase3/dev-server.json'):
        if state.exists():
            record=json.loads(state.read_text())
            import subprocess
            identity=subprocess.run(['ps','-p',str(record['pid']),'-o','lstart=,args='],capture_output=True,text=True).stdout.strip()
            if identity and identity==record.get('identity'):raise SystemExit('请先停止两个工作台服务，再合并记录。')
    with sqlite3.connect(target) as db:
        originals={r[0]:digest(r[1].encode()) for r in db.execute('SELECT id,document FROM projects')}
    if not args.apply:
        print(json.dumps({'existing_projects':len(originals),'source':str(source),'target':str(target),'writes':0},ensure_ascii=False));return
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');backup=ROOT/'.runtime/backups'/('before-phase34-release-'+stamp+'.zip')
    backup.parent.mkdir(exist_ok=True)
    backup_bundle(backup,target)
    Store(target) # additive schema migration after backup
    with sqlite3.connect(target) as db:
        db.execute('PRAGMA foreign_keys=ON');db.execute('ATTACH DATABASE ? AS accepted',(str(source),))
        ids=[r[0] for r in db.execute('SELECT id FROM accepted.projects')]
        overlap=set(ids)&set(originals)
        if overlap:raise SystemExit('存在同ID项目，拒绝覆盖；需先核对：'+','.join(sorted(overlap)))
        for table in ('projects','messages','runs','events','revisions','input_queue','domain_jobs','domain_job_events','evidence_snapshots','domain_checks','handoff_artifacts'):
            columns=[r[1] for r in db.execute(f'PRAGMA main.table_info({table})')]
            source_columns={r[1] for r in db.execute(f'PRAGMA accepted.table_info({table})')}
            if set(columns)!=source_columns:raise ValueError('待合并表结构不一致：'+table)
            names=','.join('"'+c+'"' for c in columns)
            db.execute(f'INSERT INTO main.{table} ({names}) SELECT {names} FROM accepted.{table}')
        db.commit()
        preserved={r[0]:digest(r[1].encode()) for r in db.execute('SELECT id,document FROM projects') if r[0] in originals}
        assert preserved==originals,'原有项目内容发生变化'
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    receipts=ROOT/'.runtime/phase3/live/runs';destination=ROOT/'.runtime/runs';destination.mkdir(exist_ok=True)
    if receipts.exists():
        for folder in receipts.iterdir():
            if (destination/folder.name).exists():raise ValueError('运行凭据目录冲突：'+folder.name)
            shutil.copytree(folder,destination/folder.name)
    # Handoff BLOBs are already copied through handoff_artifacts.
    result={'ok':True,'backup':str(backup),'imported_projects':len(ids),'preserved_projects':len(originals),
            'original_document_sha256':originals,'imported_project_ids':ids,'integrity':'ok'}
    (ROOT/'.runtime/phase34-deployment.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('original_document_sha256','imported_project_ids')},ensure_ascii=False))

if __name__=='__main__':main()
