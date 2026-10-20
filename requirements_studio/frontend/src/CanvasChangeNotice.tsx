import {ArrowRight,CheckCheck,GitBranch,Loader2} from 'lucide-react';
import type {Project} from './types';
import './canvas-sync.css';

export function CanvasChangeNotice({project,onOpen,onSource}:{project:Project;onOpen?:()=>void;onSource?:(id:string)=>void}){
 const sync=project.canvas_sync,last=sync?.latest;
 const queued=project.reference_submissions?.filter(a=>a.operation==='revise_reference'&&a.status==='queued')??[];
 if(!last&&!queued.length)return null;
 const pending=!!sync?.pending_count||queued.length>0;
 const running=['queued','running'].includes(project.latest_run?.status??'')||project.input_queue.length>0;
 const failed=pending&&!running&&['failed','interrupted','cancelled'].includes(project.latest_run?.status??'');
 const reqs=project.document.requirements.filter(r=>sync?.requirement_ids.includes(r.id));
 const modules=project.convergence.modules.filter(m=>sync?.module_ids.includes(m.id));
 return <section className={'canvas-change-notice '+(pending?'pending':'synced')} aria-label="器件调整同步状态" data-sync-state={pending?'pending':'synchronized'}>
  <header>{pending||running?<Loader2 size={15} className={running?'spin':''}/>:<CheckCheck size={15}/>}<strong>{queued.length?'器件修改已排队，轮到后会更新关联内容':failed?'器件修改已保存，同步未完成，请重试':pending?(running?'已保存器件修改，正在同步关联画布':'器件修改已保存，关联内容尚待整理'):running?'器件决定已保存，本轮讨论正在更新关联内容':'最新器件调整已同步'}</strong><span>v{project.document.version}</span></header>
  {last&&<p>{last.reason}。{pending?(failed?'已保存的修改和关联复核项都保留；可在对话中重试本轮分析。':'相关正文与图形仍需核对，已保存内容保留。'):running?'上次调整已整理，本轮分析结束后以最新保存版本为准。':'说明书、模块和本版器件方案已按当前决定整理，资料适用性另行核验。'}</p>}
  {!!queued.length&&<p>{queued.length} 项新修改等待处理；队列中的内容尚未作为已生效方案展示。</p>}
  {last&&<details><summary><GitBranch size={12}/>查看修改与影响{pending?' · '+reqs.length+' 项需求 / '+modules.length+' 个模块':''}</summary>
   <dl><div><dt>调整前</dt><dd>{String(last.before.mention||'未设置')} · {String(last.before.quantity??'用量待定')}</dd></div><div><dt>当前决定</dt><dd>{String(last.after.mention||'未设置')} · {String(last.after.quantity??'用量待定')}</dd></div></dl>
   {!!reqs.length&&<p>相关需求：{reqs.map(r=>r.name).join('、')}</p>}
   {!!modules.length&&<p>相关模块：{modules.map(m=>m.name).join('、')}</p>}
   {!!last.remaining.length&&<ul>{last.remaining.slice(0,8).map((r,i)=><li key={i}>{r.label}{r.reason?'：'+r.reason:''}</li>)}</ul>}
   {onSource&&<button className="text-button" onClick={()=>onSource(last.source_message_id)}>查看修改依据<ArrowRight size={12}/></button>}
  </details>}
  {onOpen&&<button className="text-button" onClick={onOpen}>查看变更影响与收敛图谱<ArrowRight size={12}/></button>}
 </section>;
}
