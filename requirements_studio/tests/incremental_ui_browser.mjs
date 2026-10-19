// Read-only inspection of the two real, incremental customer journeys.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const base=process.argv[2]??'http://[::1]:8896',root=process.cwd();
const out=path.join(root,'.runtime/ux-optimization-20261019',base.includes('8878')?'browser-published':'browser-final');await fs.mkdir(out,{recursive:true});
const allEntries=JSON.parse(await fs.readFile('.runtime/customer-acceptance/incremental-ref-20261019/index.json','utf8')).cases;
const keys=process.argv.slice(3),entries=keys.length?allEntries.filter(e=>keys.includes(e.key)):allEntries;
assert.equal(entries.length,keys.length||allEntries.length,'Unknown or duplicate customer-journey key');
const req=createRequire(path.join(root,'frontend/package.json')),bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const browser=await req('puppeteer-core').connect({browserWSEndpoint:bridge.ws,defaultViewport:null});
const report={base,products:[],errors:[],http_errors:[],mutations:[],ok:false};
const contexts=[];
async function raf(page){await page.evaluate(()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r))));}
async function tab(page,label){await page.$$eval('.canvas-tabs [role="tab"]',(ns,s)=>ns.find(e=>e.textContent.trim()===s).click(),label);await raf(page);}
async function shot(page,name){await page.bringToFront();await raf(page);await page.screenshot({path:path.join(out,name+'.webp'),type:'webp',quality:88});}
try{
 for(const entry of entries){
  const context=await browser.createBrowserContext();contexts.push(context);const page=await context.newPage(),checks=[];
  page.on('pageerror',e=>report.errors.push({key:entry.key,error:e.message}));page.on('response',r=>{if(r.status()>=400)report.http_errors.push({url:r.url(),status:r.status()});});
  page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD','OPTIONS'].includes(r.method()))report.mutations.push({method:r.method(),url:r.url()});});
  await page.setViewport({width:1440,height:1000});await page.goto(base+'/?project='+entry.project_id,{waitUntil:'domcontentloaded'});await page.waitForSelector('.specification-document');
  const p=await page.evaluate(id=>fetch('/api/projects/'+id).then(r=>r.json()),entry.project_id);
  assert.ok(!['queued','running'].includes(p.latest_run?.status));assert.equal(p.input_queue.length,0);assert.equal(p.review.internal_rechecks,0);assert.ok(['ready','approved'].includes(p.review.status));checks.push('real_analysis_complete');
  assert.deepEqual(await page.$$eval('.canvas-tabs [role="tab"]',ns=>ns.map(n=>n.textContent.trim())),['需求说明书','收敛路径','模块方案']);
  assert.equal(await page.$eval('[aria-label="筛选说明书条款"]',e=>e.value),'project');
  const defaultFields=await page.$$eval('.spec-line',ns=>ns.length);assert.ok(defaultFields<=p.specification.field_count);checks.push('default_project_reading');
  const primary=await page.$$eval('[data-primary-requirement]',ns=>ns.map(n=>n.dataset.primaryRequirement));assert.equal(primary.length,new Set(primary).size);assert.deepEqual([...primary].sort(),p.document.requirements.map(r=>r.id).sort());checks.push('every_requirement_has_one_body');
  const sizes=await page.$eval('.canvas-scroll',e=>({height:e.scrollHeight,unknown:e.querySelectorAll('.spec-unknown').length}));
  await shot(page,entry.key+'-document');await page.select('[aria-label="筛选说明书条款"]','all');assert.equal(await page.$$eval('.spec-line',ns=>ns.length),p.specification.field_count);checks.push('full_v3_reference_available');
  await page.select('[aria-label="筛选说明书条款"]','pending');assert.ok((await page.$eval('.specification-document',e=>e.textContent)).includes('本版暂无待你决定'));assert.equal(await page.$$eval('.spec-line.reviewing',ns=>ns.length),0);checks.push('empty_pending_means_complete');
  await page.select('[aria-label="筛选说明书条款"]','project');
  const link=await page.$('.spec-short-reference');if(link){const label=await link.evaluate(e=>e.textContent);await link.click();await raf(page);assert.ok(label);checks.push('short_reference_navigation');}
  await tab(page,'收敛路径');await page.waitForSelector('.decision-map');assert.ok(await page.$('.delivery-status'));assert.equal(await page.$$eval('.decision-turn,.decision-analysis,.decision-input',ns=>ns.length),0);assert.ok(await page.$('.decision-update'));checks.push('completion_and_real_revision_history');
  if(p.delivery.bom.core_count>p.delivery.bom.core_adopted){const summary=await page.$eval('.delivery-status',e=>e.textContent);assert.ok(summary.includes('仅作参考'));assert.ok(!summary.includes(`${p.delivery.bom.core_adopted}/${p.delivery.bom.core_count}`));checks.push('unused_reference_parts_are_not_unfinished_work');}
  await shot(page,entry.key+'-decisions');
  await tab(page,'模块方案');await page.waitForSelector('.reference-bom');
  assert.equal(await page.$$eval('.reference-bom tbody tr',ns=>ns.length),p.delivery.bom.core_count);
  assert.equal(await page.$$eval('.module-instance',ns=>ns.length),p.convergence.modules.filter(m=>m.state!=='not_applicable').length);assert.ok(await page.$('.interactive-diagram'));checks.push('core_bom_and_only_applicable_modules');
  const graph=await page.$('.interactive-diagram');const oldScale=await graph.$eval('.diagram-scale',e=>e.textContent);
  await graph.$eval('[aria-label="放大设计图"]',e=>e.click());await raf(page);const newScale=await graph.$eval('.diagram-scale',e=>e.textContent);assert.notEqual(oldScale,newScale);await graph.$eval('[aria-label="适应画布"]',e=>e.click());checks.push('diagram_zoom_and_fit');
  assert.equal(await page.$$eval('.adopt-reference-core:not([disabled])',ns=>ns.length),0);checks.push('already_adopted_plan_not_repeatable');
  const tableRows=await page.$$eval('.interface-contracts tbody tr',ns=>ns.length);assert.equal(tableRows,p.document.external_interfaces.length);
  if(tableRows){await page.$eval('.interface-contracts details summary',e=>e.click());assert.equal(await page.$$eval('.interface-contracts details[open] .interface-edit input',ns=>ns.length),6);checks.push('single_interface_row_editor');}
  await shot(page,entry.key+'-bom-and-interfaces');
  for(const label of ['展开完整参考 BOM','只看核心器件'])await page.$$eval('.reference-plan button',(ns,s)=>ns.find(n=>n.textContent.includes(s)).click(),label);checks.push('full_reference_bom_expand_collapse');
  const ambiguous=p.domain.references.find(r=>!r.identity&&r.candidates.length>0);
  if(ambiguous){
   await page.locator('[aria-label="打开器件与证据"]').click();await page.waitForSelector(`[data-reference-id="${ambiguous.id}"] .device-candidates`);
   await page.$eval(`[data-reference-id="${ambiguous.id}"] .device-candidates`,e=>e.open=true);
   const candidates=await page.$$eval(`[data-reference-id="${ambiguous.id}"] .candidate-identity`,ns=>ns.map(n=>n.textContent));assert.ok(candidates.every(t=>t.includes('采购号')||/C[0-9]+/.test(t)));assert.ok(candidates.every(t=>t.includes('记录')&&t.includes('修订')));
   await page.locator(`[data-reference-id="${ambiguous.id}"] .candidate-details`).click();await page.waitForSelector('.device-preview');assert.ok(await page.$('.candidate-reference-action'));assert.equal(await page.$$eval('.add-device-reference',ns=>ns.length),0);checks.push('candidate_details_keep_current_reference_context');await shot(page,entry.key+'-candidate-details');await page.locator('[aria-label="关闭器件与证据"]').click();
  }
  await page.setViewport({width:390,height:844});await page.$$eval('.mobile-switch [role="tab"]',ns=>ns.find(n=>n.textContent.includes('需求画布')).click());await tab(page,'收敛路径');await page.$eval('.canvas-scroll',e=>e.scrollTo({top:0,behavior:'instant'}));await raf(page);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
  const card=await page.$eval('.delivery-status',e=>({width:e.getBoundingClientRect().width,bottom:e.getBoundingClientRect().bottom}));assert.ok(card.width>0&&card.bottom<844);checks.push('mobile_next_step_visible_without_page_overflow');await shot(page,entry.key+'-mobile-decisions');
  await tab(page,'模块方案');assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));await shot(page,entry.key+'-mobile-bom');checks.push('mobile_bom_local_scroll');
  report.products.push({key:entry.key,id:p.id,version:p.document.version,review:p.review.status,default_fields:defaultFields,all_fields:p.specification.field_count,sizes,core_count:p.delivery.bom.core_count,core_adopted:p.delivery.bom.core_adopted,interface_rows:tableRows,checks});
  await context.close();
 }
 assert.deepEqual(report.errors,[]);assert.deepEqual(report.http_errors,[]);assert.deepEqual(report.mutations,[]);report.ok=true;
}finally{await fs.writeFile(path.join(out,keys.length?'report-'+keys.join('-')+'.json':'report.json'),JSON.stringify(report,null,2)+'\n');await Promise.allSettled(contexts.map(c=>c.close()));await browser.disconnect();}
console.log(JSON.stringify(report,null,2));
