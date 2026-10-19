import {Fragment,useState,type ReactNode} from 'react';
import {ArrowRight,CheckCheck,FileText,MessageSquare,Pencil,Plus,Search} from 'lucide-react';
import type {Project,Requirement,SpecificationField} from './types';
import {states} from './types';
import {DeliveryStatus} from './DeliveryPanel';

export function SpecificationDocument({project,busy,onDiscuss,onEdit,onSource,onModules,tail}:{project:Project;busy:boolean;onDiscuss:(text:string)=>void;onEdit:(r:Requirement|null)=>void;onSource:(id:string)=>void;onModules?:()=>void;tail:ReactNode}){
  const [query,setQuery]=useState(''),[only,setOnly]=useState('project');
  const spec=project.specification,doc=project.document;
  const reqs=new Map(doc.requirements.map(r=>[r.id,r]));
  const primary=new Map<string,string>();
  const allFields=spec.chapters.flatMap(c=>c.topics.flatMap(t=>t.fields));
  for(const f of [...allFields.filter(f=>f.id==='V3-2.1.1:F02'),...allFields.filter(f=>f.display==='requirements')])for(const id of f.requirement_ids??[])if(!primary.has(id))primary.set(id,f.id);
  const supplemental=doc.requirements.filter(r=>!primary.has(r.id));
  function matches(f:SpecificationField){
    if(only==='project'&&f.state==='tbd'&&!f.review_pending&&!f.requirement_ids?.length&&f.display!=='title'&&f.status_label!=='待你决定')return false;
    if(only==='known'&&f.state!=='confirmed')return false;
    if(only==='pending'&&!f.review_pending&&f.status_label!=='待你决定')return false;
    const text=[f.label,f.content,...(f.requirement_ids??[]).map(id=>{const r=reqs.get(id);return r?`${r.name} ${r.value}`:'';})].join(' ');
    return !query||text.toLocaleLowerCase().includes(query.toLocaleLowerCase());
  }
  function jump(id:string){
    const target=document.getElementById(id);if(!target){setOnly('project');setQuery('');requestAnimationFrame(()=>document.getElementById(id)?.scrollIntoView({behavior:'smooth',block:'center'}));return;}
    for(let p:HTMLElement|null=target;p;p=p.parentElement)if(p instanceof HTMLDetailsElement)p.open=true;
    target.scrollIntoView({behavior:'smooth',block:'start'});
  }
  function fieldRow(f:SpecificationField){
    const rows=(f.requirement_ids??[]).flatMap(id=>reqs.has(id)?[reqs.get(id)!]:[]);
    const useRows=rows.length&&['requirements','names'].includes(f.display);
    return <article className={`spec-line ${f.state} ${f.review_pending?'reviewing':''}`} key={f.id} data-field-id={f.id}>
      <div className="spec-line-heading"><h4>{f.label}</h4><span className="spec-state">{f.status_label}</span></div>
      {useRows?<div className="spec-linked-values">{rows.map(r=>f.display==='names'||primary.get(r.id)!==f.id?<button className="spec-short-reference text-button" key={r.id} onClick={()=>jump('spec-req-'+r.id)}>{r.name}<ArrowRight size={11}/></button>:<div className={`spec-value ${r.state}`} key={r.id} id={'spec-req-'+r.id} data-requirement-id={r.id} data-primary-requirement={r.id}><strong>{r.name}</strong><p>{r.value}{r.unit&&!r.value.includes(r.unit)?` ${r.unit}`:''}</p><div className="spec-value-actions"><span>{states[r.state]}</span>{r.source_message_id&&<button className="text-button" onClick={()=>onSource(r.source_message_id)}>查看用户依据</button>}<button className="text-button" onClick={()=>onDiscuss(`我想讨论“${r.name}”这项要求。请沿用已明确的其他决定，帮我调整它。`)}><MessageSquare size={11}/>讨论</button><button className="icon-button" aria-label={`编辑需求 ${r.name}`} disabled={busy} onClick={()=>onEdit(r)}><Pencil size={11}/></button></div></div>)}</div>:<p className={f.state==='tbd'&&!f.review_pending?'spec-unknown':'spec-conclusion'}>{f.content}</p>}
      {!useRows&&f.source_message_id&&<button className="text-button spec-source" onClick={()=>onSource(f.source_message_id!)}>查看依据<ArrowRight size={11}/></button>}
      {!useRows&&<button className="text-button spec-discuss" onClick={()=>onDiscuss(`关于规范条款“${f.label}”：请先复用已有信息，说明与我的产品有什么关系；需要我选择时给出容易理解的建议。`)}>讨论这一条</button>}
    </article>;
  }
  return <div className="specification-document" aria-label="按规范逐条展示的需求说明书">
    <div className="eyebrow">HARDWARE REQUIREMENTS · {spec.revision}</div><h1>{doc.title}<span>硬件需求说明书</span></h1>
    <dl className="spec-metadata"><div><dt>状态</dt><dd>{project.review.status==='approved'?'已审核':project.review.status==='ready'?'待审核':'草稿'}</dd></div><div><dt>版本</dt><dd>v{doc.version}</dd></div><div><dt>生成日期</dt><dd>{new Date(doc.created_at??doc.updated_at).toLocaleString('zh-CN',{hour12:false})}</dd></div><div><dt>最后更新</dt><dd>{new Date(doc.updated_at).toLocaleString('zh-CN',{hour12:false})}</dd></div></dl>
    <p className="spec-summary">{doc.summary}</p>
    <DeliveryStatus project={project} onModules={onModules}/>
    <p className="spec-reading-note"><FileText size={14}/>逐条对应 V3 规范。已明确、建议与待设计分别标注；无需你逐项填写参数。</p>
    <div className="spec-toolbar"><label><Search size={14}/><input aria-label="搜索说明书条款" placeholder="搜索条款或需求内容" value={query} onChange={e=>setQuery(e.target.value)}/></label><select aria-label="筛选说明书条款" value={only} onChange={e=>setOnly(e.target.value)}><option value="project">本项目内容（推荐）</option><option value="all">完整规范参考</option><option value="known">已明确的条款</option><option value="pending">待决定与关联复核</option></select><button className="text-button" disabled={busy} onClick={()=>onEdit(null)}><Plus size={13}/>补充一项</button></div>
    {only==='project'&&<p className="spec-reference-note">未涉及的参考字段已合并收起；{allFields.length} 条规范参考仍可切换查看、检索和导出。</p>}
    <nav className="spec-chapter-nav" aria-label="说明书章节">{spec.chapters.map(c=><button key={c.number} onClick={()=>jump(`chapter-${c.number}`)}>{c.number}. {c.title}</button>)}<button onClick={()=>jump('chapter-5')}>5. 电路模块</button></nav>
    {spec.chapters.map(chapter=>{
      const topics=chapter.topics.map(t=>({...t,fields:t.fields.filter(matches)})).filter(t=>t.fields.length);
      if(!topics.length)return null;
      return <section className="spec-chapter" key={chapter.number} id={`chapter-${chapter.number}`}><h2><span>第 {chapter.number} 章</span>{chapter.title}</h2>{topics.map((topic,index)=><Fragment key={topic.id}>{topic.parent&&topic.parent.section!==topics[index-1]?.parent?.section&&<h3 className="spec-parent-heading">{topic.parent.section} {topic.parent.title}</h3>}<details key={`${topic.id}:${!!query}:${only}`} className="spec-topic" id={topic.id} open={!!query||only!=='all'||chapter.number<3||topic.fields.some(f=>f.state!=='tbd'||!!f.requirement_ids?.length)}>
        <summary><span>{topic.section}</span><strong>{topic.title}</strong><small>{topic.engineering?'专业要求':''}<span>{topic.fields.length} 条</span></small></summary><div>{topic.fields.map(fieldRow)}</div>
      </details></Fragment>)}</section>;
    })}
    {(!!query||only!=='all')&&!spec.chapters.some(c=>c.topics.some(t=>t.fields.some(matches)))&&<p className="phase-hint">{only==='pending'&&!query?'本版暂无待你决定或待助手复核的条款；工程任务见交接清单。':'没有匹配的条款，可以换个关键词或切换筛选。'}</p>}
    {!!supplemental.length&&<details className="spec-unassigned" open><summary>已记录的补充要求 · {supplemental.length} 项</summary><p>这些要求的正文集中保留在这里，各条款中的引用可直接跳转。</p>{supplemental.map(r=><article className="spec-value" key={r.id} id={'spec-req-'+r.id} data-requirement-id={r.id} data-primary-requirement={r.id}><strong>{r.name}</strong><p>{r.value}{r.unit&&!r.value.includes(r.unit)?` ${r.unit}`:''}</p><div className="spec-value-actions"><span>{states[r.state]}</span>{r.source_message_id&&<button className="text-button" onClick={()=>onSource(r.source_message_id)}>查看用户依据</button>}<button className="text-button" onClick={()=>onEdit(r)} disabled={busy}>调整这项要求</button></div></article>)}</details>}
    <section className="spec-tail" id="chapter-5">{tail}</section>
    <p className="spec-reading-note"><CheckCheck size={14}/>本页与导出文件使用同一版需求内容，最终由你整体审阅。</p>
  </div>;
}
