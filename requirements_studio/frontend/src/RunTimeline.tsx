import {CheckCheck,ChevronRight,Clock3,Loader2} from 'lucide-react';
import {time,type RunActivity,type Project} from './types';

export function RunHistory({project,messageId}:{project:Project;messageId:string}){
  const runs=project.run_activity?.filter(r=>r.message_id===messageId&&!['queued','running'].includes(r.status))??[];
  if(!runs.length)return null;
  return <details className="run-history"><summary><ChevronRight size={12}/>查看本轮整理过程<span>{runs.some(r=>r.status==='completed')?'已保存':'未完成'}</span></summary>{runs.map(run=><div key={run.id} data-run-id={run.id}><small>{run.kind==='knowledge'?'器件资料核对':run.kind==='continuation'?'后续核对':'本轮分析'}</small><ol>{run.events.filter(e=>e.message).map(e=><li key={e.seq}>{e.at&&<time>{time(e.at)}</time>}<span>{e.message}</span></li>)}</ol></div>)}</details>;
}

export function LiveProgress({stage,elapsed,events}:{stage:string;elapsed:number;events:RunActivity['events']}){
  const steps=events.filter(e=>e.message&&e.phase!=='waiting').slice(-4);
  return <div className="run-progress live-analysis" role="status" aria-label="本轮分析进度"><Loader2 size={17} className="spin"/><div><strong>需求助手正在整理</strong>{!!steps.length&&<ol>{steps.map(e=><li key={e.seq}><CheckCheck size={12}/><span>{e.message}</span></li>)}</ol>}<p className="current-progress">{stage}</p><span><Clock3 size={11}/>已用 {elapsed<60?`${elapsed} 秒`:`${Math.floor(elapsed/60)} 分 ${elapsed%60} 秒`} · 可继续补充</span><small>这里展示实际处理进度与简短摘要，结果校验后会同步到画布。</small></div></div>;
}
