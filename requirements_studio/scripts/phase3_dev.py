"""Phase 3/4 development and acceptance commands, isolated from port 8878."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
PROJECT=ROOT.parent
PYTHON=PROJECT/'.venv/bin/python'
STATE=ROOT/'.runtime/phase3/dev-server.json'
FIXTURE_STATE=ROOT/'.runtime/phase3/fixture-server.json'


def environment():
 env=os.environ.copy()
 env.update(REQUIREMENTS_STUDIO_DATA=str(ROOT/'.runtime/phase3/live'),
  REQUIREMENTS_STUDIO_DIST=str(ROOT/'.runtime/phase3-dist'),REQUIREMENTS_STUDIO_EFFORT=os.environ.get('REQUIREMENTS_STUDIO_EFFORT','xhigh'),REQUIREMENTS_STUDIO_FAST='1')
 return env


def run(args,cwd=PROJECT,env=None):
 subprocess.run([str(x) for x in args],cwd=cwd,env=env or os.environ.copy(),check=True)


def unit_tests(pattern='test_*.py'):
 """Stream real test output and record evidence for the code that was tested."""
 sys.path.insert(0,str(ROOT))
 from backend.model_context import release_hash
 tested_build=release_hash()
 out=ROOT/'.runtime/phase3';out.mkdir(parents=True,exist_ok=True)
 env=environment();env['REQUIREMENTS_STUDIO_KNOWLEDGE']='0';env['REQUIREMENTS_STUDIO_DATA']=str(out/'unit-app')
 focused=pattern!='test_*.py';log=out/('unit-focused.log' if focused else 'unit-latest.log')
 command=[str(PYTHON),'-X','utf8','-m','unittest','discover','-v','-s',str(ROOT/'tests'),'-p',pattern]
 with log.open('w',encoding='utf-8') as saved:
  process=subprocess.Popen(command,cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8')
  for line in process.stdout:
   sys.stdout.write(line);sys.stdout.flush();saved.write(line)
  code=process.wait()
 raw=log.read_text(encoding='utf-8');matches=re.findall(r'Ran (\d+) tests in ([\d.]+)s',raw)
 count,elapsed=matches[-1] if matches else ('0','0')
 current_build=release_hash()
 result={'ok':code==0 and int(count)>0 and tested_build==current_build,'backend_build':tested_build,
         'unchanged_during_tests':tested_build==current_build,'tests':int(count),'elapsed_seconds':float(elapsed),
         'pattern':pattern,'log':str(log.relative_to(ROOT)),'log_sha256':hashlib.sha256(log.read_bytes()).hexdigest(),
         'completed_at':datetime.now(timezone.utc).isoformat()}
 (out/('unit-focused-result.json' if focused else 'unit-result.json')).write_text(json.dumps(result,indent=2)+'\n')
 if not result['ok']:raise subprocess.CalledProcessError(code or 1,command)


@contextmanager
def test_browser():
 """One temporary profile for an acceptance command; reuse an owned live bridge."""
 state=ROOT/'.runtime/phase3/browser-bridge.json'
 if state.exists():
  record=json.loads(state.read_text())
  identity=subprocess.run(['ps','-p',str(record['pid']),'-o','args='],capture_output=True,text=True).stdout
  if 'tests/shared_browser.mjs' in identity:
   yield
   return
 with (ROOT/'.runtime/phase3/browser-session.log').open('ab') as log:
  process=subprocess.Popen(['node',str(ROOT/'tests/shared_browser.mjs')],cwd=ROOT,stdout=log,stderr=log,start_new_session=True)
 try:
  for _ in range(100):
   if state.exists() and json.loads(state.read_text()).get('pid')==process.pid:break
   if process.poll() is not None:raise RuntimeError('测试浏览器未启动，请查看browser-session.log')
   time.sleep(.1)
  else:raise RuntimeError('测试浏览器未及时就绪')
  yield
 finally:
  if process.poll() is None:
   process.terminate()
   try:process.wait(timeout=15)
   except subprocess.TimeoutExpired:print('测试浏览器仍在结束，请查看browser-bridge.json；不会停止不明进程。')


def owned(record):
 if not record:return False
 p=subprocess.run(['ps','-p',str(record['pid']),'-o','lstart=,args='],capture_output=True,text=True)
 return p.stdout.strip()==record.get('identity')


def stop_owned(record):
 if not owned(record):return
 os.killpg(record['pid'],signal.SIGTERM)
 for _ in range(60):
  if not owned(record):return
  time.sleep(.2)
 raise RuntimeError('本脚本拥有的服务仍在结束任务，状态记录保留；请稍后重试。')


def server(action,port):
 import httpx
 record=json.loads(STATE.read_text()) if STATE.exists() else None
 if action=='stop':
  stop_owned(record)
  STATE.unlink(missing_ok=True);return
 if owned(record):print(json.dumps(record,ensure_ascii=False));return
 address=f'http://[::1]:{port}'
 with httpx.Client(trust_env=False,timeout=1) as c:
  try:c.get(address+'/api/health')
  except httpx.HTTPError:pass
  else:raise SystemExit('端口已有服务，请换端口；不会停止不明进程。')
 STATE.parent.mkdir(parents=True,exist_ok=True)
 if not (ROOT/'.runtime/phase3-dist/index.html').exists():raise SystemExit('请先运行 phase3.sh build')
 with (STATE.parent/'server.log').open('ab') as log:
  p=subprocess.Popen([str(PYTHON),'-m','uvicorn','backend.app:app','--host','::1','--port',str(port),'--timeout-graceful-shutdown','5'],
     cwd=ROOT,env=environment(),stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
 info=subprocess.run(['ps','-p',str(p.pid),'-o','lstart=,args='],capture_output=True,text=True).stdout.strip()
 record={'pid':p.pid,'identity':info,'url':address,'port':port,'model':'gpt6-luna-'+environment()['REQUIREMENTS_STUDIO_EFFORT']+' fast'};STATE.write_text(json.dumps(record,ensure_ascii=False,indent=2))
 for _ in range(50):
  if p.poll() is not None:raise SystemExit('服务启动失败，请查看 .runtime/phase3/server.log')
  try:
   with httpx.Client(trust_env=False,timeout=1) as c:
    if c.get(address+'/api/health').status_code==200:print(json.dumps(record,ensure_ascii=False));return
  except httpx.HTTPError:pass
  time.sleep(.2)
 raise SystemExit('服务仍在启动，请查看日志。')


def fixture_server(action):
 import httpx
 record=json.loads(FIXTURE_STATE.read_text()) if FIXTURE_STATE.exists() else None
 if action=='stop':
  stop_owned(record)
  FIXTURE_STATE.unlink(missing_ok=True);return
 if owned(record):return
 with httpx.Client(trust_env=False,timeout=1) as c:
  try:c.get('http://[::1]:8895/api/health')
  except httpx.HTTPError:pass
  else:raise SystemExit('8895已有非本脚本拥有的服务，不会停止不明进程。')
 from compact_traces import compact_completed,archive_controlled
 compact_completed();archive_controlled()
 FIXTURE_STATE.parent.mkdir(parents=True,exist_ok=True)
 with (FIXTURE_STATE.parent/'fixture-server.log').open('ab') as log:
  process=subprocess.Popen([str(PYTHON),'-X','utf8',str(ROOT/'tests/serve_phase3.py')],cwd=ROOT,env=environment(),
                          stdout=log,stderr=log,stdin=subprocess.DEVNULL,start_new_session=True)
 identity=subprocess.run(['ps','-p',str(process.pid),'-o','lstart=,args='],capture_output=True,text=True).stdout.strip()
 FIXTURE_STATE.write_text(json.dumps({'pid':process.pid,'identity':identity,'port':8895},indent=2))
 for _ in range(100):
  if process.poll() is not None:raise SystemExit('受控服务启动失败，查看fixture-server.log')
  try:
   with httpx.Client(trust_env=False,timeout=1) as c:
    if c.get('http://[::1]:8895/api/health').status_code==200:return
  except httpx.HTTPError:pass
  time.sleep(.3)
 raise SystemExit('受控服务仍未启动，请查看fixture-server.log')


def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['doctor','build','serve-real','start-real','stop','start-fixture','stop-fixture','test','accept','accept-real','journey','verify-handoff','verify','report'])
 p.add_argument('--port',type=int,default=8894);p.add_argument('--cases',default='');p.add_argument('--rounds',type=int,default=2);p.add_argument('--suite',default='phase4');p.add_argument('--effort',choices=['xhigh','max'])
 p.add_argument('--request',type=Path,help='journey单次真实UI操作的JSON文件')
 p.add_argument('--original',action='store_true',help='journey使用原始TXT案例目录，而非普通客户目录')
 args=p.parse_args()
 if args.effort:os.environ['REQUIREMENTS_STUDIO_EFFORT']=args.effort
 sys.path.insert(0,str(PROJECT));out=ROOT/'.runtime/phase3';out.mkdir(exist_ok=True)
 if args.action=='journey':
  if args.request is None:p.error('journey需要--request；每次只执行文件中明确的一项操作。')
  try:request=json.loads(args.request.read_text(encoding='utf-8'))
  except (OSError,ValueError) as exc:p.error('无法读取单次UI操作JSON：'+str(exc))
  if not isinstance(request,dict):p.error('单次UI操作必须是JSON对象。')
  command=['node',str(ROOT/'tests/customer_journey.mjs'),'--base',f'http://[::1]:{args.port}',
           '--suite',args.suite,'--original','1' if args.original else '0']
  with test_browser():
   subprocess.run(command,cwd=ROOT,input=json.dumps(request,ensure_ascii=False)+'\n',
                  text=True,encoding='utf-8',check=True)
  return
 if args.action=='doctor':
  from requirements_studio.backend.knowledge import catalog
  status=catalog.status(True);print(json.dumps(status,ensure_ascii=False,indent=2));return
 if args.action=='build':
  run([PYTHON,ROOT/'scripts/build_domain_resources.py'])
  run(['npm','run','build','--','--outDir','../.runtime/phase3-dist'],cwd=ROOT/'frontend');return
 if args.action in ('start-real','stop'):server(args.action,args.port);return
 if args.action in ('start-fixture','stop-fixture'):fixture_server('stop' if args.action=='stop-fixture' else 'start');return
 if args.action=='serve-real':
  run([PYTHON,'-m','uvicorn','backend.app:app','--host','::1','--port',str(args.port)],cwd=ROOT,env=environment());return
 if args.action in ('test','verify'):
  unit_tests()
  if args.action=='test':return
 if args.action in ('accept','verify'):
  fixture_server('stop')
  fixture_server('start')
  try:
   with test_browser():
    run(['node',ROOT/'tests/phase3_browser.mjs'],cwd=ROOT)
    run(['node',ROOT/'tests/phase3_races.mjs'],cwd=ROOT)
    run([PYTHON,'-X','utf8',ROOT/'tests/phase3_load.py'],cwd=ROOT)
    run([PYTHON,'-X','utf8',ROOT/'tests/phase3_sources.py'],cwd=ROOT)
  finally:fixture_server('stop')
  if args.action=='accept':return
 if args.action=='accept-real':
  run([PYTHON,'-X','utf8',ROOT/'scripts/build_case_manifest.py'],cwd=ROOT)
  with test_browser():
   run(['node',ROOT/'tests/phase4_real.mjs','--base',f'http://[::1]:{args.port}','--cases',args.cases,'--rounds',str(args.rounds),'--suite',args.suite,'--briefing','1'],cwd=ROOT)
  return
 if args.action in ('verify-handoff','report','verify'):
  command=[PYTHON,'-X','utf8',ROOT/'tests/summarize_phase34.py']
  if args.action=='verify':command.append('--require-all')
  run(command,cwd=ROOT)


if __name__=='__main__':main()
