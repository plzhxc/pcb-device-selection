// Read-only UI checks on the two real named-part customer journeys.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const root=process.cwd(),base=process.argv[2]??'http://[::1]:8896',round=Number(process.argv[3]??2);
const out=path.join(root,'.runtime/decision-refactor-20261019/browser'+(base.includes('8878')?'-published':'')+(round===1?'-r1':''));
await fs.mkdir(out,{recursive:true});
const entries=JSON.parse(await fs.readFile('.runtime/phase4/decision-pins-20261019/index.json','utf8')).cases.filter(c=>round===3?['ROBOT-R2','NFC-R3'].includes(c.key):c.round===round);
const require=createRequire(path.join(root,'frontend/package.json'));
const bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws,defaultViewport:null});
const report={base,products:[],errors:[],mutations:[],ok:false};
async function raf(page){await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));}
async function tab(page,label){await page.$$eval('.canvas-tabs [role="tab"]',(nodes,label)=>nodes.find(e=>e.textContent.trim()===label).click(),label);await raf(page);}
async function shot(page,name){await page.bringToFront();await raf(page);await page.screenshot({path:path.join(out,name+'.webp'),type:'webp',quality:88});}
async function button(page,parent,label){await page.$eval(parent,(el,label)=>{const b=[...el.querySelectorAll('button')].find(e=>e.textContent.includes(label));assertButton(b);b.click();function assertButton(b){if(!b)throw Error('Missing '+label);}},label);await raf(page);}
try{
 for(const entry of entries){
  const context=await browser.createBrowserContext(),page=await context.newPage();const checks=[];
  page.on('pageerror',e=>report.errors.push({key:entry.key,error:e.message}));
  page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD','OPTIONS'].includes(r.method()))report.mutations.push({method:r.method(),url:r.url()});});
  await page.setViewport({width:1440,height:1000});
  await page.goto(base+'/?project='+entry.project_id+'&view=convergence',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.decision-map');
  const p=await page.evaluate(id=>fetch('/api/projects/'+id).then(r=>r.json()),entry.project_id);
  assert.ok(!['running','queued'].includes(p.latest_run?.status),'Real journey must be idle');
  report.products.push({key:entry.key,id:entry.project_id,version:p.document.version,review:p.review.status,checks});
  assert.deepEqual(await page.$$eval('.canvas-tabs [role="tab"]',els=>els.map(e=>e.textContent.trim())),['需求说明书','收敛路径','模块方案']);checks.push('three_customer_views');
  assert.equal(await page.$$eval('.decision-topics button',els=>els.length),5);
  assert.equal(await page.$$eval('.decision-turn,.decision-analysis,.decision-input',els=>els.length),0);checks.push('current_decisions_instead_of_chat_copy');
  await button(page,'.decision-topics','器件与固定条件');
  const device=p.domain.references.find(r=>r.intent==='must');assert.ok(device,'Exact TXT must retain specified parts');
  await page.locator('[aria-label="搜索当前决定"]').fill(device.mention);
  await page.waitForSelector(`[data-current-decision="${device.id}"]`);
  const selector=`[data-current-decision="${device.id}"]`;
  assert.ok((await page.$eval(selector,e=>e.textContent)).includes('本版必须使用'));
  assert.equal(await page.$$eval(selector,els=>els.length),1);checks.push('specified_model_single_current_node');
  await page.select('[aria-label="筛选决定状态"]','settled');await raf(page);assert.ok(await page.$(selector));checks.push('fixed_choice_independent_of_catalog_status');
  await shot(page,entry.key+'-desktop-device-decision');
  await button(page,selector,'查看器件资料');await page.waitForSelector('.device-panel');assert.ok((await page.$eval('.device-panel',e=>e.textContent)).includes(device.mention));checks.push('decision_to_real_device_evidence');
  await shot(page,entry.key+'-desktop-evidence');await page.locator('[aria-label="关闭器件与证据"]').click();await page.waitForSelector('.decision-map');
  assert.equal(await page.$eval('[aria-label="搜索当前决定"]',e=>e.value),device.mention);checks.push('return_keeps_decision_search');
  await button(page,'.decision-topics','功能与验收');
  await page.waitForFunction(()=>document.querySelector('[aria-label="搜索当前决定"]')?.value==='');
  assert.equal(await page.$eval('[aria-label="筛选决定状态"]',e=>e.value),'all');checks.push('topic_change_resets_previous_branch_filters');
  await page.waitForSelector('.current-decision .current-dependencies button');
  const first=await page.$('.current-decision .current-dependencies button');
  if(first){await first.click();await raf(page);assert.ok(await page.$('.current-decision.focused'));checks.push('dependency_navigation_resolves_current_object');}
  await page.$eval('.canvas-scroll',e=>e.scrollTo({top:0,behavior:'instant'}));await shot(page,entry.key+'-desktop-tree');
  await tab(page,'需求说明书');await page.waitForSelector('.specification-document');
  assert.equal(await page.$$eval('.spec-line',els=>els.length),p.specification.field_count);checks.push('v3_normative_fields_preserved');
  await page.$eval('.canvas-scroll',e=>e.scrollTo({top:0,behavior:'instant'}));await shot(page,entry.key+'-desktop-document');
  await tab(page,'模块方案');await page.waitForSelector('.module-board');
  assert.equal(await page.$$eval('.module-instance',els=>els.length),p.convergence.modules.filter(m=>m.state!=='not_applicable').length);checks.push('only_current_product_modules');
  const diagram=await page.$('.interactive-diagram');assert.ok(diagram);checks.push('existing_system_diagram_present');
  await shot(page,entry.key+'-desktop-modules');
  await page.setViewport({width:390,height:844});await tab(page,'收敛路径');await raf(page);
  await page.$$eval('.mobile-switch [role="tab"]',nodes=>nodes.find(e=>e.textContent.includes('需求画布')).click());await raf(page);
  await page.$eval('.canvas-scroll',e=>e.scrollTo({top:0,behavior:'instant'}));await raf(page);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));checks.push('mobile_no_page_overflow');
  const visible=await page.$eval('.decision-map',e=>{const b=e.getBoundingClientRect();return b.width>0;});assert.ok(visible);
  await shot(page,entry.key+'-mobile-tree');
  await context.close();
 }
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.mutations,[]);report.ok=true;
}finally{await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2)+'\n');browser.disconnect();}
console.log(JSON.stringify(report,null,2));
