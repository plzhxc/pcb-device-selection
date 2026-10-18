import {createRequire} from 'node:module';import {spawn} from 'node:child_process';import fs from 'node:fs/promises';import path from 'node:path';import assert from 'node:assert/strict';
const root=process.cwd(),out=path.join(root,'.runtime/verification/ui-audit'),require=createRequire(path.join(root,'frontend/package.json'));
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));let server;
async function start(){server=spawn(path.resolve(root,'../.venv/bin/python'),['-X','utf8','-m','uvicorn','backend.app:app','--host','::1','--port','8882','--log-level','warning','--timeout-graceful-shutdown','1'],{cwd:root,env:{...process.env,PATH:path.join(root,'.runtime/ui-audit-bin')+':'+process.env.PATH,REQUIREMENTS_STUDIO_DATA:path.join(root,'.runtime/ui-audit-restart'),REQUIREMENTS_STUDIO_DIST:path.join(root,'.runtime/ui-audit-dist')},stdio:['ignore','pipe','pipe']});let logs='';server.stderr.on('data',chunk=>logs+=chunk);for(let i=0;i<100;i++){try{if((await fetch('http://[::1]:8882/api/health')).ok)return;}catch{}await sleep(100);}throw new Error('QA restart server failed: '+logs);}
async function stop(){if(!server||server.exitCode!==null)return;const process=server;await new Promise((resolve,reject)=>{const timer=setTimeout(()=>{process.kill('SIGKILL');reject(new Error('QA server did not stop gracefully'));},10000);process.once('exit',()=>{clearTimeout(timer);resolve();});process.kill('SIGTERM');});}
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',pipe:true,headless:true,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},userDataDir:path.join(out,'restart-profile'),args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage']});
const rounds=[];let page,round,checks,errors;const project=()=>page.evaluate(async()=>fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json()));
const check=(name,value)=>{assert.ok(value,name);checks.push(name);console.log(`PASS RESTART R${round} ${name}`);};
async function send(text){await page.locator('.composer textarea').fill(text);await page.locator('[aria-label="发送需求"],[aria-label="加入待处理补充"]').click();}
try{await start();for(round=1;round<=2;round++){
 checks=[];errors=[];page=await browser.newPage();await page.setViewport({width:1440,height:1000});page.on('pageerror',e=>errors.push(e.message));await page.goto('http://[::1]:8882/',{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>!document.querySelector('.loading-state'));await page.locator('.sample-entry').click();await page.waitForSelector('.requirements-list');
 const before=await project();await send('[慢分析] 分析中重启服务验收。');await page.waitForSelector('[aria-label="停止分析"]');await send('重启后补充一。');await send('重启后补充二。');await page.waitForFunction(async()=>{const p=await fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json());return p.input_queue.length===2;});
 await stop();await start();await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('.run-notice');let p=await project();
 check('restart retains the exact saved document',JSON.stringify(before.document)===JSON.stringify(p.document));
 check('interrupted run is visible and retryable',p.latest_run.status==='interrupted'&&!!await page.$('.run-notice .text-button'));
 check('pending inputs survive a real service restart',p.input_queue.length===2);
 await page.locator('.run-notice .text-button').click();await page.waitForFunction(async()=>{const p=await fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json());return p.latest_run.status==='completed'&&p.input_queue.length===0;},{timeout:60000,polling:300});p=await project();
 check('retry resumes analysis and ordered pending inputs',p.document.version===before.document.version+3);
 check('retry does not duplicate the interrupted user message',p.messages.filter(m=>m.role==='user').length===before.messages.filter(m=>m.role==='user').length+3);
 check('restart does not drop the question backlog',p.document.questions.some(q=>q.id==='accuracy'));
 check('browser has no script exceptions',errors.length===0);
 rounds.push({round,provider:'controlled_cli_fixture',checks,console_errors:errors,project_id:p.id});await fs.writeFile(path.join(out,'restart.json'),JSON.stringify({ok:true,rounds},null,2));await page.close();
}}catch(error){await page?.screenshot({path:path.join(out,`restart-failure-r${round}.png`)}).catch(()=>{});throw error;}finally{await stop();await browser.close();}
console.log(JSON.stringify({ok:true,rounds:rounds.map(r=>({round:r.round,checks:r.checks.length}))}));
