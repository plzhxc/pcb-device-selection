"""Publish two device/canvas synchronization case forks, with preservation checks.

Use --inspect while services run. For --apply stop main and isolated services
after all active work has finished. Existing projects and pending old input are
never overwritten, resumed, or reclassified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'scripts'))
from phase34_backup import backup_bundle

TABLES=('projects','messages','runs','events','revisions','input_queue','domain_jobs',
        'domain_job_events','evidence_snapshots','domain_checks','handoff_artifacts')
RUNTIME=ROOT/'.runtime/canvas-sync-20261020'


def rows_digest(db,table):
    info=list(db.execute(f'PRAGMA table_info({table})'))
    primary=[r[1] for r in sorted(info,key=lambda r:r[5]) if r[5]]
    indices=[next(i for i,r in enumerate(info) if r[1]==name) for name in primary]
    result={}
    for row in db.execute(f'SELECT * FROM {table}'):
        raw=json.dumps(tuple(row),ensure_ascii=False,separators=(',',':'),
            default=lambda value:{'bytes':value.hex()}).encode()
        result[tuple(row[i] for i in indices)]=hashlib.sha256(raw).hexdigest()
    return result


def selected_filter(table):
    if table=='projects':return 'id IN (SELECT id FROM chosen_projects)'
    if table=='events':return 'run_id IN (SELECT id FROM accepted.runs WHERE project_id IN (SELECT id FROM chosen_projects))'
    if table=='domain_job_events':return 'job_id IN (SELECT id FROM accepted.domain_jobs WHERE project_id IN (SELECT id FROM chosen_projects))'
    return 'project_id IN (SELECT id FROM chosen_projects)'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--inspect',action='store_true');args=parser.parse_args()
    index_file=ROOT/'.runtime/customer-acceptance/canvas-sync-20261020/index.json'
    index=json.loads(index_file.read_text())
    cases=index['cases']
    if {c['key'] for c in cases}!={'toy','voice'}:raise SystemExit('开发记录不是本次器件联动的玩具和语音板两份副本，停止发布。')
    if index.get('input_mode')!='existing_case_copy':raise SystemExit('必须明确标注历史案例副本。')
    ids=[c['project_id'] for c in cases]
    if len(ids)!=len(set(ids)):raise SystemExit('项目编号重复。')
    source=RUNTIME/'live/studio.sqlite3';target=ROOT/'.runtime/studio.sqlite3'
    from backend.model_context import release_hash
    freeze=json.loads((RUNTIME/'source-freeze.json').read_text())
    if freeze['backend_build']!=release_hash():raise SystemExit('后端源码已离开已验收指纹，停止发布。')
    for name,digest in freeze['frontend_files'].items():
        if hashlib.sha256((RUNTIME/'dist'/name).read_bytes()).hexdigest()!=digest:raise SystemExit('前端构建已离开冻结版本。')
    for path in (ROOT.parent/'docs/需求分析工作台_客户使用与流程介绍.html',ROOT/'frontend/dist/reports/requirements-studio-guide.html'):
        if hashlib.sha256(path.read_bytes()).hexdigest()!=freeze['independent_html_sha256']:raise SystemExit('独立介绍报告发生变化，停止发布。')
    for path in (source,target):
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            for table in ('runs','domain_jobs'):
                if db.execute(f"SELECT 1 FROM {table} WHERE status IN ('queued','running')").fetchone():
                    raise SystemExit(str(path)+' 仍有活动任务，稍后再发布。')
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as db:
        for pid in ids:
            row=db.execute('SELECT document FROM projects WHERE id=?',(pid,)).fetchone()
            if not row:raise SystemExit('缺少验收副本。')
            doc=json.loads(row[0])
            if doc.get('review_status')!='approved' or not doc.get('forked_from') or any(c['status']=='pending' for c in doc.get('canvas_changes',[])):
                raise SystemExit('验收副本尚未审核或仍有待同步项，停止发布。')
    with sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True) as db:
        before={t:rows_digest(db,t) for t in TABLES}
    if set(ids)&{key[0] for key in before['projects']}:raise SystemExit('拒绝覆盖已有同编号项目。')
    if not args.apply:
        print(json.dumps({'writes':0,'retained_products':ids,'existing_projects':len(before['projects']),
            'dist':str(RUNTIME/'dist')},ensure_ascii=False));return
    for record_file in (ROOT/'.runtime/server.json',RUNTIME/'service.json'):
        if not record_file.exists():continue
        record=json.loads(record_file.read_text())
        actual=subprocess.run(['ps','-ww','-p',str(record['pid']),'-o','args='],capture_output=True,text=True).stdout.strip()
        if ('backend.app:app' in actual or 'scripts/serve_canvas_sync.py' in actual):
            raise SystemExit('请先结束已空闲的主服务与隔离服务。')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=ROOT/'.runtime/backups'/('before-canvas-sync-'+stamp+'.zip')
    backup_bundle(backup,target,ROOT/'frontend/dist')
    copied=[]
    with sqlite3.connect(source) as db:
        marks=','.join('?' for _ in ids)
        run_ids=[row[0] for row in db.execute(f'SELECT id FROM runs WHERE project_id IN ({marks})',ids)]
    try:
        for run_id in run_ids:
            origin=RUNTIME/'live/runs'/run_id;destination=ROOT/'.runtime/runs'/run_id
            if destination.exists():raise ValueError('运行凭据目录冲突：'+run_id)
            if origin.exists():shutil.copytree(origin,destination);copied.append(destination)
        with sqlite3.connect(target) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('ATTACH DATABASE ? AS accepted',(str(source),))
            db.execute('CREATE TEMP TABLE chosen_projects(id TEXT PRIMARY KEY)')
            db.executemany('INSERT INTO chosen_projects VALUES(?)',[(pid,) for pid in ids])
            imported={}
            for table in TABLES:
                columns=[r[1] for r in db.execute(f'PRAGMA main.table_info({table})')]
                if set(columns)!={r[1] for r in db.execute(f'PRAGMA accepted.table_info({table})')}:raise ValueError('表结构不一致：'+table)
                names=','.join('"'+column+'"' for column in columns)
                db.execute(f'INSERT INTO main.{table} ({names}) SELECT {names} FROM accepted.{table} WHERE {selected_filter(table)}')
                imported[table]=db.execute('SELECT changes()').fetchone()[0]
            for table in TABLES:
                after=rows_digest(db,table)
                if any(after.get(key)!=digest for key,digest in before[table].items()):raise ValueError('旧记录发生变化：'+table)
                if len(after)!=len(before[table])+imported[table]:raise ValueError('记录数量不一致：'+table)
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok' or db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('合并完整性未通过。')
    except BaseException:
        for directory in copied:shutil.rmtree(directory)
        raise
    # Keep earlier assets and the standalone report available to existing tabs.
    dist=RUNTIME/'dist';destination=ROOT/'frontend/dist'
    for item in dist.iterdir():
        if item.name=='index.html':continue
        if item.is_dir():shutil.copytree(item,destination/item.name,dirs_exist_ok=True)
        else:shutil.copy2(item,destination/item.name)
    temporary=destination/'index.next.html';shutil.copy2(dist/'index.html',temporary);temporary.replace(destination/'index.html')
    result={'ok':True,'backup':str(backup),'imported':imported,'old_records_preserved':True,
        'projects':[{'id':pid,'url':'http://[::1]:8878/?project='+pid} for pid in ids],
        'existing_projects':len(before['projects']),'project_count':len(before['projects'])+len(ids)}
    (RUNTIME/'old-row-digests.json').write_text(json.dumps({table:[{'primary_key':list(key),'sha256':digest} for key,digest in hashes.items()] for table,hashes in before.items()},ensure_ascii=False,indent=2)+'\n')
    (RUNTIME/'publication.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    index['development_base']=index['base'];index['base']='http://[::1]:8878'
    for case in cases:
        case['development_url']='http://[::1]:8896/?project='+case['project_id']
        case['production_url']='http://[::1]:8878/?project='+case['project_id']
        case['service_base']='http://[::1]:8878'
    index_file.write_text(json.dumps(index,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
