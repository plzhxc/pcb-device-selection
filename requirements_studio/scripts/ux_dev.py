"""Repeat the answer/document UX acceptance without changing normal 8878 projects."""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description='需求工作台体验回归')
parser.add_argument('action',choices=['build','unit','accept','check']);args=parser.parse_args()
out=ROOT/'.runtime/ux/verification';out.mkdir(parents=True,exist_ok=True)

def run(command,cwd=ROOT,log=None):
    print('运行：'+' '.join(map(str,command)),flush=True)
    if log:
        with (out/log).open('w',encoding='utf-8') as stream:subprocess.run(command,cwd=cwd,check=True,stdout=stream,stderr=subprocess.STDOUT)
    else:subprocess.run(command,cwd=cwd,check=True)

if args.action in ('unit','check'):run([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-p','test_*.py'],log='unit.log')
if args.action in ('build','accept','check'):run(['npm','run','build','--','--outDir','../.runtime/ux-dist'],cwd=ROOT/'frontend')
if args.action in ('accept','check'):
    with httpx.Client(trust_env=False,timeout=1) as client:
        try:occupied=client.get('http://[::1]:8893/api/health').status_code==200
        except httpx.HTTPError:occupied=False
    if occupied:raise SystemExit('8893已有验收服务，请先结束该隔离服务后再运行；不会停止已有进程。')
    with (out/'server.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([sys.executable,'-X','utf8','tests/serve_ux.py'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            with httpx.Client(trust_env=False,timeout=1) as client:
                for _ in range(80):
                    if process.poll() is not None:raise RuntimeError('验收服务启动失败，见server.log')
                    try:
                        if client.get('http://[::1]:8893/api/health').status_code==200:break
                    except httpx.HTTPError:pass
                    time.sleep(.25)
                else:raise RuntimeError('验收服务未就绪')
            run(['node','tests/ux_browser.mjs'],log='browser.log')
            data=json.loads((out/'browser.json').read_text(encoding='utf-8'))
            assert data['ok'] and len(data['rounds'])==2
            print(json.dumps({'ok':True,'rounds':[{'round':r['round'],'checks':len(r['checks'])} for r in data['rounds']],'report':str(out/'browser.json')},ensure_ascii=False))
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=6)
                except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
