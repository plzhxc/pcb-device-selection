"""Reproducible phase-2 development and acceptance entrypoint, isolated by default."""
from __future__ import annotations
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
PHASE=ROOT/'.runtime/phase2';PHASE.mkdir(parents=True,exist_ok=True)


def run(command,**kwargs):
    print('运行：'+' '.join(map(str,command)),flush=True)
    subprocess.run(command,cwd=ROOT,check=True,**kwargs)


def build():
    run([sys.executable,'-X','utf8','scripts/build_phase2_catalog.py'])
    subprocess.run(['npm','run','build','--','--outDir','../.runtime/phase2-dist'],cwd=ROOT/'frontend',check=True)


def tests():
    env={**os.environ,'REQUIREMENTS_STUDIO_DATA':str(PHASE/'unit-app')}
    for n in (1,2):
        with (PHASE/f'unit-round-{n}.log').open('w',encoding='utf-8') as log:
            run([sys.executable,'-X','utf8','-m','unittest','discover','-s','tests','-p','test_*.py','-v'],env=env,stdout=log,stderr=subprocess.STDOUT)


def acceptance(real=False):
    port=8892 if real else 8891;mode='real' if real else 'controlled'
    with httpx.Client(trust_env=False,timeout=1) as client:
        try:client.get(f'http://[::1]:{port}/api/health')
        except httpx.ConnectError:pass
        else:raise SystemExit(f'隔离验收端口 {port} 已占用；请先结束已有验收服务。正式8878不受影响。')
    if not real:run([sys.executable,'-X','utf8','tests/seed_phase2.py'])
    with (PHASE/f'{mode}-server.log').open('a',encoding='utf-8') as log:
        server=subprocess.Popen([sys.executable,'-X','utf8','tests/serve_phase2.py',mode],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            for _ in range(100):
                if server.poll() is not None:raise RuntimeError('验收服务启动失败，请检查日志。')
                try:
                    with httpx.Client(trust_env=False,timeout=1) as c:
                        if c.get(f'http://[::1]:{port}/api/health').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:raise RuntimeError('验收服务启动超时。')
            if real:
                run(['node','tests/phase2_real.mjs'])
                run(['node','tests/phase2_parent_browser.mjs'])
            else:
                run(['node','tests/phase2_browser.mjs'])
                run([sys.executable,'-X','utf8','tests/phase2_load.py'])
        finally:
            os.killpg(server.pid,signal.SIGTERM) if server.poll() is None else None
            try:server.wait(timeout=12)
            except subprocess.TimeoutExpired:os.killpg(server.pid,signal.SIGKILL);server.wait()


def main():
    parser=argparse.ArgumentParser(description='第二阶段：目录提取、UI构建、隔离开发和两轮随机验收')
    parser.add_argument('command',choices=['build','serve','serve-real','test','accept','accept-real','verify','report'])
    args=parser.parse_args()
    if args.command=='build':build()
    elif args.command in ('serve','serve-real'):
        mode='real' if args.command=='serve-real' else 'controlled'
        os.execv(sys.executable,[sys.executable,'-X','utf8',str(ROOT/'tests/serve_phase2.py'),mode])
    elif args.command=='test':tests()
    elif args.command=='accept':acceptance()
    elif args.command=='accept-real':acceptance(real=True)
    elif args.command=='verify':build();tests();acceptance()
    elif args.command=='report':run([sys.executable,'-X','utf8','tests/summarize_phase2.py'])


if __name__=='__main__':main()
