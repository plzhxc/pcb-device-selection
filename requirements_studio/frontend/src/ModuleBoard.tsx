import {ReferencePlan,InterfaceTable} from './DeliveryPanel';
import {useEffect,useRef,useState} from 'react';
import {ArrowRight,BookOpen,Check,ChevronRight,GitBranch,Layers3,Loader2,Pencil,Plus,Search,Trash2,X} from 'lucide-react';
import {DiagramCanvas} from './DiagramCanvas';
import {moduleGraph} from './diagramGraph';
import {ModulePlanReview} from './ModulePlanReview';
import {api,type ModuleDetail,type ModuleInstance,type ModuleType,type Project} from './types';

interface Catalog {revision:string;source_file:string;source_sha256:string;categories:{id:string;name:string;parents:number;children:number}[];types:ModuleType[]}
const stateLabels={proposed:'AI 建议',confirmed:'已确认方案',design_stage:'实现交给设计阶段',tbd:'待核对',not_applicable:'不纳入本版'};
const kindLabels={power:'供电',data:'数据',control:'控制',clock:'时钟'};

function ModuleMap({project,onSelect}:{project:Project;onSelect?:(id:string)=>void}) {
  if(!project.convergence.modules.some(m=>m.state!=='not_applicable'))return null;
  return <DiagramCanvas graph={moduleGraph({...project.document,modules:project.convergence.modules},project.convergence.module_links)} title="本产品系统框图" storageKey={project.id+':module-plan'} onDiscuss={onSelect}/>;
}

function ModuleEditor({project,catalog,type,module,onClose,onSaved}:{project:Project;catalog:Catalog;type:ModuleType;module?:ModuleInstance;onClose:()=>void;onSaved:(p:Project)=>void}) {
  const ref=useRef<HTMLDialogElement>(null);
  const [typeId,setTypeId]=useState(module?.type_id??type.id),[name,setName]=useState(module?.name??type.name);
  const [purpose,setPurpose]=useState(module?.purpose??''),[boundary,setBoundary]=useState(module?.boundary??'');
  const [quantity,setQuantity]=useState(module?.quantity??1),[state,setState]=useState(module?.state==='proposed'?'confirmed':module?.state??'confirmed');
  const [refs,setRefs]=useState<string[]>(module?.requirement_ids??[]),[saving,setSaving]=useState(false),[error,setError]=useState('');
  useEffect(()=>{ref.current?.showModal();return()=>ref.current?.close();},[]);
  const valid=name.trim()&&purpose.trim()&&refs.length&&Number.isInteger(quantity)&&quantity>=1&&quantity<=1000&&(state!=='design_stage'||boundary.trim());
  async function save(e:React.FormEvent) {
    e.preventDefault();if(!valid||saving)return;setSaving(true);setError('');
    try{const result=await api<Project>(`/projects/${project.id}/modules`,{method:'POST',body:JSON.stringify({base_version:project.document.version,id:module?.id??'',type_id:typeId,name:name.trim(),purpose:purpose.trim(),boundary:boundary.trim(),quantity,state,requirement_ids:refs})});onSaved(result);onClose();}
    catch(e){setError((e as Error).message);}finally{setSaving(false);}
  }
  return <dialog className="dialog module-editor" ref={ref} onCancel={e=>{e.preventDefault();if(!saving)onClose();}} onClick={e=>{if(e.target===e.currentTarget&&!saving)onClose();}}><div className="dialog-heading"><h2>{module?'调整模块方案':'纳入模块方案'}</h2><button className="icon-button" aria-label="关闭模块编辑" disabled={saving} onClick={onClose}><X size={18}/></button></div><form className="editor-form" onSubmit={save}>
    <p className="muted">选择功能类别和边界。具体器件、引脚及电路验证会留给后续设计。</p>
    <label>规范类别<select aria-label="模块规范类别" value={typeId} onChange={e=>setTypeId(e.target.value)}>{catalog.categories.map(c=><optgroup key={c.id} label={c.name}>{catalog.types.filter(t=>t.category_id===c.id).map(t=><option key={t.id} value={t.id}>{t.level==='submodule'?'　↳ ':''}{t.name}</option>)}</optgroup>)}</select></label>
    <div className="form-grid"><label>模块名称<input aria-label="模块方案名称" required maxLength={160} value={name} onChange={e=>setName(e.target.value)}/></label><label>独立电路数量<input aria-label="模块数量" type="number" min={1} max={1000} required value={quantity} onChange={e=>setQuantity(Number(e.target.value))}/></label></div>
    <label>它在产品中做什么<textarea aria-label="模块用途" required maxLength={1000} rows={2} placeholder="例如：为主控和温度采集提供稳定电源" value={purpose} onChange={e=>setPurpose(e.target.value)}/></label>
    <fieldset className="module-requirement-picker"><legend>承接哪些需求 <small>至少选择一项</small></legend><div>{project.document.requirements.map(r=><label key={r.id}><input type="checkbox" value={r.id} checked={refs.includes(r.id)} disabled={!refs.includes(r.id)&&refs.length>=80} onChange={e=>setRefs(old=>e.target.checked?[...old,r.id]:old.filter(id=>id!==r.id))}/><span>{r.name}<small>{r.value}</small></span></label>)}</div></fieldset>
    <label>方案状态<select aria-label="模块方案状态" value={state} onChange={e=>setState(e.target.value as typeof state)}><option value="confirmed">确认这一类别与用途</option><option value="design_stage">确认目标，具体实现交给设计阶段</option><option value="tbd">保留待核对</option><option value="not_applicable">不纳入本版</option></select></label>
    <label>范围和待设计内容{state==='design_stage'?'（必填）':''}<textarea aria-label="模块实现边界" maxLength={1500} rows={2} required={state==='design_stage'} placeholder="例如：满足已确认的供电和功耗要求；具体拓扑、芯片和外围由设计阶段确定" value={boundary} onChange={e=>setBoundary(e.target.value)}/></label>
    {error&&<p className="inline-error" role="alert">{error}</p>}<div className="dialog-actions"><button type="button" className="button secondary" onClick={onClose} disabled={saving}>取消</button><button className="button primary" disabled={saving||!valid}>{saving?<Loader2 className="spin" size={15}/>:<Check size={15}/>}保存模块方案</button></div>
  </form></dialog>;
}

export function ModuleBoard({project,busy,onSaved,onDiscuss,onEvidence,onRequirement,onSource,onAdopt}:{project:Project;busy:boolean;onSaved:(p:Project)=>void;onDiscuss:(text:string)=>void;onEvidence?:(moduleId:string)=>void;onRequirement?:(id:string)=>void;onSource?:(id:string)=>void;onAdopt?:(text:string)=>Promise<boolean>}) {
  const detailRef=useRef<HTMLElement>(null);
  const [catalog,setCatalog]=useState<Catalog|null>(null),[error,setError]=useState(''),[category,setCategory]=useState('all'),[query,setQuery]=useState('');
  const [selected,setSelected]=useState(''),[detail,setDetail]=useState<ModuleDetail|null>(null),[detailLoading,setDetailLoading]=useState(false);
  const [editor,setEditor]=useState<{type:ModuleType;module?:ModuleInstance}|null>(null),[saving,setSaving]=useState(false);
  useEffect(()=>{const controller=new AbortController();api<Catalog>('/module-catalog',{signal:controller.signal}).then(setCatalog).catch(e=>{if(!controller.signal.aborted)setError(e.message);});return()=>controller.abort();},[]);
  useEffect(()=>{if(!selected){setDetail(null);return;}const controller=new AbortController();setDetail(null);setDetailLoading(true);api<ModuleDetail>(`/module-catalog/${selected}`,{signal:controller.signal}).then(setDetail).catch(e=>{if(!controller.signal.aborted)setError(e.message);}).finally(()=>{if(!controller.signal.aborted)setDetailLoading(false);});return()=>controller.abort();},[selected]);
  useEffect(()=>{if(detail)detailRef.current?.scrollIntoView({behavior:'smooth',block:'start'});},[detail]);
  const c=project.convergence;
  const modules=c.modules.filter(m=>m.state!=='not_applicable');
  const excluded=c.modules.filter(m=>m.state==='not_applicable');
  const matches=(t:ModuleType)=>!query||`${t.name} ${catalog?.categories.find(c=>c.id===t.category_id)?.name??''}`.toLowerCase().includes(query.toLowerCase());
  const parents=catalog?.types.filter(t=>t.level==='parent'&&(category==='all'||category===t.category_id)&&(matches(t)||catalog.types.some(child=>child.parent_id===t.id&&matches(child))))??[];
  function edit(m:ModuleInstance){const type=catalog?.types.find(t=>t.id===m.type_id);if(type)setEditor({type,module:m});setSelected(m.type_id);}
  async function remove(m:ModuleInstance){setSaving(true);setError('');try{onSaved(await api<Project>(`/projects/${project.id}/modules/${m.id}?base_version=${project.document.version}`,{method:'DELETE'}));}catch(e){setError((e as Error).message);}finally{setSaving(false);}}
  return <div className="module-board"><div className="eyebrow">HARDWARE BUILDING BLOCKS</div><h1>产品功能，怎样由模块承接</h1><p className="phase-intro">先看每个模块为什么需要、承接什么功能，再核对规范类别。AI 建议与你已确认的方案分别标明。</p>{onAdopt&&<ModulePlanReview compact project={project} busy={busy} disabled={saving} onConfirm={onAdopt} onInspect={()=>document.querySelector('.module-instance-list')?.scrollIntoView({behavior:'smooth',block:'start'})}/>}<ReferencePlan project={project} onSaved={onSaved}/><InterfaceTable project={project} onSaved={onSaved}/>
    {error&&<p role="alert" className="inline-error">{error}</p>}
    <div className="section-heading"><h2><GitBranch size={15}/>本版模块方案</h2><span>{c.modules.filter(m=>m.state!=='not_applicable').length} 项</span></div>
    <ModuleMap project={project} onSelect={id=>{const r=project.document.requirements.find(r=>r.id===id);if(r)onDiscuss(`我想讨论模块承接的“${r.name}”要求，请沿用已经明确的决定，说明需要调整什么。`);}}/>
    <div className="module-instance-list">{modules.map(m=><article key={m.id} className={`module-instance ${m.state}`} data-module-id={m.id}><div className="module-instance-top"><div><small>{m.category_name} / {m.type_name}</small><h3>{m.name}<span>× {m.quantity}</span></h3></div><span className="module-state">{stateLabels[m.state]}</span></div><div className="module-purpose"><span>为什么需要它</span><p>{m.purpose}</p></div><div className="module-responsibilities"><span>承接的产品要求</span>{m.requirement_ids.map(id=>{const r=project.document.requirements.find(r=>r.id===id);return r?<details key={id}><summary>{r.name}<small>{r.state==='stated'?'用户已明确':r.state==='suggested'?'AI 建议':'待核对'}</small></summary><p>{r.value}</p><button className="text-button" onClick={()=>onRequirement?.(id)}>在说明书中查看<ArrowRight size={11}/></button></details>:null;})}</div>{m.boundary&&<div className="module-design-boundary"><span>范围与待设计事项</span><p>{m.boundary}</p></div>}{m.source_message_id&&<button className="text-button module-source" onClick={()=>onSource?.(m.source_message_id)}>{m.state==='proposed'?'查看提案的需求依据':'查看用户决定的依据'}<ArrowRight size={11}/></button>}<div className="module-card-actions">{onEvidence&&<button className="text-button" onClick={()=>onEvidence(m.id)}><BookOpen size={12}/>器件与证据{project.domain?.references.some(r=>r.module_ids.includes(m.id))?` · ${project.domain.references.filter(r=>r.module_ids.includes(m.id)).length}`:""}</button>}<button className="text-button" onClick={()=>setSelected(m.type_id)}><BookOpen size={12}/>查看规范</button><button className="text-button" onClick={()=>onDiscuss(`关于模块「${m.name}」（${m.type_name}）：请核对它的用途、边界和关联需求，并给出必要建议。`)}>继续讨论<ArrowRight size={12}/></button><button className="icon-button" aria-label={`调整模块 ${m.name}`} disabled={busy||saving||!catalog} onClick={()=>edit(m)}><Pencil size={13}/></button><button className="icon-button" aria-label={`移除模块 ${m.name}`} disabled={busy||saving} onClick={()=>void remove(m)}><Trash2 size={13}/></button></div></article>)}{!modules.length&&<div className="phase-empty"><Layers3 size={25}/><strong>先明确功能，再选合适的类别</strong><p>AI 会根据对话提出方案。专业用户也可以展开规范目录，直接调整。</p></div>}</div>

    {!!c.module_gaps.length&&<div className="phase-hint"><CircleHelpIcon/><p>以下必需功能尚无已确认的硬件模块承接：{c.module_gaps.map(id=>project.document.requirements.find(r=>r.id===id)?.name??id).join('、')}。</p></div>}
    {!!excluded.length&&<details className="excluded-modules"><summary>已排除的模块 · {excluded.length} 项</summary>{excluded.map(m=><p key={m.id}>{m.name}：不纳入当前产品。</p>)}</details>}
    <details className="module-library"><summary><BookOpen size={15}/><strong>查看完整模块规范目录</strong><span>专业选择 · 10 大类</span></summary><p className="phase-hint">这里是类别参考库，不属于本项目的模块清单。按需要选择，不必逐类确认。</p>
    <div className="section-heading catalog-heading"><h2><BookOpen size={15}/>模块规范目录</h2><span>10 大类 · 60 父类 · 197 子类</span></div>
    <div className="catalog-search"><Search size={15}/><input aria-label="搜索模块类别" placeholder="搜索电源、传感器、蓝牙、主控……" value={query} onChange={e=>setQuery(e.target.value)}/></div>
    <div className="catalog-categories" role="group" aria-label="模块大类"><button className={category==='all'?'active':''} onClick={()=>setCategory('all')}>全部</button>{catalog?.categories.map(c=><button key={c.id} className={category===c.id?'active':''} onClick={()=>setCategory(c.id)}>{c.name.replace('类模块','')}</button>)}</div>
    {!catalog&&!error&&<p className="phase-hint"><Loader2 size={15} className="spin"/>正在读取规范目录……</p>}
    <div className="catalog-tree">{parents.map(p=><details key={p.id} open={query?true:undefined}><summary><ChevronRight size={13}/><span>{p.name}</span><small>{catalog?.categories.find(c=>c.id===p.category_id)?.name}</small><button className="text-button" aria-label={`查看类别 ${p.name}`} onClick={e=>{e.preventDefault();setSelected(p.id);}}>查看类别</button></summary><button className={`catalog-type parent ${selected===p.id?'selected':''}`} data-type-id={p.id} onClick={()=>setSelected(p.id)}><span>先按「{p.name}」确定需求边界</span><ArrowRight size={12}/></button>{catalog?.types.filter(t=>t.parent_id===p.id&&(!query||matches(t)||matches(p))).map(t=><button className={`catalog-type ${selected===t.id?'selected':''}`} key={t.id} data-type-id={t.id} onClick={()=>setSelected(t.id)}><span>{t.name}</span><ArrowRight size={12}/></button>)}</details>)}{catalog&&!parents.length&&<p className="phase-hint">没有找到该类别。可以换个功能名称，或清空搜索查看完整目录。</p>}</div>
    </details>
    {detailLoading&&<p role="status" className="phase-hint"><Loader2 size={14} className="spin"/>正在打开模块规范……</p>}
    {detail&&<section ref={detailRef} className="module-spec-detail" aria-label="模块规范详情"><div className="section-heading"><h2>{detail.name}</h2><button className="icon-button" aria-label="关闭模块规范详情" onClick={()=>setSelected('')}><X size={14}/></button></div><p className="module-breadcrumb">{detail.category_name}{detail.parent_name?` › ${detail.parent_name}`:''} › {detail.level==='parent'?'父类别':'子类别'}</p>{detail.description&&<p>{detail.description}</p>}{detail.level==='parent'&&<p>这是功能父类别。若具体拓扑或实现未定，可以先登记目标边界；不必现在选定芯片。</p>}
      <button className="button primary small add-module" disabled={busy||saving||!project.document.requirements.length} onClick={()=>setEditor({type:detail})}><Plus size={14}/>纳入模块方案</button>{!project.document.requirements.length&&<p className="muted">请先在对话中描述产品，形成至少一项需求。</p>}
      {!!detail.ports?.length&&<details className="spec-section"><summary>抽象输入输出角色</summary><p className="muted">来自规范类型卡，不是器件封装脚号；实际连接要在后续设计中核对。</p><div className="role-port-list">{detail.ports.map(p=><span key={p.role}><strong>{p.role}</strong><small>{p.direction} · {p.kind} · {p.required}</small></span>)}</div></details>}
      {!!detail.parameters?.length&&<details className="spec-section"><summary>工程参数与核对方法（{detail.parameters.length+(detail.inherited_parameters?.length??0)} 项）</summary><p className="muted">以下是类型参数模板。需求阶段只明确适用目标；器件保证值、计算和验证证据在设计阶段补充。</p><div className="parameter-table-scroll"><table><thead><tr><th>参数</th><th>单位</th><th>填写与条件</th></tr></thead><tbody>{[...(detail.inherited_parameters??[]),...detail.parameters].map(p=><tr key={p.id}><th>{p.name}<small>{p.required}</small></th><td>{p.unit}</td><td>{p.value_rule}<small>{p.conditions}</small><small>{p.match_rule}</small></td></tr>)}</tbody></table></div></details>}
      {!!detail.engineering_card?.length&&<details className="spec-section"><summary>功能边界与完整工程卡</summary>{detail.engineering_card.map((line,i)=><p key={i}>{line}</p>)}</details>}
      <details className="spec-section"><summary>规范来源</summary><p>{catalog?.source_file} · 版本 {catalog?.revision}</p><p className="muted">原文章节 {detail.spec_ref}；类别名称和功能定义以原规范为准。</p></details>
    </section>}
    {editor&&catalog&&<ModuleEditor key={editor.module?.id??editor.type.id} project={project} catalog={catalog} type={editor.type} module={editor.module} onClose={()=>setEditor(null)} onSaved={onSaved}/>}
  </div>;
}
function CircleHelpIcon(){return <GitBranch size={16}/>;}
