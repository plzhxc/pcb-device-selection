"""Require complete final two-round evidence; historical partial reports cannot pass."""
import hashlib
import json
import re
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1];PHASE=ROOT/'.runtime/phase2';OUT=PHASE/'verification'


def read(name):
    data=json.loads((OUT/name).read_text(encoding='utf-8'))
    assert data.get('ok') is True,f'{name}: not complete'
    return data


def main():
    modules=json.loads((ROOT/'backend/resources/module_catalog.json').read_text(encoding='utf-8'))
    source=ROOT.parent/modules['source_file']
    assert hashlib.sha256(source.read_bytes()).hexdigest()==modules['source_sha256']
    assert (len(modules['categories']),len(modules['parents']),len(modules['modules']))==(10,60,197)
    assert sum(len(m['parameters']) for m in modules['modules'])==1260
    assert sum(len(g['parameters']) for g in modules['parameter_groups'].values())==82
    units=[]
    for n in (1,2):
        log=(PHASE/f'unit-round-{n}.log').read_text(encoding='utf-8')
        count=int(re.search(r'Ran (\d+) tests',log)[1]);assert count>=84 and log.rstrip().endswith('OK')
        units.append(count)
    fixtures=json.loads((OUT/'fixtures.json').read_text(encoding='utf-8'))['rounds']
    html=(ROOT/'.runtime/phase2-dist/index.html').read_text(encoding='utf-8')
    script=re.search(r'src="(/assets/[^\"]+\.js)"',html)[1]
    browser=read('browser.json');assert len(browser['rounds'])==2
    for result,fixture in zip(browser['rounds'],fixtures):
        assert result['seed']==fixture['seed'] and result['project_ids']['progress']==fixture['progress']
        assert len(result['checks'])>=55 and result['script_asset']==script and not result['console_errors']
        assert result['provider']=='controlled_phase2_fixture' and result['real_inference'] is False
    load=read('load.json');assert len(load['rounds'])==2
    for row in load['rounds']:
        assert len(row['checks'])>=10 and row['accepted']==20 and row['backpressure']==12
        assert row['read_requests']==180 and not row['http_500']
    parents=read('parent-categories.json');assert len(parents['rounds'])==2
    for row in parents['rounds']:assert len(row['checks'])==8 and not row['console_errors'] and not row['mutations']
    real=read('real.json');assert len(real['rounds'])==2
    for row in real['rounds']:
        assert row['provider']=='real_codex_cli' and row['fields_closed']==247 and len(row['modules'])>=3
        assert len(row['checks'])>=12 and not row['console_errors']
        saved=json.loads((OUT/f'real-approved-{row["round"]}.json').read_text(encoding='utf-8'))
        assert saved['review']['status']=='approved' and saved['review']['closed']==50
        assert not saved['convergence']['issues'] and not saved['convergence']['module_gaps'] and not saved['document']['questions']
    sources={}
    for folder in ['backend','frontend/src','scripts']:
        for file in sorted((ROOT/folder).rglob('*')):
            if file.is_file() and file.suffix in ('.py','.ts','.tsx','.css','.json','.sh'):
                sources[str(file.relative_to(ROOT))]=hashlib.sha256(file.read_bytes()).hexdigest()
    result={'ok':True,'created_at':datetime.now(timezone.utc).isoformat(),'catalog':{'categories':10,'parents':60,'submodules':197,'specific_parameters':1260,'common_parameters':82,'source_sha256':modules['source_sha256']},
            'core_tests_per_round':units,'random_browser_checks':[len(r['checks']) for r in browser['rounds']],
            'parent_checks':[len(r['checks']) for r in parents['rounds']],'load_checks':[len(r['checks']) for r in load['rounds']],
            'load_read_p95_ms':[r['read_p95_ms'] for r in load['rounds']],
            'real_checks':[len(r['checks']) for r in real['rounds']],'real_user_turns':[r['user_turns'] for r in real['rounds']],
            'real_fields_closed':[247,247],'script_asset':script,'source_sha256':sources,
            'scope':'独立需求分析与规范类别方案；用户确认的设计交接不等于器件或硬件验证。'}
    # Development acceptance remains independent of a local production release.
    # When release evidence exists, it must describe this exact verified build.
    if (OUT/'production-release.json').is_file():
        release=read('production-release.json');production=read('production-ui.json')
        baseline=json.loads((OUT/'production-before.json').read_text(encoding='utf-8'))
        assert len(production['rounds'])==2 and release['table_sha256']==baseline['table_sha256']
        assert release['published_index_sha256']==release['verified_index_sha256']==hashlib.sha256((ROOT/'.runtime/phase2-dist/index.html').read_bytes()).hexdigest()
        for name,digest in release['backend_sha256'].items():
            assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest
        for row in production['rounds']:
            assert row['script_asset']==script and len(row['checks'])>=17 and not row['console_errors'] and not row['mutations']
        result['production_checks']=[len(r['checks']) for r in production['rounds']]
        result['production_projects_preserved']=release['projects_preserved']
        result['production_url']=release['url']
    (OUT/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='source_sha256'},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
