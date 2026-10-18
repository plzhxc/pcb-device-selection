import {useState} from 'react';
import {ArrowRight,CheckCheck,CircleHelp,Flag,GitBranch,Search,ShieldAlert,SlidersHorizontal} from 'lucide-react';
import type {Project,ResolvedField} from './types';

export const resolutionLabels={confirmed:'已明确',not_applicable:'不适用',design_stage:'设计阶段确定',tbd:'待明确',conflict:'需解决冲突'};
const chapterNames=['项目目标','功能与体验','性能与环境','制造与验收','模块与接口'];
const closed=(f:ResolvedField)=>['confirmed','not_applicable','design_stage'].includes(f.state);

export function ConvergenceBoard({project,compact=false,onDiscuss,onModules,onSource,onOpen,onReview}:{project:Project;compact?:boolean;onDiscuss:(text:string)=>void;onModules:()=>void;onSource:(id:string)=>void;onOpen?:()=>void;onReview:()=>void}) {
  const [mode,setMode]=useState<'product'|'engineering'|'reference'>('product');
  const [filter,setFilter]=useState('pending'),[chapter,setChapter]=useState('all'),[query,setQuery]=useState('');
  const c=project.convergence;if(!c)return null;
  const scope=c.product_review,decisions=new Set(scope.decision_fields.map(f=>f.id));
  const modules=c.modules.filter(m=>m.state!=='not_applicable');
  const locked=new Set((project.question_submissions??[]).filter(s=>['processing','queued','sending'].includes(s.status)).map(s=>s.question_id));
  const questions=project.document.questions.filter(q=>!locked.has(q.id));
  const stages=[{label:'产品目标',done:scope.decision_fields.filter(f=>f.chapter===1).every(closed)},
    {label:'功能与约束',done:scope.decision_fields.filter(f=>f.chapter===2||f.chapter===3).every(closed)&&project.document.requirements.some(r=>r.category==='functions')},
    {label:'模块方案',done:!c.module_gaps.length&&!c.module_pending.length&&(modules.length>0||project.document.requirements.some(r=>r.category==='functions'&&r.implementation==='software'))},
    {label:'一致性与验收',done:['ready','approved'].includes(project.review.status)},
    {label:'整体审阅',done:project.review.status==='approved'}];
  const relevant=mode==='reference'?c.fields:[...scope.decision_fields,...scope.engineering_fields];
  const filtered=relevant.filter(f=>(chapter==='all'||String(f.chapter)===chapter)&&(filter==='all'||filter==='pending'&&!closed(f)||filter===f.state)&&(!query||`${f.label} ${f.conclusion??''}`.toLowerCase().includes(query.toLowerCase())));
  const groups=project.review.topics.map(t=>({...t,items:filtered.filter(f=>f.topic_id===t.id)})).filter(t=>t.items.length);
  function ask(f:ResolvedField){onDiscuss(decisions.has(f.id)?`关于「${f.label}」：${f.conclusion?`已有记录为“${f.conclusion}”。`:''}请先复用已有回答，说明还需要我决定什么，并给出建议和取舍。`:`关于实现项「${f.label}」：请整理它需要满足的产品目标与后续设计核对事项；没有依据的实现参数保持待设计，不要求我填写器件能力值。`);}
  return <section className={`convergence-board ${compact?'compact':''}`} aria-label="持续需求收敛">
    {!compact&&<><div className="eyebrow">YOUR PRODUCT, TAKING SHAPE</div><h1>先把产品想清楚</h1><p className="phase-intro">你决定用途、体验和验收目标。器件、引脚与工艺等实现细节，会保留给后续设计核对。</p></>}
    <div className="convergence-stages">{stages.map((s,i)=><button key={s.label} className={s.done?'complete':''} onClick={()=>{if(i===4)onReview();else if(i===2)onModules();else if(compact)onOpen?.();else setMode('product');}}><span>{s.done?<CheckCheck size={13}/>:i+1}</span><strong>{s.label}</strong></button>)}</div>
    <div className="decision-summary"><span><CheckCheck size={14}/>{scope.decision_closed} 项产品信息已整理</span><span><GitBranch size={14}/>{modules.length} 个模块</span><span><CircleHelp size={14}/>{questions.length} 个待答问题</span></div>
    {!c.enabled&&project.document.version>0&&<p className="phase-hint">已有需求继续保留。下一轮分析会沿用原文整理产品决定，不需要重新填写整份规范。</p>}
    {!!c.issues.length&&<div className="consistency-list">{c.issues.map(issue=><article className={`consistency-issue ${issue.kind}`} key={issue.id}><ShieldAlert size={17}/><div><strong>{issue.title}</strong><p>{issue.description}</p><button className="text-button" onClick={()=>onDiscuss(`请帮我解决「${issue.title}」：${issue.description}。请复用已知要求，给出可行取舍。`)}>一起解决<ArrowRight size={12}/></button></div></article>)}</div>}
    {compact?<button className="text-button convergence-open" onClick={onOpen}>查看产品决定与下一步<ArrowRight size={12}/></button>:<>
      <div className="scope-tabs" role="group" aria-label="核对内容层级">{([['product','产品决定'],['engineering','专业核对'],['reference','完整规范（参考）']] as const).map(([key,name])=><button key={key} className={mode===key?'active':''} onClick={()=>setMode(key)}>{name}</button>)}</div>
      {mode==='product'?<>
        <div className="convergence-metrics"><div><span>产品信息已整理</span><strong>{scope.decision_closed}<small> 项</small></strong></div><div><span>需要你回答</span><strong>{questions.length}<small> 个问题</small></strong></div><div><span>本版实际模块</span><strong>{modules.length}<small> 个</small></strong></div></div>
        <section className="product-next"><div className="section-heading"><h2><CircleHelp size={15}/>接下来做什么</h2></div>{questions.length?<><p>先回答对话区的问题，可以只答一个。其他分支和已提交的回答会保留。</p>{questions.slice(0,3).map(q=><div className="product-decision-prompt" key={q.id}><strong>{q.question}</strong><span>{q.impact||q.reason}</span></div>)}</>:<p>{locked.size?'你的回答已经收到，正在分析，无需再次选择。':project.review.status==='approved'?'本版需求已审核。后续改变会继续记录并核对。':project.review.status==='ready'?'产品边界与当前需求已整理好，可以整体审阅说明书。':c.module_pending.length?'先核对建议中的模块类别与用途，可在对话区成组确认。':(scope.decision_rechecks.length||scope.module_rechecks.length)?'正在依据已有资料核对关联内容，不需要重复回答。':'继续描述你想做什么；需要产品取舍时，助手会提出少量问题。'}</p>}</section>
        <details className="product-decisions"><summary>查看已整理的产品信息 · {scope.decision_fields.length} 项</summary>{scope.decision_fields.map(f=><div key={f.id}><span>{f.label}</span><p>{f.conclusion||'正在根据对话整理。'}</p><small>{resolutionLabels[f.state]}</small></div>)}</details>
        <p className="engineering-note">专业视图保留实际相关的实现待办；完整规范用于参考，不是需要你逐项回答的清单。</p>
      </>:<>
        <p className="phase-hint">{mode==='reference'?'完整参考目录包含尚未使用的模块与参数。它不属于需求说明书正文，也不是本项目全部需要处理的任务。':'仅展示本版产品决定和相关实现核对。未知实现参数可以保留待设计，不会单独阻止需求说明书审阅。'}</p>
        <div className="field-toolbar"><label><Search size={14}/><input aria-label="搜索需求核对字段" placeholder="搜索相关参数……" value={query} onChange={e=>setQuery(e.target.value)}/></label><select aria-label="筛选核对状态" value={filter} onChange={e=>setFilter(e.target.value)}><option value="pending">待明确与冲突</option><option value="all">全部内容</option><option value="confirmed">已明确</option><option value="design_stage">设计阶段确定</option><option value="not_applicable">不适用</option><option value="conflict">存在冲突</option></select><select aria-label="筛选需求章节" value={chapter} onChange={e=>setChapter(e.target.value)}><option value="all">所有章节</option>{chapterNames.map((name,i)=><option key={name} value={i+1}>{name}</option>)}</select></div>
        <div className="section-heading"><h2><SlidersHorizontal size={15}/>{mode==='reference'?'规范参考条目':'相关工程核对'}</h2><span>{filtered.length} 项</span></div>
        <div className="field-groups">{groups.map(t=><details key={t.id} className="field-topic" data-topic-id={t.id}><summary><span>{t.section} {t.title}</span><small>{t.items.length} 项</small></summary>{t.items.map(f=><article className={`resolved-field ${f.state}`} key={f.id} data-field-id={f.id}><div><strong>{f.label}</strong><span className="field-state">{resolutionLabels[f.state]}</span></div><p>{f.conclusion||(decisions.has(f.id)?'正在整理产品决定。':'实现待设计核对；没有依据的参数继续保持未知。')}</p>{(f.source_quote||f.recheck_evidence?.source_quote)&&<details className="field-evidence"><summary>{f.source_quote?'查看依据':'上次依据（待复核）'}</summary><blockquote>{f.source_quote||f.recheck_evidence?.source_quote}</blockquote><button className="text-button" onClick={()=>{const id=f.source_message_id||f.recheck_evidence?.source_message_id;if(id)onSource(id);}}>定位原始消息<ArrowRight size={12}/></button></details>}<button className="text-button" onClick={()=>ask(f)}>{decisions.has(f.id)?'讨论或修改':'讨论设计边界'}</button></article>)}</details>)}{!groups.length&&<div className="phase-empty"><CheckCheck size={24}/><strong>当前筛选下没有遗漏项</strong><p>可切换状态或章节查看已有内容。</p></div>}</div>
      </>}
      {!!(c.module_gaps.length||c.module_pending.length)&&<button className="module-gap-callout" onClick={onModules}><GitBranch size={17}/><span>还有必要功能的模块方案需要核对</span><ArrowRight size={15}/></button>}
      {!!scope.module_rechecks.length&&<button className="module-gap-callout" onClick={()=>onDiscuss('模块方案已有变化，请依据已有产品要求内部复核实际模块与连接关系，不要求我重复回答已知信息。')}><GitBranch size={17}/><span>模块方案已改变，需要助手复核框架与关系</span><ArrowRight size={15}/></button>}
      {!project.review.has_flow&&<button className="module-gap-callout workflow-gap-callout" onClick={()=>onDiscuss('请依据已明确的功能整理工作流程草案并关联已有需求，先复用已知内容。')}><GitBranch size={17}/><span>工作流程正在形成，可根据已有功能继续整理</span><ArrowRight size={15}/></button>}
      {!!c.progress.length&&<details className="convergence-history"><summary><Flag size={14}/>查看需求演进记录</summary><div>{c.progress.slice(-10).map(p=><span key={p.version} title={`v${p.version}：更新${p.changed_requirements}项需求，${p.module_count}个模块`}><small>v{p.version}</small><em>{p.changed_requirements} 项更新</em></span>)}</div></details>}
      {project.review.status==='ready'&&<button className="button primary small review-from-path" onClick={onReview}>审阅完整说明书<ArrowRight size={13}/></button>}
    </>}
  </section>;
}
