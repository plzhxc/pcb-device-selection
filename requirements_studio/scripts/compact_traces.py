"""Losslessly pack completed CLI inputs; leave answers/receipts and all UI data intact."""
from contextlib import closing
from datetime import datetime,timezone
import argparse
import io
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def pack_files(destination,files,base):
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for p in files:z.write(p,p.relative_to(base))
    raw=buffer.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        if z.testzip() is not None:raise ValueError('归档内容校验失败，原文件保留。')
    created=False
    try:
        with destination.open('xb') as stream:
            created=True
            stream.write(raw)
            stream.flush();os.fsync(stream.fileno())
        with zipfile.ZipFile(destination) as z:
            if z.testzip() is not None:raise ValueError('归档文件校验失败，原文件保留。')
    except Exception:
        if created:destination.unlink(missing_ok=True)
        raise
    return len(raw)


def compact_completed():
    count=before=after=0
    for runtime in (ROOT/'.runtime',ROOT/'.runtime/phase3/live'):
        dbfile=runtime/'studio.sqlite3'
        if not dbfile.exists():continue
        with closing(sqlite3.connect(dbfile.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            ids=[r[0] for r in db.execute("SELECT id FROM runs WHERE status='completed'")]
        for rid in ids:
            if not re.fullmatch('[a-f0-9]{32}',rid):continue
            folder=runtime/'runs'/rid
            if not folder.exists() or (folder/'inputs.zip').exists():continue
            files=[p for parent in (folder,folder/'repair-1') for name in ('context.json','response.schema.json') if (p:=parent/name).is_file() and p.stat().st_size>16384]
            if not files:continue
            original=sum(p.stat().st_size for p in files)
            packed=pack_files(folder/'inputs.zip',files,folder)
            for p in files:p.unlink()
            before+=original;after+=packed;count+=1
    result={'at':datetime.now(timezone.utc).isoformat(),'completed_inputs_archived':count,'original_bytes':before,'archive_bytes':after,
            'ui_database_changed':False,'answers_and_receipts_changed':False}
    log=ROOT/'.runtime/phase3/trace-compaction.jsonl';log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('a',encoding='utf-8') as stream:stream.write(json.dumps(result,ensure_ascii=False)+'\n')
    return result


def archive_controlled():
    source=ROOT/'.runtime/phase3/controlled'
    if not source.exists():return
    if (ROOT/'.runtime/phase3/fixture-server.json').exists():raise RuntimeError('请先停止受控服务再归档。')
    files=[p for p in source.rglob('*') if p.is_file()]
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target=ROOT/'.runtime/backups'/('controlled-'+stamp+'.zip');target.parent.mkdir(exist_ok=True)
    pack_files(target,files,source)
    shutil.rmtree(source)


def compact_acceptance():
    """Run after browser drivers stop. Preserve every snapshot byte and PNG path."""
    count=before=after=duplicates=saved=0
    root=ROOT/'.runtime/phase4'
    for file in root.glob('*/*.json'):
        if not re.fullmatch(r'CASE-\d{2}-R\d+\.json',file.name) or file.stat().st_size<100000:continue
        raw=file.read_bytes();json.loads(raw)
        packed=gzip.compress(raw,compresslevel=6,mtime=0)
        assert gzip.decompress(packed)==raw
        target=file.with_suffix('.json.gz');temporary=target.with_name(target.name+'.'+str(os.getpid())+'.tmp')
        try:
            with temporary.open('xb') as stream:stream.write(packed)
            assert gzip.decompress(temporary.read_bytes())==raw
            temporary.replace(target)
            file.unlink()
        finally:temporary.unlink(missing_ok=True)
        count+=1;before+=len(raw);after+=len(packed)
    seen={}
    for file in sorted(root.glob('*/*.png')):
        key=hashlib.sha256(file.read_bytes()).digest()
        original=seen.get(key)
        if original is None:seen[key]=file;continue
        if os.path.samefile(original,file):continue
        # The driver replaces screenshots atomically, so later captures break the
        # link and cannot rewrite another retained screenshot.
        allocated=file.stat().st_blocks*512
        temporary=file.with_name(file.name+'.'+str(os.getpid())+'.link')
        try:os.link(original,temporary);temporary.replace(file)
        finally:temporary.unlink(missing_ok=True)
        duplicates+=1;saved+=allocated
    return {'snapshots_compressed':count,'original_bytes':before,'compressed_bytes':after,
            'identical_screenshot_links':duplicates,'reclaimed_screenshot_allocation':saved,'ui_database_changed':False}


def archive_screenshots():
    """Explicit, lossless historical image archive; run with all drivers stopped."""
    root=ROOT/'.runtime/phase4'
    files=sorted(p for p in root.glob('*/*.png') if p.is_file() and not p.is_symlink())
    if not files:return {'archived_screenshots':0}
    manifest={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:xz',preset=6) as archive:
        for file in files:archive.add(file,arcname=file.relative_to(root).as_posix())
        raw=json.dumps({'schema':'pcbagent.acceptance-screenshots/1.0','sha256':manifest},indent=2).encode()
        info=tarfile.TarInfo('manifest.json');info.size=len(raw)
        archive.addfile(info,io.BytesIO(raw))
    packed=buffer.getvalue()
    # Decompress the full XZ stream (including CRC), then verify every image,
    # including hard-linked members, against its original byte checksum.
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(packed)),mode='r:') as archive:
        for name,checksum in manifest.items():
            assert hashlib.sha256(archive.extractfile(name).read()).hexdigest()==checksum
    target=ROOT/'.runtime/backups'/('case-screenshots-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.tar.xz')
    target.parent.mkdir(exist_ok=True)
    created=False
    try:
        with target.open('xb') as stream:created=True;stream.write(packed)
        assert hashlib.sha256(target.read_bytes()).digest()==hashlib.sha256(packed).digest()
    except Exception:
        if created:target.unlink(missing_ok=True)
        raise
    for file in files:
        if hashlib.sha256(file.read_bytes()).hexdigest()!=manifest[file.relative_to(root).as_posix()]:
            raise RuntimeError('截图已改变，归档已保留，未删除任何展开文件。')
    original=sum(p.stat().st_size for p in files)
    for file in files:file.unlink()
    result={'archive':str(target),'archived_screenshots':len(files),'original_bytes':original,
            'archive_bytes':len(packed),'sha256':hashlib.sha256(packed).hexdigest(),'ui_database_changed':False}
    with (ROOT/'.runtime/phase3/screenshot-archives.jsonl').open('a') as stream:stream.write(json.dumps(result)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--archive-screenshots',action='store_true',help='停止所有案例驱动后，将历史PNG无损归档')
    args=parser.parse_args()
    result=compact_completed();result['acceptance']=compact_acceptance()
    if args.archive_screenshots:result['screenshots']=archive_screenshots()
    print(json.dumps(result,ensure_ascii=False))
