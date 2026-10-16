import { Children, isValidElement, useCallback, useEffect, useRef, useState } from 'react';
import type { CSSProperties, FormEvent, ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  ArrowDown, ArrowDownToLine, ArrowRight, ArrowUp, BookOpen, Check, CheckCheck,
  ChevronDown, ChevronRight, CircleHelp, Clock3, Download, FileText, Folder,
  GitBranch, History, Layers3, Leaf, Loader2, LockKeyhole, Maximize2,
  MessageSquare, Minimize2, PanelLeftClose, PanelLeftOpen,
  PanelRightClose, PanelRightOpen, Pencil, Plus, Search, Sparkles, Square,
  X, AlertCircle, CircleDot, CheckCircle2, Cpu, ArrowUpRight, Boxes,
} from 'lucide-react';
import { Diagrams, DocumentDiagram } from './Diagrams';
import { QuestionCards } from './QuestionCards';
import { CoveragePanel } from './CoveragePanel';
import { ConvergenceBoard } from './ConvergenceBoard';
import { ModuleBoard } from './ModuleBoard';
import {RunHistory,LiveProgress} from './RunTimeline';
import {ModulePlanReview} from './ModulePlanReview';
import {DeviceEvidencePanel,DeviceChips,DomainSummary} from './DeviceEvidencePanel';
import {HandoffDialog} from './HandoffDialog';
import './phase2.css';
import './domain.css';
import { active, api, categories, readSetting, saveSetting, states, time } from './types';
import type { Category, Document, Project, ProjectSummary, QuestionAnswer, Requirement, State,RunActivity } from './types';

function splitLimit(handle: HTMLElement) { const width=handle.parentElement?.clientWidth??1200; return Math.max(360,Math.min(650,width-361,width*.65)); }

function Markdown({ text, documentView=false, diagramDocument, diagramScope='', onDiagramSelect }: { text: string; documentView?: boolean; diagramDocument?: Document; diagramScope?: string; onDiagramSelect?: (id:string)=>void }) {
  return <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{
    pre:({children})=>{
      const code=Children.toArray(children)[0];
      if(documentView&&diagramDocument&&isValidElement<{className?:string;children?:ReactNode}>(code)&&code.props.className?.split(/\s+/).includes('language-mermaid')){
        return <DocumentDiagram source={String(code.props.children??'').trimEnd()} document={diagramDocument} storageKey={diagramScope} onSelect={onDiagramSelect}/>;
      }
      return <pre>{children}</pre>;
    },
    p:({children})=>{
      const parts=Children.toArray(children),plain=parts.every(part=>typeof part==='string')?parts.join(''):'';
      const fields=plain.split(/[|\n]/).map(field=>field.trim()).filter(Boolean);
      if(!documentView||!plain.startsWith('版本：')||!plain.includes('生成日期：')||!fields.every(field=>/^(版本|状态|生成日期|更新时间)：/.test(field)))return <p>{children}</p>;
      return <p className="document-metadata">{fields.map((field,index)=>{
        const match=field.match(/^(生成日期|更新时间)：(.+)$/),date=match?new Date(match[2].trim()):null;
        return <span key={index}>{match&&date&&!Number.isNaN(date.getTime())?<>{match[1]}：<time dateTime={match[2].trim()} title={match[2].trim()}>{date.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZoneName:'short'})}</time></>:field}</span>;
      })}</p>;
    },
    td:({children})=><td>{Children.map(children,child=>typeof child==='string'?child.split(/<br\s*\/?>/gi).flatMap((part,i)=>i?[<br key={i}/>,part]:[part]):child)}</td>
  }}>{text}</ReactMarkdown></div>;
}
function BrandMark({ small = false }: { small?: boolean }) { return <span className={`brand-mark ${small ? 'small' : ''}`} aria-hidden="true"><GitBranch size={small ? 15 : 19} strokeWidth={1.8}/></span>; }
function Status({ state }: { state: State }) { return <span className={`status state-${state}`}><span/>{states[state]}</span>; }
function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const el = ref.current; el?.showModal(); return () => { el?.close(); }; }, []);
  return <dialog ref={ref} className="dialog" onCancel={e => { e.preventDefault(); onClose(); }} onClick={e => { if (e.target === e.currentTarget) onClose(); }}><div className="dialog-heading"><h2>{title}</h2><button className="icon-button" aria-label="关闭弹窗" onClick={onClose}><X size={18}/></button></div>{children}</dialog>;
}

function RequirementEditor({ requirement, project, onClose, onSaved }: { requirement: Requirement | null; project: Project; onClose: () => void; onSaved: (project: Project) => void }) {
  const [name, setName] = useState(requirement?.name ?? '');
  const [value, setValue] = useState(requirement?.value ?? '');
  const [unit, setUnit] = useState(requirement?.unit ?? '');
  const [category, setCategory] = useState<Category>(requirement?.category ?? 'functions');
  const [implementation,setImplementation]=useState(requirement?.implementation??'hardware');
  const [priority, setPriority] = useState(requirement?.priority ?? 'must');
  const [state, setState] = useState<State>(requirement?.state ?? 'stated');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  async function save(e: FormEvent) {
    e.preventDefault(); if (!name.trim() || saving) return;
    setSaving(true); setError('');
    try {
      const updated = await api<Project>(`/projects/${project.id}/requirements`, { method: 'PATCH', body: JSON.stringify({ base_version: project.document.version, id: requirement?.id ?? '', name: name.trim(), value: value.trim(), unit: unit.trim(), category, priority, state,implementation }) });
      onSaved(updated); onClose();
    } catch (e) { setError((e as Error).message); } finally { setSaving(false); }
  }
  return <Dialog title={requirement ? `编辑需求 · ${requirement.id}` : '添加一项需求'} onClose={() => { if (!saving) onClose(); }}>
    <form onSubmit={save} className="editor-form">
      <p className="muted">保存后会记录新版本，并同步到说明书和设计图。</p>
      <label>需求名称<input autoFocus required maxLength={160} value={name} onChange={e => setName(e.target.value)} placeholder="例如：续航目标"/></label>
      <label>需求内容<textarea rows={3} maxLength={3000} value={value} onChange={e => setValue(e.target.value)} placeholder="例如：至少 30 天"/></label>
      <div className="form-grid"><label>所属章节<select value={category} onChange={e => setCategory(e.target.value as Category)}>{Object.entries(categories).map(([k,v]) => <option key={k} value={k}>{v}</option>)}</select></label><label>单位<input maxLength={40} value={unit} onChange={e => setUnit(e.target.value)} placeholder="天、V、元……"/></label></div>
      {category==='functions'&&<label>实现责任<select aria-label="需求实现责任" value={implementation} onChange={e=>setImplementation(e.target.value as typeof implementation)}><option value="hardware">硬件电路功能</option><option value="software">软件功能</option><option value="service">外部服务</option><option value="system">系统级目标</option></select></label>}
      <div className="form-grid"><label>优先级<select value={priority} onChange={e => setPriority(e.target.value as Requirement['priority'])}><option value="must">必须实现</option><option value="should">重要</option><option value="could">可选</option></select></label><label>当前状态<select value={state} onChange={e => setState(e.target.value as State)}>{Object.entries(states).map(([k,v]) => <option key={k} value={k}>{v}</option>)}</select></label></div>
      {requirement?.source_quote && <div className="source-preview"><span>原始依据</span><p>“{requirement.source_quote}”</p></div>}
      {error && <div className="inline-error" role="alert"><AlertCircle size={15}/>{error}</div>}
      <div className="dialog-actions"><button type="button" className="button secondary" onClick={onClose} disabled={saving}>取消</button><button className="button primary" disabled={saving || !name.trim()}>{saving ? <Loader2 className="spin" size={15}/> : <Check size={15}/>}保存需求</button></div>
    </form>
  </Dialog>;
}

export function App() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [project, setProjectState] = useState<Project | null>(null);
  const selected = useRef('');
  const loadSequence = useRef(0);
  const setProject=useCallback((incoming:Project|null)=>setProjectState(current=>{
    if(!incoming||!current||incoming.id!==current.id)return incoming;
    if(incoming.document.version<current.document.version)return current;
    if(incoming.document.version===current.document.version&&(incoming.domain_revision??0)<(current.domain_revision??0))return current;
    if(incoming.document.version===current.document.version&&current.latest_run){
      if(!incoming.latest_run||incoming.latest_run.created_at<current.latest_run.created_at)return current;
      if(incoming.latest_run.id===current.latest_run.id&&!active(current.latest_run)&&active(incoming.latest_run))return current;
    }
    return incoming;
  }),[]);
  const acceptMutation=useCallback((item:Project)=>{loadSequence.current++;setProject(item);},[setProject]);
  const sendLock = useRef(false);
  const [health, setHealth] = useState<{ cli_available: boolean; model_label: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [search, setSearch] = useState('');
  const [sidebar, setSidebar] = useState(readSetting('sidebar', 'open') === 'open');
  const [canvasOpen, setCanvasOpen] = useState(true);
  const [canvasFocus, setCanvasFocus] = useState(false);
  const [mobilePane, setMobilePane] = useState<'chat'|'canvas'>('chat');
  const [tab, setTab] = useState<'overview'|'document'|'history'|'convergence'|'modules'>('overview');
  const [devicePanel,setDevicePanel]=useState<{referenceId?:string;moduleId?:string;requirementId?:string}|null>(null);
  const [handoffOpen,setHandoffOpen]=useState(false);
  const canvasTop=useRef(0);
  function openDevices(options:{referenceId?:string;moduleId?:string;requirementId?:string}={}){canvasTop.current=document.querySelector('.canvas-scroll')?.scrollTop??0;setDevicePanel(options);setCanvasOpen(true);setMobilePane('canvas');requestAnimationFrame(()=>{const el=document.querySelector('.canvas-scroll');if(el)el.scrollTop=0;});}
  function closeDevices(){setDevicePanel(null);requestAnimationFrame(()=>{const el=document.querySelector('.canvas-scroll');if(el)el.scrollTop=canvasTop.current;});}
  useEffect(()=>{document.querySelector('.canvas-tabs [aria-selected="true"]')?.scrollIntoView({block:'nearest',inline:'nearest'});},[tab]);
  const [chatWidth, setChatWidth] = useState(Math.min(650, Math.max(360, Number(readSetting('chat-width', '480')) || 480)));
  const [draft, setDraft] = useState('');
  const [context, setContext] = useState<Requirement | null>(null);
  const [stage, setStage] = useState('正在理解你的需求。');
  const [liveEvents,setLiveEvents]=useState<RunActivity['events']>([]);
  const [elapsed, setElapsed] = useState(0);
  const [sending, setSending] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const exportRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState<Requirement | null | undefined>(undefined);
  const [renaming, setRenaming] = useState(false);
  const [newTitle, setNewTitle] = useState('');
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState('');
  const composer = useRef<HTMLTextAreaElement>(null);
  const chatScroll = useRef<HTMLDivElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const nearBottom = useRef(true);
  const [showJump, setShowJump] = useState(false);
  const busy = active(project?.latest_run);
  const doc = project?.document;

  const refreshList = useCallback(async () => { const list = await api<ProjectSummary[]>('/projects'); setProjects(current=>list.map(item=>{const old=current.find(p=>p.id===item.id);return old&&old.version>item.version?old:item;})); }, []);
  const loadProject = useCallback(async (id: string, switching = false) => {
    if (!switching && selected.current !== id) return;
    const sequence = ++loadSequence.current;
    if (switching) {
      setDevicePanel(null);setHandoffOpen(false);
      selected.current = id;
      const url=new URL(window.location.href);url.searchParams.set('project',id);window.history.replaceState(null,'',url);
      saveSetting('project', id);
      setLoading(true); setProject(null); setContext(null); setEditing(undefined); setError('');
      setDraft(readSetting(`draft:${id}`, '')); nearBottom.current = true;
    }
    try {
      const item = await api<Project>(`/projects/${id}`);
      if (selected.current === id && sequence === loadSequence.current) {
        setProject(item);
        setProjects(list=>list.map(p=>p.id===id?{...p,title:item.title,version:item.document.version,requirement_count:item.document.requirements.length,updated_at:item.updated_at}:p));
      }
    } finally { if (sequence === loadSequence.current) setLoading(false); }
  }, []);

  async function bootstrap() {
    setError(''); setLoading(true);
    try {
      const [list, status] = await Promise.all([api<ProjectSummary[]>('/projects'), api<{ cli_available: boolean; model_label: string }>('/health')]);
      setHealth(status); setProjects(list);
      if (list.length) {
        const saved = new URL(window.location.href).searchParams.get('project') || readSetting('project', '');
        await loadProject(list.find(p => p.id === saved)?.id ?? list[0].id, true);
      } else {
        const item = await api<Project>('/projects', { method: 'POST', body: '{}' });
        await refreshList(); await loadProject(item.id, true);
      }
    } catch (e) { setError((e as Error).message); setLoading(false); }
  }
  useEffect(() => { void bootstrap(); }, []);
  useEffect(() => {
    const media=window.matchMedia('(max-width: 759px)');
    const update=()=>{if(media.matches)setSidebar(false);};
    update(); media.addEventListener('change',update);
    return ()=>media.removeEventListener('change',update);
  }, []);
  useEffect(() => { saveSetting('sidebar', sidebar ? 'open' : 'closed'); }, [sidebar]);
  useEffect(() => { saveSetting('chat-width', String(chatWidth)); }, [chatWidth]);
  useEffect(() => { if (selected.current) saveSetting(`draft:${selected.current}`, draft); }, [draft]);
  useEffect(() => { if (!toast) return; const timer = setTimeout(() => setToast(''), 3500); return () => clearTimeout(timer); }, [toast]);
  useEffect(() => {
    function outside(e: MouseEvent) { if (exportRef.current && !exportRef.current.contains(e.target as Node)) setExportOpen(false); }
    function escape(e: KeyboardEvent) { if (e.key === 'Escape') { setExportOpen(false); if (window.innerWidth < 760) setSidebar(false); } }
    document.addEventListener('click', outside); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('click', outside); document.removeEventListener('keydown', escape); };
  }, []);
  useEffect(() => {
    const el = composer.current;
    if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 180)}px`; }
  }, [draft]);
  useEffect(() => {
    if (nearBottom.current) bottom.current?.scrollIntoView({ block: 'end', behavior: 'instant' as ScrollBehavior });
  }, [project?.messages.length, project?.document.version, busy, loading]);
  useEffect(() => {
    if (!busy || !project?.latest_run) return;
    const run = project.latest_run, id = project.id;
    setLiveEvents(project.run_activity?.find(r=>r.id===run.id)?.events??[]);
    setStage(run.kind==='continuation'?'继续整理独立部分，你可以随时回答。':run.status === 'queued' ? '已收到需求，准备开始分析。' : '正在整理需求与关键问题。');
    const updateTime = () => setElapsed(Math.max(0, Math.floor((Date.now() - new Date(run.created_at).getTime()) / 1000)));
    updateTime(); const timer = setInterval(updateTime, 1000);
    const source = new EventSource(`/api/runs/${run.id}/events`);
    source.onmessage = e => {
      if(selected.current!==id)return;
      try {
        const data = JSON.parse(e.data);
        if(data.kind==='reconnecting')setStage(data.message);
        if (['stage','progress'].includes(data.kind)) {setStage(data.message);const seq=Number(e.lastEventId);setLiveEvents(old=>old.some(item=>item.seq===seq)?old:[...old,{...data,seq}]);}
        if (data.kind === 'queue') void loadProject(id).catch(() => {});
        if (['completed', 'failed', 'cancelled', 'interrupted'].includes(data.kind)) {
          source.close();
          void Promise.all([loadProject(id), refreshList()]).catch(e => setError(e.message));
          if (data.kind === 'completed') setToast('需求文档已更新并保存');
        }
      } catch { setStage('正在恢复更新状态。'); }
    };
    source.onerror = () => setStage('连接恢复中，后台分析仍会继续。');
    const poll = setInterval(() => {
      const sequence=loadSequence.current;
      void api<Project>(`/projects/${id}`).then(item => {
        if (selected.current === id && sequence===loadSequence.current) {if(item.latest_run?.id!==run.id || !active(item.latest_run))source.close();setProject(item);if(!active(item.latest_run))void refreshList().catch(() => {});}
      }).catch(() => {});
    }, 3000);
    return () => { source.close(); clearInterval(timer); clearInterval(poll); };
  }, [project?.id, project?.latest_run?.id, busy, loadProject, refreshList]);
  useEffect(() => {
    if (!project || busy) return;
    const id=project.id;
    const refresh=()=>{void loadProject(id).catch(()=>{});};
    const timer=setInterval(refresh,project.domain?.active_jobs?1800:5000);
    window.addEventListener('focus',refresh);
    return ()=>{clearInterval(timer);window.removeEventListener('focus',refresh);};
  }, [project?.id,busy,!!project?.domain?.active_jobs,loadProject]);

  async function createProject(example = false) {
    if (sending || loading) return;
    setSending(true); setLoading(true); setError('');
    try {
      const item = await api<Project>('/projects', { method:'POST', body: JSON.stringify({example}) });
      await refreshList(); await loadProject(item.id, true); setTab('overview'); setMobilePane('chat');
      if (window.innerWidth < 760) setSidebar(false);
    } catch(e) { setError((e as Error).message); } finally { setSending(false); setLoading(false); }
  }
  async function send(text = draft, answers: QuestionAnswer[] = [], fromComposer = true): Promise<boolean> {
    if (!project || sendLock.current || !text.trim()) return false;
    const id = project.id;
    sendLock.current = true; setSending(true); setError(''); nearBottom.current = true;
    const content = context && fromComposer ? `针对 ${context.id}「${context.name}」：\n${text.trim()}` : text.trim();
    const signature=JSON.stringify({content,answers});
    let request:{signature:string;id:string};
    try{request=JSON.parse(readSetting(`request:${id}`,'{}'));}catch{request={signature:'',id:''};}
    if(request.signature!==signature||!request.id)request={signature,id:crypto.randomUUID()};
    saveSetting(`request:${id}`,JSON.stringify(request));
    try {
      const result = await api<{ project: Project; queued:boolean }>(`/projects/${id}/messages`, { method:'POST', body: JSON.stringify({ text:content, request_id:request.id, answers, after_run_id:busy?project.latest_run?.id:null }) });
      if (selected.current === id) { acceptMutation(result.project); if(fromComposer){setDraft(current=>current===text?'':current);setContext(null);} if(result.queued)setToast('已加入待处理补充，分析会按顺序继续'); }
      if(fromComposer&&readSetting(`draft:${id}`,'')===text)saveSetting(`draft:${id}`, '');
      saveSetting(`request:${id}`,'{}'); await refreshList();
      return true;
    } catch(e) { setError((e as Error).message); return false; } finally { setSending(false); sendLock.current = false; }
  }
  async function retry() {
    if (!project || sending) return; const id=project.id;
    setSending(true); setError(''); nearBottom.current=true;
    try { const result=await api<{project:Project}>(`/projects/${id}/retry`,{method:'POST',body:JSON.stringify({request_id:crypto.randomUUID()})}); if(selected.current===id)acceptMutation(result.project); }
    catch(e){setError((e as Error).message);} finally{setSending(false);}
  }
  async function cancel() {
    if (!project?.latest_run) return; const id=project.id;
    try { const updated=await api<Project>(`/runs/${project.latest_run.id}/cancel`,{method:'POST'}); if(selected.current===id)acceptMutation(updated); }
    catch(e){setError((e as Error).message);}
  }
  async function queueAction(queueId?: string) {
    if(!project||sending)return;
    const id=project.id;setSending(true);setError('');
    try{const item=await api<Project>(queueId?`/projects/${id}/queue/${queueId}`:`/projects/${id}/queue/resume`,{method:queueId?'DELETE':'POST'});if(selected.current===id)acceptMutation(item);}
    catch(e){setError((e as Error).message);}finally{setSending(false);}
  }
  async function approveDocument() {
    if(!project||sending)return;
    const id=project.id;setSending(true);setError('');
    try{const item=await api<Project>(`/projects/${id}/approve`,{method:'POST',body:JSON.stringify({base_version:project.document.version,base_domain_revision:project.domain_revision??0})});if(selected.current===id)acceptMutation(item);await refreshList();setToast('本版需求已审核，后续修改将重新核对');}
    catch(e){setError((e as Error).message);}finally{setSending(false);}
  }
  function discuss(id: string) {
    const req=doc?.requirements.find(r=>r.id===id); if(!req)return;
    setContext(req); setMobilePane('chat'); setCanvasFocus(false);
    requestAnimationFrame(()=>composer.current?.focus());
  }
  function jumpToSource(req:Requirement) {
    setMobilePane('chat'); setCanvasFocus(false);
    requestAnimationFrame(()=>document.querySelector(`[data-message-id="${req.source_message_id}"]`)?.scrollIntoView({behavior:'smooth',block:'center'}));
  }
  function discussDecision(text:string){if(!draft.includes(text)){const merged=draft.trim()?`${draft}\n\n${text}`:text;if(merged.length>16000)setToast('草稿接近长度上限，请先整理或发送后继续讨论。');else setDraft(merged);}setMobilePane('chat');setCanvasFocus(false);requestAnimationFrame(()=>composer.current?.focus());}
  function locateMessage(id:string){setMobilePane('chat');setCanvasFocus(false);requestAnimationFrame(()=>document.querySelector(`[data-message-id="${id}"]`)?.scrollIntoView({behavior:'smooth',block:'center'}));}
  function resize(e: React.PointerEvent<HTMLDivElement>) {
    if(e.button!==0)return;
    const start=e.clientX, width=e.currentTarget.previousElementSibling?.getBoundingClientRect().width??chatWidth;
    e.currentTarget.setPointerCapture(e.pointerId);
    const el=e.currentTarget;
    document.body.classList.add('resizing');
    const move=(event:PointerEvent)=>setChatWidth(Math.max(360,Math.min(splitLimit(el),width+event.clientX-start)));
    const end=()=>{el.removeEventListener('pointermove',move);el.removeEventListener('pointerup',end);el.removeEventListener('pointercancel',end);document.body.classList.remove('resizing');};
    el.addEventListener('pointermove',move);el.addEventListener('pointerup',end);el.addEventListener('pointercancel',end);
  }
  const filtered=projects.filter(p=>p.title.toLowerCase().includes(search.toLowerCase()));
  const pending=doc?.requirements.filter(r=>['tbd','conflict'].includes(r.state))??[];
  const lastRun=project?.latest_run;
  const failed=lastRun && ['failed','cancelled','interrupted'].includes(lastRun.status);
  const queuedInputs=project?.input_queue??[];

  return <div className={`app ${sidebar?'sidebar-open':'sidebar-closed'} ${!canvasOpen?'canvas-hidden':''} ${canvasFocus?'canvas-focus':''} mobile-${mobilePane}`} style={{'--chat-width':`${chatWidth}px`} as CSSProperties}>
    {sidebar && <button className="sidebar-scrim" aria-label="关闭项目侧栏" onClick={()=>setSidebar(false)}/>}
    <aside className="sidebar" aria-label="项目导航">
      <div className="brand"><BrandMark/><div><strong>PCBAgent<span> / </span><small>Studio</small></strong><span className="brand-caption">让硬件想法，逐渐清晰</span></div><button className="icon-button sidebar-close" aria-label="收起项目侧栏" onClick={()=>setSidebar(false)}><PanelLeftClose size={16}/></button></div>
      <button className="new-project" onClick={()=>void createProject()} disabled={sending||loading}><Plus size={16}/><span>新建需求</span><span className="new-short">NEW</span></button>
      <label className="project-search"><Search size={14}/><input aria-label="搜索项目" placeholder="搜索项目" value={search} onChange={e=>setSearch(e.target.value)}/>{search&&<button className="icon-button" aria-label="清除搜索" onClick={()=>setSearch('')}><X size={12}/></button>}</label>
      <div className="sidebar-section-label"><span>我的项目</span><span>{projects.length}</span></div>
      <nav className="project-list">{filtered.map(p=><button className={`project-item ${project?.id===p.id?'selected':''}`} key={p.id} data-project-id={p.id} onClick={()=>{void loadProject(p.id,true).catch(e=>setError(e.message));if(window.innerWidth<760)setSidebar(false);}}><Folder size={16}/><div><span className="project-name">{p.title}</span><span className="project-meta">{p.requirement_count?p.requirement_count+' 项需求':'从一个想法开始'}<span>·</span>v{p.version}{p.is_example&&<em>示例</em>}</span></div>{project?.id===p.id&&<span className="selected-dot"/>}</button>)}{!filtered.length&&<p className="search-empty">没有找到相关项目</p>}</nav>
      <button className="sample-entry" onClick={()=>void createProject(true)} disabled={sending||loading}><BookOpen size={15}/><span>查看完整示例</span><ArrowRight size={14}/></button>
      <div className="sidebar-note"><Leaf size={17}/><p>从一句想法开始，<br/>一起完成你的需求说明书。</p></div>
      <div className="workspace-profile"><span className="avatar">CX</span><div><strong>本地工作区</strong><span><i className={health?'online':''}/>{health?'所有文档保存在本机':'正在连接服务'}</span></div><LockKeyhole size={13}/></div>
    </aside>

    <div className="workbench">
      <header className="topbar">
        <div className="breadcrumb"><button className="icon-button toggle-sidebar" aria-label={sidebar?'收起项目侧栏':'展开项目侧栏'} onClick={()=>setSidebar(v=>!v)}>{sidebar?<PanelLeftClose size={17}/>:<PanelLeftOpen size={17}/>}</button><span className="breadcrumb-root">需求工作台</span><ChevronRight size={13}/><button className="project-title-button" title="重命名项目" disabled={!project||busy||loading} onClick={()=>{setNewTitle(project?.title??'');setRenameError('');setRenaming(true);}}>{project?.title??'正在加载'}<Pencil size={11}/></button><span className="draft-badge">{project?.review?.status==='approved'?'已审核':project?.review?.status==='ready'?'待审核':'草稿'}</span></div>
        <div className="topbar-actions"><span className="save-indicator"><CheckCheck size={14}/>{busy?'已有版本已保存':'已保存'}</span><div className="export-menu" ref={exportRef}><button className="button secondary small" disabled={loading||!project||!doc?.requirements.length} onClick={()=>setExportOpen(v=>!v)} aria-expanded={exportOpen}><Download size={14}/><span>导出</span><ChevronDown size={12}/></button>{exportOpen&&project&&<div className="dropdown"><span>导出当前版本 v{doc?.version}</span><a href={`/api/projects/${project.id}/export/md`} onClick={()=>setExportOpen(false)} download={`${project.title}_需求说明书_v${doc?.version}.md`}><FileText size={15}/>Markdown 文档<ArrowDownToLine size={13}/></a><a href={`/api/projects/${project.id}/export/studio-json`} onClick={()=>setExportOpen(false)} download={`${project.title}_需求说明书_v${doc?.version}.json`}><Layers3 size={15}/>完整工作台 JSON<ArrowDownToLine size={13}/></a><button className="export-handoff" onClick={()=>{setExportOpen(false);setHandoffOpen(true);}}><CheckCheck size={15}/>选型交接包<ArrowRight size={13}/></button><a className="legacy-export" href={`/api/projects/${project.id}/export/json`} onClick={()=>setExportOpen(false)} download>旧版结构 JSON · v1</a></div>}</div><a className="button secondary small device-selection-link" href={project?`http://${location.hostname}:8899/?project=${project.id}`:undefined} target="_blank" rel="noopener" aria-disabled={!project} title="在器件选型阶段打开这个项目" onClick={(e)=>{if(!project)e.preventDefault();}}><Boxes size={14}/><span>器件选型</span><ArrowUpRight size={12}/></a><button className="icon-button canvas-toggle" aria-label={canvasOpen?'收起需求画布':'展开需求画布'} onClick={()=>{setCanvasOpen(v=>!v);setCanvasFocus(false);}}>{canvasOpen?<PanelRightClose size={18}/>:<PanelRightOpen size={18}/>}</button></div>
      </header>
      <div className="mobile-switch" role="tablist" aria-label="工作区"><button role="tab" aria-selected={mobilePane==='chat'} onClick={()=>setMobilePane('chat')}><MessageSquare size={14}/>需求讨论</button><button role="tab" aria-selected={mobilePane==='canvas'} onClick={()=>{setMobilePane('canvas');setCanvasOpen(true);}}><BookOpen size={14}/>需求画布{doc&&doc.version>0&&<span>v{doc.version}</span>}</button></div>
      {error&&<div className="error-banner" role="alert"><AlertCircle size={15}/><span>{error}</span>{!project&&<button className="text-button" onClick={()=>void bootstrap()}>重新连接</button>}<button className="icon-button" aria-label="关闭提示" onClick={()=>setError('')}><X size={14}/></button></div>}
      <main className="workspace">
        <section className="chat-panel" aria-label="需求讨论">
          <div className="panel-header"><span><MessageSquare size={15}/>需求讨论</span><span className="panel-hint"><i/>与画布同步</span></div>
          <div className="chat-scroll" ref={chatScroll} onScroll={()=>{const el=chatScroll.current;if(el){nearBottom.current=el.scrollHeight-el.scrollTop-el.clientHeight<100;setShowJump(!nearBottom.current);}}}>
            {loading?<div className="loading-state"><Loader2 className="spin" size={23}/><span>打开你的工作区</span></div>:project?.messages.length? <div className="messages">
              {project.is_example&&<div className="example-notice"><BookOpen size={13}/><span>示例项目 · 继续对话将调用真实 AI</span></div>}
              {project.messages.map(m=><article className={`message ${m.role}`} key={m.id} data-message-id={m.id}>{m.role==='assistant'?<><div className="assistant-label"><BrandMark small/><strong>需求助手</strong><span>{time(m.created_at)}</span></div><Markdown text={m.content}/></>:<><div className="user-content">{m.content}</div><span className="message-time">你 · {time(m.created_at)}</span><RunHistory project={project} messageId={m.id}/><DeviceChips project={project} messageId={m.id} onOpen={referenceId=>openDevices({referenceId})}/></>}</article>)}
              {!!doc?.questions.length&&!doc.questions_paused&&<QuestionCards key={project.id} projectId={project.id} submissions={project.question_submissions} questions={doc.questions.map(q=>q.branch?q:{...q,branch:doc.requirements.find(r=>q.requirement_ids.includes(r.id))?.category??'overview'})} disabled={sending} onAnswer={(text,answers)=>send(text,answers,false)}/>}
              <ModulePlanReview project={project} busy={busy} disabled={sending} onConfirm={text=>send(text,[],false)} onInspect={()=>{setTab('modules');setMobilePane('canvas');setCanvasOpen(true);}}/>
            </div>:<div className="chat-empty"><div className="welcome-symbol"><BrandMark/></div><div className="eyebrow">YOUR NEXT HARDWARE IDEA</div><h1>从一个想法开始</h1><p>告诉我你想做什么，<br/>我们一起把它变成清晰的需求。</p><div className="starter-prompts">{['做一个电池供电的蓝牙温度记录器','设计一块室内空气质量监测板','我有一个硬件想法，帮我梳理需求'].map(text=><button key={text} onClick={()=>{setDraft(text);composer.current?.focus();}}><Sparkles size={14}/><span>{text}</span><ArrowUp size={13}/></button>)}</div><button className="text-button example-link" onClick={()=>void createProject(true)}>先看看一份需求如何形成<ArrowRight size={13}/></button></div>}
            {busy&&<LiveProgress stage={stage} elapsed={elapsed} events={liveEvents}/>}
            {!busy&&!!doc?.questions.length&&<div className="conversation-waiting" role="status"><CircleHelp size={16}/><div><strong>{doc.questions_paused?'已暂停提问，未决事项仍保留':'已整理当前资料，等你继续补充'}</strong><p>{doc.questions.length} 个待答问题 · 会话持续保留，可随时回答或补充其他需求。</p>{doc.questions_paused&&<button className="text-button" disabled={sending} onClick={()=>void send('继续完善需求说明书，请重新梳理待确认问题。',[],false)}>继续完善需求<ArrowRight size={12}/></button>}</div></div>}
            {failed&&<div className={`run-notice ${lastRun.status==='failed'?'failure':''}`} role="status"><AlertCircle size={16}/><div><strong>{lastRun.status==='cancelled'?'已停止本轮分析':lastRun.status==='interrupted'?'本轮分析已中断':'本轮分析未完成'}</strong><p>{lastRun.error}</p><button className="text-button" disabled={sending} onClick={()=>void retry()}>重试本轮<ArrowRight size={12}/></button></div></div>}
            <div ref={bottom}/>
          </div>
          {showJump&&<button className="jump-latest" onClick={()=>{nearBottom.current=true;bottom.current?.scrollIntoView({behavior:'smooth'});}}><ArrowDown size={13}/>回到最新消息</button>}
          <div className="composer-area">{context&&<div className="context-chip"><FileText size={12}/><span>讨论 {context.id} · {context.name}</span><button className="icon-button" aria-label="取消需求引用" onClick={()=>setContext(null)}><X size={12}/></button></div>}
            {!!queuedInputs.length&&<section className="queued-inputs" aria-label="待处理补充"><header><span><Clock3 size={12}/>待处理补充 · {queuedInputs.length}</span>{!busy&&<button className="text-button" disabled={sending} onClick={()=>void queueAction()}>继续处理<ArrowRight size={12}/></button>}</header><div>{queuedInputs.map((item,index)=><div className="queued-input" key={item.id}><span>{index+1}</span><p title={item.text}>{item.text}</p><button className="icon-button" aria-label={`移除补充 ${index+1}`} disabled={sending} onClick={()=>void queueAction(item.id)}><X size={13}/></button></div>)}</div></section>}
            <div className={`composer ${busy?'is-busy':''}`}><textarea ref={composer} disabled={loading||!project} aria-label="输入硬件需求" placeholder={busy?'随时补充，将按顺序继续分析……':'描述你的想法，或补充一项需求……'} value={draft} maxLength={16000} rows={2} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing&&e.nativeEvent.keyCode!==229){e.preventDefault();void send();}}}/><div className="composer-bottom"><span className="model-label"><span className="model-dot"/>{health?.model_label??'gpt6-luna-xhigh'}<LockKeyhole size={10}/></span><div className="composer-buttons">{busy&&<button className="send-button stop" aria-label="停止分析" onClick={()=>void cancel()}><Square size={12} fill="currentColor"/></button>}<button className="send-button" aria-label={busy?'加入待处理补充':'发送需求'} disabled={!draft.trim()||sending||loading||!project} onClick={()=>void send()}>{sending?<Loader2 className="spin" size={18}/>:busy?<Plus size={19}/>:<ArrowUp size={19}/>}</button></div></div></div>
            <div className="composer-note"><span>{busy?'Enter 加入补充队列':'Enter 发送'} · Shift + Enter 换行</span><span><LockKeyhole size={10}/>本机保存</span></div>
          </div>
        </section>
        <div className="resize-handle" role="separator" aria-label="调整对话区宽度" aria-orientation="vertical" aria-valuenow={chatWidth} aria-valuemin={360} aria-valuemax={650} tabIndex={0} onPointerDown={resize} onKeyDown={e=>{if(['ArrowLeft','ArrowRight'].includes(e.key)){e.preventDefault();setChatWidth(Math.max(360,Math.min(splitLimit(e.currentTarget),(e.currentTarget.previousElementSibling?.getBoundingClientRect().width??chatWidth)+(e.key==='ArrowRight'?16:-16))));}}}/>
        <section className="canvas-panel" aria-label="需求画布">
          <div className="panel-header canvas-header"><span><BookOpen size={15}/>需求画布<span className="live-badge"><i/>LIVE</span></span><div><button className="text-button domain-header-entry" aria-label="打开器件与证据" disabled={loading||!project} onClick={()=>openDevices()}><Cpu size={14}/><span className="domain-header-label">器件与证据</span></button><span className="version-label">v{doc?.version??0}</span><button className="icon-button" aria-label={canvasFocus?'恢复对话与画布':'全屏查看画布'} onClick={()=>setCanvasFocus(v=>!v)}>{canvasFocus?<Minimize2 size={15}/>:<Maximize2 size={15}/>}</button></div></div>
          <div className="canvas-tabs" role="tablist" aria-label="需求画布视图">{([['overview','设计总览',Layers3],['document','需求说明书',FileText],['history','变更记录',History],['convergence','收敛路径',CheckCheck],['modules','模块方案',GitBranch]] as const).map(([key,label,Icon])=><button role="tab" aria-selected={tab===key} key={key} className={tab===key?'active':''} onClick={()=>{setTab(key);setDevicePanel(null);}}><Icon size={14}/>{label}{key==='history'&&!!project?.revisions.length&&<span>{project.revisions.length}</span>}</button>)}</div>
          <div className="canvas-scroll" key={project?.id}>
            {devicePanel&&project?<DeviceEvidencePanel key={project.id} project={project} initialReference={devicePanel.referenceId} moduleId={devicePanel.moduleId} requirementId={devicePanel.requirementId} onClose={closeDevices} onDiscuss={discussDecision} onSaved={item=>{if(selected.current===item.id)acceptMutation(item);void refreshList().catch(()=>{});}}/>:tab==='modules'&&project?<ModuleBoard key={project.id} project={project} busy={busy||sending} onDiscuss={discussDecision} onEvidence={moduleId=>openDevices({moduleId})} onSaved={item=>{if(selected.current===item.id)acceptMutation(item);void refreshList().catch(e=>setError(e.message));setToast('模块方案已保存，需求保持同步');}}/>:tab==='convergence'&&project?<div className="document-page convergence-page"><ConvergenceBoard key={project.id} project={project} onDiscuss={discussDecision} onModules={()=>setTab('modules')} onSource={locateMessage} onReview={()=>setTab('document')}/></div>:loading?<div className="loading-state"><Loader2 className="spin" size={23}/><span>正在打开需求画布</span></div>:!doc?.requirements.length?<div className="canvas-empty"><div className="empty-document-preview"><div className="paper-line small"/><div className="paper-line title"/><div className="paper-line"/><div className="paper-line medium"/><div className="empty-mini-map"><span/><i/><span/><i/><span/></div><div className="paper-line"/><div className="paper-line medium"/><span className="paper-spark"><Sparkles size={18}/></span></div><h2>你的需求，将在这里逐渐成形</h2><p>每一次讨论，都会让说明书更清晰一点。</p><div className="empty-feature-list"><span><FileText size={13}/>需求说明书</span><span><GitBranch size={13}/>设计导图</span><span><History size={13}/>演进记录</span></div><div className="document-outline">{Object.entries(categories).map(([key,label],i)=><div key={key}><span>{String(i+1).padStart(2,'0')}</span>{label}<span className="outline-placeholder"/></div>)}</div></div>:tab==='document'?<div className="document-page full-document"><div className="document-topline"><span><FileText size={12}/>持续更新的需求文档</span><span>v{doc.version}</span></div>{project&&<div className="document-review-banner"><div><strong>{project.review.status==='approved'?'本版需求已审核':project.review.status==='ready'?'请整体审阅，确认本版需求':'这份说明书仍在完善'}</strong><p>{project.review.status==='draft'?(project.review.analysis_pending||'可返回收敛路径查看待明确事项与模块边界。'):'核对产品目标、模块方案和留到设计阶段的内容；后续修改会重新核对。'}</p></div>{project.review.status==='draft'?<button className="button secondary small" onClick={()=>setTab('convergence')}>查看收敛路径</button>:<button className="button primary small document-approve" disabled={sending||busy||!!queuedInputs.length||!!project.domain?.active_jobs||project.review.status!=='ready'} onClick={()=>void approveDocument()}>{project.review.status==='approved'?'已审核':'确认本版需求'}</button>}</div>}<Markdown text={project?.markdown??''} documentView diagramDocument={{...doc,modules:project?.convergence.modules??doc.modules}} diagramScope={project?.id??doc.title} onDiagramSelect={discuss}/></div>:tab==='history'?<div className="history-page"><div className="eyebrow">DESIGN EVOLUTION</div><h2>每一步，都有迹可循</h2><p className="muted">记录从初步想法到清晰需求的每一次变化。</p><div className="revision-timeline">{project?.revisions.map((revision,i)=><div className="revision" key={revision.version}><span className={`revision-dot ${i===0?'current':''}`}/><div className="revision-title"><strong>需求版本 v{revision.version}</strong>{i===0&&<span>当前版本</span>}<time>{new Date(revision.created_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</time></div><ul>{revision.changes.map((change,index)=><li key={index}>{change}</li>)}</ul></div>)}</div></div>:<div className="document-page overview-page">
              <div className="document-topline"><span><span className="tiny-dot"/>HARDWARE REQUIREMENTS</span><span>{project?.is_example?'示例 · ':''}{project?.review?.status==='approved'?'已审核需求':project?.review?.status==='ready'?'待审核需求':'需求草稿'} · v{doc.version}</span></div>
              <h1>{doc.title}</h1><p className="document-summary">{doc.summary||'继续讨论，逐步明确产品目标与设计边界。'}</p>
              <div className="document-stats"><span><CheckCircle2 size={13}/>{doc.requirements.length} 项需求已记录</span><span><CircleHelp size={13}/>{Math.max(doc.questions.length,pending.length)+(project?.convergence.module_pending.length??0)} 项待处理</span><span><Clock3 size={12}/>{time(doc.updated_at)} 更新</span></div>
              {project?.review&&<CoveragePanel review={project.review} busy={busy||!!queuedInputs.length||!!project.domain?.active_jobs} disabled={sending} onContinue={()=>void send('请根据已有对话继续完善本项目的需求说明书，先内部核对已知要求、实际模块、工作流程与关键验收边界。只询问需要我决定的产品取舍；未知实现参数保留待设计，不要求逐项填写完整参考规范。',[],false)} onApprove={()=>void approveDocument()} onTopic={topic=>{setDraft(`关于 ${topic.id}「${topic.title}」：`);setMobilePane('chat');setCanvasFocus(false);requestAnimationFrame(()=>composer.current?.focus());}}/>}
              {project&&<DomainSummary project={project} onOpen={()=>openDevices()}/>} {project&&<ConvergenceBoard project={project} compact onDiscuss={discussDecision} onModules={()=>setTab('modules')} onSource={locateMessage} onReview={()=>setTab('document')} onOpen={()=>setTab('convergence')}/>}<div className="section-heading"><h2><GitBranch size={15}/>设计逐渐清晰</h2><span>与需求同步</span></div><Diagrams document={{...doc,modules:project?.convergence.modules??doc.modules}} moduleLinks={project?.convergence.module_links} storageKey={project?.id??doc.title} onSelect={discuss}/>
              <div className="section-heading requirement-heading"><h2><FileText size={15}/>需求清单<span className="count-badge">{doc.requirements.length}</span></h2><button className="text-button" disabled={busy} onClick={()=>setEditing(null)}><Plus size={13}/>添加需求</button></div>
              <div className="requirements-list">{(Object.entries(categories) as [Category,string][]).filter(([key])=>doc.requirements.some(r=>r.category===key)).map(([key,label])=><section className="requirement-section" key={key}><h3><span>{label}</span><span>{doc.requirements.filter(r=>r.category===key).length}</span></h3>{doc.requirements.filter(r=>r.category===key).map(r=><div className={`requirement-row ${doc.changed_ids.includes(r.id)?'recent-change':''}`} key={r.id} id={`req-${r.id}`}><button className="requirement-main" aria-label={`编辑 ${r.id} ${r.name}`} disabled={busy} onClick={()=>setEditing(r)}><span className="requirement-id">{r.id}</span><span className="requirement-name">{r.name}</span><span className="requirement-value">{r.value||'待确认'}{r.unit&&<small> {r.unit}</small>}</span><Status state={r.state}/><Pencil size={12}/></button><div className="requirement-detail-actions"><button className="text-button" onClick={()=>discuss(r.id)}>继续讨论</button>{r.source_quote&&<button className="text-button" title={r.source_quote} onClick={()=>jumpToSource(r)}>查看依据</button>}{project?.domain?.references.some(d=>d.requirement_ids.includes(r.id))&&<button className="text-button" onClick={()=>openDevices({requirementId:r.id})}>器件依据</button>}</div></div>)}</section>)}</div>
              {(!!doc.questions.length||!!pending.length)&&<section className="open-questions"><div className="section-heading"><h2><CircleHelp size={15}/>下一步，一起确认</h2><button className="text-button" onClick={()=>{setMobilePane('chat');setCanvasFocus(false);nearBottom.current=true;bottom.current?.scrollIntoView({behavior:'smooth'});}}>前往讨论<ArrowRight size={12}/></button></div>{doc.questions.length?doc.questions.map(q=><div className="open-question" key={q.id}><CircleDot size={13}/><div><strong>{q.question}</strong><p>{q.reason}</p></div></div>):pending.map(r=><div className="open-question" key={r.id}><CircleDot size={13}/><div><strong>{r.name}</strong><p>{r.value||'仍待明确'}</p></div></div>)}</section>}
              <div className="document-footer"><BrandMark small/><span>随讨论持续完善 · 最终需求由你审阅</span><CheckCheck size={13}/></div>
            </div>}
          </div>
          <div className="canvas-statusbar"><span><span className="tiny-dot"/>{busy?'讨论进行中 · 保留上个已保存版本':'需求与图形使用同一份已保存数据'}</span><span>{doc?.requirements.length??0} 项需求</span></div>
        </section>
      </main>
    </div>
    {toast&&<div className="toast" role="status"><CheckCircle2 size={15}/>{toast}</div>}
    {handoffOpen&&project&&<HandoffDialog key={project.id} project={project} onClose={()=>setHandoffOpen(false)} onDiscuss={discussDecision}/>}
    {editing!==undefined&&project&&<RequirementEditor key={editing?.id??'new'} requirement={editing} project={project} onClose={()=>setEditing(undefined)} onSaved={item=>{if(selected.current===item.id)acceptMutation(item);void refreshList().catch(e=>setError(e.message));setToast('需求已保存，图文已同步');}}/>}
    {renaming&&project&&<Dialog title="重命名项目" onClose={()=>{if(!renameSaving)setRenaming(false);}}><form className="editor-form" onSubmit={async e=>{e.preventDefault();if(!newTitle.trim()||renameSaving)return;setRenameSaving(true);setRenameError('');try{const item=await api<Project>(`/projects/${project.id}`,{method:'PATCH',body:JSON.stringify({title:newTitle.trim(),base_version:project.document.version})});if(selected.current===item.id)acceptMutation(item);await refreshList();setRenaming(false);}catch(e){setRenameError((e as Error).message);}finally{setRenameSaving(false);}}}><label>项目名称<input autoFocus required maxLength={120} value={newTitle} onChange={e=>setNewTitle(e.target.value)}/></label>{renameError&&<div className="inline-error" role="alert"><AlertCircle size={15}/>{renameError}</div>}<div className="dialog-actions"><button type="button" className="button secondary" disabled={renameSaving} onClick={()=>setRenaming(false)}>取消</button><button className="button primary" disabled={renameSaving||!newTitle.trim()}>{renameSaving?<Loader2 className="spin" size={15}/>:<Check size={15}/>}保存名称</button></div></form></Dialog>}
  </div>;
}
