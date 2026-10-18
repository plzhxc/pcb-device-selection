// Operate the normal 8878 UI. A human (Codex acting as product owner) chooses each next action.
// Usage: node tests/ux_live.mjs .runtime/ux/live-action.json
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
const root=process.cwd(),out=path.join(root,'.runtime/ux/verification');await fs.mkdir(out,{recursive:true});
const action=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
const file=path.join(out,'live-cases.json'),cases=await fs.readFile(file,'utf8').then(JSON.parse).catch(()=>({}));
const require=createRequire(path.join(root,'frontend/package.json')),profile=path.join(out,'live-profile');
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',pipe:true,headless:true,userDataDir:profile,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage']});
const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1440,height:1000});
async function input(selector,text){await page.focus(selector);await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.press('Backspace');await page.keyboard.sendCharacter(text);}
let id=cases[action.case]?.id;
try{
  if(id)await page.evaluateOnNewDocument(id=>localStorage.setItem('requirements-studio:project',id),id);
  await page.goto('http://[::1]:8878/',{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>!document.querySelector('[aria-label="输入硬件需求"]')?.disabled,{timeout:20000});
  if(action.action==='start'){
    assert.ok(!id,'case already exists');await page.locator('.new-project').click();await page.waitForFunction(()=>document.querySelector('.chat-empty'));
    id=await page.evaluate(()=>localStorage.getItem('requirements-studio:project'));
    await page.locator('.project-title-button').click();await page.waitForSelector('.dialog[open] .editor-form input');await input('.dialog[open] .editor-form input',action.title);await page.locator('.dialog-actions .primary').click();await page.waitForFunction(()=>!document.querySelector('.dialog[open]'));
    cases[action.case]={id,title:action.title,actions:[],created_at:new Date().toISOString()};await fs.writeFile(file,JSON.stringify(cases,null,2));
  }
  assert.ok(id,'start this case first');
  const get=()=>page.evaluate(async id=>(await fetch('/api/projects/'+id)).json(),id);
  const before=await get();
  if(action.action==='send'||action.action==='start'){
    await input('[aria-label="输入硬件需求"]',action.text);await page.locator('.composer-buttons .send-button:not(.stop)').click();await page.waitForFunction(()=>document.querySelector('[aria-label="输入硬件需求"]').value==='');
  }else if(action.action==='answer'){
    const selector=`[data-question-id="${action.question_id}"]`;
    if(await page.$(`${selector}.question-receipt .text-button`))await page.locator(`${selector}.question-receipt .text-button`).click();
    await page.waitForSelector(`${selector} .custom-answer`);
    if(action.option!==undefined)await page.locator(`${selector} .question-option:nth-child(${action.option+1})`).click();else await input(`${selector} .custom-answer`,action.text);
    await page.locator('.question-footer .primary').click();await page.waitForSelector(`${selector}.question-receipt`);
    assert.equal(await page.$$eval(`${selector} .question-option`,els=>els.length),0,'submitted choices must close');
    await page.waitForFunction(async({id,qid})=>(await fetch('/api/projects/'+id).then(r=>r.json())).question_submissions.some(s=>s.question_id===qid),{timeout:15000},{id,qid:action.question_id});
  }else if(action.action==='approve'){
    assert.equal(before.review.status,'ready');await page.locator('.canvas-tabs [role="tab"]:nth-child(2)').click();await page.waitForSelector('.document-approve');await page.locator('.document-approve').click();await page.waitForFunction(()=>document.querySelector('.draft-badge')?.textContent==='已审核');
  }else if(action.action==='confirm-modules'){
    await page.waitForSelector('.confirm-module-plan');await page.locator('.confirm-module-plan').click();await page.waitForFunction(async({id,run})=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run.id!==run,{}, {id,run:before.latest_run?.id});
  }else if(action.action==='continue'){
    await page.locator('.coverage-actions .text-button').click();await page.waitForFunction(async({id,run})=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run.id!==run,{}, {id,run:before.latest_run?.id});
  }else if(action.action==='retry'){
    await page.locator('.run-notice .text-button').click();await page.waitForFunction(async({id,run})=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run.id!==run,{}, {id,run:before.latest_run?.id});
  }
  if(action.action==='screenshot'){
    if(action.tab)await page.locator(`.canvas-tabs [role="tab"]:nth-child(${action.tab})`).click();
    if(action.history){await page.waitForSelector('.run-history');await page.locator('.run-history:last-of-type summary').click();}
  }
  const p=await get();
  cases[action.case].actions.push({...action,at:new Date().toISOString(),before_version:before.document.version,after_version:p.document.version,run_id:p.latest_run?.id,errors});await fs.writeFile(file,JSON.stringify(cases,null,2));
  await fs.writeFile(path.join(out,`live-${action.case}.json`),JSON.stringify(p,null,2));await fs.writeFile(path.join(out,`live-${action.case}.md`),p.markdown);
  await page.screenshot({path:path.join(out,`live-${action.case}-${action.action}${action.tab??''}.png`)});
  console.log(JSON.stringify({id,title:p.title,status:p.review.status,version:p.document.version,run:p.latest_run?{id:p.latest_run.id,status:p.latest_run.status,error:p.latest_run.error}:null,queue:p.input_queue,questions:p.document.questions,submissions:p.question_submissions,product:p.convergence.product_review.decision_fields,modules:p.convergence.modules.map(m=>({id:m.id,name:m.name,type:m.type_name,state:m.state,boundary:m.boundary})),issues:p.convergence.issues,last_replies:p.messages.filter(m=>m.role==='assistant').slice(-2).map(m=>m.content),errors},null,2));
}finally{await browser.close();}
