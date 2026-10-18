import { useEffect, useRef, useState } from 'react';
import { ArrowRight, Check, CheckCheck, ChevronDown, CircleHelp, Clock3 } from 'lucide-react';
import { categories, readSetting, saveSetting, type Question, type QuestionAnswer, type QuestionSubmission } from './types';

type Draft = { selected: string; custom: string; question: string };

export function QuestionCards({ projectId, questions, submissions=[], disabled, onAnswer }: {
  projectId: string; questions: Question[]; disabled: boolean;
  submissions?:QuestionSubmission[];
  onAnswer: (text: string, answers: QuestionAnswer[]) => Promise<boolean>;
}) {
  const [drafts, setDrafts] = useState<Record<string, Draft>>(() => {
    try { return JSON.parse(readSetting(`answers:${projectId}`, '{}')); } catch { return {}; }
  });
  const [branch, setBranch] = useState('all');
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState('');
  const posting=useRef(false);
  const [optimistic,setOptimistic]=useState<Record<string,QuestionSubmission>>({});
  const [editing,setEditing]=useState<Record<string,boolean>>({});
  useEffect(() => { saveSetting(`answers:${projectId}`, JSON.stringify(drafts)); }, [drafts, projectId]);
  useEffect(()=>{setOptimistic(old=>{const kept=Object.fromEntries(Object.entries(old).filter(([id,s])=>questions.some(q=>q.id===id)&&!submissions.some(a=>a.question_id===id&&a.answer===s.answer)));return Object.keys(kept).length===Object.keys(old).length?old:kept;});},[questions,submissions]);
  const receipts=Object.fromEntries(submissions.map(s=>[s.question_id,s]));
  Object.assign(receipts,optimistic);
  function receipt(q:Question):QuestionSubmission|undefined{return editing[q.id]?undefined:receipts[q.id]??(q.status==='deferred'?{question_id:q.id,answer:'这项已暂缓，之后可以继续回答。',deferred:true,status:'needs_input'}:undefined);}
  const branches = [...new Set(questions.map(q => q.branch ?? 'overview'))];
  useEffect(()=>{if(branch!=='all'&&!questions.some(q=>(q.branch??'overview')===branch)){setBranch('all');setExpanded(false);}},[questions,branch]);
  const filtered = questions.filter(q => branch === 'all' || (q.branch ?? 'overview') === branch);
  const actionable=filtered.filter(q=>!receipt(q));
  const visible = expanded ? actionable : actionable.slice(0, 3);
  const draftFor = (q: Question) => drafts[q.id]?.question === q.question ? drafts[q.id] : undefined;
  const answerFor = (q: Question) => (draftFor(q)?.custom || draftFor(q)?.selected || '').trim();
  const answered = questions.filter(q => !receipt(q)&&answerFor(q));
  function update(q: Question, values: Partial<Draft>) {
    setDrafts(old => ({ ...old, [q.id]: { selected: '', custom: '', ...(old[q.id]?.question===q.question?old[q.id]:{}), ...values, question: q.question } }));
  }
  async function submit(deferred = false) {
    const selected = deferred ? visible : answered;
    const answers = selected.map(q => ({ question_id: q.id, answer: deferred ? '暂时不确定，请保留待确认，先分析其他部分。' : answerFor(q), deferred }));
    const text = selected.map((q, i) => `关于「${q.question}」：${answers[i].answer}`).join('\n');
    if (!text || disabled || posting.current) return;
    if (text.length > 16000) { setError('回答内容较长，请分批提交；当前填写已保留。'); return; }
    setError('');
    posting.current=true;
    setEditing(old=>Object.fromEntries(Object.entries(old).filter(([id])=>!answers.some(a=>a.question_id===id))));
    setOptimistic(old=>({...old,...Object.fromEntries(answers.map(a=>[a.question_id,{...a,status:'sending' as const}]))}));
    try{if (await onAnswer(text, answers)) {
      if(!deferred)setDrafts(old => Object.fromEntries(Object.entries(old).filter(([id]) => !answers.some(a => a.question_id === id))));
      setOptimistic(old=>Object.fromEntries(Object.entries(old).map(([id,s])=>[id,answers.some(a=>a.question_id===id)?{...s,status:'processing'}:s])));
    }else{setOptimistic(old=>Object.fromEntries(Object.entries(old).filter(([id])=>!answers.some(a=>a.question_id===id))));setEditing(old=>({...old,...Object.fromEntries(answers.map(a=>[a.question_id,true]))}));}}
    catch{setError('回答暂未提交，填写内容已保留，请重试。');setOptimistic(old=>Object.fromEntries(Object.entries(old).filter(([id])=>!answers.some(a=>a.question_id===id))));setEditing(old=>({...old,...Object.fromEntries(answers.map(a=>[a.question_id,true]))}));}
    finally{posting.current=false;}
  }
  return <form className="question-group" onKeyDown={e=>{if(e.key==='Enter'&&e.nativeEvent.isComposing)e.preventDefault();}} onSubmit={e => { e.preventDefault(); void submit(); }}>
    <div className="question-group-title"><CircleHelp size={15}/><span>{actionable.length?'待你补充':'回答已收到'}</span><span className="question-count">{actionable.length?`${actionable.length} 个待答问题`:'可以继续补充其他想法'}</span></div>
    <p className="question-guidance">{actionable.length?'可以先答一个。提交后会收起选项，其他问题仍可回答。':'已提交的回答会随分析保存，不需要重复选择。'}</p>
    {branches.length > 1 && <label className="question-filter">问题分支<select aria-label="筛选问题分支" value={branch} onChange={e => { setBranch(e.target.value); setExpanded(false); }}><option value="all">全部分支</option>{branches.map(key => <option key={key} value={key}>{categories[key]}</option>)}</select></label>}
    {filtered.filter(q=>receipt(q)).map(q=>{const s=receipt(q)!;const locked=['sending','processing','queued'].includes(s.status);return <article className="question-card question-receipt" key={q.id} data-question-id={q.id}><div><CheckCheck size={15}/><strong>{q.question}</strong></div><p>{s.answer}</p><div className="answer-receipt-status" role="status"><Clock3 size={12}/><span>{s.status==='sending'?'正在提交回答…':s.status==='processing'?'回答已收到，正在分析':s.status==='queued'?'已排队，将按顺序处理':s.status==='failed'?'回答已保存，本轮分析未完成，可重试':s.deferred?'已暂缓，之后再答':'这项还需补充，可以继续回答'}</span>{!locked&&<button type="button" className="text-button" disabled={disabled} onClick={()=>setEditing(old=>({...old,[q.id]:true}))}>{s.deferred?'继续回答':'修改或补充'}</button>}</div></article>;})}
    {visible.map((q, index) => <fieldset className="question-card" key={q.id} data-question-id={q.id} disabled={disabled}>
      <legend><span className="question-number">{String(index + 1).padStart(2, '0')}</span>{q.question}</legend>
      <div className="question-meta"><span>{categories[q.branch ?? 'overview']}</span>{q.priority===1&&<span className="question-priority">优先决定</span>}{q.status === 'deferred' && <span>已暂缓，随时可答</span>}</div>
      <p className="question-reason">{q.reason}</p>
      {q.impact&&<p className="question-impact">这个决定会影响：{q.impact}</p>}
      <div className="question-options">{q.options.map((option, i) => <button type="button" className={`question-option ${draftFor(q)?.selected === option && !draftFor(q)?.custom ? 'selected' : ''}`} key={option} onClick={() => update(q, { selected: option, custom: '' })}><span className="option-letter">{String.fromCharCode(65 + i)}</span><span>{option}{q.option_impacts?.[i]&&<small className="option-impact">{q.option_impacts[i]}</small>}</span>{draftFor(q)?.selected === option && !draftFor(q)?.custom && <Check size={14}/>}</button>)}</div>
      <input className="custom-answer" aria-label={`自定义回答：${q.question}`} placeholder="也可以写下你的想法……" maxLength={2000} value={draftFor(q)?.custom ?? ''} onChange={e => update(q, { custom: e.target.value })}/>
    </fieldset>)}
    {actionable.length > 3 && <button className="text-button question-expand" type="button" onClick={() => setExpanded(v => !v)}><ChevronDown size={13}/>{expanded ? '收起其他问题' : `展开其余 ${actionable.length - 3} 个问题`}</button>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {!!actionable.length&&<div className="question-footer"><button type="button" className="text-button" disabled={disabled || !visible.length} onClick={() => void submit(true)}>暂缓这组问题</button><button className="button small primary" disabled={!answered.length || disabled}>提交{answered.length ? ` ${answered.length} 项` : ''}回答<ArrowRight size={13}/></button></div>}
  </form>;
}
