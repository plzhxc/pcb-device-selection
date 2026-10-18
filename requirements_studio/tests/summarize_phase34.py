"""Recompute acceptance from retained outputs; never turn a ready badge into PASS."""
import argparse
from datetime import datetime,timezone
import hashlib
import gzip
import io
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from check_case import check_case
from backend.handoff import verify_archive,generation_adapter,build_spec
from backend.model_context import release_hash


def read(path):
    raw=gzip.decompress(path.read_bytes()).decode('utf-8') if path.suffix=='.gz' else path.read_text(encoding='utf-8')
    return json.loads(raw)


def real_receipts(project):
    receipts=[]
    for run in project.get('run_activity',[]):
        if run.get('kind')=='knowledge':continue
        if run['status']!='completed':continue
        folder=next((root/run['id'] for root in (ROOT/'.runtime/runs',ROOT/'.runtime/phase3/live/runs') if (root/run['id']/'receipt.json').exists()),None)
        if folder is None:
            receipts.append({'run_id':run['id'],'real_inference':False,'error':'完成任务缺少模型凭据','backend_build':None})
            continue
        if (folder/'repair-1/receipt.json').exists():folder=folder/'repair-1'
        receipt=read(folder/'receipt.json')
        usage=[e.get('usage',{}) for e in receipt['events'] if e['type']=='turn.completed']
        actual=receipt.get('exit_code')==0 and receipt.get('model')=='gpt-6-luna' and any(u.get('input_tokens',0)>0 and u.get('output_tokens',0)>0 for u in usage)
        actual=actual and not (folder/'test-provider.json').exists() and not any(e.get('item_type') in {'command_execution','file_change','mcp_tool_call','web_search'} for e in receipt['events'])
        receipts.append({'run_id':run['id'],'real_inference':actual,'model':receipt.get('model'),'effort':receipt.get('effort'),
          'fast_requested':receipt.get('service_tier_requested')=='fast','backend_build':receipt.get('backend_build'),
          'network_reconnects':receipt.get('network_reconnects_used',0),
          'elapsed_seconds':receipt.get('elapsed_seconds'),'usage':usage[-1] if usage else {},'repair':folder.name=='repair-1'})
    return receipts

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--require-all',action='store_true');args=parser.parse_args()
    catalog={c['id']:c for c in read(ROOT/'tests/phase4_case_catalog.json')}
    runs=[];corrupt=[]
    for path in sorted((ROOT/'.runtime/phase4').glob('*/index.json')):
        try:index=read(path)
        except Exception as exc:corrupt.append({'path':str(path),'error':type(exc).__name__});continue
        for entry in index['cases']:
            snapshot=path.parent/(entry['key']+'.json')
            if not snapshot.exists():snapshot=snapshot.with_suffix('.json.gz')
            if not snapshot.exists():continue
            try:
                project=read(snapshot);case=catalog[entry['case_id']]
                decisions=path.parent/(entry.get('key',entry['case_id'])+'-customer-decisions.json')
                roles=path.parent/(entry.get('key',entry['case_id'])+'-customer-roles.json')
                result=check_case(entry['case_id'],project,intent_decisions=read(decisions) if decisions.exists() else None,
                                  role_decisions=read(roles) if roles.exists() else None)
                users=[m for m in project['messages'] if m['role']=='user']
                exact=bool(users and users[0]['content']==case['prompt'])
                if not exact:result['failures'].append('首条用户消息不是原始提示词')
                current_hash=hashlib.sha256(Path(case['source']).read_bytes()).hexdigest()
                if current_hash!=case['sha256']:result['failures'].append('原TXT文件与登记hash不符')
                archive=path.parent/(entry['key']+'-handoff.zip');archive_result=None;adapter_result=None
                if project['review']['status']=='approved' and archive.exists():
                    archive_result=verify_archive(archive.read_bytes())
                    spec=build_spec(project)
                    with zipfile.ZipFile(io.BytesIO(archive.read_bytes())) as bundle:
                        archived_spec=json.loads(bundle.read('requirement-spec.json'))
                        if archived_spec!=spec:result['failures'].append('交接包RequirementSpec与保存的同版项目不一致')
                        if bundle.read('requirements.md').decode('utf-8')!=project['markdown']:result['failures'].append('交接说明书与同版页面正文不一致')
                    adapter=generation_adapter(spec)
                    adapter_result={k:v for k,v in adapter.items() if k!='generation_input'}
                    if not adapter['accepted']:result['failures'].append('下游输入适配未接收')
                elif entry.get('status')=='approved':result['failures'].append('审核或交接包缺失')
                receipts=real_receipts(project)
                if not receipts or not all(r['real_inference'] for r in receipts):result['failures'].append('真实模型完成凭据不足或混入受控provider')
                if entry.get('browser_errors'):result['failures'].append('该轮界面仍有浏览器运行异常')
                result['ok']=not result['failures'] and entry.get('status')=='approved' and archive_result is not None
                # The first four manual UI journeys predate input_strategy.
                # Classify their retained actions without rewriting old records.
                focused_ui=entry.get('input_strategy')=='exact_txt_then_focused_customer_answers'
                legacy_ui=index['suite'].startswith('client-original') and index.get('input_mode')=='original_txt'
                legacy_ui=legacy_ui and any(a.get('kind')=='question_answers' for a in entry.get('actions',[]))
                legacy_ui=legacy_ui and all(a.get('controls_closed') and 1<=len(a.get('answers',{}))<=3
                    for a in entry.get('actions',[]) if a.get('kind')=='question_answers')
                row={**result,'suite':index['suite'],'round':entry['round'],'status':entry['status'],
                     'title':entry['title'],'first_input_exact':exact,'user_turns':len(users),
                     'customer_journey':(focused_ui or legacy_ui)
                       and any(action.get('kind')=='explicit_ui_approval' and action.get('review_note')
                               for action in entry.get('actions',[])),
                     'customer_journey_evidence':'explicit_strategy' if focused_ui else 'retained_legacy_ui_actions' if legacy_ui else None,
                     'professional_review_recorded':(path.parent/(entry['key']+'-professional-review.json')).exists(),
                     'backend_build':entry.get('backend_build') or index.get('health',{}).get('backend_build'),
                     'model_profile':sorted({str(r.get('model'))+'/'+str(r.get('effort'))+('/fast requested' if r.get('fast_requested') else '') for r in receipts}),
                     'browser_asset':entry.get('browser_asset'),'browser_errors':entry.get('browser_errors',[]),
                     'persona_sha256':entry.get('persona_sha256'),'oracle_sha256':entry.get('oracle_sha256'),
                     # This report has just rerun the current checker above.
                     # Preserve the initial driver fingerprint separately.
                     'checker_sha256':hashlib.sha256((ROOT/'tests/check_case.py').read_bytes()).hexdigest(),
                     'initial_checker_sha256':entry.get('checker_sha256'),
                     'oracle_file_sha256':entry.get('oracle_file_sha256'),
                     'failed_analysis_runs':sum(r.get('kind')!='knowledge' and r['status'] in ('failed','interrupted','cancelled') for r in project.get('run_activity',[])),
                     'receipts':receipts,'last_backend_build':receipts[-1]['backend_build'] if receipts else None,
                     'project_url':entry.get('service_base',index['base'])+'/?project='+project['id'],
                     'snapshot':str(snapshot.relative_to(ROOT)),'archive':archive_result,'adapter':adapter_result}
                runs.append(row)
            except Exception as exc:corrupt.append({'path':str(snapshot),'error':type(exc).__name__+': '+str(exc)[:400]})
    rank=lambda r:(bool(r.get('customer_journey')),r['last_backend_build']==release_hash(),r['backend_build']==release_hash(),r['round'])
    best={}
    for row in runs:
        if row['ok']:
            previous=best.get(row['case_id'])
            if previous is None or rank(row)>=rank(previous):best[row['case_id']]=row
    missing=sorted(set(catalog)-set(best))
    customer_best={}
    for row in runs:
        if row['ok'] and row['customer_journey']:
            previous=customer_best.get(row['case_id'])
            if previous is None or rank(row)>=rank(previous):customer_best[row['case_id']]=row
    customer_missing=sorted(set(catalog)-set(customer_best))
    historical_repeat=[r for r in runs if r['suite'].startswith('final-repeat') and r['backend_build']!=release_hash()]
    repeat=[r for r in runs if r['suite'].startswith('final-repeat') and r['backend_build']==release_hash()]
    repeat_counts={cid:sum(r['case_id']==cid for r in repeat) for cid in {r['case_id'] for r in repeat}}
    repeat_ok=len(repeat)>=4 and len(repeat_counts)>=2 and all(n>=2 for n in repeat_counts.values()) and all(r['ok'] and not r['browser_errors'] for r in repeat)
    repeat_ok=repeat_ok and {r['last_backend_build'] for r in repeat}=={release_hash()} and len({r['browser_asset'] for r in repeat})==1
    repeat_ok=repeat_ok and all(len({r['persona_sha256'] for r in repeat if r['case_id']==cid})==1 for cid in repeat_counts)
    personas=read(ROOT/'tests/phase4_profiles.json')
    persona_hash=lambda cid:hashlib.sha256(json.dumps(personas[cid],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    repeat_ok=repeat_ok and all(r['persona_sha256']==persona_hash(r['case_id']) for r in repeat)
    oracle_rules=read(ROOT/'tests/phase4_oracles.json')
    rule_hash=lambda cid:hashlib.sha256(json.dumps(oracle_rules[cid],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    repeat_ok=repeat_ok and all(r['oracle_sha256']==rule_hash(r['case_id']) for r in repeat)
    checker_hash=hashlib.sha256((ROOT/'tests/check_case.py').read_bytes()).hexdigest()
    repeat_ok=repeat_ok and {r['checker_sha256'] for r in repeat}=={checker_hash}
    repeat_ok=repeat_ok and all({x['backend_build'] for x in r['receipts']}=={release_hash()} for r in repeat)
    repeat_ok=repeat_ok and all(r['backend_build']==release_hash() and r['failed_analysis_runs']==0 for r in repeat)
    browser_path=ROOT/'.runtime/phase3/browser/report.json';load_path=ROOT/'.runtime/phase3/browser/load.json'
    browser=read(browser_path) if browser_path.exists() else None;load=read(load_path) if load_path.exists() else None
    sources_path=ROOT/'.runtime/phase3/sources.json';races_path=ROOT/'.runtime/phase3/browser/races.json'
    sources=read(sources_path) if sources_path.exists() else None;races=read(races_path) if races_path.exists() else None
    controlled_current=bool(browser and all(r.get('backend_build')==release_hash() for r in browser.get('rounds',[])))
    controlled_current=controlled_current and bool(races and all(r.get('backend_build')==release_hash() for r in races.get('rounds',[]))) and bool(sources and sources.get('backend_build')==release_hash())
    unit_path=ROOT/'.runtime/phase3/unit-result.json'
    unit=read(unit_path) if unit_path.exists() else None
    unit_current=bool(unit and unit.get('ok') and unit.get('backend_build')==release_hash())
    production={'available':False,'retained_projects':0}
    try:
        import httpx
        with httpx.Client(trust_env=False,timeout=4) as client:
            health=client.get('http://[::1]:8878/api/health').raise_for_status().json()
            visible={p['id'] for p in client.get('http://[::1]:8878/api/projects').raise_for_status().json()}
            mismatched=[];matched=0
            for cid,row in best.items():
                if row['project_id'] not in visible:continue
                current=client.get('http://[::1]:8878/api/projects/'+row['project_id']).raise_for_status().json()
                snapshot=read(ROOT/row['snapshot'])
                if (current['document']==snapshot['document'] and current['markdown']==snapshot['markdown']
                    and build_spec(current)==build_spec(snapshot) and current['review']['status']=='approved'):
                    matched+=1
                else:mismatched.append(cid)
        production={'available':health.get('version')=='0.3.0' and health.get('backend_build')==release_hash(),
                    'backend_build':health.get('backend_build'),'retained_projects':sum(r['project_id'] in visible for r in best.values()),
                    'matching_current_documents':matched,'mismatched_cases':mismatched}
        for row in runs:
            if row['project_id'] in visible:row['project_url']='http://[::1]:8878/?project='+row['project_id']
    except Exception as exc:production['error']=type(exc).__name__
    final_corrupt=[c for c in corrupt if '/final-' in c['path']]
    report={'generated_at':datetime.now(timezone.utc).isoformat(),'real_inference':True,'total_cases':len(catalog),
            'passed_cases':len(best),'missing_cases':missing,'fixed_repeat_ok':repeat_ok,
            'fixed_repeat_completion':{'journeys':len(repeat),'counts_by_case':repeat_counts,
              'approved_and_checked_documents':sum(bool(r['ok']) for r in repeat),
              'retained_failed_analysis_runs':sum(r['failed_analysis_runs'] for r in repeat),
              'rule':'Document completion and the unchanged failure-free gate are reported separately. Retried failures remain visible and do not pass the failure-free gate.'},
            'customer_centered_acceptance':{'passed_cases':len(customer_best),'total_cases':len(catalog),
              'missing_cases':customer_missing,'passed_ids':sorted(customer_best),
              'rule':'Exact original TXT, then focused real UI answers, explicit human review, same-version export and source audit. Professional supplements are separately labelled.'},
            'unit_tests':unit,'unit_current_build':unit_current,'controlled_browser':browser,'controlled_load':load,'controlled_version_races':races,'real_sources':sources,
            'production':production,'runs':runs,'historical_invalid_artifacts':corrupt,'final_invalid_artifacts':final_corrupt,
            'fixed_repeat_build':release_hash(),'historical_fixed_repeat_runs':historical_repeat,
            'controlled_current_build':controlled_current,
            'complete':not missing and not customer_missing and repeat_ok and unit_current and controlled_current and bool(browser and browser.get('ok')) and bool(load and load.get('ok')) and bool(races and races.get('ok')) and bool(sources and sources.get('ok')) and not final_corrupt and production['available'] and production['retained_projects']==len(catalog) and production.get('matching_current_documents')==len(catalog)}
    out=ROOT/'.runtime/phase34-report.json';out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    lines=['# 第三、四阶段案例验收索引','',
           f'本轮客户逐轮问答验收：{len(customer_best)}/{len(catalog)}；固定复测无故障门槛：'+('通过' if repeat_ok else '未通过'),'',
           f'历史及当前版本合计真实案例通过：{len(best)}/{len(catalog)}。历史结果不代替本轮客户视角验收。',
           '本轮保留原始TXT作为首次输入，后续通过真实UI逐项问答；专业审阅补充单独注明，不视为普通客户自带知识或平台自动发现。','']
    lines+=['| 案例 | 当前结果 | 用户回合 | 工作台完整过程 |','| --- | --- | --- | --- |']
    for cid,case in catalog.items():
        item=customer_best.get(cid) or next((r for r in reversed(runs) if r['case_id']==cid and r['suite'].startswith(('client-original','final-repeat-release11'))),None)
        title=re.sub(r'\[(?:已验证|未验证)\]|【(?:已验证|未验证)】','',case['title']).strip()
        lines.append(f'| {cid} · {title} | '+('通过' if cid in customer_best else '本轮待完成/待修正')+f' | {item["user_turns"] if item else "—"} | '+(f'[打开项目]({item["project_url"]})' if item else '—')+' |')
    if repeat:
        lines+=['','## 当前版本的固定案例复测','',
                '每轮分别核对引导过程与最终文档的合理性，不要求同一案例输出一致。',
                f'当前共{len(repeat)}轮，{sum(bool(r["ok"]) for r in repeat)}份最终文档通过；保留{sum(r["failed_analysis_runs"] for r in repeat)}个失败分析任务。重试恢复不抹除失败，也不改变原无故障门槛。','',
                '| 案例 | 轮次 | 当前结果 | 用户回合 | 完整过程 |','| --- | --- | --- | --- | --- |']
        for item in sorted(repeat,key=lambda row:(row['case_id'],row['round'])):
            lines.append(f'| {item["case_id"]} | R{item["round"]} | '+('通过' if item['ok'] else '待完成/待修正')+f' | {item["user_turns"]} | [打开项目]({item["project_url"]}) |')
    lines+=['','受控provider仅用于故障、并发与交互验证；不计入上述真实AI通过数。开发阶段失败与修复记录按原样保留。']
    (ROOT.parent/'docs/需求分析工作台_第三四阶段案例验收索引.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('total_cases','passed_cases','missing_cases','fixed_repeat_ok','complete')},ensure_ascii=False,indent=2))
    if args.require_all and not report['complete']:raise SystemExit(1)

if __name__=='__main__':main()
