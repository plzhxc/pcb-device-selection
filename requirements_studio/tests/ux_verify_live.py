"""Read-only verification/export of the two UI-operated, real Codex conversations."""
import hashlib
import json
import sys
from pathlib import Path

import httpx

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.module_catalog import TYPES

out=ROOT/'.runtime/ux/verification';cases=json.loads((out/'live-cases.json').read_text(encoding='utf-8'))
report={'ok':False,'provider':'real_codex_cli','model':'gpt-6-luna','effort':'xhigh','cases':[]}
transcript=['# 正式UI真实使用完整记录','','所有用户输入由助手模拟，AI回应来自真实Codex CLI。失败、重试和内部续整理均保留。','']
with httpx.Client(trust_env=False,timeout=20) as client:
    health=client.get('http://[::1]:8878/api/health').json()
    assert health['ok'] and health['model']=='gpt-6-luna' and health['effort']=='xhigh'
    for key in ('A','B'):
        p=client.get('http://[::1]:8878/api/projects/'+cases[key]['id']).json();doc=p['document'];c=p['convergence'];checks=[]
        def check(name,value):
            assert value,(key,name)
            checks.append(name)
        check('explicit UI approval reached',p['review']['status']=='approved' and any(a['action']=='approve' for a in cases[key]['actions']))
        check('no running call, queued input or unanswered question',p['latest_run']['status']=='completed' and not p['input_queue'] and not doc['questions'])
        check('no unsettled product requirements or consistency issues',not p['review']['unsettled_requirements'] and not c['issues'])
        check('every necessary hardware function has an accepted module',not c['module_gaps'] and not c['module_pending'])
        check('all four functional categories are real catalogue entries',len(c['modules'])==4 and all(m['type_id'] in TYPES and TYPES[m['type_id']]['name']==m['type_name'] and m['state'] in ('confirmed','design_stage') for m in c['modules']))
        check('workflow and actual module relationships are retained',bool(doc['flow']) and bool(doc['module_links']))
        source={m['id']:m['content'] for m in p['messages'] if m['role']=='user'}
        closed=[r for group in ('requirements','field_resolutions','modules') for r in doc.get(group,[]) if r.get('state') in ('stated','confirmed','design_stage','not_applicable')]
        check('all saved confirmed/delegated content retains a literal user source',all(r.get('source_message_id') in source and bool(r.get('source_quote')) and r['source_quote'].strip() in source[r['source_message_id']] for r in closed))
        check('unused catalogue templates do not appear in specification',not any(s in p['markdown'] for s in ('### 5.2.4.1','Wi-Fi 子模块','充电管理子模块','字段核对与来源')))
        run_receipts=[]
        for run in p['run_activity']:
            folder=ROOT/'.runtime/runs'/run['id'];file=folder/'receipt.json'
            check_marker=folder/'test-provider.json'
            assert not check_marker.exists(),'Controlled provider entered a real project'
            if file.is_file():
                receipt=json.loads(file.read_text(encoding='utf-8'))
                assert receipt['model']=='gpt-6-luna' and receipt['effort']=='xhigh'
                assert not any(e.get('item_type') in ('command_execution','file_change','mcp_tool_call','web_search') for e in receipt['events'])
                run_receipts.append({'id':run['id'],'status':run['status'],'exit_code':receipt['exit_code']})
        check('real CLI receipts cover all visible runs',len(run_receipts)==len(p['run_activity']))
        check('actual processing records retain timestamps',all(run['events'] and all(e.get('at') for e in run['events']) for run in p['run_activity']))
        values='\n'.join(r['value'] for r in doc['requirements'])
        if key=='A':
            check('agreed range, accuracy, resolution, sample batch and budget survive',all(s in values for s in ('50','±1','0.1','100','60','90','30','5V')))
            check('unspecified implementation limits are not marked user-confirmed',all(f['state']!='confirmed' for f in c['fields'] if f['id'] in ('V3-1.3:F04','V3-1.3:F06','V3-3.1.1:F01')))
        else:
            check('timer, pause and resume requirements survive',all(s in values for s in ('25','10','暂停','继续','25:00','闪烁')))
            check('cancelled buzzer is removed from modules and graph',not any('蜂鸣器' in m['type_name'] or '扬声器' in m['type_name'] for m in c['modules']) and not any(e['source']=='MOD-005' or e['target']=='MOD-005' for e in doc['module_links']) and 'MOD_005' not in p['markdown'])
            check('no invented hard cost limit',any('没有硬性' in r['value'] and '成本' in r['value'] for r in doc['requirements']))
        exported=client.get(f"http://[::1]:8878/api/projects/{p['id']}/export/md")
        check('download equals the current project specification',exported.status_code==200 and exported.text==p['markdown'] and exported.headers['X-Document-Version']==str(doc['version']))
        (out/f'live-{key}.json').write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
        (out/f'live-{key}.md').write_text(p['markdown'],encoding='utf-8')
        transcript+=['## '+p['title'],'',f"项目：{p['id']}；已审核v{doc['version']}。",'']
        for m in p['messages']:
            transcript+=['### '+('用户' if m['role']=='user' else '需求助手')+' · '+m['created_at'],'',m['content'],'']
            for run in p['run_activity']:
                if m['role']=='user' and run['message_id']==m['id']:
                    transcript+=[f"整理记录：{run['kind']} / {run['status']}（{run['id']}）",'']
                    transcript += [f"- {e.get('at','')}：{e['message']}" for e in run['events'] if e.get('message')]+['']
        report['cases'].append({'case':key,'id':p['id'],'title':p['title'],'version':doc['version'],'user_turns':len(source),'assistant_messages':sum(m['role']=='assistant' for m in p['messages']),'requirements':len(doc['requirements']),'product_decisions':c['product_review']['decision_closed'],'engineering_pending':c['product_review']['engineering_pending'],'modules':[{'name':m['name'],'type':m['type_name']} for m in c['modules']],'checks':checks,'runs':run_receipts})
    originals=json.loads((ROOT/'.runtime/ux/release/before-projects.json').read_text(encoding='utf-8'));unchanged=[]
    for old in originals:
        p=client.get('http://[::1]:8878/api/projects/'+old['id']).json()
        assert all(p[k]==old[k] for k in ('document','messages','revisions'))
        unchanged.append(p['id'])
    report['original_projects_unchanged']=unchanged
    manifest=json.loads((ROOT/'.runtime/ux/release-final/manifest.json').read_text(encoding='utf-8'))
    for name,expected in manifest['sha256'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==expected,name
    asset=manifest['asset'];expected=hashlib.sha256((ROOT/('.runtime/ux-dist'+asset)).read_bytes()).hexdigest()
    assert hashlib.sha256(client.get('http://[::1]:8878'+asset).content).hexdigest()==expected
    report['asset']=asset;report['ok']=True
(out/'live-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
(out/'live-transcript.md').write_text('\n'.join(transcript),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False,indent=2))
