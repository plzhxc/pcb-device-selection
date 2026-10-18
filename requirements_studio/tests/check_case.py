"""Independent case obligations; gate signals alone are not semantic acceptance."""
import argparse
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.module_catalog import TYPES
from backend.evidence import literal_span

RULES=json.loads((ROOT/'tests/phase4_oracles.json').read_text(encoding='utf-8'))

def check_case(case_id,project,*,intent_decisions=None,role_decisions=None):
    rule=RULES[case_id];doc=project['document'];failures=[]
    reqs=[r for r in doc['requirements'] if r['state']!='not_applicable']
    refs=[r for r in doc.get('device_references',[]) if r.get('lifecycle')=='active']
    modules=[m for m in doc.get('modules',[]) if m['state']!='not_applicable']
    # Source quotes are deliberately excluded: retaining the whole prompt in one
    # quote must not hide lost facts in the actual requirements.
    semantic='\n'.join([doc.get('summary',''),*[r['name']+' '+r['value']+' '+r.get('unit','') for r in reqs],
                        *[m['name']+' '+m['purpose']+' '+m['boundary'] for m in modules],
                        *[r['mention']+' '+r['purpose'] for r in refs]])
    models={r['mention'].casefold() for r in refs}
    users={m['id']:m['content'] for m in project['messages'] if m['role']=='user'}
    reviewed_roles=[]
    # "Backup" was a legacy label for the second power input. A customer's
    # later preference for DC must not remove either physical input, nor force
    # the customer back to that label. This is deliberately not a general fact
    # waiver: only this role, its real quote, and both retained inputs qualify.
    for role,decision in (role_decisions or {}).items():
        quote=decision.get('source_quote','') if isinstance(decision,dict) else ''
        source=decision.get('source_message_id','') if isinstance(decision,dict) else ''
        valid=bool(case_id=='CASE-12' and role=='dc_input_role' and isinstance(decision,dict)
                   and decision.get('decision')=='preferred_with_pd_retained'
                   and len(decision.get('review_basis',''))>=20 and quote
                   and literal_span(quote,users.get(source,''))
                   and all(word in quote for word in ('优先','DC','USB-C','PD','保留')))
        dc=[r for r in reqs if re.search(r'DC|直流',r['name'],re.I)
            and re.search(r'DC\s*5\.5\s*[×xX*]\s*2\.1',r['value']) and '输入' in r['value']]
        pd=[r for r in reqs if re.search(r'USB[ -]*C.*PD',r['name'],re.I)
            and re.search(r'协商|供电',r['value'])]
        chosen=any(r.get('source_message_id')==source and '优先' in r['value']
                   and literal_span(quote,r.get('source_quote','')) for r in dc)
        if not (valid and dc and pd and chosen):
            failures.append('供电角色验收口径缺少真实客户决定、人工说明或两种输入：'+role)
        else:reviewed_roles.append(role)
    expected_intents=dict(rule.get('intents',{}))
    # Legacy scenarios had prewritten persona choices. A real customer's later
    # explicit choice may differ. Require its actual saved quote and a human
    # review explanation; all original functional obligations remain unchanged.
    for model,decision in (intent_decisions or {}).items():
        if (model not in expected_intents or not isinstance(decision,dict)
                or decision.get('intent') not in ('must','prefer','reference','undecided')
                or len(decision.get('review_basis',''))<20
                or not decision.get('source_quote')
                or not literal_span(decision['source_quote'],users.get(decision.get('source_message_id'),''))):
            failures.append('器件意图验收口径缺少真实客户决定和人工说明：'+model)
        else:
            expected_intents[model]=decision['intent']
    for model in rule['models']:
        if model.casefold() not in models:failures.append('缺少独立器件引用：'+model)
    for model,intent in expected_intents.items():
        found=next((r for r in refs if r['mention'].casefold()==model.casefold()),None)
        if found and found['intent']!=intent:failures.append(f'最新器件意图不符：{model}应为{intent}，实际{found["intent"]}')
    for pattern in rule['facts']:
        if not re.search(pattern,semantic,re.I):
            if pattern=='备用' and 'dc_input_role' in reviewed_roles:continue
            failures.append('实际需求缺少目标事实：'+pattern)
    for model,quantity in rule.get('reference_quantities',{}).items():
        rows=[r for r in refs if r['mention'].casefold()==model.casefold()]
        if not rows or rows[0]['quantity']!=quantity:failures.append(f'器件明确数量未保留：{model} × {quantity}')
    approx=rule['approx_bom']
    if not any(str(approx) in r['value'] and re.search(r'约|左右|近似|估算|参考',r['value']) for r in reqs):
        failures.append(f'近似整板元件规模未准确保留：约{approx}')
    for group,rows in [('需求',reqs),('器件引用',refs)]:
        for row in rows:
            if group=='需求' and row['state']!='stated':continue
            text=users.get(row.get('source_message_id'),'')
            quote=row.get('source_quote','')
            if not quote or not literal_span(quote,text):failures.append(f'{group}{row["id"]}无真实用户逐字依据')
    for row in modules:
        if row['type_id'] not in TYPES:failures.append('模块类别不在规范：'+row['id'])
        if row['type_id'].startswith(('P_8_1','M_8_1')) and re.search(r'LED.*(?:状态|工作).*指示|(?:状态|工作).*指示.*LED',row['name'],re.I) and not re.search(r'数码管|点阵|电量|条形',row['name']):
            failures.append('普通状态LED误归数码管或点阵类别，应保留功能并由实际主控/供电/接口模块承接：'+row['id'])
    if not doc['flow']:failures.append('缺少工作流程')
    if project['review']['status'] not in ('ready','approved'):failures.append('尚未达到文档审阅门槛')
    if doc['questions']:failures.append('仍有未处理产品问题')
    if project['domain']['blockers']:failures.append('仍有明确工程冲突未处理')
    if project['input_queue'] or project['domain']['active_jobs'] or (project.get('latest_run') or {}).get('status') in ('queued','running'):
        failures.append('仍有后台任务，不能用中间状态验收')
    if project.get('analysis_pending') or (project.get('latest_run') or {}).get('status') in ('failed','interrupted','cancelled'):
        failures.append('最近一轮未成功完成需求整理，不能用先前版本代替最新输入的验收')
    return {'ok':not failures,'case_id':case_id,'project_id':project['id'],'version':doc['version'],
            'requirements':len(reqs),'modules':len(modules),'references':len(refs),
            'reviewed_customer_roles':reviewed_roles,'failures':failures}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('case_id');parser.add_argument('project_json');args=parser.parse_args()
    result=check_case(args.case_id,json.loads(Path(args.project_json).read_text(encoding='utf-8')))
    print(json.dumps(result,ensure_ascii=False,indent=2));sys.exit(0 if result['ok'] else 1)
