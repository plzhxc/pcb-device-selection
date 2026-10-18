"""Verify full original-case coverage on the unchanged frozen release.

Run summarize_phase34.py first. This adds a stronger current-build coverage
check; it does not edit projects, source prompts, case rules, or model receipts.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.model_context import release_hash


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--require-all',action='store_true');args=parser.parse_args()
    read=lambda path:json.loads(path.read_text(encoding='utf-8'))
    digest=lambda path:hashlib.sha256(path.read_bytes()).hexdigest()
    freeze=read(ROOT/'.runtime/phase3/final-freeze.json')
    report=read(ROOT/'.runtime/phase34-report.json')
    catalog=read(ROOT/'tests/phase4_case_catalog.json')
    expected={case['id'] for case in catalog};build=release_hash()
    changed=[]
    if build!=freeze['backend_build']:changed.append('backend_build')
    for name,sha in {**freeze['frontend_files'],**freeze['acceptance_files']}.items():
        path=ROOT/name
        if not path.is_file() or digest(path)!=sha:changed.append(name)
    for case in catalog:
        if digest(Path(case['source']))!=case['sha256']:changed.append(case['id']+' original TXT')
    plan_path=ROOT/'.runtime/phase3/current-matrix-plan.json'
    if plan_path.exists():
        plan=read(plan_path)
        if plan['backend_build']!=build or plan['frozen_release_sha256']!=digest(ROOT/'.runtime/phase3/final-freeze.json'):
            changed.append('current-matrix-plan release')
        if plan.get('verifier_sha256')!=digest(Path(__file__)):
            changed.append('current-matrix-plan verifier')
    else:changed.append('current-matrix-plan missing')
    candidates=[r for r in report['runs'] if r['ok'] and r['first_input_exact']
                and r['backend_build']==build and r['last_backend_build']==build
                and r['receipts'] and all(x['real_inference'] and x['backend_build']==build for x in r['receipts'])]
    selected={}
    for row in candidates:
        previous=selected.get(row['case_id'])
        if previous is None or row['round']>=previous['round']:selected[row['case_id']]=row
    missing=sorted(expected-set(selected))
    live=report.get('production',{})
    ready=bool(report.get('complete') and live.get('available')
               and live.get('matching_current_documents')==len(expected))
    result={'generated_at':datetime.now(timezone.utc).isoformat(),'ok':not changed and not missing and ready,
            'backend_build':build,'unchanged_frozen_files':not changed,'changed_files':changed,
            'total_original_cases':len(expected),'current_build_passed_cases':len(selected),'missing_cases':missing,
            'current_build_approved_sessions':len(candidates),'fixed_repeat_ok':report.get('fixed_repeat_ok',False),
            'production_matches_all_cases':ready,
            'cases':[{k:r[k] for k in ('case_id','project_id','suite','round','user_turns','requirements','modules','references',
                         'failed_analysis_runs','project_url','model_profile','snapshot')} for _,r in sorted(selected.items())]}
    output=ROOT/'.runtime/phase3/current-matrix-result.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('ok','current_build_passed_cases','total_original_cases','current_build_approved_sessions','missing_cases','changed_files')},ensure_ascii=False,indent=2))
    if args.require_all and not result['ok']:raise SystemExit(1)


if __name__=='__main__':main()
