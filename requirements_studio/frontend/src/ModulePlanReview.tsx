import {ArrowRight,GitBranch} from 'lucide-react';
import type {Project} from './types';

export function ModulePlanReview({project,busy,disabled,onConfirm,onInspect,compact=false}:{project:Project;busy:boolean;disabled:boolean;onConfirm:(text:string)=>Promise<boolean>;onInspect:()=>void;compact?:boolean}){
  const all=project.convergence.modules.filter(m=>project.convergence.module_pending.includes(m.id));
  const pending:typeof all=[];
  const line=(m:typeof all[number])=>`- ${m.name}：${m.category_name} / ${m.type_name}，${m.quantity}路功能电路；用途：${m.purpose}。`;
  for(const module of all){if(pending.length>=8||pending.length&&[...pending,module].map(line).join('\n').length>2400)break;pending.push(module);}
  if(!pending.length||project.document.questions.length||project.document.questions_paused)return null;
  const text='确认本版功能模块方案：\n'+pending.map(line).join('\n')+'\n我采用上述功能类别、用途、数量及已展示的适用条件；条件候选仅在条件成立时实施，不因此强制增加独立芯片，不同功能可以共用器件。具体器件、拓扑、真实引脚、PCB与接口实现委托设计阶段确定，必须满足此前已明确的产品要求和使用边界；这不是电路已验证。';
  return <section className={`module-plan-review ${compact?'compact':''}`} aria-label="确认功能模块方案"><header><GitBranch size={16}/><strong>这组功能模块适合你的产品吗？</strong></header><p>确认功能类别、用途和数量。条件候选仅在需要时实施；模块数量不等于芯片数量。具体器件与连接留待设计，如需调整，直接在对话中说明。</p>{!compact&&<ul>{pending.map(m=><li key={m.id}><strong>{m.name}<small>×{m.quantity}</small></strong><span>{m.type_name}</span><p>{m.purpose}</p></li>)}</ul>}<footer><button type="button" className="text-button" onClick={onInspect}>查看方案详情</button><button type="button" className="button primary small confirm-module-plan" disabled={disabled||busy||!!project.input_queue.length} onClick={()=>void onConfirm(text)}>采用这组模块方案<ArrowRight size={13}/></button></footer>{busy&&<small>正在整理本轮变化，方案更新后可确认。</small>}</section>;
}
