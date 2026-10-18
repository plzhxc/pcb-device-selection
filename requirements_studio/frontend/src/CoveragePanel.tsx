import { ArrowRight, CheckCheck, ChevronRight, CircleHelp } from 'lucide-react';
import { useState } from 'react';
import type { CoverageTopic, Review } from './types';

const labels={confirmed:'已明确',not_applicable:'明确不适用',design_stage:'设计阶段确定',tbd:'待确认',conflict:'存在冲突'};
const chapters=['项目概述','功能需求详解','性能指标要求','生产与测试','电路模块分析'];
export function CoveragePanel({review,busy,disabled,onContinue,onApprove,onTopic}:{review:Review;busy:boolean;disabled:boolean;onContinue:()=>void;onApprove:()=>void;onTopic:(topic:CoverageTopic)=>void}) {
  const [expanded,setExpanded]=useState(false);
  const total=review.decision_total??review.total,closed=review.decision_closed??review.closed;
  const blockers=[review.analysis_pending,Math.max(0,total-closed)&&`${total-closed} 项产品信息待整理`,review.open_questions&&`${review.open_questions} 个问题待回答`,review.unsettled_requirements.length&&`${review.unsettled_requirements.length} 项需求待明确`,review.module_pending?.length&&`${review.module_pending.length} 个模块方案待确认`,review.module_gaps?.length&&`${review.module_gaps.length} 项功能待承接`,review.consistency_issues?.length&&`${review.consistency_issues.length} 项一致性问题`,review.internal_rechecks&&`${review.internal_rechecks} 项关联内容待助手内部复核`,!review.has_flow&&'工作流程待整理'].filter(Boolean);
  return <section className="coverage-panel" aria-label="需求收敛检查">
    <div className="section-heading"><h2><CheckCheck size={15}/>需求审阅准备</h2><span>{closed} / {total} 项产品信息已整理</span></div>
    <div className="coverage-progress" role="progressbar" aria-label="产品决定整理进度" aria-valuenow={closed} aria-valuemin={0} aria-valuemax={total}><span style={{width:`${total?closed/total*100:0}%`}}/></div>
    <button className="text-button coverage-expand" onClick={()=>setExpanded(v=>!v)} aria-expanded={expanded}><ChevronRight size={13}/>{expanded?'收起专业参考':'查看专业参考'}</button>
    <p className="field-coverage-caption">{review.open_questions} 个问题待处理 · {review.module_pending?.length??0} 个模块方案待确认 · {review.module_gaps?.length??0} 项功能待承接 · {review.consistency_issues?.length??0} 项一致性问题</p>
    {!review.has_flow&&<p className="field-coverage-caption">工作流程草案尚未整理，可在收敛路径中继续完善。</p>}
    {expanded&&<p className="coverage-explanation">V3完整规范保留为工程参考，包含尚未使用的模块。未知实现项保持待设计核对，不是需要你全部回答的问卷。内部已处理 {review.fields_closed??review.closed}/{review.fields_total??review.total} 项。</p>}
    {expanded&&<div className="coverage-chapters">{chapters.map((title,index)=>{
      const topics=review.topics.filter(t=>t.chapter===index+1),closed=topics.filter(t=>['confirmed','not_applicable','design_stage'].includes(t.state)).length;
      return <details className="coverage-chapter" key={title}><summary><ChevronRight size={13}/><span>{index+1}. {title}</span><span>{closed}/{topics.length}</span></summary><ul>{topics.map(topic=><li className={`coverage-topic coverage-${topic.state}`} key={topic.id}><div><button className="coverage-topic-title" type="button" onClick={()=>onTopic(topic)}>{topic.section} {topic.title}</button><span>{labels[topic.state]}</span></div><p>{topic.conclusion||`待核对：${topic.fields.join('、')}`}</p>{topic.source_quote&&<blockquote>依据：{topic.source_quote}</blockquote>}</li>)}</ul></details>;
    })}</div>}
    <div className={`review-readiness ${review.status}`}><CircleHelp size={15}/><p>{review.status==='approved'?'本版需求已审核，后续修改会重新核对。':review.status==='ready'?'当前产品要求与实际模块已整理好，请整体审阅并确认本版需求。':`${blockers.length?'待完成：'+blockers.join('；'):'正在整理本版需求'}。实现细节保留给后续设计。`}</p></div>
    <div className="coverage-actions"><button className="text-button" disabled={disabled||review.status==='approved'} onClick={onContinue}>继续完善缺口<ArrowRight size={12}/></button><button className="button secondary small approve-document" disabled={disabled||busy||review.status!=='ready'} onClick={onApprove} title={review.status==='draft'?'补齐缺口并处理未决事项后可审核':'请先阅读需求说明书'}><CheckCheck size={13}/>{review.status==='approved'?'已审核':'确认本版需求'}</button></div>
  </section>;
}
