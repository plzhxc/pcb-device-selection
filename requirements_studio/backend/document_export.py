"""Render a project's requirements, rather than the complete reference template."""
import re

STATE={'stated':'用户已提出','suggested':'AI建议','tbd':'待明确','conflict':'存在冲突','not_applicable':'不纳入本版'}
MODULE_STATE={'proposed':'建议，待确认','confirmed':'已确认类别与用途','design_stage':'实现交给设计阶段','tbd':'待核对'}


def render_project_document(doc,domain=None):
    from .coverage import review_document
    from .convergence import view
    review=review_document(doc);phase=view(doc);scope=phase['product_review']
    status={'draft':'草稿','ready':'待审核','approved':'已审核'}[review['status']]
    if doc.get('analysis_pending'):status='草稿（最新补充尚未完成整理）'
    if domain and domain.get('blockers'):status='草稿（器件约束需复核）'
    cell=lambda v:str(v).replace('|','\\|').replace('\r','').replace('\n','<br>')
    label=lambda v:re.sub(r'["<>\[\]{}\n\r`]',' ',str(v))[:140]
    sources={}
    def source(row):
        key=(row.get('source_message_id'),row.get('source_quote',''))
        if not key[0] or not key[1]:return 'AI整理 / 待核对'
        if key not in sources:sources[key]=len(sources)+1
        return '来源'+str(sources[key])
    lines=[f"# {doc['title']} · 硬件需求说明书",'',f"版本：v{doc['version']}　|　状态：{status}",
           f"生成日期：{doc.get('created_at','未记录')}　|　更新时间：{doc['updated_at']}",'',
           '> 本文仅记录本项目的产品要求、实际模块和设计边界。工程实现待办不表示器件已经选定或电路已经验证。','',
           '## 设计摘要','',doc['summary'] or '正在根据产品想法整理。','']
    if doc.get('analysis_pending'):
        lines[6:6]=['> 最新用户补充尚未完成需求整理。下文保留先前已保存内容，请完成分析并重新整体审阅后正式交接。','']
    headings=[('1. 产品目标与场景',['overview']),('2. 功能与使用体验',['functions']),
              ('3. 性能、成本与边界',['performance','selection']),('4. 生产与验收',['manufacturing'])]
    def rows_table(rows):
        if not rows:return ['尚未记录独立条目；以已明确的产品范围为准。','']
        out=['| 需求 | 内容与验收边界 | 优先级 | 状态 | 依据 |','| --- | --- | --- | --- | --- |']
        for r in rows:
            content=r['value']+((' '+r['unit']) if r['unit'] and r['unit'] not in r['value'] else '')
            out.append('| '+' | '.join(cell(x) for x in [r['id']+' · '+r['name'],content or '待明确',{'must':'必须','should':'重要','could':'可选'}[r['priority']],STATE[r['state']],source(r)])+' |')
        return out+['']
    for title,categories in headings:
        rows=[r for r in doc['requirements'] if r['category'] in categories and r['state']!='not_applicable']
        if rows or categories==['functions']:
            lines += ['## '+title,'']+rows_table(rows)
    # A valid decision can exist in the field ledger without a duplicate REQ row.
    # Keep that actual project information while excluding unknown template fields.
    written='\n'.join(r['value'] for r in doc['requirements'] if r['state']!='not_applicable')
    decisions={}
    for f in scope['decision_fields']:
        conclusion=f.get('conclusion','').strip()
        if f['state']=='confirmed' and conclusion and conclusion not in written:
            decisions.setdefault(conclusion,f)
    if decisions:
        lines+=['## 补充产品边界','']+[f'- {text}（{source(row)}）' for text,row in decisions.items()]+['']
    lines+=['## 5. 系统与模块方案','']
    architecture=[r for r in doc['requirements'] if r['category']=='architecture' and r['state']!='not_applicable']
    if architecture:lines+=rows_table(architecture)
    modules=[m for m in phase['modules'] if m['state']!='not_applicable']
    if modules:
        lines+=['以下只列本版纳入的功能电路。类别和用途已确认时，芯片、具体拓扑和引脚仍可留待设计。','',
                '| 模块 | 规范类别 | 数量 | 在本产品中的用途与边界 | 状态 | 依据 |','| --- | --- | --- | --- | --- | --- |']
        for m in modules:
            lines+=['| '+' | '.join(cell(v) for v in [m['name'],m['category_name']+' / '+m['type_name'],m['quantity'],m['purpose']+'；'+m.get('boundary',''),MODULE_STATE[m['state']],source(m)])+' |']
        lines+=['','### 模块关系草案','','以下关系表达需求方案，不能替代真实引脚与电气连接核对。','','```mermaid','flowchart LR']
        ids={m['id'] for m in modules}
        for m in modules:lines.append(f'  {m["id"].replace("-","_")}["{label(m["name"])}"]')
        for edge in phase['module_links']:
            if edge['source'] not in ids or edge['target'] not in ids:continue
            kind={'power':'供电','data':'数据','control':'控制','clock':'时钟'}[edge['kind']]
            lines.append(f'  {edge["source"].replace("-","_")} -->|{kind}| {edge["target"].replace("-","_")}')
        lines+=['```','']
    else:lines+=['模块方案将根据已明确的产品功能逐步形成。','']
    excluded=[r for r in doc['requirements'] if r['state']=='not_applicable' and r.get('source_message_id')]
    if excluded:
        lines+=['## 本版范围边界','']+[f"- {r['name']}：{r['value']}（{source(r)}）" for r in excluded]+['']
    lines+=['## 工作流程草案','']
    lines += [f"{i}. {step['label']}" for i,step in enumerate(doc['flow'],1)] or ['工作流程待根据产品功能整理。']
    if doc['flow']:
        lines+=['','```mermaid','flowchart LR']
        for i,step in enumerate(doc['flow']):
            lines.append(f'  Step{i}["{label(step["label"])}"]')
            if i:lines.append(f'  Step{i-1} --> Step{i}')
        lines+=['```']
    lines+=['','## 设计交接与待核对内容','']
    selected=set(scope['module_topics'])
    professional={}
    product_ids={f['id'] for f in scope['decision_fields']}
    for f in phase['fields']:
        conclusion=f.get('conclusion','').strip()
        if f['id'] in product_ids or (f['chapter']>=5 and f['topic_id'] not in selected):continue
        if f['state']=='confirmed' and conclusion and conclusion not in written and conclusion not in decisions:
            professional.setdefault(conclusion,f)
    lines += [f'- 已明确的设计边界：{text}（{source(row)}）' for text,row in professional.items()]
    handoff={}
    for f in phase['handoff']:
        if (f['chapter']>=5 and f['topic_id'] not in selected) or not f.get('conclusion'):continue
        key=f['conclusion'];handoff.setdefault(key,[]).append(f['label'])
    lines += [f"- {conclusion}" for conclusion in handoff]
    if scope['engineering_pending']:
        lines+=['- 器件、连接、PCB与工艺等实现内容仍待设计核对；保留在专业视图，不要求用户逐项填写，也不将未知值认定为已确认参数。']
    if not handoff and not scope['engineering_pending']:lines+=['- 具体器件、引脚与电路验证在后续设计中完成。']
    lines+=['','## 待确认问题','']
    lines += [f"- {'[暂缓] ' if q.get('status')=='deferred' else ''}{q['question']}\n  - {q['reason']}" for q in doc['questions']] or ['当前没有待回答的问题。'+('本版需求已审核。' if status=='已审核' else '仍需整体审阅本版内容。')]
    if phase['issues']:
        lines+=['','## 需要处理的一致性问题','']+[f"- {i['title']}：{i['description']}" for i in phase['issues']]
    references=[r for r in (domain or {}).get('references',doc.get('device_references',[])) if r.get('lifecycle')=='active']
    if references:
        from .device_domain import INTENTS
        lines+=['','## 器件引用与选型边界','',
                '下表记录用户的使用意图与实际资料状态。目录命中、需求审核和最终选用分别记录。','',
                '| 引用 | 使用意图 | 身份与范围 | 本项目中的作用 | 资料状态 | 用户依据 |',
                '| --- | --- | --- | --- | --- | --- |']
        labels={'matched':'已定位目录身份','family':'系列约束，具体型号待选','ambiguous':'存在不同候选','not_found':'本次目录未找到','unavailable':'来源暂不可用','unresolved':'待检索'}
        for r in references:
            identity=r.get('identity') or {}
            lines+=['| '+' | '.join(cell(v) for v in [r['id']+' · '+r['mention'],INTENTS[r['intent']],
                      ' / '.join(v for v in [identity.get('model'),identity.get('manufacturer'),identity.get('supplier_part')] if v) or {'family':'系列','assembly':'组件','exact_part':'指定型号'}.get(r['identity_scope'],'待核对'),
                      r.get('purpose',''),labels.get(r.get('resolution_status','unresolved'),'待核对')+'；手册：'+{'available':'已有片段','missing':'缺资料','unavailable':'暂不可用','not_checked':'待核对'}.get(r.get('document_status','not_checked'),'待核对'),source(r)])+' |']
        lines+=['','具体器件数量按明确的数量要求处理。功能模块数量不直接等于芯片数量，未明确的用量保留给设计阶段。','']
    if domain and domain.get('checks'):
        lines+=['## 领域核对与待补证','']
        meanings={'supported':'在所列条件下有依据','contradicted':'存在已核实冲突','conditional':'有条件/待补证','unknown':'尚不可判断','stale':'待重新核对','not_applicable':'不适用'}
        for check in domain['checks']:
            lines+=[f"- {check['title']}：{meanings.get(check['status'],check['status'])}。{check['description']} 依据：{', '.join(check['evidence_ids']) or '待取得'}。"]
            if check.get('formula'):lines+=[f"  - 比较口径：{check['formula']}"]
        lines+=['']
    if domain and domain.get('evidence'):
        lines+=['## 领域资料索引','', '| 证据 | 对象与资料 | 定位 | 版本与可用性 |','| --- | --- | --- | --- |']
        seen_evidence=set()
        for e in domain['evidence']:
            if e['id'] in seen_evidence:continue
            seen_evidence.add(e['id'])
            place=e['locator']+(f"；第{e['page_number']}页" if e.get('page_number') is not None else '')
            version=e.get('version_context',{})
            readable='；'.join(f'{k}={v}' for k,v in version.items() if v and k not in ('source_run',))
            lines+=['| '+' | '.join(cell(v) for v in [e['id'],e['title'],place,readable or '原资料版本待核实'])+' |']
        lines+=['','资料保留原值与适用条件。片段 hash 用于核对本次文本，不代表整份 PDF 的 hash；缺失资料不视为工程通过。','']
    if sources:
        lines+=['','## 需求来源','']
        for (_,quote),number in sources.items():
            lines += [f'### 来源{number} · 用户原话','']+['> '+line for line in quote.splitlines()]+['']
    lines+=['## 本版修改','']+[f'- {change}' for change in doc['changes']]
    return '\n'.join(lines)+'\n'
