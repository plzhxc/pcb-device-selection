// Real UI checks against saved acceptance cases, with no state/AI mocking.
import fs from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const root=process.cwd(),args=process.argv.slice(2),option=(k,d)=>{const i=args.indexOf('--'+k);return i<0?d:args[i+1];};
const base=option('base','http://[::1]:8896'),phase=option('phase','pending');
assert.match(phase,/^[a-z0-9-]+$/);
const dir='.runtime/canvas-sync-20261020',cases=JSON.parse(await fs.readFile(dir+'/case-clones.json','utf8'));
const bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const require=createRequire(root+'/frontend/package.json'),browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws});
const context=await browser.createBrowserContext(),results=[];
try{
 for(const [key,id] of Object.entries(cases)){
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.setViewport({width:1600,height:1100});await page.goto(base+'/?project='+id,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.canvas-tabs');await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(n=>n.textContent.includes('收敛路径')).click());
  await page.waitForSelector('.convergence-graphs');
  const p=await page.evaluate(id=>fetch('/api/projects/'+id).then(r=>r.json()),id);
  const item={key,id,phase,version:p.document.version,run:p.latest_run?.status,pending:p.canvas_sync.pending_count,graphs:[]};
  for(const [view,label] of [['solution','方案关系'],['impact','变更影响'],['closure','收敛检查']]){
   await page.$$eval('.graph-view-switch button',(nodes,label)=>nodes.find(n=>n.textContent===label).click(),label);
   const selector=`[data-knowledge-view="${view}"]`;await page.waitForSelector(selector+' .diagram-node');
   await page.locator(selector+' [aria-label="适应画布"]').click();
   const geometry=await page.$eval(selector,e=>{
    const nodes=[...e.querySelectorAll('[data-diagram-node]')],bad=[];
    for(const node of nodes){const w=Number(node.dataset.nodeWidth),h=Number(node.dataset.nodeHeight);
     for(const text of node.querySelectorAll('text')){const b=text.getBBox();if(b.x<0||b.y<0||b.x+b.width>w+1||b.y+b.height>h+1)bad.push({node:node.dataset.diagramNode,text:text.textContent,box:[b.x,b.y,b.width,b.height],size:[w,h]});}
    }
    return {nodes:nodes.length,edges:e.querySelectorAll('.diagram-edge').length,bad,invalidPaths:[...e.querySelectorAll('path[d]')].filter(p=>/NaN|Infinity/.test(p.getAttribute('d'))).length};
   });
   assert.equal(geometry.bad.length,0,JSON.stringify(geometry.bad));assert.equal(geometry.invalidPaths,0);
   const first=await page.$(selector+' [data-diagram-node]');await first.evaluate(e=>e.focus());await page.keyboard.press('Enter');await page.waitForSelector(selector+' .diagram-inspector');
   const detail=await page.$eval(selector+' .diagram-inspector',e=>e.textContent);assert.ok(detail.length>10);
   await page.locator(selector+' [aria-label="关闭节点详情"]').click();
   await page.$eval(selector+' .diagram-viewport',e=>e.scrollIntoView({block:'center'}));
   const old=await first.evaluate(e=>e.getAttribute('transform')),box=await first.boundingBox();assert.ok(box);
   await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await page.mouse.move(box.x+box.width/2+25,box.y+box.height/2+18,{steps:6});await page.mouse.up();
   assert.notEqual(await first.evaluate(e=>e.getAttribute('transform')),old,'Node dragging did not change layout');
   const scale=await page.$eval(selector+' .diagram-world',e=>Number(e.dataset.cameraScale));
   await page.locator(selector+' [aria-label="放大设计图"]').click();assert.ok(await page.$eval(selector+' .diagram-world',(e,scale)=>Number(e.dataset.cameraScale)>scale,scale));
   await page.locator(selector+' [aria-label="适应画布"]').click();
   await page.locator(selector+' [aria-label="全屏查看设计图"]').click();await page.waitForSelector('.diagram-fullscreen-dialog[open]');
   await page.screenshot({path:dir+'/'+key+'-'+phase+'-'+view+'.webp',type:'webp',quality:88});
   await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('.diagram-fullscreen-dialog[open]'));
   let filter=false,deviceNavigation=false;
   if(view==='solution'){
    const req=p.document.requirements.find(r=>p.convergence.modules.some(m=>m.requirement_ids.includes(r.id)));
    if(req){await page.select('[aria-label="图谱需求范围"]',req.id);const count=await page.$$eval(selector+' .diagram-node',ns=>ns.length);assert.ok(count<=geometry.nodes);filter=true;await page.select('[aria-label="图谱需求范围"]','');}
    const device=await page.$(selector+' [data-diagram-node="REF-002"]');assert.ok(device);
    await device.evaluate(e=>e.focus());await page.keyboard.press('Enter');await page.waitForSelector(selector+' .diagram-inspector .diagram-discuss');
    await page.locator(selector+' .diagram-inspector .diagram-discuss').click();await page.waitForSelector('.device-panel [data-reference-id="REF-002"]');
    assert.ok((await page.$eval('.device-panel [data-reference-id="REF-002"] h2',e=>e.textContent)).includes(p.document.device_references.find(r=>r.id==='REF-002').mention));
    await page.locator('[aria-label="关闭器件与证据"]').click();await page.waitForSelector('.convergence-graphs');deviceNavigation=true;
   }
   item.graphs.push({view,label,...geometry,detail:detail.slice(0,1000),drag:true,zoom:true,fullscreen:true,filter,deviceNavigation});
  }
  const position=await page.$eval('[data-knowledge-view="closure"] [data-diagram-node]',e=>e.getAttribute('transform'));
  await page.reload({waitUntil:'domcontentloaded'});await page.waitForSelector('[data-knowledge-view="closure"] [data-diagram-node]');
  assert.equal(await page.$eval('[data-knowledge-view="closure"] [data-diagram-node]',e=>e.getAttribute('transform')),position,'Dragged layout did not survive refresh');item.layout_persists=true;
  await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(n=>n.textContent.includes('模块方案')).click());await page.waitForSelector('.current-bom');
  const current=await page.$$eval('.current-bom tbody tr',rows=>rows.map(r=>r.textContent));
  assert.equal(current.length,p.delivery.bom.current_rows.length);const model=p.document.device_references.find(r=>r.id==='REF-002').mention;
  assert.ok(current.some(r=>r.includes(model)));item.current_bom=current;
  await page.$$eval('.bom-view-switch button',nodes=>nodes.find(n=>n.textContent.includes('参考对照')).click());await page.waitForSelector('.reference-bom');
  item.reference_rows=await page.$$eval('.reference-bom tbody tr',rows=>rows.map(r=>r.textContent));
  assert.ok(item.reference_rows.some(r=>r.includes(key==='toy'?'C10S01X':'RT9013-33GB')),'Original reference design was rewritten');
  await page.setViewport({width:390,height:844});await page.$$eval('.mobile-switch button',nodes=>nodes.find(n=>n.textContent.includes('需求画布')).click());
  await page.$$eval('.canvas-tabs [role="tab"]',nodes=>nodes.find(n=>n.textContent.includes('收敛路径')).click());await page.waitForSelector('.convergence-graphs');
  await page.$eval('.convergence-graphs',e=>e.scrollIntoView({block:'start'}));
  const mobile=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,graph:document.querySelector('.convergence-graphs').getBoundingClientRect().toJSON()}));assert.ok(mobile.scroll<=mobile.width+1,JSON.stringify(mobile));item.mobile=mobile;
  await page.screenshot({path:dir+'/'+key+'-'+phase+'-mobile.webp',type:'webp',quality:87});
  assert.equal(errors.length,0,errors.join('\n'));item.errors=errors;results.push(item);await page.close();
 }
 await fs.writeFile(dir+'/browser-'+phase+'.json',JSON.stringify({base,at:new Date().toISOString(),results},null,2)+'\n');
 console.log(JSON.stringify(results.map(r=>({key:r.key,version:r.version,pending:r.pending,graphs:r.graphs.map(g=>({view:g.view,nodes:g.nodes,edges:g.edges,drag:g.drag})),mobile:r.mobile,errors:r.errors})),null,2));
}finally{await context.close();await browser.disconnect();}
