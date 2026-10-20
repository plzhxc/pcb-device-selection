import {active,type DecisionNode,type DeviceChange,type Project} from './types';
import type {DiagramEdge,DiagramGraph,DiagramNode} from './diagramGraph';

function factNode(node:DecisionNode,project:Project):DiagramNode{
 const isSync=project.canvas_sync?.requirement_ids.includes(node.id)||project.canvas_sync?.module_ids.includes(node.id);
 const ref=project.domain?.references.find(r=>r.id===node.id),candidate=project.delivery?.bom.current_rows?.find(r=>r.reference_id===node.id)?.status==='proposed';
 const status=isSync?'AI同步中':candidate?'候选待采用':ref?.intent==='must'?'必须使用':ref?.intent==='prefer'?'优先方案':node.status_label;
 const tone=isSync?'graph-ai':candidate||['waiting','deferred','proposed','tbd'].includes(node.status)?'graph-customer':node.status==='conflict'?'graph-conflict':node.status==='review'||node.status==='processing'?'graph-ai':node.status==='design_stage'?'graph-engineering':'graph-confirmed';
 return {id:node.id,title:node.title,badge:status,tone,compact:true,
  detail:node.conclusion+'\n\n'+node.facts.map(f=>f.label+'：'+f.value+(f.conditions?'；'+f.conditions:'')).join('\n')+(node.notes.length?'\n\n核对与边界：\n'+node.notes.join('\n'):''),
  requirementIds:node.requirement_ids,actionId:node.question_id?'question:'+node.question_id:node.id,actionLabel:node.kind==='device'?'打开当前器件与资料':'查看条目与依据'};
}
function edge(source:string,target:string,label:string):DiagramEdge{return {id:source+'>'+target+':'+label,source,target,label,arrow:true};}
function graph(nodes:DiagramNode[],edges:DiagramEdge[]):DiagramGraph{
 const ids=new Set(nodes.map(n=>n.id)),unique=new Map(edges.filter(e=>ids.has(e.source)&&ids.has(e.target)).map(e=>[e.id,e]));
 return {layout:'directed',direction:'LR',nodes,edges:[...unique.values()]};
}
export function solutionGraph(project:Project,focus=''):DiagramGraph{
 const rows=project.decision_map.nodes,reqs=rows.filter(n=>n.kind==='requirement'&&n.status!=='not_applicable'&&(!focus||n.id===focus));
 const mids=new Set(project.convergence.modules.filter(m=>m.state!=='not_applicable'&&(!focus||m.requirement_ids.includes(focus))).map(m=>m.id));
 const currentIds=new Set((project.delivery?.bom.current_rows??[]).map(r=>r.reference_id));
 const chosen=rows.filter(n=>n.kind==='module'&&mids.has(n.id)||n.kind==='device'&&currentIds.has(n.id)&&(!focus||n.requirement_ids.includes(focus)||n.module_ids.some(id=>mids.has(id))));
 const root:DiagramNode={id:'solution:root',title:'本版方案',badge:'v'+project.document.version+' · '+reqs.length+'项需求',detail:project.document.summary,tone:'root',actionId:'review',actionLabel:'查看需求说明书'};
 const nodes=[root,...reqs.map(n=>factNode(n,project)),...chosen.map(n=>factNode(n,project))],edges=reqs.map(n=>edge(root.id,n.id,'产品要求'));
 for(const module of project.convergence.modules.filter(m=>mids.has(m.id))){
  for(const rid of module.requirement_ids)edges.push(edge(rid,module.id,'功能承接'));
 }
 for(const ref of project.domain?.references??[]){
  if(!currentIds.has(ref.id))continue;
  for(const mid of ref.module_ids)edges.push(edge(mid,ref.id,ref.intent==='must'?'指定器件':ref.intent==='prefer'?'优先器件':'候选方案'));
  if(!ref.module_ids.some(id=>mids.has(id)))for(const rid of ref.requirement_ids)edges.push(edge(rid,ref.id,'器件条件'));
 }
 return graph(nodes,edges);
}
export function impactGraph(project:Project,change?:DeviceChange):DiagramGraph{
 if(!change)return graph([{id:'impact:empty',title:'尚无器件调整记录',badge:'当前方案',preview:'修改器件后，这里会展示相关需求、模块、接口和条款。',tone:'root'}],[]);
 const root:DiagramNode={id:'impact:'+change.id,title:change.reason,badge:change.status==='pending'?'AI正在同步':change.status==='superseded'?'已被后续决定替代':'本次调整已同步',tone:change.status==='pending'?'graph-ai':'root',
  detail:'调整前：'+String(change.before.mention||'未设置')+'\n调整后：'+String(change.after.mention||'未设置')+'\n本版用量：'+String(change.after.quantity??'待核定')+'\n\n'+(change.remaining.map(r=>r.label+(r.reason?'：'+r.reason:'')).join('\n')||'相关内容已核对。图中的条目显示当前保存版本，历史原话保留在变更记录中。'),actionId:change.reference_id,actionLabel:'打开当前器件与资料'};
 const selected=new Set([change.reference_id,...change.requirement_ids,...change.module_ids,...change.link_ids.map(id=>'interface:'+id)]);
 const related=project.decision_map.nodes.filter(n=>selected.has(n.id)&&n.status!=='not_applicable');
 const nodes=[root,...related.map(n=>factNode(n,project))],edges:DiagramEdge[]=[];
 for(const n of related){
  if(n.kind==='requirement')edges.push(edge(root.id,n.id,'相关需求'));
  else if(n.kind==='device')edges.push(edge(root.id,n.id,'当前器件'));
  else if(n.kind==='module')edges.push(edge(root.id,n.id,'相关模块'));
  else if(n.kind==='interface')for(const mid of n.module_ids)edges.push(edge(mid,n.id,'接口核对'));
 }
 for(const e of project.decision_map.edges){
  if(selected.has(e.source)&&selected.has(e.target)&&e.kind==='implemented_by')edges.push(edge(e.source,e.target,e.label));
 }
 const fields=project.specification.chapters.flatMap(c=>c.topics.flatMap(t=>t.fields)),topics=new Map<string,typeof fields>();
 for(const field of fields.filter(f=>change.field_ids.includes(f.id))){const key=field.topic_id;topics.set(key,[...(topics.get(key)??[]),field]);}
 for(const [id,items] of topics){
  const reviewing=items.some(f=>f.review_pending),key='impact:spec:'+id;
  nodes.push({id:key,title:'§ '+id.replace('V3-','')+' 相关条款',badge:items.length+'个字段 · '+(reviewing?'AI同步中':'已核对'),tone:reviewing?'graph-ai':'graph-confirmed',compact:true,
   detail:items.map(f=>f.label+'：'+(f.conclusion||f.content||f.status_label)).join('\n\n'),actionId:change.requirement_ids[0]||'review',actionLabel:'查看相关决定'});
  edges.push(edge(root.id,key,'规范条款'));
 }
 return graph(nodes,edges);
}
export function closureGraph(project:Project):DiagramGraph{
 const processing=active(project.latest_run)||!!project.analysis_pending||project.input_queue.length>0;
 const ready=!processing&&['ready','approved'].includes(project.review.status),nodes:DiagramNode[]=[{id:'closure:root',title:ready?'本版需求可交接':'本版需求继续收敛',badge:ready?'需求与承接已整理':'依据真实审核状态',tone:'root',detail:'需求可交接与完整电路生成准备分开。工程任务保留负责人和完成条件；有待答问题或AI同步时不能作为完成结果。',actionId:'review',actionLabel:'查看需求说明书'}],edges:DiagramEdge[]=[];
 for(const c of project.delivery?.checks??[]){
  const state=c.state==='complete'?'已整理':c.state==='design_task'?'工程承接':['needs_user','conflict'].includes(c.state)?'待产品取舍':'AI待整理',id='closure:check:'+c.key;
  nodes.push({id,title:c.label,badge:state,tone:c.state==='conflict'?'graph-conflict':c.state==='needs_user'?'graph-customer':c.state==='design_task'?'graph-engineering':c.state==='complete'?'graph-confirmed':'graph-ai',compact:true,detail:c.conclusion+(c.next_action?'\n\n下一步：'+c.next_action:''),actionId:'review',actionLabel:'查看说明书与交接检查'});
  edges.push(edge('closure:root',id,'交接检查'));
 }
 const customerNodes=project.decision_map.nodes.filter(n=>n.kind==='question'||n.kind==='module'&&project.convergence.module_pending.includes(n.id)||n.kind==='device'&&project.delivery?.bom.current_rows?.some(r=>r.reference_id===n.id&&r.status==='proposed'));
 if(customerNodes.length){
  nodes.push({id:'closure:customer',title:'待客户决定',badge:customerNodes.length+'项实际取舍',tone:'graph-customer',detail:'仅列当前待答问题、待采用模块或器件方案。'});edges.push(edge('closure:root','closure:customer','需要你的选择'));
  for(const n of customerNodes){nodes.push(factNode(n,project));edges.push(edge('closure:customer',n.id,'待决定'));}
 }
 if(project.review.internal_rechecks||processing){
  const id='closure:ai',changes=project.canvas_sync?.history.filter(c=>c.status==='pending')??[];
  const detail=[...changes.flatMap(c=>[c.reason,...c.remaining.map(r=>r.label+(r.reason?'：'+r.reason:''))]),...project.decision_map.nodes.filter(n=>n.status==='review').map(n=>n.title)].join('\n');
  nodes.push({id,title:'AI同步与关联核对',badge:processing?'正在处理输入':project.latest_run?.status==='failed'?'未完成，请重试':project.review.internal_rechecks+'项待整理',tone:'graph-ai',
   detail:detail||'正在依据新输入整理方案，未完成前保留当前保存版本。',actionId:changes[0]?.reference_id||'review',actionLabel:'查看相关内容'});
  edges.push(edge('closure:root',id,'助手继续整理'));
 }
 const owners=new Map<string,NonNullable<Project['delivery']>['engineering_tasks']>();
 for(const task of project.delivery?.engineering_tasks??[])owners.set(task.owner,[...(owners.get(task.owner)??[]),task]);
 for(const [owner,tasks] of owners){
  const id='closure:engineering:'+owner;nodes.push({id,title:owner,badge:tasks.length+'项工程任务',tone:'graph-engineering',compact:true,detail:tasks.map(t=>t.description+'\n完成条件：'+t.completion_condition).join('\n\n')});edges.push(edge('closure:root',id,'交工程实现'));
 }
 for(const issue of project.convergence.issues){
  const id='closure:issue:'+issue.id;nodes.push({id,title:issue.title,badge:issue.kind==='conflict'?'存在冲突':'待复核',tone:'graph-conflict',compact:true,detail:issue.description,actionId:issue.requirement_ids[0]||'review',actionLabel:'查看相关要求'});edges.push(edge('closure:root',id,'需要处理'));
 }
 return graph(nodes,edges);
}
