import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const root=process.cwd(),out=path.join(root,'.runtime/verification/ui-audit');
const require=createRequire(path.join(root,'frontend/package.json'));
const fixtures=JSON.parse(await fs.readFile(path.join(out,'fixtures.json'),'utf8'));
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',headless:true,pipe:true,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},userDataDir:path.join(out,'interaction-profile'),args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage','--disable-background-networking']});
const reports=[];let page,round,checks,errors;
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const check=(name,value)=>{assert.ok(value,name);checks.push(name);console.log(`PASS R${round} ${name}`);};
const project=()=>page.evaluate(async()=>fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json()));
const ready=()=>page.waitForFunction(()=>!!document.querySelector('.composer textarea')&&!document.querySelector('.loading-state'));
async function choose(id){if(await page.$eval('.sidebar',el=>el.getBoundingClientRect().width<50))await page.locator('.toggle-sidebar').click();await page.locator(`.project-item[data-project-id="${id}"]`).click();await ready();await page.waitForFunction(id=>document.querySelector(`.project-item[data-project-id="${id}"]`)?.classList.contains('selected'),{},id);}
async function settle(){await page.waitForFunction(async()=>{const p=await fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json());return !['queued','running'].includes(p.latest_run?.status)&&p.input_queue.length===0;},{timeout:180000,polling:300});await page.waitForFunction(()=>!document.querySelector('[aria-label="停止分析"]'),{timeout:20000});const p=await project();assert.equal(p.latest_run?.status,'completed',p.latest_run?.error);return p;}
async function send(text){await page.locator('.composer textarea').fill(text);await page.locator('[aria-label="发送需求"], [aria-label="加入待处理补充"]').click();}
async function tab(text){await page.$$eval('.canvas-tabs button',(buttons,text)=>buttons.find(b=>b.textContent.includes(text)).click(),text);}

try {
for(const fixture of fixtures.rounds){
  round=fixture.round;checks=[];errors=[];page=await browser.newPage();await page.setViewport({width:1440,height:1000});
  page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  await page.evaluateOnNewDocument(id=>localStorage.setItem('requirements-studio:project',id),fixture.example);
  await page.goto('http://[::1]:8881/',{waitUntil:'domcontentloaded'});await ready();await page.waitForSelector('.question-card');
  await page.select('.question-filter select','performance');check('branch filter isolates performance questions',(await page.$$('.question-card')).length===2);
  await page.select('.question-filter select','all');
  await page.locator('[aria-label="自定义回答：温度测量需要达到怎样的精度？"]').fill('±0.5°C');
  await page.reload({waitUntil:'domcontentloaded'});await ready();
  check('unsubmitted question answer survives refresh',await page.$eval('[aria-label="自定义回答：温度测量需要达到怎样的精度？"]',el=>el.value==='±0.5°C'));
  await send('预算仍为80元，先补充使用说明。');await settle();
  check('question answer survives a different discussion turn',await page.$eval('[aria-label="自定义回答：温度测量需要达到怎样的精度？"]',el=>el.value==='±0.5°C'));
  await page.focus('[aria-label="自定义回答：温度测量需要达到怎样的精度？"]');await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.press('Backspace');
  await page.locator('.composer textarea').fill('尚未发送的独立想法');
  await page.locator('[data-question-id="phone_platform"] .question-option').click();await page.locator('.question-footer .primary').click();
  let p=await settle();
  check('partial answer removes only the answered branch',!p.document.questions.some(q=>q.id==='phone_platform')&&p.document.questions.some(q=>q.id==='accuracy')&&p.document.questions.some(q=>q.id==='temperature_range'));
  check('question submission preserves composer draft',await page.$eval('.composer textarea',el=>el.value==='尚未发送的独立想法'));
  check('answer is linked to a user message',p.document.requirements.some(r=>r.id==='REQ-003'&&r.value==='Android'&&r.source_type==='user'));
  await page.locator('.question-footer .text-button').click();p=await settle();
  check('deferred questions remain open',p.document.questions.filter(q=>['accuracy','temperature_range'].includes(q.id)).every(q=>q.status==='deferred'));
  await page.locator('[data-question-id="accuracy"] .custom-answer').fill('±0.5°C');await page.locator('.question-footer .primary').click();p=await settle();
  check('late answer resolves a deferred question',!p.document.questions.some(q=>q.id==='accuracy')&&p.document.questions.some(q=>q.id==='temperature_range'));
  await send('[慢分析] 增加报警，并自动续分析。');
  check('questions stay visible during analysis',(await page.$$('.question-card')).length>0);
  await page.waitForFunction(async()=>{const p=await fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json());return p.latest_run?.kind==='continuation'&&p.latest_run.status==='running';},{timeout:60000,polling:250});
  check('independent analysis continues while waiting for answers',true);
  check('questions remain answerable during independent analysis',await page.$eval('.custom-answer',el=>!el.disabled));
  await send('[慢分析] 补充A：保存30天。');await send('这条补充即将移除。');await send('补充B：预算仍为80元。');
  check('multiple follow-ups are durably queued',(await project()).input_queue.length===3);
  await page.locator('[aria-label="移除补充 2"]').click();
  check('queued message can be removed',(await project()).input_queue.length===2);
  await page.reload({waitUntil:'domcontentloaded'});await ready();check('queue survives refresh',(await project()).input_queue.length>=1);
  p=await settle();
  check('queued inputs are processed in order',p.messages.filter(m=>m.role==='user').slice(-2).map(m=>m.content).join('|')==='[慢分析] 补充A：保存30天。|补充B：预算仍为80元。');
  check('removed input never enters the conversation',p.messages.every(m=>!m.content.includes('这条补充即将移除')));
  check('new question branches accumulate without dropping old ones',['temperature_range','alarm_threshold','size_branch'].every(id=>p.document.questions.some(q=>q.id===id)));
  if(await page.$('.question-expand'))await page.locator('.question-expand').click();check('all accumulated questions remain accessible',(await page.$$('.question-card')).length===p.document.questions.length);
  await send('[慢分析] 先分析一轮。');await send('停止后待处理的补充。');await page.locator('[aria-label="停止分析"]').click();await page.waitForSelector('.run-notice');
  p=await project();check('stop keeps pending inputs paused',p.latest_run.status==='cancelled'&&p.input_queue.length===1);
  await page.locator('.queued-inputs header .text-button').click();await settle();check('user can resume pending inputs after stop',true);
  for(const flag of ['[测试调用失败]','[测试损坏JSON]','[测试无效编号]','[测试工具越界]']){
    const before=await project();await send(flag+' 请记录当前要求。');await page.waitForSelector('.run-notice.failure',{timeout:30000});
    const failed=await project();check(`${flag} preserves the previous document`,JSON.stringify(failed.document)===JSON.stringify(before.document));
    await page.locator('.run-notice .text-button').click();const recovered=await settle();check(`${flag} retry recovers without duplicate user input`,recovered.messages.filter(m=>m.role==='user').length===before.messages.filter(m=>m.role==='user').length+1);
  }
  await page.locator('.project-title-button').click();await page.locator('dialog input').fill(`人工操作验收项目 ${round}`);await page.locator('dialog .primary').click();await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
  check('rename persists and is visible',(await project()).title===`人工操作验收项目 ${round}`);
  await tab('设计总览');await page.locator('.requirement-heading .text-button').click();await page.locator('dialog input[maxlength="160"]').fill('新增人工需求');await page.locator('dialog textarea').fill('A|B\n多行中文 <script>alert(1)</script>');await page.locator('dialog .primary').click();await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
  p=await project();check('manual add creates a sourced requirement',p.document.requirements.at(-1).name==='新增人工需求'&&p.document.requirements.at(-1).source_type==='manual');
  const version=p.document.version;await page.locator('.requirement-main').click();await page.locator('dialog textarea').fill('不应保存的修改');await page.keyboard.press('Escape');check('Escape cancels an edit',(await project()).document.version===version);
  await page.locator('.coverage-expand').click();check('coverage lists all five chapters',(await page.$$('.coverage-chapter')).length===5);
  await page.locator('.coverage-chapter summary').click();await page.locator('.coverage-topic-title').click();check('coverage topic can be discussed',await page.$eval('.composer textarea',el=>el.value.includes('V3-')));
  check('incomplete document cannot be approved',await page.$eval('.approve-document',el=>el.disabled));
  await choose(fixture.ready);check('completed applicability enables review',!(await page.$eval('.approve-document',el=>el.disabled)));
  await page.locator('.approve-document').click();await page.waitForFunction(()=>document.querySelector('.draft-badge')?.textContent==='已审核');
  check('review requires and records a user click',(await project()).review.status==='approved'&&await page.$eval('.overview-page .document-topline',el=>el.textContent.includes('已审核需求')));
  await send('修改场景为室内办公区，沿用之前所有设计阶段边界，请根据已有资料自行核对，不重复询问已知资料。');
  p=await settle();check('linked sections are rechecked without asking known facts again',p.latest_run.kind==='continuation'&&p.review.closed===50&&p.review.status==='ready'&&p.document.questions.length===0);
  await page.locator('.approve-document').click();await page.waitForFunction(()=>document.querySelector('.draft-badge')?.textContent==='已审核');
  await page.locator('.requirement-main').click();await page.locator('dialog textarea').fill('审核后的室外场景修改');await page.locator('dialog .primary').click();await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
  check('editing a reviewed document reopens the relevant coverage',(await project()).review.status==='draft');
  const start=Date.now();await choose(fixture.large);await page.waitForSelector('.requirements-list');const loadMs=Date.now()-start;
  check('300 requirements render and remain editable',(await page.$$('.requirement-row')).length===300&&loadMs<15000);
  check('large document does not create page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  await page.locator('.question-expand').click();check('20 accumulated questions remain accessible',(await page.$$('.question-card')).length===20);
  check('injected HTML is rendered as text',await page.$eval('.requirements-list',el=>el.textContent.includes('<img src=x onerror=alert(1)>'))&&!(await page.$('.requirements-list img')));
  await page.$eval('.chat-scroll',el=>el.scrollTop=0);await page.waitForSelector('.jump-latest');await page.locator('.jump-latest').click();check('long conversation provides a jump to latest',true);
  for(const width of [1280,768,390,320]){await page.setViewport({width,height:844});if(width<960)await page.locator('.mobile-switch button:nth-child(2)').click();check(`long document fits ${width}px`,await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));}
  await page.setViewport({width:1440,height:1000});
  await page.screenshot({path:path.join(out,`interaction-round-${round}.png`)});
  check('browser has no runtime errors',errors.length===0);
  reports.push({round,provider:'controlled_cli_fixture',checks,console_errors:errors,large_load_ms:loadMs});
  await fs.writeFile(path.join(out,'interaction.json'),JSON.stringify({ok:true,rounds:reports},null,2));await page.close();
}
}catch(error){await page?.screenshot({path:path.join(out,`interaction-failure-r${round}.png`)}).catch(()=>{});await fs.writeFile(path.join(out,'interaction-failure.json'),JSON.stringify({round,error:String(error),checks,console_errors:errors},null,2));throw error;}
finally{await browser.close();}
console.log(JSON.stringify({ok:true,rounds:reports.map(r=>({round:r.round,checks:r.checks.length,large_load_ms:r.large_load_ms}))}));
