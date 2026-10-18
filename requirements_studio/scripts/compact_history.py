"""Compact only this workbench's revision JSON, retaining every logical byte."""
from contextlib import closing
from datetime import datetime, timezone
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
from backend.history_codec import history_text,pack_history
from phase34_backup import backup_bundle


def fingerprints(db):
    current=hashlib.sha256();history=hashlib.sha256()
    for pid,document in db.execute('SELECT id,document FROM projects ORDER BY id'):
        current.update((pid+'\0'+document).encode('utf-8'))
    for pid,version,document in db.execute('SELECT project_id,version,document FROM revisions ORDER BY project_id,version'):
        history.update((pid+'\0'+str(version)+'\0'+history_text(document)).encode('utf-8'))
    return {'current_documents':current.hexdigest(),'all_historical_documents':history.hexdigest()}


def compact_database(database,backup):
    database=Path(database);before=database.stat().st_size
    backup_bundle(Path(backup),database)
    with closing(sqlite3.connect(database)) as db:
        if db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running')").fetchone()[0]:
            raise ValueError('仍有模型任务，停止服务并等待任务结束后再整理。')
        if db.execute("SELECT COUNT(*) FROM domain_jobs WHERE status IN ('queued','running')").fetchone()[0]:
            raise ValueError('仍有资料任务，停止服务并等待任务结束后再整理。')
        original=fingerprints(db);rows=db.execute('SELECT project_id,version,document FROM revisions').fetchall()
        changed=raw_bytes=stored_bytes=0
        for pid,version,value in rows:
            text=history_text(value);packed=pack_history(text)
            assert history_text(packed)==text
            raw_bytes+=len(text.encode('utf-8'))
            stored_bytes+=len(packed) if isinstance(packed,bytes) else len(packed.encode('utf-8'))
            if packed!=value:
                db.execute('UPDATE revisions SET document=? WHERE project_id=? AND version=?',(packed,pid,version));changed+=1
        db.commit()
        assert fingerprints(db)==original,'当前文档或历史文档发生变化，停止整理。'
        db.execute('PRAGMA temp_store=MEMORY');db.execute('VACUUM')
        assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert fingerprints(db)==original
    return {'database':str(database),'backup':str(backup),'revisions':len(rows),'compacted':changed,
            'raw_utf8_bytes':raw_bytes,'stored_bytes':stored_bytes,'database_bytes_before':before,
            'database_bytes_after':database.stat().st_size,'logical_sha256':original,'all_document_bytes_unchanged':True}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--target',choices=('main','dev','both'),default='main');parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    targets={'main':ROOT/'.runtime/studio.sqlite3','dev':ROOT/'.runtime/phase3/live/studio.sqlite3'}
    selected=targets if args.target=='both' else {args.target:targets[args.target]}
    if not args.apply:
        print(json.dumps({'databases':{k:str(v) for k,v in selected.items()},'writes':0}));return
    for file in (ROOT/'.runtime/server.json',ROOT/'.runtime/phase3/dev-server.json'):
        if not file.exists():continue
        info=json.loads(file.read_text());actual=subprocess.run(['ps','-p',str(info['pid']),'-o','lstart=,args='],capture_output=True,text=True).stdout.strip()
        if actual and actual==info.get('identity'):raise SystemExit('请先停止主工作台和开发工作台；不会整理正在运行的数据库。')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');results=[]
    for name,path in selected.items():
        if path.exists():results.append(compact_database(path,ROOT/'.runtime/backups'/f'history-{name}-{stamp}.zip'))
    out=ROOT/'.runtime/phase3/history-compaction.json';out.write_text(json.dumps({'results':results},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
