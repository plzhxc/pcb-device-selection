"""Lossless terminal-run input archives; answers and receipts remain directly readable."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import zipfile


def archive_run_inputs(folder: Path):
    files=[p for parent in (folder,folder/'repair-1')
           for name in ('context.json','response.schema.json')
           if (p:=parent/name).is_file() and not p.is_symlink()]
    if not files:return {'files':0}
    target=folder/'inputs.zip';entries={}
    if target.exists():
        with zipfile.ZipFile(target) as prior:
            entries={name:prior.read(name) for name in prior.namelist() if name!='inputs-manifest.json'}
    originals={p:p.read_bytes() for p in files}
    entries.update({p.relative_to(folder).as_posix():raw for p,raw in originals.items()})
    hashes={name:hashlib.sha256(raw).hexdigest() for name,raw in entries.items()}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for name,raw in entries.items():archive.writestr(name,raw)
        archive.writestr('inputs-manifest.json',json.dumps({'sha256':hashes},sort_keys=True))
    temporary=target.with_name('inputs.'+str(os.getpid())+'.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(buffer.getvalue());stream.flush();os.fsync(stream.fileno())
        with zipfile.ZipFile(temporary) as archive:
            for name,digest in hashes.items():
                if hashlib.sha256(archive.read(name)).hexdigest()!=digest:
                    raise ValueError('运行输入归档校验失败；原文件保留。')
        temporary.replace(target)
        directory=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
        for file,raw in originals.items():
            if file.read_bytes()==raw:file.unlink()
        return {'files':len(files),'original_bytes':sum(map(len,originals.values())),
                'archive_bytes':target.stat().st_size}
    finally:temporary.unlink(missing_ok=True)
