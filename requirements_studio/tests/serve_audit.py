"""Start an isolated real or controlled acceptance server, never production."""
import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='独立需求工作台验收服务')
parser.add_argument('mode',choices=['real','controlled'])
args=parser.parse_args()
dist=ROOT/'.runtime/ui-audit-dist'
if not (dist/'index.html').is_file():
    raise SystemExit('请先执行：cd frontend && npm run build -- --outDir ../.runtime/ui-audit-dist')
if args.mode=='controlled':
    folder=ROOT/'.runtime/ui-audit-bin';folder.mkdir(parents=True,exist_ok=True)
    binary=folder/'codex'
    binary.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -X utf8 '+shlex.quote(str(ROOT/'tests/provider_fixture.py'))+' "$@"\n',encoding='utf-8');binary.chmod(0o755)
    os.environ['PATH']=str(folder)+os.pathsep+os.environ.get('PATH','')
    runtime=ROOT/'.runtime/ui-audit-controlled';port=8881
else:
    binary=shutil.which('codex')
    if not binary or 'ui-audit-bin' in binary:
        raise SystemExit('真实验收必须使用已安装并登录的 Codex CLI，不能使用受控替身。')
    runtime=ROOT/'.runtime/ui-audit-real-final';port=8880
os.environ['REQUIREMENTS_STUDIO_DATA']=str(runtime)
os.environ['REQUIREMENTS_STUDIO_DIST']=str(dist)
os.chdir(ROOT)
print(f'验收模式：{args.mode}；地址 http://[::1]:{port}；数据 {runtime}',flush=True)
os.execv(sys.executable,[sys.executable,'-m','uvicorn','backend.app:app','--host','::1','--port',str(port),'--timeout-graceful-shutdown','3'])
