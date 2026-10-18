"""Isolated acceptance server; the controlled provider cannot enter production."""
import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['real','controlled']);args=parser.parse_args()
runtime=ROOT/'.runtime/phase2'/args.mode
runtime.mkdir(parents=True,exist_ok=True)
if args.mode=='controlled':
    folder=ROOT/'.runtime/phase2/fixture-bin';folder.mkdir(parents=True,exist_ok=True)
    binary=folder/'codex';binary.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -X utf8 '+shlex.quote(str(ROOT/'tests/phase2_provider.py'))+' "$@"\n',encoding='utf-8');binary.chmod(0o755)
    os.environ['PATH']=str(folder)+os.pathsep+os.environ.get('PATH','');port=8891
else:
    binary=shutil.which('codex')
    if not binary or 'fixture-bin' in binary or 'ui-audit-bin' in binary:raise SystemExit('真实验收必须使用已登录的 Codex CLI。')
    port=8892
os.environ['REQUIREMENTS_STUDIO_DATA']=str(runtime)
os.environ['REQUIREMENTS_STUDIO_DIST']=str(ROOT/'.runtime/phase2-dist')
os.chdir(ROOT)
os.execv(sys.executable,[sys.executable,'-X','utf8','-m','uvicorn','backend.app:app','--host','::1','--port',str(port),'--timeout-graceful-shutdown','3'])
