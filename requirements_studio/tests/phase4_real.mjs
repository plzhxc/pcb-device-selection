// Real-user browser driver. The original case TXT is always the first chat input.
// Product decisions live in reviewable persona/answer files; no database seeding or approval bypass.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {gzipSync,gunzipSync} from 'node:zlib';

const root=path.resolve(path.dirname(new URL(import.meta.url).pathname),'..');
const args=process.argv.slice(2),arg=(key,fallback='')=>{const i=args.indexOf('--'+key);return i<0?fallback:args[i+1];};
const base=arg('base','http://[::1]:8894'),suite=arg('suite','phase4'),mode=arg('mode','auto');
if(!/^[A-Za-z0-9_-]+$/.test(suite))throw new Error('Invalid suite name');
const rounds=Number(arg('rounds','1')),roundStart=Number(arg('round-start','1')),requested=arg('cases').split(',').filter(Boolean),out=path.join(root,'.runtime/phase4',suite);
assert.ok(Number.isInteger(roundStart)&&Number.isInteger(rounds)&&roundStart>=1&&rounds>=roundStart,'Invalid round interval');
await fs.mkdir(out,{recursive:true});
const cases=JSON.parse(await fs.readFile(path.join(root,'.runtime/phase3/cases.json'),'utf8')).filter(c=>!requested.length||requested.includes(c.id));
const profiles=JSON.parse(await fs.readFile(path.join(root,'tests/phase4_profiles.json'),'utf8'));
const oracleBytes=await fs.readFile(path.join(root,'tests/phase4_oracles.json'));
const oracleRules=JSON.parse(oracleBytes);
const indexPath=path.join(out,'index.json');
let index;try{index=JSON.parse(await fs.readFile(indexPath,'utf8'));}catch{index={suite,base,provider:'real_codex_cli',requested_model:'recorded_from_server_health',cases:[],created_at:new Date().toISOString()};}
const require=createRequire(path.join(root,'frontend/package.json'));
const profile=path.join(out,'browser-'+process.pid);
let shared=process.env.REQUIREMENTS_STUDIO_BROWSER_WS;
if(!shared){try{shared=JSON.parse(await fs.readFile(path.join(root,'.runtime/phase3/browser-bridge.json'),'utf8')).ws;}catch{}}
const browser=shared?await require('puppeteer-core').connect({browserWSEndpoint:shared,protocolTimeout:1400000}):await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',headless:true,pipe:true,
 userDataDir:profile,protocolTimeout:1400000,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},
 args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage','--incognito','--disable-gpu','--disable-gpu-shader-disk-cache','--disk-cache-size=1','--media-cache-size=1','--disable-component-update','--disable-background-networking']});
let page,current,entry,pageErrors=[];const ownedContexts=new Set();
async function saveIndex(){
 const lockPath=indexPath+'.lock';let handle;
 for(let n=0;n<200;n++){try{handle=await fs.open(lockPath,'wx');break;}catch(e){if(e.code!=='EEXIST')throw e;await new Promise(r=>setTimeout(r,20));}}
 if(!handle)throw Error('Acceptance index is busy');
 try{let previous;try{previous=JSON.parse(await fs.readFile(indexPath,'utf8'));}catch{previous={cases:[]};}
  const merged=new Map(previous.cases.map(c=>[c.key,c]));if(entry)merged.set(entry.key,entry);
  const next={...previous,...index,cases:[...merged.values()]};
  const temporary=indexPath+'.'+process.pid+'.tmp';await fs.writeFile(temporary,JSON.stringify(next,null,2)+'\n');await fs.rename(temporary,indexPath);
 }finally{await handle.close();await fs.rm(lockPath,{force:true});}
}
async function get(){return page.evaluate(async id=>{for(let n=0;n<5;n++){const r=await fetch('/api/projects/'+id);if(r.ok)return r.json();if(r.status!==503)throw Error('Project read failed: '+r.status);await new Promise(resolve=>setTimeout(resolve,250*(n+1)));}throw Error('Project read failed repeatedly: 503');},entry.project_id);}
async function screenshot(file){
 if(arg('screenshots','1')==='0')return;
 const temporary=file+'.'+process.pid+'.tmp.png';
 try{await page.screenshot({path:temporary});await fs.rename(temporary,file);}
 finally{await fs.rm(temporary,{force:true});}
}
async function compactSnapshot(){
 const file=path.join(out,entry.key+'.json'),raw=await fs.readFile(file),packed=gzipSync(raw,{level:6});
 assert.ok(gunzipSync(packed).equals(raw));
 const temporary=file+'.'+process.pid+'.tmp.gz';
 try{
  const handle=await fs.open(temporary,'wx');
  try{await handle.writeFile(packed);await handle.sync();}finally{await handle.close();}
  assert.ok(gunzipSync(await fs.readFile(temporary)).equals(raw));
  await fs.rename(temporary,file+'.gz');await fs.unlink(file);
 }
 finally{await fs.rm(temporary,{force:true});}
}
async function capture(label){
 const p=await get();await fs.writeFile(path.join(out,entry.key+'.json'),JSON.stringify(p,null,2)+'\n');
 const transcript=['# '+entry.title,'','原始提示词：'+entry.source,'','模型：'+index.health?.model_label,''];
 for(const m of p.messages){transcript.push('## '+(m.role==='user'?'用户':'需求助手')+' · '+m.created_at,'',m.content,'');for(const r of p.run_activity??[]){if(r.message_id===m.id&&m.role==='user'){transcript.push(`处理记录：${r.kind} / ${r.status}`,'',...r.events.filter(e=>e.message).map(e=>`- ${e.at??''} ${e.message}`),'');}}}
 await fs.writeFile(path.join(out,entry.key+'-transcript.md'),transcript.join('\n'));
 await screenshot(path.join(out,entry.key+'-'+label+'.png'));
 entry.version=p.document.version;entry.review=p.review.status;entry.requirements=p.document.requirements.length;
 entry.modules=p.convergence.modules.length;entry.references=p.domain?.references.length??0;
 entry.user_turns=p.messages.filter(m=>m.role==='user').length;entry.questions=p.document.questions;
 entry.latest_error=p.latest_run?.error;entry.browser_errors=[...new Set([...(entry.browser_errors??[]),...pageErrors])];await saveIndex();return p;
}
async function settle(firstReply=false){
 const deadline=Date.now()+1250000;let last='';
 while(Date.now()<deadline){
  const p=await get();const status=p.latest_run?.status??'none';
  const firstAvailable=firstReply&&p.messages.some(m=>m.role==='assistant')&&status!=='failed';
  if(firstAvailable||(!['queued','running'].includes(status)&&!p.input_queue.length&&!(p.domain?.active_jobs))){
   await page.waitForFunction(v=>document.querySelector('.canvas-header .version-label')?.textContent===`v${v}`,{timeout:20000},p.document.version);
   if(status==='failed'||status==='interrupted'||status==='cancelled'){entry.status='run_failed';await capture('failed');return p;}
   return p;
  }
  const marker=`v${p.document.version} ${status} domain:${p.domain?.active_jobs??0}`;
  if(marker!==last){console.log(entry.key,marker);last=marker;}
  await new Promise(r=>setTimeout(r,1800));
 }
 throw new Error('Conversation did not settle within model/job budgets');
}
async function fill(selector,value){await page.bringToFront();await page.waitForSelector(selector,{visible:true});await page.focus(selector);await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.press('Backspace');await page.keyboard.sendCharacter(value);}
async function send(text,kind='composer'){
 const before=(await get()).messages.filter(m=>m.role==='user').length;
 await fill('[aria-label="输入硬件需求"]',text);assert.equal(await page.$eval('[aria-label="输入硬件需求"]',e=>e.value),text);
 await page.locator('.composer-buttons .send-button:not(.stop)').click();
 await page.waitForFunction(async({id,before})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length>0;},{timeout:20000},{id:entry.project_id,before});
 entry.actions.push({kind,text,at:new Date().toISOString()});await saveIndex();
 return settle(kind==='original_case_txt'||kind==='original_case_txt_recovered');
}
async function answer(answers){
 let p=await get();const pending=p.document.questions.filter(q=>answers[q.id]);
 const before=p.messages.filter(m=>m.role==='user').length;
 if(!pending.length)throw new Error('No current question matches the supplied answers');
 if(await page.$('[aria-label="筛选问题分支"]'))await page.select('[aria-label="筛选问题分支"]','all');
 if(await page.$('.question-expand'))await page.locator('.question-expand').click();
 for(const q of pending){const receipt=await page.$(`[data-question-id="${q.id}"] .answer-receipt-status button`);if(receipt){await page.bringToFront();await receipt.click();}const field=`[data-question-id="${q.id}"] .custom-answer`;if(!await page.$(field)&&await page.$('.question-expand')&&await page.$eval('.question-expand',e=>e.textContent.startsWith('展开')))await page.locator('.question-expand').click();await fill(field,answers[q.id]);}
 await page.locator('.question-footer .primary').click();
 for(const q of pending)await page.waitForFunction(id=>!document.querySelector(`fieldset[data-question-id="${id}"]`),{},q.id);
 await page.waitForFunction(async({id,before})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length>0;},{timeout:20000},{id:entry.project_id,before});
 entry.actions.push({kind:'question_answers',answers,at:new Date().toISOString()});await saveIndex();
 return settle();
}
function moduleConsent(p){return '确认本版实际模块的规范类别、既定功能、独立电路数量和已列边界：'+p.convergence.modules.filter(m=>m.state!=='not_applicable').map(m=>`${m.type_name}（${m.quantity}个独立电路，承接${m.requirement_ids.join('、')}）`).join('；')+'。这些模块共同承接已经明确的实际功能；此项确认不把初稿里的器件介绍或未核验参数新增为硬指标，参数约束以用户已明确的目标和设计委托为准。模块的旧标题若仍含已取消或待核验的数值/通道数，请同步改为与最新决定一致的名称。具体器件用量、拓扑、物理引脚和PCB实现由设计阶段核对，必须满足已经明确的产品要求。不表示器件参数已验证或硬件已通过测试。';}
function response(q,p,persona){
 const overrides=persona?.answers??[];
 if(/(?:使用|应用|目标).{0,4}场景|主要用于|产品定位/.test(q.question)&&persona?.context)return persona.context;
 for(const rule of overrides.filter(r=>r.pattern!=='.*'))if(new RegExp(rule.pattern,'i').test(q.question))return rule.answer;
 if(/模块|功能架构|硬件方案|类别|上述方案/.test(q.question))return moduleConsent(p);
 if(/预算|成本|单价|交期|交付|尺寸|外形|层数|铜厚/.test(q.question))return persona?.commercial??'本版是按原文功能开展的研发原型，没有额外的单价、交期或外形硬限制；保持原文已有的约束，具体制造与结构方案由设计阶段确定。';
 if(/用途|使用场景|使用环境|原型|研发|产品定位/.test(q.question)&&persona?.context)return persona.context;
 if(/验收|测试|判定|验证范围/.test(q.question)&&persona?.acceptance)return persona.acceptance;
 if(/器件|型号|选型|必须|优先|替换/.test(q.question)&&persona?.devices)return persona.devices;
 for(const rule of overrides.filter(r=>r.pattern==='.*'))if(new RegExp(rule.pattern,'i').test(q.question))return rule.answer;
 return null;
}
async function approveAndExport(p){
 assert.equal(pageErrors.length,0,'browser runtime error: '+pageErrors.join('; '));
 await capture('review');
 const inspected=spawnSync(path.join(root,'../.venv/bin/python'),['-X','utf8',path.join(root,'tests/check_case.py'),entry.case_id,path.join(out,entry.key+'.json')],{cwd:root,encoding:'utf8'});
 if(!inspected.stdout.trim())throw Error('Independent case checker failed: '+inspected.stderr.slice(-1000));
 entry.oracle=JSON.parse(inspected.stdout);await fs.writeFile(path.join(out,entry.key+'-oracle.json'),JSON.stringify(entry.oracle,null,2)+'\n');
 if(!entry.oracle.ok){entry.status='semantic_review_required';console.log('SEMANTIC REVIEW',entry.key,JSON.stringify(entry.oracle.failures));await capture('semantic-gaps');return;}
 await page.$$eval('.canvas-tabs [role="tab"]',ns=>ns[1].click());
 if(p.review.status==='ready'){
  await page.waitForSelector('.document-approve:not([disabled])');await page.locator('.document-approve:not([disabled])').click();
  await page.waitForFunction(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())).review.status==='approved',{},entry.project_id);
  entry.actions.push({kind:'explicit_ui_approval',at:new Date().toISOString()});
 }else{assert.equal(p.review.status,'approved');entry.actions.push({kind:'approved_snapshot_ui_reexport',backend_build:index.health.backend_build,at:new Date().toISOString()});}
 p=await capture('approved');
 await page.locator('.export-menu>.button').click();await page.locator('.export-handoff').click();
 await page.waitForFunction(()=>!!document.querySelector('.handoff-status'));
 const status=await page.$eval('.handoff-status',e=>e.textContent);assert.ok(!status.includes('还有内容需要处理'),status);
 await page.waitForFunction(()=>[...document.querySelectorAll('.handoff-dialog button')].some(n=>n.textContent.includes('生成交接包')&&!n.disabled));
 await page.$$eval('.handoff-dialog button',nodes=>nodes.find(n=>n.textContent.includes('生成交接包'))?.click());
 await page.waitForFunction(()=>!!document.querySelector('.handoff-dialog a[download]')||!!document.querySelector('.handoff-dialog .inline-error'));
 const buildError=await page.$('.handoff-dialog .inline-error');if(buildError)throw Error('UI交接生成失败：'+await buildError.evaluate(n=>n.textContent));
 await page.waitForSelector('.handoff-dialog a[download]');
 const href=await page.$eval('.handoff-dialog a[download]',e=>e.href);
 const archive=await fetch(href);assert.ok(archive.ok);await fs.writeFile(path.join(out,entry.key+'-handoff.zip'),Buffer.from(await archive.arrayBuffer()));
 entry.actions.push({kind:'ui_handoff_export',url:href,at:new Date().toISOString()});
 await screenshot(path.join(out,entry.key+'-handoff.png'));
 await page.locator('[aria-label="关闭需求交接"]').click();
 await page.locator('[aria-label="打开器件与证据"]').click();await page.waitForSelector('.device-panel');
 await screenshot(path.join(out,entry.key+'-evidence.png'));
 entry.status='approved';entry.completed_at=new Date().toISOString();await capture('final');await compactSnapshot();
}

try{
 for(let round=roundStart;round<=rounds;round++)for(const c of cases){
  const key=c.id+'-R'+round;
  entry=index.cases.find(x=>x.key===key);
  if(entry?.status==='approved'&&mode==='auto'){console.log('SKIP already approved',key);continue;}
  const browserContext=await browser.createBrowserContext();ownedContexts.add(browserContext);page=await browserContext.newPage();await page.setViewport({width:1520,height:1020});
  const errors=pageErrors=[];page.on('pageerror',e=>errors.push(e.message));
  if(entry)await page.evaluateOnNewDocument(id=>localStorage.setItem('requirements-studio:project',id),entry.project_id);
  await page.goto(base+'/',{waitUntil:'domcontentloaded',timeout:30000});
  await page.waitForFunction(()=>!!document.querySelector('[aria-label="输入硬件需求"]')&&!document.querySelector('.loading-state'));
  index.health=await page.evaluate(()=>fetch('/api/health').then(r=>r.json()));index.requested_model=index.health.model_label;assert.equal(index.health.model,'gpt-6-luna');
  if(!entry){
   if(await page.$eval('.sidebar',e=>e.getBoundingClientRect().width<50))await page.locator('.toggle-sidebar').click();
   await page.locator('.new-project').click();await page.waitForSelector('.chat-empty');
   const id=await page.evaluate(()=>localStorage.getItem('requirements-studio:project'));
   entry={key,case_id:c.id,round,project_id:id,title:`验收 ${suite} · ${c.id} · R${round} · ${c.title}`,source:c.source,source_sha256:c.sha256,actions:[],status:'in_progress'};index.cases.push(entry);await saveIndex();
   entry.backend_build=index.health.backend_build;entry.persona_sha256=createHash('sha256').update(JSON.stringify(profiles[c.id]??{})).digest('hex');
   entry.oracle_sha256=createHash('sha256').update(JSON.stringify(oracleRules[c.id])).digest('hex');
   entry.oracle_file_sha256=createHash('sha256').update(oracleBytes).digest('hex');
   entry.checker_sha256=createHash('sha256').update(await fs.readFile(path.join(root,'tests/check_case.py'))).digest('hex');
   entry.browser_asset=await page.$eval('script[src]',e=>new URL(e.src).pathname);
   await page.locator('.project-title-button').click();await page.waitForSelector('.dialog input');await fill('.dialog input',entry.title);await page.locator('.dialog-actions .primary').click();await page.waitForFunction(()=>!document.querySelector('.dialog[open]'));
   current=await send(c.prompt,'original_case_txt');
  }else{current=await settle();if(!current.messages.some(m=>m.role==='user'))current=await send(c.prompt,'original_case_txt_recovered');}
  if(mode==='export'){assert.equal(current.review.status,'approved');await approveAndExport(current);console.log('Verified UI re-export',key);await browserContext.close();continue;}
  if(current.latest_run&&['failed','interrupted','cancelled'].includes(current.latest_run.status)&&arg('retry-failed','0')==='1'){
   const previous=current.latest_run.id;await page.waitForSelector('.run-notice .text-button');await page.locator('.run-notice .text-button').click();
   await page.waitForFunction(async({id,previous})=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run?.id!==previous,{timeout:30000},{id:entry.project_id,previous});
   entry.actions.push({kind:'explicit_ui_retry',previous_run:previous,at:new Date().toISOString()});await saveIndex();current=await settle();
  }
  if(mode==='first'){entry.status=current.latest_run?.status==='completed'?'needs_input':'run_failed';await capture('first');console.log(JSON.stringify({key,project_id:entry.project_id,status:entry.status,review:current.review.status,questions:current.document.questions,modules:current.convergence.modules.map(m=>({name:m.name,state:m.state,quantity:m.quantity})),reply:current.messages.at(-1)?.content},null,2));await browserContext.close();continue;}
  if(mode==='answer'){
   const file=arg('answers-file'),textfile=arg('text-file');
   if(file)current=await answer(JSON.parse(await fs.readFile(file,'utf8')));
   else if(textfile)current=await send(await fs.readFile(textfile,'utf8'));
   else throw new Error('answer mode needs --answers-file or --text-file');
   if(current.latest_run?.status==='failed'||current.latest_run?.status==='interrupted'||current.latest_run?.status==='cancelled'){entry.status='run_failed';await capture('failed');}else if(current.review.status==='ready')await approveAndExport(current);
   else{entry.status='needs_input';await capture('answered');}
   console.log(JSON.stringify({key,project_id:entry.project_id,status:entry.status,review:current.review.status,questions:current.document.questions,reply:current.messages.at(-1)?.content},null,2));await browserContext.close();continue;
  }
  if(current.latest_run&&['failed','interrupted','cancelled'].includes(current.latest_run.status)){
   entry.status='run_failed';await capture('failed');await browserContext.close();continue;
  }
  const persona=profiles[c.id];let attempts=0,internalAttempts=0;
  if(arg('briefing','0')==='1'&&persona?.briefing&&!entry.actions.some(a=>a.kind==='persona_design_boundary')){
   current=await send(persona.briefing,'persona_design_boundary');await capture('briefing');
  }
  while(current.review.status!=='ready'&&current.review.status!=='approved'&&attempts++<9){
   if(current.latest_run?.status==='failed'){entry.status='run_failed';break;}
   if(current.document.questions.length){
    let manual={};try{manual=JSON.parse(await fs.readFile(path.join(out,key+'-answers.json'),'utf8'));}catch{}
    const answers={};for(const q of current.document.questions){const a=manual[q.id]??response(q,current,persona);const repeated=entry.actions.some(action=>action.kind==='question_answers'&&action.answers[q.id]===a);if(a&&!repeated)answers[q.id]=a;}
    if(!Object.keys(answers).length){entry.status='needs_manual';break;}
    current=await answer(answers);
   }else if(current.convergence.module_pending.length){current=await send(moduleConsent(current),'module_consent');}
   else{if(internalAttempts++>=2){entry.status='needs_manual';break;}current=await send('请依据已经提供的原话完成内部核对，保留全部实际功能、数量和指定器件。已明确的决定不要重复询问；未给出的实现参数保持待设计。若仍有阻碍整体需求审阅的真正产品问题，请明确提出。','internal_recheck');}
   await capture('turn-'+attempts);
  }
  if(current.latest_run?.status==='failed'||current.latest_run?.status==='interrupted'||current.latest_run?.status==='cancelled'){entry.status='run_failed';await capture('failed');}else if(current.review.status==='ready')await approveAndExport(current);
  else if(current.review.status==='approved'){entry.status='approved';await capture('final');}
  else{entry.status=entry.status==='in_progress'?'needs_manual':entry.status;await capture('pending');}
  entry.browser_errors=[...new Set([...(entry.browser_errors??[]),...errors])];await saveIndex();console.log(JSON.stringify({key,project_id:entry.project_id,status:entry.status,review:current.review.status,user_turns:entry.user_turns,questions:current.document.questions.map(q=>({id:q.id,question:q.question}))}));await browserContext.close();
 }
}catch(error){if(entry){entry.status='driver_failed';entry.error=String(error);await capture('driver-failed').catch(()=>{});}console.error(error);process.exitCode=1;}
finally{await Promise.allSettled([...ownedContexts].map(c=>c.close()));if(shared)browser.disconnect();else await browser.close();await fs.rm(profile,{recursive:true,force:true});await saveIndex();}
