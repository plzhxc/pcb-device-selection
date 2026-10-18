// Two seeded, isolated browser walkthroughs. Includes deliberate network/provider faults.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const root=process.cwd(),out=path.join(root,'.runtime/ux/verification');
const fixtures=JSON.parse(await fs.readFile(path.join(out,'fixtures.json'),'utf8'));
const require=createRequire(path.join(root,'frontend/package.json')),profile=path.join(out,'browser-profile');
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',pipe:true,headless:true,userDataDir:profile,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage']});
const rounds=[];let page,id,round,checks,errors;let delayed=false,abortPost=false,posts=0;
const get=()=>page.evaluate(async id=>(await fetch('/api/projects/'+id)).json(),id);
const check=(name,value)=>{assert.ok(value,name);checks.push(name);console.log(`PASS UX R${round} ${name}`);};
async function input(selector,text){await page.focus(selector);await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.press('Backspace');await page.keyboard.sendCharacter(text);}
async function idle(failed=false){await page.waitForFunction(async id=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.latest_run&&!['queued','running'].includes(p.latest_run.status)&&!p.input_queue.length;},{timeout:60000},id);const p=await get();if(!failed)assert.equal(p.latest_run.status,'completed');await page.waitForFunction(v=>document.querySelector('.canvas-header .version-label')?.textContent===`v${v}`,{timeout:15000},p.document.version);return p;}
async function open(next){id=next;await page.evaluate(id=>localStorage.setItem('requirements-studio:project',id),id);await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('.question-group,.coverage-panel');await page.waitForFunction(()=>!document.querySelector('[aria-label="输入硬件需求"]').disabled);}
async function send(text){await input('[aria-label="输入硬件需求"]',text);await page.locator('.composer-buttons .send-button:not(.stop)').click();await page.waitForFunction(()=>document.querySelector('[aria-label="输入硬件需求"]').value==='');}
const tab=n=>page.locator(`.canvas-tabs [role="tab"]:nth-child(${n})`).click();
try{for(const f of fixtures.rounds){
  round=f.round;checks=[];errors=[];posts=0;page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1440,height:1000});await page.setRequestInterception(true);
  page.on('request',async req=>{if(req.method()==='POST'&&req.url().endsWith('/messages')){posts++;if(abortPost){abortPost=false;await req.abort('failed');return;}if(delayed){delayed=false;await new Promise(r=>setTimeout(r,1500));}}await req.continue();});
  const injection=await page.evaluateOnNewDocument(id=>localStorage.setItem('requirements-studio:project',id),f.answers);id=f.answers;
  await page.goto('http://[::1]:8893/',{waitUntil:'domcontentloaded'});await page.waitForSelector('.question-option');await page.removeScriptToEvaluateOnNewDocument(injection.identifier);
  check('fixed Codex model label',await page.$eval('.model-label',e=>e.textContent.includes('gpt6-luna-xhigh')));
  await input('[aria-label="输入硬件需求"]','独立草稿：想补充外壳颜色');await page.locator('[data-question-id="budget"] .question-option:first-child').click();delayed=true;
  await page.$eval('.question-group',form=>{form.requestSubmit();form.requestSubmit();});
  await page.waitForSelector('.question-receipt[data-question-id="budget"]');
  check('answer choices close before HTTP response',await page.$$eval('[data-question-id="budget"] .question-option',e=>e.length)===0);
  check('other branches remain available',await page.$$eval('fieldset.question-card',e=>e.length)===2);
  await page.waitForFunction(()=>document.querySelector('.question-receipt')?.textContent.includes('正在分析'));
  check('rapid double submit makes one request',posts===1);
  check('answer preserves independent composer draft',await page.$eval('[aria-label="输入硬件需求"]',e=>e.value==='独立草稿：想补充外壳颜色'));
  check('receipt exists on server',(await get()).question_submissions.some(s=>s.question_id==='budget'&&s.status==='processing'));
  const duplicate=await page.evaluate(async id=>{const r=await fetch('/api/projects/'+id+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:'[慢分析] 60元',request_id:'duplicate-'+crypto.randomUUID(),answers:[{question_id:'budget',answer:'[慢分析] 60元',deferred:false}]})});return r.status;},id);
  check('server rejects repeated answer with a different request id',duplicate===409);
  await page.locator('[data-question-id="size"] .question-option:first-child').click();await page.locator('.question-footer .primary').click();await page.waitForFunction(()=>document.querySelector('[data-question-id="size"]')?.textContent.includes('已排队'));
  check('second branch can queue while first is analysed',(await get()).input_queue.length===1);
  await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('.question-receipt[data-question-id="budget"]');
  check('refresh retains both answer receipts',await page.$$eval('.question-receipt',e=>e.length)===2);
  check('refresh cannot reopen submitted choices',await page.$$eval('[data-question-id="budget"] button.question-option,[data-question-id="size"] button.question-option',e=>e.length)===0);
  await page.waitForSelector('[aria-label="本轮分析进度"]');
  check('live progress describes actual stages',await page.$eval('.live-analysis',e=>e.textContent.includes('已用')&&e.textContent.includes('实际处理进度')));
  await page.screenshot({path:path.join(out,`receipts-running-${round}.png`)});await idle();
  check('completed answers remove only their branches',(await get()).document.questions.map(q=>q.id).join(',')==='refresh');
  await page.waitForSelector('[data-question-id="refresh"] .custom-answer');await input('[data-question-id="refresh"] .custom-answer','稍后补充刷新要求');await page.locator('.question-footer>button:first-child').click();await idle();await page.waitForSelector('.question-receipt[data-question-id="refresh"]');
  check('deferral collapses options while retaining question',(await get()).document.questions[0].status==='deferred');
  await page.locator('.question-receipt .text-button').click();await page.waitForSelector('[data-question-id="refresh"] .custom-answer');
  check('explicit resume restores the unsent answer draft',await page.$eval('[data-question-id="refresh"] .custom-answer',e=>e.value==='稍后补充刷新要求'));
  await input('[data-question-id="refresh"] .custom-answer','[故障重试] 每1秒刷新');abortPost=true;await page.locator('.question-footer .primary').click();await page.waitForSelector('.error-banner');await page.waitForSelector('[data-question-id="refresh"] .custom-answer');
  check('network failure restores choices and answer draft',await page.$eval('[data-question-id="refresh"] .custom-answer',e=>e.value==='[故障重试] 每1秒刷新'));
  await page.locator('.question-footer .primary').click();let p=await idle(true);check('provider failure leaves the previous document intact',p.latest_run.status==='failed'&&p.document.questions.length===1);
  await page.waitForSelector('.question-receipt');check('failed answer remains saved without selectable options',await page.$$eval('[data-question-id="refresh"] .question-option',e=>e.length)===0);
  const messages=p.messages.filter(m=>m.role==='user').length,previousRun=p.latest_run.id;await page.locator('.run-notice .text-button').click();await page.waitForFunction(async({id,run})=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run.id!==run,{}, {id,run:previousRun});p=await idle();
  check('explicit retry reuses the user message',p.messages.filter(m=>m.role==='user').length===messages);
  check('retry resolves the remaining branch',p.document.questions.length===0);
  await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('.run-history');await page.locator('.run-history summary').click();
  check('actual processing history survives refresh',await page.$eval('.run-history[open]',e=>e.textContent.includes('等待开始分析')&&!!e.querySelector('time')));
  await tab(4);await page.waitForSelector('.scope-tabs');
  check('default canvas avoids the full engineering checklist',await page.$eval('.scope-tabs .active',e=>e.textContent)==='产品决定'&&await page.$('.field-toolbar')===null);
  await page.locator('.scope-tabs button:nth-child(2)').click();await page.select('[aria-label="筛选核对状态"]','all');
  check('professional view omits unused module topics',!(await page.$$eval('.field-topic',els=>els.some(e=>e.dataset.topicId.startsWith('V3-5.2.4')))));
  await page.locator('.scope-tabs button:nth-child(3)').click();await page.select('[aria-label="筛选核对状态"]','all');
  check('complete reference remains available separately',await page.$$eval('.field-topic',e=>e.length)===50);
  await open(f.ready);p=await get();await tab(2);await page.waitForSelector('.full-document .markdown');
  check('product can be reviewed with honest unknown engineering tasks',p.review.status==='ready'&&p.convergence.fields_closed===5&&p.convergence.product_review.engineering_pending>0);
  check('specification contains selected modules only',p.markdown.includes('LCD与OLED显示模块')&&!p.markdown.includes('Wi-Fi 子模块')&&!p.markdown.includes('充电管理子模块'));
  await page.locator('.document-approve').click();await page.waitForFunction(()=>document.querySelector('.draft-badge')?.textContent==='已审核');
  check('whole-document approval is an explicit UI action',(await get()).review.status==='approved');
  await open(f.proposal);await page.waitForSelector('.module-plan-review');
  check('unconfirmed module plan offers one understandable group decision',(await get()).review.status==='draft'&&await page.$$eval('.module-plan-review li',e=>e.length)===1);
  check('review explains the actual module blocker instead of listing only zeros',await page.$eval('.review-readiness.draft',e=>e.textContent.includes('1 个模块方案待确认')));
  await page.locator('.confirm-module-plan').click();await page.waitForFunction(()=>document.querySelector('.confirm-module-plan')?.disabled===true);await idle();
  check('group confirmation saves explicit consent to actual categories',(await get()).convergence.modules.every(m=>m.state==='confirmed')&&(await get()).messages.some(m=>m.role==='user'&&m.content.includes('LCD与OLED显示模块')));
  check('group confirmation does not ask for chip or pin parameters',(await get()).review.status==='ready'&&(await get()).convergence.fields_closed===5);
  await open(f.ready);
  await tab(4);await page.locator('.scope-tabs button:nth-child(2)').click();await page.select('[aria-label="筛选核对状态"]','all');
  for(const width of [320,390,768,1440]){await page.setViewport({width,height:1000});if(width<760)await page.locator('.mobile-switch [role="tab"]:nth-child(2)').click();await page.waitForFunction(()=>document.querySelector('.canvas-panel').getBoundingClientRect().width>280);check(`no whole-page overflow at ${width}px`,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));if(width===390)await page.screenshot({path:path.join(out,`mobile-${round}.png`)});}
  await page.screenshot({path:path.join(out,`ready-${round}.png`)});check('zero browser runtime errors',errors.length===0);
  rounds.push({round,seed:f.seed,product:f.product,provider:'controlled_ux_fixture',real_inference:false,checks,errors,script_asset:await page.$eval('script[src]',e=>new URL(e.src).pathname)});
  await fs.writeFile(path.join(out,'browser.json'),JSON.stringify({ok:rounds.length===2,rounds},null,2));await page.close();
}}
catch(error){await page?.screenshot({path:path.join(out,`failure-${round}.png`)}).catch(()=>{});await fs.writeFile(path.join(out,'browser-failure.json'),JSON.stringify({round,id,error:error.message,checks},null,2));throw error;}
finally{await browser.close();await fs.rm(profile,{recursive:true,force:true});}
console.log(JSON.stringify({ok:true,rounds:rounds.map(r=>({round:r.round,checks:r.checks.length}))}));
