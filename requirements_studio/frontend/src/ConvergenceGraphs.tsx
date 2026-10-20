import {useEffect,useMemo,useState} from 'react';
import {GitBranch} from 'lucide-react';
import {DiagramCanvas} from './DiagramCanvas';
import {closureGraph,impactGraph,solutionGraph} from './convergenceGraphs';
import {readSetting,saveSetting,type Project} from './types';

const views=[['solution','方案关系','需求 → 模块 → 器件，查看实际功能由谁承接。'],['impact','变更影响','查看器件调整前后，以及关联条款、模块和接口的同步情况。'],['closure','收敛检查','区分待客户决定、AI整理和已交给工程的事项。']] as const;
type View=typeof views[number][0];
export function ConvergenceGraphs({project,onDecision,onEvidence,onQuestion,onReview}:{project:Project;onDecision:(id:string)=>void;onEvidence?:(id:string)=>void;onQuestion?:(id:string)=>void;onReview:()=>void}){
 const key='convergence-graphs:'+project.id,stored=readSetting(key,'solution');
 const [view,setView]=useState<View>(views.some(v=>v[0]===stored)?stored as View:'solution'),[focus,setFocus]=useState(''),[edit,setEdit]=useState('');
 useEffect(()=>saveSetting(key,view),[key,view]);
 useEffect(()=>{if(focus&&!project.document.requirements.some(r=>r.id===focus&&r.state!=='not_applicable'))setFocus('');},[project.document.requirements,focus]);
 const history=project.canvas_sync?.history??[],change=history.find(c=>c.id===edit)??history.at(-1);
 const data=useMemo(()=>view==='impact'?impactGraph(project,change):view==='closure'?closureGraph(project):solutionGraph(project,focus),[project,view,change,focus]);
 function navigate(id:string){if(id==='review')onReview();else if(id.startsWith('question:'))onQuestion?.(id.slice(9));else if(id.startsWith('REF-'))onEvidence?.(id);else onDecision(id);}
 return <section className="convergence-graphs" aria-label="收敛关系图谱" data-graph-version={project.document.version}>
  <header><div><GitBranch size={16}/><h2>用关系图看清当前方案</h2></div><span>v{project.document.version} · {data.nodes.length} 个节点</span></header>
  <div className="graph-view-switch" role="tablist" aria-label="收敛图谱视角">{views.map(([id,label])=><button type="button" role="tab" key={id} aria-selected={view===id} onClick={()=>setView(id)}>{label}</button>)}</div>
  <p className="graph-caption">{views.find(v=>v[0]===view)?.[2]} 连线来自当前条目关联。先看全貌，放大或点击节点读详情。</p>
  {view==='solution'&&<label className="graph-filter">查看范围<select aria-label="图谱需求范围" value={focus} onChange={e=>setFocus(e.target.value)}><option value="">本版全部需求</option>{project.document.requirements.filter(r=>r.state!=='not_applicable').map(r=><option key={r.id} value={r.id}>{r.name}</option>)}</select></label>}
  {view==='impact'&&history.length>0&&<label className="graph-filter">器件调整<select aria-label="图谱变更记录" value={change?.id??''} onChange={e=>setEdit(e.target.value)}>{[...history].reverse().map(c=><option key={c.id} value={c.id}>v{c.created_version} · {c.reason}</option>)}</select></label>}
  <div className="graph-legend"><span className="confirmed">已明确方案</span><span className="customer">待客户决定</span><span className="ai">AI同步</span><span className="engineering">工程承接</span></div>
  <div data-knowledge-view={view}><DiagramCanvas overview graph={data} title={views.find(v=>v[0]===view)?.[1]+'图谱'} storageKey={project.id+':convergence:'+view} onDiscuss={navigate}/></div>
 </section>;
}
