"""Render a project's requirements, rather than the complete reference template."""
import re

STATE={'stated':'用户已提出','suggested':'AI建议','tbd':'待明确','conflict':'存在冲突','not_applicable':'不纳入本版'}
MODULE_STATE={'proposed':'建议，待确认','confirmed':'已确认类别与用途','design_stage':'实现交给设计阶段','tbd':'待核对'}


def render_project_document(doc,domain=None,*,structured=False):
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
    from .specification import specification_view,render_normative_chapters,render_normative_topics
    spec=specification_view(doc,phase)
    compact_blocks=[]
    lines=[f"# {doc['title']} 硬件需求说明书",'',
           '| 项目 | 内容 |','| --- | --- |',
           f"| 生成日期 | {cell(doc.get('created_at','未记录'))} |",f"| 状态 | {status} |",
           f"| 最后更新 | {cell(doc['updated_at'])} |",f"| 版本 | v{doc['version']} |",'',
           '> 按 V3.0 规范逐条整理。用户已明确、AI 建议、委托设计和未知内容分别标注；未知实现参数不代表工程验证通过。','']
    if doc.get('analysis_pending'):
        lines+=['> 最新补充正在整理，以下保留上次已保存版本。','']
    lines+=['## 设计摘要','',doc['summary'] or '正在根据产品想法整理。','']
    lines+=render_normative_chapters(spec,doc,source)
    tail_start=len(lines)
    def module_fields(topics,skip_heading=False):
        full=render_normative_topics(topics,doc,source)
        selected=[{**t,'fields':[f for f in t['fields'] if f['state']!='tbd' or f['review_pending'] or f['requirement_ids']]} for t in topics]
        selected=[t for t in selected if t['fields']]
        compact=render_normative_topics(selected,doc,source)
        if skip_heading:full=full[2:];compact=compact[2:]
        start=len(lines);lines.extend(full);compact_blocks.append((start,len(lines),compact))
    lines+=['## 第 5 章 电路模块分析','','### 5.1 系统框图与功能责任','']
    def rows_table(rows):
        out=[]
        for row in rows:
            out+=[f"- **{row['name']}**：{row['value']}（{row['id']}；{STATE[row['state']]}；{source(row)}）"]
        return out+['']
    architecture=[r for r in doc['requirements'] if r['category']=='architecture' and r['state']!='not_applicable']
    if architecture:lines+=rows_table(architecture)
    modules=[m for m in phase['modules'] if m['state']!='not_applicable']
    if modules:
        lines+=['','### 系统框图草案','','以下关系表达需求方案，不能替代真实引脚与电气连接核对。','','```mermaid','flowchart LR']
        ids={m['id'] for m in modules}
        for m in modules:lines.append(f'  {m["id"].replace("-","_")}["{label(m["name"])}"]')
        for edge in phase['module_links']:
            if edge['source'] not in ids or edge['target'] not in ids:continue
            kind={'power':'供电','data':'数据','control':'控制','clock':'时钟'}[edge['kind']]
            lines.append(f'  {edge["source"].replace("-","_")} -->|{kind}| {edge["target"].replace("-","_")}')
        lines+=['```','']
        lines+=['### 5.2 电路模块清单','','以下只列本版纳入的功能电路。类别和用途已确认时，芯片、具体拓扑和引脚仍可留待设计。','',
                '| 模块 | 规范类别 | 数量 | 在本产品中的用途与边界 | 状态 | 依据 |','| --- | --- | --- | --- | --- | --- |']
        for m in modules:
            lines+=['| '+' | '.join(cell(v) for v in [m['name'],m['category_name']+' / '+m['type_name'],m['quantity'],m['purpose']+'；'+m.get('boundary',''),MODULE_STATE[m['state']],source(m)])+' |']
    else:lines+=['模块方案将根据已明确的产品功能逐步形成。','']
    if modules:
        module_fields([t for t in spec['module_topics'] if t['id']!='V3-5.3'])
    lines+=['### 5.3 模块间接口定义','','关系图记录供电、数据、控制和时钟职责；具体信号、电平、时序、线缆与连接器按已明确要求核定。未给出的实现参数保留待设计。','']
    interface_topic=next((t for t in spec['module_topics'] if t['id']=='V3-5.3'),None)
    if interface_topic:
        # The section heading is already above; retain the exact field rows.
        module_fields([interface_topic],skip_heading=True)
    for edge in phase['module_links']:
        by_id={m['id']:m for m in modules}
        if edge['source'] in by_id and edge['target'] in by_id:
            lines += [f"- {by_id[edge['source']]['name']} → {by_id[edge['target']]['name']}：{edge['description']}（{'已确认关系' if edge['state']=='confirmed' else '关系草案，待设计核定'}）。"]
            for field,title in (('protocol','协议'),('signals','信号'),('voltage','电平/电源域'),('rate','速率'),('boundary','边界'),('role','角色'),('conditions','适用条件')):
                value=edge.get(field)
                if value:lines.append('  - '+title+'：'+('、'.join(value) if isinstance(value,list) else value))
    lines+=['']
    if doc.get('external_interfaces'):
        lines+=['### 本版外部接口合同','','| 连接位置 | 用途 / 外部对象 | 协议与信号 | 电平 / 共用关系 | 状态 |','| --- | --- | --- | --- | --- |']
        for row in doc['external_interfaces']:
            lines.append('| '+' | '.join(cell(x) for x in [row['name'],row['purpose']+'；'+row.get('external_object',''),row.get('protocol','')+'；'+'、'.join(row.get('signals',[])),row.get('voltage','')+'；'+row.get('shared_link',''),row['state']])+' |')
        lines+=['']
    if doc.get('reference_designs'):
        from .reference_design import bom_view
        bom=bom_view(doc,domain)
        lines+=['## 参考设计与本版核心器件方案','','参考是设计依据；以下区分观测数量与本版采用数量，不等于完成电气核验或生产BOM。','',
                '| 器件 | 参考位号 | 采购号 / 封装 | 参考数量 | 本版数量 | 用途与采用情况 |','| --- | --- | --- | --- | --- | --- |']
        for row in (r for r in bom['rows'] if r['core']):
            lines.append('| '+' | '.join(cell(x) for x in [row['model'],','.join(row['designators']),row['supplier_part']+' / '+row['package'],row['observed_quantity'],row['quantity'] if row['quantity'] is not None else '待采用/核定',row['purpose']+'；'+{'adopted':'已采用为本版优先方案','proposed':'建议，待采用','reference':'仅作参考'}[row['status']]])+' |')
        lines+=['','参考完整BOM与原始网络保存在同版结构化交接合同中；CSV可从模块方案导出。','']
    if doc.get('delivery_checks'):
        lines+=['## 系统能力链与交接核对','']
        from .delivery import LABELS
        for check in doc['delivery_checks']:lines.append('- '+LABELS[check['key']]+'：'+check['conclusion']+('；下一步：'+check['next_action'] if check.get('next_action') else ''))
        lines+=['']
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
    if review.get('intake',{}).get('enabled'):
        lines+=['','## 需求交接必要边界','', '| 边界 | 当前状态 | 本版结论 | 依据 |','| --- | --- | --- | --- |']
        for check in review['intake']['checks']:
            state={'confirmed':'已明确','open':'待产品决定','review':'待内部复核','design_stage':'已委托设计','not_applicable':'不适用'}[check['state']]
            lines.append('| '+' | '.join(cell(v) for v in (check['label'],state,check['conclusion'],source(check)))+' |')
        lines+=['','这些是需求交接边界，不能替代器件选型、实际电路与工程验证。','']
    lines+=['','## 设计交接与待核对内容','']
    selected=set(scope['module_topics'])
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
    lines+=['## 第 6 章 器件选型边界','','本章依规范在需求审核后进入选型。当前只记录引用意图与资料状态；未完成的候选比较、定型和 BOM 不作为已完成结论。','']
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
        sources_start=len(lines)
        lines+=['','## 需求来源','']
        for (_,quote),number in sources.items():
            lines += [f'### 来源{number} · 用户原话','']+['> '+line for line in quote.splitlines()]+['']
        compact_blocks.append((sources_start,len(lines),['','## 需求来源','','用户原话与附件完整保留。可从需求条目查看依据，或展开专业参考阅读全部来源。','']))
    lines+=['## 本版修改','']+[f'- {change}' for change in doc['changes']]
    markdown='\n'.join(lines)+'\n'
    if not structured:return markdown
    compact=lines[tail_start:]
    for start,end,replacement in reversed(compact_blocks):compact[start-tail_start:end-tail_start]=replacement
    return {**spec,'tail_markdown':'\n'.join(lines[tail_start:])+'\n','compact_tail_markdown':'\n'.join(compact)+'\n','markdown':markdown}
