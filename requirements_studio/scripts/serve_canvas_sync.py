"""Isolated real-CLI service for device/canvas synchronization acceptance."""
import os,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
RUNTIME=ROOT/'.runtime/canvas-sync-20261020'
sys.path.insert(0,str(ROOT))
os.environ['REQUIREMENTS_STUDIO_DATA']=str(RUNTIME/'live')
os.environ['REQUIREMENTS_STUDIO_DIST']=str(RUNTIME/'dist')
os.environ.setdefault('REQUIREMENTS_STUDIO_EFFORT','xhigh')
os.environ.setdefault('REQUIREMENTS_STUDIO_FAST','1')
if __name__=='__main__':
 import socket
 with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as probe:
  probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
  probe.bind(('::',8896))
 RUNTIME.mkdir(parents=True,exist_ok=True)
 (RUNTIME/'service.json').write_text(json.dumps({'pid':os.getpid(),'script':str(Path(__file__).resolve()),'port':8896},indent=2))
 import uvicorn
 from backend.app import app
 uvicorn.run(app,host='::',port=8896,log_level='warning')
