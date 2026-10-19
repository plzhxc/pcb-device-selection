import {useRef,useState,type FormEvent} from 'react';
import {ArrowRight,CheckCheck,FileUp,Loader2,Download,ChevronDown} from 'lucide-react';
import {api,active,type Project,type ExternalInterface} from './types';

export function ReferenceUpload({project,onSaved}:{project:Project;onSaved:(p:Project)=>void}){
 const input=useRef<HTMLInputElement>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 async function upload(file:File){if(busy)return;setError('');if(file.size>2*1024*1024){setError('参考文件需在 2 MiB 以内。');return;}setBusy(true);try{
  const encoded=await new Promise<string>((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(String(r.result).split(',')[1]);r.onerror=()=>reject(Error('读取附件失败'));r.readAsDataURL(file);});
  const p=await api<Project>(`/projects/${project.id}/reference-designs`,{method:'POST',body:JSON.stringify({filename:file.name,content_base64:encoded,request_id:crypto.randomUUID()})});onSaved(p);
 }catch(e){setError((e as Error).message);}finally{setBusy(false);if(input.current)input.current.value='';}}
 return <div className="reference-upload"><input ref={input} className="reference-file-input" type="file" accept=".enet,.csv,.json" aria-label="上传参考网表或BOM" hidden onChange={e=>{const f=e.target.files?.[0];if(f)void upload(f);}}/><button className="text-button" disabled={busy} onClick={()=>input.current?.click()}>{busy?<Loader2 size={13} className="spin"/>:<FileUp size={13}/>} {busy?'正在读取参考资料':'添加参考网表 / BOM'}</button>{error&&<span className="inline-error" role="alert">{error}</span>}</div>;
}

export function DeliveryStatus({project,onModules}:{project:Project;onModules?:()=>void}){
 const d=project.delivery;if(!d)return null;
 const directCore=project.domain?.references.filter(r=>r.module_ids.length&&['must','prefer'].includes(r.intent)).length??0,coreLabel=d.bom.core_count?`${d.bom.core_adopted} 项`:directCore?`${directCore} 项`:(d.checks.some(c=>c.key==='core_parts'&&['complete','design_task'].includes(c.state))?'方案已整理':'持续整理');
 const referenceOnly=d.bom.core_count-d.bom.core_adopted-d.bom.proposed_count;
 return <section className="delivery-status" aria-label="本版下一步"><div><CheckCheck size={16}/><strong>{project.latest_run?.status==='queued'&&project.latest_run.kind==='continuation'&&project.document.questions.length?'可以先回答，助手稍后继续整理':d.next_label}</strong></div><dl><div><dt>待你决定</dt><dd>{d.user_decisions}</dd></div><div><dt>助手整理</dt><dd>{d.internal_tasks}</dd></div><div><dt>{d.bom.core_count?'核心采用':'核心方案'}</dt><dd>{coreLabel}</dd></div></dl>{d.bom.core_count>d.bom.core_adopted&&<p className="domain-caption">已提取 {d.bom.core_count} 项参考核心器件{d.bom.proposed_count>0?`，${d.bom.proposed_count} 项建议待采用`:''}{referenceOnly>0?`，${referenceOnly} 项仅作参考`:''}。</p>}{(d.bom.proposed_count>0||project.convergence.module_pending.length>0)&&onModules&&<button className="text-button" onClick={onModules}>{d.bom.proposed_count>0?'查看核心器件方案':'查看待采用的模块方案'}<ArrowRight size={12}/></button>}
 {!!d.checks.length&&<details className="delivery-checks"><summary>功能、能力链与交接检查<ChevronDown size={12}/></summary>{d.checks.map(c=><div key={c.key}><strong>{c.label} <small>{({complete:'已整理',design_task:'设计承接',needs_user:'待选择',review:'复核中',missing:'待整理',conflict:'存在冲突'} as Record<string,string>)[c.state]??c.state}</small></strong><p>{c.conclusion}</p>{c.next_action&&<small>{c.next_action}</small>}</div>)}</details>}
 {!!d.engineering_tasks.length&&<details className="delivery-engineering"><summary>后续设计待办 · {d.engineering_tasks.length} 项</summary><p>以下由设计与选型阶段承接，不是要求你再填一份问卷。</p><ul>{d.engineering_tasks.map(t=><li key={t.id}><strong>{t.owner}</strong><p>{t.description}</p><small>完成条件：{t.completion_condition}</small></li>)}</ul></details>}
 </section>;
}

export function ReferencePlan({project,onSaved}:{project:Project;onSaved:(p:Project)=>void}){
 const b=project.delivery?.bom,[all,setAll]=useState(false),[sending,setSending]=useState(false),[error,setError]=useState('');if(!b?.sources.length)return null;
 async function adopt(source:string){if(sending)return;setSending(true);setError('');try{onSaved(await api<Project>(`/projects/${project.id}/reference-designs/adopt`,{method:'POST',body:JSON.stringify({source_id:source,base_version:project.document.version,request_id:crypto.randomUUID()})}));}catch(e){setError((e as Error).message);}finally{setSending(false);}}
 const rows=all?b.rows:b.rows.filter(r=>r.core);
 return <section className="reference-plan" aria-label="参考设计与核心BOM"><div className="section-heading"><div><h2>核心器件方案与参考 BOM</h2><p>先看本版用途，再采用方案。参考数量与本版数量分别记录。</p></div><a className="text-button" href={`/api/projects/${project.id}/reference-bom.csv`} download><Download size={13}/>导出 BOM</a></div>
 {b.sources.map(s=><div className="reference-source" key={s.id}><strong>{s.filename}</strong><a className="text-button" href={`/api/projects/${project.id}/reference-designs/${s.id}`} download>下载原参考</a><small>{s.instance_count} 个参考实例 · {s.warnings.join('；')||'数量来自文件观测，电气适用性待核验'}</small>{b.rows.some(r=>r.source_id===s.id&&r.core&&r.status==='proposed')&&<button className="button primary small adopt-reference-core" disabled={sending||(active(project.latest_run)&&!(project.latest_run?.status==='queued'&&project.latest_run.kind==='continuation'))} onClick={()=>void adopt(s.id)}>{sending?'已收到，正在整理':'采用已展示的核心器件方案'}</button>}</div>)}
 <div className="reference-table-wrap"><table className="reference-bom"><thead><tr><th>器件 / 位号</th><th>采购号 / 封装</th><th>参考数 / 本版数</th><th>本版用途</th><th>采用状态</th></tr></thead><tbody>{rows.map((r,i)=><tr key={r.source_id+r.key+i}><td><strong>{r.model}</strong><small>{r.designators.join(', ')}</small></td><td>{r.supplier_part||'采购身份待核对'}<small>{r.package||'封装待补'}</small></td><td>{r.observed_quantity} / {r.quantity??'未采用'}<small>{r.quantity_basis}</small></td><td>{r.purpose||'等待结合本版功能分析'}</td><td>{r.status==='adopted'?'本版已采用':r.status==='proposed'?'建议，待采用':'仅作参考'}</td></tr>)}</tbody></table></div>
 <button className="text-button" onClick={()=>setAll(v=>!v)}>{all?'只看核心器件':`展开完整参考 BOM · ${b.rows.length} 行`}<ChevronDown size={13}/></button>{error&&<p className="inline-error">{error}</p>}
 </section>;
}

const interfaceFields=[['purpose','用途'],['external_object','外接对象'],['protocol','协议'],['signalText','信号'],['voltage','电平/供电'],['shared_link','共用关系']] as const;
type InterfaceField=typeof interfaceFields[number][0];
function InterfaceRow({row,project,onSaved}:{row:ExternalInterface;project:Project;onSaved:(p:Project)=>void}){
 const [draft,setDraft]=useState<Partial<Record<InterfaceField,string>>>({}),[sending,setSending]=useState(false),[error,setError]=useState('');
 const lock=useRef(false),details=useRef<HTMLDetailsElement>(null);
 const current=(key:InterfaceField)=>key==='signalText'?row.signals.join(', '):row[key];
 const changed=interfaceFields.filter(([key])=>draft[key]!==undefined&&draft[key]!==current(key));
 const marker=`只修改外部接口“${row.name}”`;
 const busy=active(project.latest_run)||!!project.input_queue.length;
 const pending=project.input_queue.some(q=>q.text.startsWith(marker))||!!(project.analysis_pending&&project.messages.some(m=>m.id===project.analysis_pending?.message_id&&m.content.startsWith(marker)));
 async function save(e:FormEvent){e.preventDefault();if(lock.current||busy||!changed.length)return;lock.current=true;setSending(true);setError('');try{
  const text=marker+'这一行，仅调整下列字段，其他已知内容沿用：\n'+changed.map(([key,label])=>label+'：'+(draft[key]||'本字段暂不锁定，由设计核定；不表示取消功能')).join('\n');
  const result=await api<{project:Project}>(`/projects/${project.id}/messages`,{method:'POST',body:JSON.stringify({text,request_id:crypto.randomUUID(),answers:[]})});setDraft({});if(details.current)details.current.open=false;onSaved(result.project);
 }catch(e){setError((e as Error).message);}finally{lock.current=false;setSending(false);}}
 return <tr data-interface-id={row.id}><td><strong>{row.name}</strong><small>{row.state==='confirmed'?'本版要求':row.state==='proposed'?'参考建议':'实现待设计'}</small></td><td>{row.purpose}<small>{row.external_object}</small></td><td>{row.protocol}<small>{row.signals.join(' / ')}</small></td><td>{row.voltage||'电气条件待设计'}<small>{row.shared_link}</small>{pending&&<small role="status">这一行修改已收到，正在整理</small>}<details ref={details}><summary>调整这一行</summary><form className="interface-edit" onSubmit={e=>void save(e)}>{interfaceFields.map(([key,label])=><label key={key}>{label}<input name={key} disabled={sending||busy} value={draft[key]??current(key)} onChange={e=>{const value=e.target.value;setDraft(v=>({...v,[key]:value}));}}/></label>)}<button className="button secondary small" disabled={sending||busy||!changed.length}>{sending?'正在提交':pending?'已收到，正在整理':busy?'等待本轮整理完成':'发送这一行修改'}</button>{error&&<p role="alert">{error}</p>}</form></details></td></tr>;
}
export function InterfaceTable({project,onSaved}:{project:Project;onSaved:(p:Project)=>void}){
 const rows=project.document.external_interfaces??[];if(!rows.length)return null;
 return <section className="interface-contracts"><h2>外部接口与连接位置</h2><p>连接位置、独立链路和复用关系分别说明；可单独修改一行。</p><div className="reference-table-wrap"><table><thead><tr><th>位置</th><th>用途 / 外部对象</th><th>协议 / 信号</th><th>电平 / 共用关系</th></tr></thead><tbody>{rows.map(r=><InterfaceRow key={r.id} row={r} project={project} onSaved={onSaved}/>)}</tbody></table></div></section>;
}
