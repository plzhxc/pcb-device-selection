"""Start, inspect, or stop only this studio's local background server."""
from __future__ import annotations

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
RUNTIME=ROOT/'.runtime'
STATE=RUNTIME/'server.json'


def identity(pid):
    result=subprocess.run(['ps','-p',str(pid),'-o','lstart=,args='],capture_output=True,text=True)
    return result.stdout.strip()


def load_state():
    try:return json.loads(STATE.read_text())
    except (OSError,ValueError):return None


def owned(state):
    return bool(state and state.get('identity') and identity(state['pid'])==state['identity'])


def url(host,port):return f"http://{'['+host+']' if ':' in host else host}:{port}"


def healthy(address):
    try:
        with httpx.Client(trust_env=False,timeout=1) as client:
            result=client.get(address+'/api/health')
            return result.status_code==200 and result.json().get('app')=='requirements-studio'
    except Exception:return False


def main():
    parser=argparse.ArgumentParser(description='独立需求分析工作台')
    parser.add_argument('action',nargs='?',choices=['start','stop','status'],default='start')
    parser.add_argument('--host',choices=['::1','127.0.0.1','localhost'],default='::1')
    parser.add_argument('--port',type=int,default=8878)
    parser.add_argument('--foreground',action='store_true')
    args=parser.parse_args()
    RUNTIME.mkdir(parents=True,exist_ok=True)
    state=load_state()
    if args.action=='status':
        if owned(state):
            address=url(state['host'],state['port'])
            print(json.dumps({'running':True,'healthy':healthy(address),'pid':state['pid'],'url':address},ensure_ascii=False))
        else:print(json.dumps({'running':False,'message':'后台服务未运行。'},ensure_ascii=False))
        return
    if args.action=='stop':
        if not owned(state):
            print('未发现本启动器管理的运行进程。');return
        os.killpg(state['pid'],signal.SIGTERM)
        for _ in range(40):
            if not owned(state):break
            time.sleep(.2)
        if owned(state):
            raise SystemExit('服务仍在结束当前请求，请稍后再检查状态。')
        STATE.unlink(missing_ok=True)
        print('需求工作台已停止，文档保存在 .runtime/studio.sqlite3。');return
    if owned(state):
        print('工作台已启动：'+url(state['host'],state['port']));return
    if not 1024<=args.port<=65535:raise SystemExit('端口应在 1024～65535 之间。')
    if not (ROOT/'frontend/dist/index.html').is_file():raise SystemExit('请先在 frontend 执行 npm install 和 npm run build。')
    address=url(args.host,args.port)
    if healthy(address):raise SystemExit('此地址已有工作台运行，请直接访问：'+address)
    command=[sys.executable,'-m','uvicorn','backend.app:app','--host',args.host,'--port',str(args.port),'--timeout-graceful-shutdown','3']
    print('需求工作台：'+address,flush=True)
    if args.foreground:
        os.chdir(ROOT)
        os.execv(sys.executable,command)
    with (RUNTIME/'server.log').open('ab') as log:
        process=subprocess.Popen(command,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    record={'pid':process.pid,'host':args.host,'port':args.port,'identity':identity(process.pid)}
    STATE.write_text(json.dumps(record,ensure_ascii=False,indent=2))
    for _ in range(40):
        if process.poll() is not None:
            STATE.unlink(missing_ok=True)
            raise SystemExit('服务未能启动，请查看 .runtime/server.log。')
        if healthy(address):
            print('已就绪。模型：gpt6-luna-xhigh（Codex CLI）。')
            print('后台运行；使用 ./start.sh status 查看状态，./start.sh stop 停止。')
            return
        time.sleep(.2)
    raise SystemExit('服务启动较慢，请查看 .runtime/server.log 并运行 ./start.sh status。')


if __name__=='__main__':main()
