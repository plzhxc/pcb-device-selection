// Two real-browser version races. Only the deliberately controlled provider is used.
import fs from 'node:fs/promises';import path from 'node:path';import assert from 'node:assert/strict';import {createRequire} from 'node:module';
const root=process.cwd(),require=createRequire(path.join(root,'frontend/package.json')),out=path.join(root,'.runtime/phase3/browser');
const bridge=JSON.parse(await fs.readFile(path.join(root,'.runtime/phase3/browser-bridge.json'),'utf8'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws}),fixtures=JSON.parse(await fs.readFile(path.join(out,'fixtures.json'),'utf8')),results=[];
async function open(page,id){await page.bringToFront();await page.setCacheEnabled(false);await page.setViewport({width:1520,height:1020});await page.goto('http://[::1]:8895/?project='+id,{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>!!document.querySelector('.message.user')&&!document.querySelector('.loading-state'),{polling:200});console.log('opened',id);}
async function chat(page,id,text){await page.bringToFront();const v=await page.evaluate(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())).document.version,id);await page.locator('[aria-label="输入硬件需求"]').fill(text);await page.locator('.composer-buttons .send-button:not(.stop)').click();await page.waitForFunction(async({id,v})=>{const p=await fetch('/api/projects/'+id).then(r=>r.json());return p.document.version>v&&!['queued','running'].includes(p.latest_run?.status)&&!p.domain.active_jobs;},{timeout:60000},{id,v});return page.evaluate(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())).document.version,id);}
try{for(const f of fixtures.rounds){const context=await browser.createBrowserContext(),a=await context.newPage(),b=await context.newPage(),id=f.late,errors=[];a.on('pageerror',e=>errors.push(e.message));await open(a,id);await open(b,id);
 await a.bringToFront();await a.locator('.export-menu>.button').click();await a.locator('.export-handoff').click();await a.waitForSelector('.handoff-status');
 let delayed='',release,received;await a.setRequestInterception(true);a.on('request',async req=>{if(req.method()==='POST'&&req.url().endsWith(delayed)&&delayed){delayed='';received?.();await new Promise(r=>release=r);}await req.continue();});
 const checks=[];
 for(const kind of ['downstream-adapter','handoff']){let observed=new Promise(r=>received=r);delayed='/'+kind;
  if(kind==='downstream-adapter')await a.locator('.handoff-adapter button').click();else await a.$$eval('.dialog-actions button',ns=>ns.find(n=>n.textContent.includes('生成')).click());
  await observed;const version=await chat(b,id,'补充本次使用说明：'+kind+'验证仍采用室内实验室原型边界，其他要求保持。');await a.bringToFront();
  await a.waitForFunction(v=>document.querySelector('.handoff-status')?.textContent.includes('需求 v'+v),{timeout:20000},version);
  // Saving a new document precedes finishing its run. Wait for this tab to
  // observe the settled review state as well, not only the version number.
  await a.waitForFunction(()=>document.querySelector('.draft-badge')?.textContent==='待审核',{timeout:20000});release();
  await a.waitForFunction(()=>![...document.querySelectorAll('.handoff-dialog button')].some(n=>n.textContent.includes('生成')&&n.disabled)&&!document.querySelector('.handoff-adapter button')?.disabled,{timeout:15000});
  assert.equal(await a.$('.handoff-adapter [role="status"]'),null);assert.equal(await a.$('.handoff-dialog a[download]'),null);assert.equal(await a.$('.handoff-dialog .inline-error'),null);checks.push(kind+': old version response discarded');
 }
 await a.bringToFront();await a.locator('.handoff-adapter button').click();await a.waitForSelector('.handoff-adapter [role="status"]');checks.push('new version adapter can be checked normally');
 await a.$$eval('.dialog-actions button',ns=>ns.find(n=>n.textContent.includes('生成')).click());await a.waitForSelector('.handoff-dialog a[download]');checks.push('new version draft export remains explicitly labelled');assert.ok(await a.$eval('.handoff-dialog a[download]',e=>e.textContent.includes('草稿')));
 const prior=await b.evaluate(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())).document.version,id);
 await b.bringToFront();await b.locator('[aria-label="输入硬件需求"]').fill('输入竞态保持本轮未写入：新增的产品补充应先完成整理，不能沿用之前的就绪状态。');await b.locator('.composer-buttons .send-button:not(.stop)').click();
 await b.waitForFunction(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())).analysis_pending,{timeout:10000},id);await a.bringToFront();
 await a.waitForFunction(()=>!document.querySelector('.handoff-adapter [role="status"]')&&!document.querySelector('.handoff-dialog a[download]'),{timeout:10000});
 const during=await a.evaluate(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())),id);assert.equal(during.document.version,prior);checks.push('new input clears adapter and download before document version changes');
 await a.waitForFunction(()=>document.querySelector('.handoff-blockers')?.textContent.includes('最新用户补充'),{timeout:10000});checks.push('unprocessed input is explicitly blocked in the handoff dialog');
 await b.bringToFront();await b.locator('.composer-buttons .stop').click();await b.waitForFunction(async id=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run.status==='cancelled',{},id);await a.bringToFront();
 assert.equal(errors.length,0);const health=await a.evaluate(()=>fetch('/api/health').then(r=>r.json()));const browser_asset=await a.$eval('script[src]',e=>new URL(e.src).pathname);await a.screenshot({path:path.join(out,`version-race-${f.round}.png`)});results.push({round:f.round,seed:f.seed,checks,errors,backend_build:health.backend_build,browser_asset,real_inference:false});await fs.writeFile(path.join(out,'races.json'),JSON.stringify({ok:results.length===2,rounds:results},null,2)+'\n');await context.close();console.log('PASS version races R'+f.round);
}}finally{browser.disconnect();}
