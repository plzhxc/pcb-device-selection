// Human-reviewed acceptance: each invocation performs one real UI action.
// No generated bulk specification, automatic customer answers, or approval bypass.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {gzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import assert from 'node:assert/strict';

const root=process.cwd(),args=process.argv.slice(2);
const arg=(name,fallback='')=>{const i=args.indexOf('--'+name);return i<0?fallback:args[i+1];};
const suite=arg('suite','plain-users-release8'),base=arg('base','http://[::1]:8878');
assert.match(suite,/^[A-Za-z0-9_-]+$/);
const original=arg('original','0')==='1';
const dir=path.join(root,original?'.runtime/phase4':'.runtime/customer-acceptance',suite);
await fs.mkdir(dir,{recursive:true});
let input='';for await(const chunk of process.stdin)input+=chunk.toString('utf8');
const request=JSON.parse(input);
if(original&&request.use_case_txt){
 const cases=JSON.parse(await fs.readFile('.runtime/phase3/cases.json','utf8')),c=cases.find(c=>c.id===request.case_id);assert.ok(c);
 const bytes=await fs.readFile(c.source);assert.equal(createHash('sha256').update(bytes).digest('hex'),c.sha256,'Case source changed');
 request.text=c.prompt;
}
const key=request.key;assert.match(key,/^[A-Za-z0-9_-]+$/);
const indexFile=path.join(dir,'index.json');
let index;try{index=JSON.parse(await fs.readFile(indexFile,'utf8'));}catch{index={suite,base,input_mode:original?'original_txt':'ordinary_customer',cases:[],created_at:new Date().toISOString()};}
let entry=index.cases.find(e=>e.key===key),page;
const bridge=JSON.parse(await fs.readFile(path.join(root,'.runtime/phase3/browser-bridge.json'),'utf8'));
const require=createRequire(path.join(root,'frontend/package.json'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws,defaultViewport:null});
const context=await browser.createBrowserContext();const errors=[];
async function save(){
 const lock=indexFile+'.lock';let handle;
 for(let n=0;n<200;n++){try{handle=await fs.open(lock,'wx');break;}catch(e){if(e.code!=='EEXIST')throw e;await new Promise(r=>setTimeout(r,30));}}
 assert.ok(handle,'Acceptance index is busy');
 try{let previous;try{previous=JSON.parse(await fs.readFile(indexFile,'utf8'));}catch{previous={cases:[]};}
  const merged=new Map(previous.cases.map(c=>[c.key,c]));if(entry)merged.set(key,entry);
  const next={...previous,...index,cases:[...merged.values()]},temp=indexFile+'.'+process.pid+'.tmp';await fs.writeFile(temp,JSON.stringify(next,null,2)+'\n');await fs.rename(temp,indexFile);index=next;
 }finally{await handle.close();await fs.rm(lock,{force:true});}
}
async function project(){return page.evaluate(id=>fetch('/api/projects/'+id).then(r=>{if(!r.ok)throw Error('Project read '+r.status);return r.json();}),entry.project_id);}
async function capture(){
 const p=await project();const bytes=Buffer.from(JSON.stringify(p));
 if(original){entry.input_strategy='exact_txt_then_focused_customer_answers';entry.persona_usage='reference_catalogue_hash_only_not_an_answer_template';}
 await fs.writeFile(path.join(dir,key+'.json.gz'),gzipSync(bytes));
 const lines=['# '+entry.title,'',`输入方式：${index.input_mode}`,''];
 for(const m of p.messages){lines.push(`## ${m.role==='user'?'客户':'需求助手'} · ${m.created_at}`,'',m.content,'');}
 await fs.writeFile(path.join(dir,key+'-transcript.md'),lines.join('\n'));
 entry.version=p.document.version;entry.review=p.review.status;entry.requirements=p.document.requirements.length;
 entry.user_turns=p.messages.filter(m=>m.role==='user').length;
 entry.status=p.review.status==='approved'?'approved':p.latest_run?.status==='failed'?'run_failed':['queued','running'].includes(p.latest_run?.status)||p.input_queue.length?'analyzing':'needs_review';
 entry.browser_errors=[...new Set([...(entry.browser_errors??[]),...errors])];entry.updated_at=new Date().toISOString();await save();
 if(request.screenshot){await page.bringToFront();await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));await page.screenshot({path:path.join(dir,key+'-'+request.screenshot+'.webp'),type:'webp',quality:87});}
 const v=p.convergence?.product_review;
 return {key,project_id:p.id,url:base+'/?project='+p.id,version:p.document.version,run:p.latest_run&&{id:p.latest_run.id,status:p.latest_run.status,error:p.latest_run.error},queue:p.input_queue.length,domain_jobs:p.domain?.active_jobs,
  review:p.review.status,decision_pending:v?.decision_pending,internal_rechecks:p.review.internal_rechecks,dependency_reviews:p.review.dependency_reviews,module_pending:p.convergence?.module_pending,issues:p.convergence?.issues,
  questions:p.document.questions.map(q=>({id:q.id,question:q.question,options:q.options,why:q.why,decision_key:q.decision_key})),
  modules:request.include_modules?p.convergence?.modules?.filter(m=>m.state!=='not_applicable').map(m=>({name:m.name,type:m.type_name,state:m.state,quantity:m.quantity,purpose:m.purpose})):undefined,
  reply:p.messages.filter(m=>m.role==='assistant').slice(-2).map(m=>m.content),user_turns:entry.user_turns,browser_errors:entry.browser_errors};
}
async function send(text){
 // Paste literal text: locator.fill dispatches Enter for newlines, while this
 // product intentionally uses Enter to send and Shift+Enter for line breaks.
 await page.focus('[aria-label="输入硬件需求"]');
 await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');
 await page.keyboard.press('Backspace');await page.keyboard.sendCharacter(text);
 assert.equal(await page.$eval('[aria-label="输入硬件需求"]',e=>e.value),text);
 const p=await project(),before=p.messages.filter(m=>m.role==='user').length,queued=p.input_queue.length;
 await page.locator('.composer-buttons .send-button:not(.stop)').click();
 await page.waitForFunction(async({id,before,queued})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length>queued;},{timeout:20000},{id:entry.project_id,before,queued});
 entry.actions.push({kind:'customer_message',text,at:new Date().toISOString()});await save();
}
try{
 page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1440,height:1000});
 await page.goto(base+'/' +(entry?'?project='+entry.project_id:''),{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>!!document.querySelector('[aria-label="输入硬件需求"]')&&!document.querySelector('.loading-state'));
 const health=await page.evaluate(()=>fetch('/api/health').then(r=>r.json()));index.health=health;
 if(request.action==='init'){
  assert.ok(!entry,'Refusing to duplicate an existing journey');
  if(!await page.$('.sidebar-open'))await page.locator('.toggle-sidebar').click();
  await page.locator('.new-project').click();await page.waitForSelector('.chat-empty');
  const id=await page.evaluate(()=>localStorage.getItem('requirements-studio:project'));
  entry={key,case_id:request.case_id,round:request.round??1,project_id:id,title:request.title,service_base:base,actions:[],status:'new',backend_build:health.backend_build};index.cases.push(entry);await save();
  entry.browser_asset=await page.$eval('script[src]',e=>new URL(e.src).pathname);
  if(original){
   const cases=JSON.parse(await fs.readFile('.runtime/phase3/cases.json','utf8')),c=cases.find(c=>c.id===entry.case_id);assert.ok(c);
   assert.equal(request.text,c.prompt,'Original-case input must exactly match the source TXT');entry.source=c.source;entry.source_sha256=c.sha256;
   const personas=JSON.parse(await fs.readFile('tests/phase4_profiles.json','utf8')),rules=JSON.parse(await fs.readFile('tests/phase4_oracles.json','utf8'));
   entry.persona_sha256=createHash('sha256').update(JSON.stringify(personas[c.id])).digest('hex');entry.oracle_sha256=createHash('sha256').update(JSON.stringify(rules[c.id])).digest('hex');entry.checker_sha256=createHash('sha256').update(await fs.readFile('tests/check_case.py')).digest('hex');
   entry.oracle_file_sha256=createHash('sha256').update(await fs.readFile('tests/phase4_oracles.json')).digest('hex');
  }
  await page.locator('.project-title-button').click();await page.waitForSelector('.dialog input');await page.locator('.dialog input').fill(entry.title);await page.locator('.dialog-actions .primary').click();await page.waitForFunction(()=>!document.querySelector('.dialog[open]'));
  await send(request.text);
 }else{
  assert.ok(entry,'Unknown journey');await page.waitForFunction(id=>localStorage.getItem('requirements-studio:project')===id,{},entry.project_id);
  if(request.action==='send')await send(request.text);
  else if(request.action==='discuss_module'){
   const p=await project(),m=p.convergence.modules.find(m=>m.id===request.module_id);assert.ok(m,'Unknown module');
   await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(e=>e.textContent.includes('模块方案')).click());
   const card=`.module-instance[data-module-id="${m.id}"]`;
   await page.waitForSelector(card);
   await page.$eval(card,e=>{const button=[...e.querySelectorAll('button')].find(b=>b.textContent.includes('继续讨论'));if(!button)throw Error('Module discussion button missing');button.click();});
   await page.waitForFunction(name=>document.querySelector('[aria-label="输入硬件需求"]')?.value.includes(name),{},m.name);
   const text=await page.$eval('[aria-label="输入硬件需求"]',e=>e.value);
   entry.actions.push({kind:'module_discussion_button',module_id:m.id,module_name:m.name,prefilled_text:text,at:new Date().toISOString()});await save();
   await send(text+(request.text?'\n\n'+request.text:''));
  }
  else if(request.action==='upload_reference'){
   const file=await fs.realpath(request.path);assert.ok(file.startsWith('/share/home/chenxin/'),'Reference path outside authorized workspace');
   const bytes=await fs.readFile(file),p=await project(),before=p.messages.filter(m=>m.role==='user').length,queued=p.input_queue.length;
   const response=page.waitForResponse(r=>r.url().endsWith('/reference-designs')&&r.request().method()==='POST');
   const input=await page.$('.reference-file-input');assert.ok(input);await input.uploadFile(file);
   assert.ok((await response).ok(),'Reference upload rejected');
   await page.waitForFunction(async({id,before,queued})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length>queued;},{timeout:20000},{id:entry.project_id,before,queued});
   entry.actions.push({kind:'ui_reference_upload',path:file,sha256:createHash('sha256').update(bytes).digest('hex'),bytes:bytes.length,at:new Date().toISOString()});await save();
  }
  else if(request.action==='edit_interface'){
   const p=await project();assert.ok(!['queued','running'].includes(p.latest_run?.status)&&!p.input_queue.length);
   const row=p.document.external_interfaces.find(r=>r.id===request.interface_id);assert.ok(row);assert.ok(['purpose','external_object','protocol','signalText','voltage','shared_link'].includes(request.field));
   await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(n=>n.textContent.includes('模块方案')).click());
   const selector=`[data-interface-id="${row.id}"]`;await page.locator(selector+' details summary').click();await page.locator(selector+` input[name="${request.field}"]`).fill(request.value);
   const before=p.messages.filter(m=>m.role==='user').length;await page.locator(selector+' form button').click();
   await page.waitForFunction(async({id,before})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length;},{timeout:20000},{id:entry.project_id,before});
   assert.equal(await page.$eval(selector+' details',e=>e.open),false,'Submitted interface form must collapse');
   await page.locator(selector+' details summary').click();assert.equal(await page.$eval(selector+' form button',e=>e.disabled),true,'Submitted row cannot be submitted repeatedly');
   entry.actions.push({kind:'ui_interface_row_edit',interface_id:row.id,field:request.field,value:request.value,before_rows:p.document.external_interfaces,controls_closed:true,repeat_submit_disabled:true,at:new Date().toISOString()});await save();
  }
  else if(request.action==='adopt_reference'){
   const p=await project();assert.ok(request.review_note,'Review the current core-device plan before adoption');assert.ok(!['queued','running'].includes(p.latest_run?.status)||(p.latest_run?.kind==='continuation'&&p.latest_run.status==='queued'));
   await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(e=>e.textContent.includes('模块方案')).click());await page.waitForSelector('.adopt-reference-core:not([disabled])');
   const before=p.messages.filter(m=>m.role==='user').length;const offered=p.delivery.bom.rows.filter(r=>r.core&&r.status==='proposed');assert.ok(offered.length);
   await page.locator('.adopt-reference-core:not([disabled])').click();
   await page.waitForFunction(async({id,before})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length;},{timeout:20000},{id:entry.project_id,before});
   entry.actions.push({kind:'ui_core_plan_adoption',offered:offered.map(r=>({model:r.model,quantity:r.observed_quantity,purpose:r.purpose})),review_note:request.review_note,at:new Date().toISOString()});await save();
  }
  else if(request.action==='adopt_modules'){
   const p=await project();assert.ok(request.review_note,'Read the offered module purposes before adopting');
   assert.ok(!['queued','running'].includes(p.latest_run?.status)&&!p.input_queue.length,'Wait for the current changes');
   const offered=await page.$$eval('.module-plan-review li',els=>els.map(e=>e.textContent));assert.ok(offered.length);
   const before=p.messages.filter(m=>m.role==='user').length;
   if(request.from_module_view){await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(e=>e.textContent.includes('模块方案')).click());await page.waitForSelector('.module-board .confirm-module-plan:not([disabled])');}
   await page.locator(request.from_module_view?'.module-board .confirm-module-plan:not([disabled])':'.confirm-module-plan:not([disabled])').click();
   await page.waitForFunction(async({id,before})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length;},{timeout:20000},{id:entry.project_id,before});
   await page.waitForFunction(()=>!document.querySelector('.confirm-module-plan')||document.querySelector('.confirm-module-plan').disabled,{timeout:20000});
   entry.actions.push({kind:'ui_module_plan_adoption',from_module_view:!!request.from_module_view,offered,review_note:request.review_note,at:new Date().toISOString()});await save();
  }
  else if(request.action==='answer'){
   const p=await project(),typed=request.answers??{},choices=request.choices??{};
   const ids=[...new Set([...Object.keys(typed),...Object.keys(choices)])],resolved={...typed};assert.ok(ids.length>0&&ids.length<=3);
   const before=p.messages.filter(m=>m.role==='user').length,queued=p.input_queue.length;
   for(const id of ids)assert.ok(p.document.questions.some(q=>q.id===id),'Question is not pending: '+id);
   if(await page.$('[aria-label="筛选问题分支"]'))await page.select('[aria-label="筛选问题分支"]','all');
   if(await page.$('.question-expand')&&await page.$eval('.question-expand',e=>e.textContent.startsWith('展开')))await page.locator('.question-expand').click();
   for(const id of ids){
    if(!await page.$(`[data-question-id="${id}"] .custom-answer`)){
     const followup=await page.$(`[data-question-id="${id}"] .answer-receipt-status button`);
     if(followup){await followup.click();entry.actions.push({kind:'open_followup_answer',question_id:id,at:new Date().toISOString()});}
    }
    await page.waitForSelector(`[data-question-id="${id}"] .custom-answer`);
    if(Object.hasOwn(choices,id)){
     assert.ok(!Object.hasOwn(typed,id),'Choose either an option or a custom reply');
     const q=p.document.questions.find(q=>q.id===id),choice=choices[id];assert.ok(Number.isInteger(choice)&&choice>=0&&choice<q.options.length);
     const label=q.options[choice],selector=`[data-question-id="${id}"] .question-options button:nth-child(${choice+1})`;
     assert.ok((await page.$eval(selector,e=>e.textContent)).includes(label),'Question options changed before click');
     await page.locator(selector).click();resolved[id]=label;
    }else await page.locator(`[data-question-id="${id}"] .custom-answer`).fill(typed[id]);
   }
   await page.locator('.question-footer .primary').click();
   for(const id of ids)await page.waitForFunction(id=>!document.querySelector(`fieldset[data-question-id="${id}"]`),{timeout:20000},id);
   await page.waitForFunction(async({id,before,queued})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length>queued;},{timeout:20000},{id:entry.project_id,before,queued});
   entry.actions.push({kind:'question_answers',answers:resolved,choice_indexes:choices,at:new Date().toISOString(),controls_closed:true});await save();
  }else if(request.action==='retry'){
   const p=await project();assert.ok(['failed','interrupted','cancelled'].includes(p.latest_run?.status));
   const previous=p.latest_run.id;await page.locator('.run-notice .text-button').click();
   await page.waitForFunction(async({id,previous})=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run?.id!==previous,{timeout:20000},{id:entry.project_id,previous});
   entry.actions.push({kind:'explicit_ui_retry',previous_run:previous,at:new Date().toISOString()});await save();
  }else if(request.action==='continue'){
   const p=await project(),before=p.messages.filter(m=>m.role==='user').length,queued=p.input_queue.length;
   await page.locator('.coverage-actions .text-button:not([disabled])').click();
   await page.waitForFunction(async({id,before,queued})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.messages.filter(m=>m.role==='user').length>before||p.input_queue.length>queued;},{timeout:20000},{id:entry.project_id,before,queued});
   entry.actions.push({kind:'visible_continue_button',at:new Date().toISOString()});await save();
  }else if(request.action==='approve'){
   const p=await project();assert.equal(p.review.status,'ready','Only reviewed ready documents can be approved');assert.ok(request.review_note);
   await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(e=>e.textContent.includes('需求说明书')).click());await page.waitForSelector('.document-approve:not([disabled])');await page.locator('.document-approve:not([disabled])').click();
   await page.waitForFunction(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())).review.status==='approved',{timeout:30000},entry.project_id);
   entry.actions.push({kind:'explicit_ui_approval',review_note:request.review_note,at:new Date().toISOString()});await save();
  }else if(request.action==='refresh_knowledge'){
   const p=await project();assert.equal(p.domain.active_jobs,0,'Wait for the previous knowledge job');
   await page.locator('[aria-label="打开器件与证据"]').click();await page.waitForSelector('.device-panel');
   const response=page.waitForResponse(r=>r.url().endsWith('/knowledge-jobs')&&r.request().method()==='POST');
   await page.$$eval('.device-panel .section-heading button',nodes=>nodes.find(n=>n.textContent.includes('重新核对')).click());
   const receipt=await (await response).json();entry.actions.push({kind:'ui_refresh_real_knowledge',job_id:receipt.job_id,at:new Date().toISOString()});await save();
   const observations=[];const start=Date.now();
   while(Date.now()-start<50000){const current=await project();observations.push({elapsed_ms:Date.now()-start,active_jobs:current.domain.active_jobs,revision:current.domain_revision,references:current.domain.references.map(r=>({mention:r.mention,status:r.resolution_status,documents:r.document_status,evidence:r.evidence_ids.length}))});if(!current.domain.active_jobs)break;await new Promise(r=>setTimeout(r,1500));}
   await fs.writeFile(path.join(dir,key+'-knowledge-refresh.json'),JSON.stringify({job_id:receipt.job_id,observations},null,2)+'\n');
   await page.locator('[aria-label="关闭器件与证据"]').click();
  }else if(request.action==='export'||request.action==='export_draft'){
   const p=await project(),draft=request.action==='export_draft';
   if(draft){assert.ok(['ready','draft'].includes(p.review.status));assert.ok(!['running','queued'].includes(p.latest_run?.status));}
   else assert.equal(p.review.status,'approved');
   await page.locator('.export-menu>.button').click();await page.locator('.export-handoff').click();await page.waitForSelector('.handoff-status');
   const status=await page.$eval('.handoff-status',e=>e.textContent);if(!draft)assert.ok(!status.includes('还有内容需要处理'),status);
   const response=page.waitForResponse(r=>r.url().endsWith('/downstream-adapter')&&r.request().method()==='POST');
   await page.locator('.handoff-adapter>.text-button').click();const adapter=await (await response).json();assert.ok(adapter.accepted,'Downstream intake rejected the requirements');
   await page.waitForSelector('.handoff-adapter [role="status"]');await fs.writeFile(path.join(dir,key+'-adapter.json'),JSON.stringify(adapter,null,2)+'\n');
   entry.adapter={accepted:adapter.accepted,can_start_generation:adapter.can_start_generation,state:adapter.state,selection_needed:adapter.selection_needed.length};
   await page.waitForFunction(label=>[...document.querySelectorAll('.handoff-dialog button')].some(n=>n.textContent.includes(label)&&!n.disabled),{},draft?'生成草稿包':'生成交接包');
   await page.$$eval('.handoff-dialog button',(nodes,label)=>nodes.find(n=>n.textContent.includes(label)).click(),draft?'生成草稿包':'生成交接包');await page.waitForSelector('.handoff-dialog a[download]');
   const href=await page.$eval('.handoff-dialog a[download]',e=>e.href),r=await fetch(href);assert.ok(r.ok);await fs.writeFile(path.join(dir,key+'-handoff.zip'),Buffer.from(await r.arrayBuffer()));
   await page.screenshot({path:path.join(dir,key+'-handoff.webp'),type:'webp',quality:87});await page.locator('[aria-label="关闭需求交接"]').click();entry.actions.push({kind:draft?'ui_draft_handoff_export':'ui_handoff_export',at:new Date().toISOString()});await save();
  }else assert.equal(request.action,'read');
 }
 if(request.view){await page.$$eval('.canvas-tabs [role="tab"]',(nodes,label)=>nodes.find(e=>e.textContent.includes(label)).click(),request.view);await new Promise(r=>setTimeout(r,300));}
 console.log(JSON.stringify(await capture(),null,2));
}catch(e){if(entry){entry.driver_error=String(e);await save();}throw e;}finally{await context.close();browser.disconnect();}
