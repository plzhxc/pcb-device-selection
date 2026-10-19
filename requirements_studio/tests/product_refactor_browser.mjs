// Two retained products only. Read-only UI checks: no customer answers or approvals.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const root=process.cwd(),args=process.argv.slice(2),base=args[0]??'http://[::1]:8896';
const out=path.join(root,'.runtime/refactor-20261019/browser'+(base.includes('8878')?'-published':''));await fs.mkdir(out,{recursive:true});
const index=JSON.parse(await fs.readFile('.runtime/customer-acceptance/product-refactor-20261019/index.json','utf8'));
assert.equal(index.cases.length,2);
const require=createRequire(path.join(root,'frontend/package.json'));
const bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws});
const report={base,products:[],mutations:[],errors:[],ok:false};
async function raf(page){await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));}
async function tab(page,label){for(const button of await page.$$('.canvas-tabs [role="tab"]')){if((await button.evaluate(el=>el.textContent)).trim()===label){await button.click();await raf(page);return;}}throw Error('No tab '+label);}
async function shot(page,name){await page.bringToFront();await raf(page);await page.screenshot({path:path.join(out,name+'.webp'),type:'webp',quality:86});}
try{
 for(const entry of index.cases){
  const context=await browser.createBrowserContext(),page=await context.newPage(),checks=[];
  page.on('pageerror',error=>report.errors.push({product:entry.key,error:error.message}));
  page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method())&&r.url().includes('/api/'))report.mutations.push({method:r.method(),url:r.url()});});
  await page.setViewport({width:1440,height:1000});
  await page.goto(base+'/?project='+entry.project_id+'&view=document',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.specification-document');
  const project=await page.evaluate(id=>fetch('/api/projects/'+id).then(r=>r.json()),entry.project_id);
  assert.ok(!['running','queued'].includes(project.latest_run?.status),'Wait until this product is idle');
  const record={key:entry.key,id:entry.project_id,version:project.document.version,review:project.review.status,checks};report.products.push(record);
  assert.deepEqual(await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.map(el=>el.textContent.trim())),['需求说明书','收敛路径','模块方案']);checks.push('only_three_views');
  assert.equal(await page.$$eval('.spec-line',nodes=>nodes.length),project.specification.field_count);checks.push('normative_fields_present');
  assert.equal(await page.$$eval('.spec-chapter',nodes=>nodes.length),4);
  const term=entry.key==='DESK-TEMP'?'摄氏':'声音';
  await page.locator('[aria-label="搜索说明书条款"]').fill(term);await raf(page);
  assert.ok(await page.$('.spec-line'));assert.ok((await page.$eval('.specification-document',e=>e.textContent)).includes(term));
  await page.locator('[aria-label="搜索说明书条款"]').fill('');await raf(page);checks.push('search_and_restore');
  const edit=await page.$('.spec-value .icon-button');if(edit){await edit.click();await page.waitForSelector('.editor-form');await page.locator('.dialog-actions .secondary').click();await page.waitForFunction(()=>!document.querySelector('.dialog[open]'));checks.push('edit_open_cancel');}
  const source=await page.$('.spec-value-actions .text-button');if(source){await source.click();await raf(page);assert.ok(await page.$('.message.user'));checks.push('user_source_location');}
  await page.$eval('.canvas-scroll',e=>e.scrollTop=0);await shot(page,entry.key+'-desktop-document');
  await tab(page,'收敛路径');await page.waitForSelector('.decision-path');
  assert.equal(await page.$$eval('.decision-branch.waiting',nodes=>nodes.length),project.document.questions.filter(q=>q.status!=='deferred').length);checks.push('resolved_branches_not_waiting');
  if(await page.$('.path-expand'))await page.locator('.path-expand').click();
  if(entry.key==='WATER-REMINDER'){
   const branch='.decision-branch[data-decision-id="Q-003"]';await page.waitForSelector(branch);
   assert.ok(await page.$(branch+'.answered'));assert.ok(await page.$(branch+' .decision-adjusted'));
   assert.ok((await page.$eval(branch+' .decision-answer',e=>e.textContent)).includes('灯光'));
   assert.ok((await page.$eval(branch+' .decision-results',e=>e.textContent)).includes('声音'));
   await page.$eval(branch,e=>{for(const d of e.querySelectorAll('.decision-results details'))d.open=true;e.scrollIntoView({block:'center'});});checks.push('old_light_choice_and_current_sound_result');
  }
  await shot(page,entry.key+'-desktop-path');
  const jump=await page.$('.decision-facts button.current');if(jump){await jump.click();await page.waitForSelector('.spec-focus');assert.ok(await page.$('.spec-focus p'),'Jump must show the actual requirement content');checks.push('path_to_actual_document_content');}
  await page.evaluate(async()=>{const el=document.querySelector('.canvas-scroll');let last=-1,stable=0;for(let i=0;i<100;i++){await new Promise(r=>setTimeout(r,25));const top=el.scrollTop;if(Math.abs(top-last)<1)stable++;else stable=0;last=top;if(stable>=5)return;}});
  const remembered=await page.$eval('.canvas-scroll',e=>e.scrollTop);await tab(page,'模块方案');await page.waitForSelector('.module-board');await tab(page,'需求说明书');await raf(page);
  const restored=await page.$eval('.canvas-scroll',e=>e.scrollTop);assert.ok(Math.abs(restored-remembered)<15,JSON.stringify({remembered,restored}));checks.push('tab_reading_position');
  await tab(page,'模块方案');await page.waitForSelector('.module-board');
  assert.equal(await page.$$eval('.module-instance-list>.module-instance',nodes=>nodes.length),project.convergence.modules.filter(m=>m.state!=='not_applicable').length);
  assert.equal(await page.$eval('.module-library',e=>e.open),false);checks.push('actual_modules_and_collapsed_catalogue');
  for(const button of await page.$$('.module-card-actions .text-button')){
   const label=await button.evaluate(e=>e.textContent);
   if(label.includes('查看规范')){await button.click();await page.waitForSelector('.module-spec-detail');await page.locator('[aria-label="关闭模块规范详情"]').click();await page.waitForFunction(()=>!document.querySelector('.module-spec-detail'));checks.push('module_norm_open_close');break;}
  }
  for(const button of await page.$$('.module-card-actions .text-button')){
   if((await button.evaluate(e=>e.textContent)).includes('继续讨论')){await button.click();const value=await page.$eval('[aria-label="输入硬件需求"]',e=>e.value);assert.ok(value.includes('模块'));await page.focus('[aria-label="输入硬件需求"]');await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.press('Backspace');assert.equal(await page.$eval('[aria-label="输入硬件需求"]',e=>e.value),'');checks.push('discussion_prefill_without_send');break;}
  }
  if(await page.$('.module-board [aria-label="全屏查看设计图"]')){
   await page.locator('.module-board [aria-label="全屏查看设计图"]').click();await page.waitForSelector('.diagram-fullscreen-dialog[open]');
   await page.locator('.diagram-fullscreen-dialog [aria-label="适应画布"]').click();await raf(page);
   const selector='.diagram-fullscreen-dialog [data-diagram-node]',node=await page.$(selector);assert.ok(node);
   const before=await node.evaluate(e=>e.getAttribute('transform')),box=await node.boundingBox();assert.ok(box);
   await page.mouse.move(box.x+box.width*.5,box.y+box.height*.5);await page.mouse.down();await page.mouse.move(box.x+box.width*.5+45,box.y+box.height*.5+32,{steps:10});await page.mouse.up();await raf(page);
   assert.notEqual(await page.$eval(selector,e=>e.getAttribute('transform')),before);
   assert.ok(await page.$('.diagram-fullscreen-dialog[open]'));checks.push('diagram_fullscreen_drag');
   await page.locator('.diagram-fullscreen-dialog [aria-label="重置节点布局"]').click();await page.locator('[aria-label="关闭图形全屏"]').click();await page.waitForFunction(()=>!document.querySelector('.diagram-fullscreen-dialog[open]'));
   await new Promise(r=>setTimeout(r,200));await raf(page);
   const contained=await page.$eval('.module-board .diagram-viewport',el=>{const box=el.getBoundingClientRect();return [...el.querySelectorAll('[data-diagram-node]')].every(n=>{const b=n.getBoundingClientRect();return b.left>=box.left-2&&b.right<=box.right+2&&b.top>=box.top-2&&b.bottom<=box.bottom+2;});});assert.ok(contained,'Small system graph must fit when returning from fullscreen');checks.push('small_graph_fits_embedded_canvas');
  }
  await page.$eval('.canvas-scroll',e=>e.scrollTop=0);await shot(page,entry.key+'-desktop-modules');
  await tab(page,'需求说明书');
  assert.equal(await page.$$eval('.spec-tail pre code',nodes=>nodes.filter(e=>e.textContent.includes('flowchart')).length),0);checks.push('document_diagrams_rendered');
  await page.setViewport({width:390,height:844});await raf(page);
  const switcher=await page.$('.mobile-switch [role="tab"]:last-child');await switcher.click();
  for(const [label,name] of [['需求说明书','document'],['收敛路径','path'],['模块方案','modules']]){
   await tab(page,label);await page.$eval('.canvas-scroll',e=>e.scrollTop=0);await raf(page);
   const dimensions=await page.evaluate(()=>({width:innerWidth,body:document.documentElement.scrollWidth}));assert.ok(dimensions.body<=dimensions.width+1,JSON.stringify(dimensions));
   await shot(page,entry.key+'-mobile-'+name);
  }
  checks.push('mobile_three_views_no_page_overflow');await context.close();
 }
 assert.equal(report.mutations.length,0,'Read-only checks must not send answers, approve, or run the model');assert.equal(report.errors.length,0,'UI raised a JavaScript error');report.ok=true;
}catch(error){report.failure=String(error);throw error;}finally{await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2)+'\n');browser.disconnect();}
console.log(JSON.stringify({ok:report.ok,products:report.products.map(p=>({key:p.key,checks:p.checks.length,version:p.version,review:p.review})),mutations:report.mutations.length,errors:report.errors.length,report:path.join(out,'report.json')},null,2));
