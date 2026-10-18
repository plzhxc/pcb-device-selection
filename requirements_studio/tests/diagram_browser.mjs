// Read-only diagram regression: existing projects, long labels, interaction and
// mobile gestures. Never invokes AI or writes project data.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
const root=process.cwd(),require=createRequire(path.join(root,'frontend/package.json'));
const args=process.argv.slice(2),arg=(key,fallback)=>{const i=args.indexOf('--'+key);return i<0?fallback:args[i+1];};
const base=arg('base','http://[::1]:8896'),out=arg('out','.runtime/phase3/diagram-check');await fs.mkdir(out,{recursive:true});
const bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws});
const projects=['3ea063afa8734afaaad4de1563085919','9e22a443a08149389d41c7f1a54bbfd5'];
const errors=[],writes=[],checks=[],ownedContexts=new Set(),assets=new Set();
async function check(name,fn){await fn();checks.push(name);console.log('PASS',name);}
const wait=ms=>new Promise(r=>setTimeout(r,ms));
async function geometry(page,selector){
 return page.$eval(selector,root=>{
  const nodes=[...root.querySelectorAll('[data-diagram-node]')];const errors=[];
  const stage=root.querySelector('.graph-stage')?.getBoundingClientRect(),nav=root.querySelector('.diagram-minimap')?.getBoundingClientRect();
  if(stage&&nav&&stage.left<nav.right&&stage.right>nav.left&&stage.top<nav.bottom&&stage.bottom>nav.top+1)errors.push('Navigation overlaps the drawing');
  for(const node of nodes){
   const w=+node.getAttribute('data-node-width'),h=+node.getAttribute('data-node-height');
   for(const text of node.querySelectorAll('text')){
    const b=text.getBBox();if(b.x<7||b.y<3||b.x+b.width>w-6||b.y+b.height>h-4)errors.push({node:node.getAttribute('data-diagram-node'),text:text.textContent,box:[b.x,b.y,b.width,b.height],size:[w,h]});
   }
  }
  const boxes=nodes.map(n=>{const m=n.transform.baseVal.consolidate().matrix;return{id:n.getAttribute('data-diagram-node'),x:m.e,y:m.f,w:+n.getAttribute('data-node-width'),h:+n.getAttribute('data-node-height')}});
  for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++){
   const a=boxes[i],b=boxes[j];if(a.x<b.x+b.w-.5&&a.x+a.w>b.x+.5&&a.y<b.y+b.h-.5&&a.y+a.h>b.y+.5)errors.push({overlap:[a.id,b.id]});
  }
  return{nodes:nodes.length,edges:root.querySelectorAll('.diagram-edge').length,errors};
 });
}
async function button(page,root,label){await page.locator(root+' [aria-label="'+label+'"]').click();await wait(80);}
async function scale(page,root){return page.$eval(root+' .diagram-world',e=>+e.getAttribute('data-camera-scale'));}
async function interact(page,selector,label){
 await page.$eval(selector,e=>e.scrollIntoView({block:'center'}));await wait(100);
 await check(label+' labels fit their nodes without overlap',async()=>{const r=await geometry(page,selector);assert.ok(r.nodes>0);assert.deepEqual(r.errors,[]);});
 await check(label+' zoom and fit controls work',async()=>{let before=await scale(page,selector);await button(page,selector,'放大设计图');assert.ok((await scale(page,selector))>before);await button(page,selector,'缩小设计图');await button(page,selector,'以实际大小查看设计图');assert.equal(await scale(page,selector),1);await button(page,selector,'适应画布');});
 const firstId=await page.$eval(selector,e=>e.querySelector('[data-diagram-node]').getAttribute('data-diagram-node'));
 const actualNode=selector+' [data-diagram-node="'+firstId+'"]';
 await check(label+' click shows full details',async()=>{await page.locator(actualNode).click();await page.waitForSelector(selector+' .diagram-inspector');const p=await page.$eval(selector+' .diagram-inspector p',n=>n.textContent);assert.ok(p.length>0);await button(page,selector,'关闭节点详情');});
 await check(label+' dragging moves a node and reconnects edges',async()=>{
  await page.$eval(actualNode,e=>e.scrollIntoView({block:'center'}));await wait(100);
  const before=await page.$eval(actualNode,e=>e.getAttribute('transform'));
  const paths=await page.$$eval(selector+' .diagram-edge>path',ns=>ns.map(n=>n.getAttribute('d')).join('|'));
  const box=await page.$eval(actualNode,e=>{const b=e.getBoundingClientRect();return{x:b.x+b.width/2,y:b.y+b.height/2}});
  await page.mouse.move(box.x,box.y);await page.mouse.down();await page.mouse.move(box.x+38,box.y+24,{steps:8});await page.mouse.up();await wait(100);
  assert.notEqual(await page.$eval(actualNode,e=>e.getAttribute('transform')),before);
  if(paths)assert.notEqual(await page.$$eval(selector+' .diagram-edge>path',ns=>ns.map(n=>n.getAttribute('d')).join('|')),paths);
  assert.equal(await page.$(selector+' .diagram-inspector'),null,'dragging must not open node details');
 });
 await check(label+' node layout survives refresh',async()=>{
  const before=await page.$eval(actualNode,e=>e.getAttribute('transform'));
  await page.reload({waitUntil:'networkidle0'});await page.waitForSelector('.canvas-tabs');
  if(selector.includes('full-document')){await page.$eval('.canvas-tabs [role="tab"]:nth-child(2)',e=>e.click());}
  else if(label.includes('framework'))await page.$eval('.diagram-tabs [role="tab"]:nth-child(2)',e=>e.click());
  else if(label.includes('flow'))await page.$eval('.diagram-tabs [role="tab"]:nth-child(3)',e=>e.click());
  await page.waitForSelector(actualNode);await page.$eval(selector,e=>e.scrollIntoView({block:'center'}));
  assert.equal(await page.$eval(actualNode,e=>e.getAttribute('transform')),before);
 });
 await check(label+' reset restores automatic layout',async()=>{await button(page,selector,'重置节点布局');assert.deepEqual((await geometry(page,selector)).errors,[]);});
 await check(label+' blank canvas pans without changing nodes',async()=>{
  const before=await page.$eval(selector+' .diagram-world',e=>e.getAttribute('transform'));
  const node=await page.$eval(actualNode,e=>e.getAttribute('transform'));
  const blank=await page.$eval(selector+' .graph-stage',e=>{const b=e.getBoundingClientRect();const top=Math.max(b.y+15,165),bottom=Math.min(b.bottom-15,innerHeight-45);for(const p of [[20,(top+bottom)/2-b.y],[b.width-25,(top+bottom)/2-b.y],[20,bottom-b.y]]){const x=b.x+p[0],y=b.y+p[1],target=document.elementFromPoint(x,y);if(target?.closest('.graph-stage')===e&&!target.closest('[data-diagram-node]')&&!target.closest('.diagram-minimap'))return{x,y};}throw Error('No blank space');});
  await page.mouse.move(blank.x,blank.y);await page.mouse.down();await page.mouse.move(blank.x+35,blank.y+20,{steps:5});await page.mouse.up();
  assert.notEqual(await page.$eval(selector+' .diagram-world',e=>e.getAttribute('transform')),before);assert.equal(await page.$eval(actualNode,e=>e.getAttribute('transform')),node);
  await button(page,selector,'适应画布');
 });
 await check(label+' wheel zoom and normal document scrolling coexist',async()=>{
  await page.$eval(selector+' .graph-stage',e=>e.scrollIntoView({block:'center'}));await wait(100);
  const box=await page.$eval(selector+' .graph-stage',e=>{const b=e.getBoundingClientRect();return{x:b.x+b.width/2,y:b.y+b.height/2}});
  await page.mouse.move(box.x,box.y);let before=await scale(page,selector);await page.keyboard.down('Control');await page.mouse.wheel({deltaY:-70});await page.keyboard.up('Control');await wait(100);assert.ok(await scale(page,selector)>before);
  before=await scale(page,selector);await page.mouse.wheel({deltaY:60});await wait(100);assert.equal(await scale(page,selector),before);await button(page,selector,'适应画布');
 });
 await check(label+' keyboard can move a node',async()=>{
  const before=await page.$eval(actualNode,e=>e.getAttribute('transform'));
  await page.$eval(actualNode,e=>e.focus());await page.keyboard.press('ArrowRight');await wait(80);
  assert.notEqual(await page.$eval(actualNode,e=>e.getAttribute('transform')),before);await button(page,selector,'重置节点布局');
 });
 await check(label+' navigator can move to another part of the graph',async()=>{
  await button(page,selector,'以实际大小查看设计图');
  const nav=selector+' .diagram-minimap>svg';
  if(await page.$(nav)){
   await page.$eval(nav,e=>e.scrollIntoView({block:'center'}));
   const before=await page.$eval(selector+' .diagram-world',e=>e.getAttribute('transform'));
   const box=await page.$eval(nav,e=>{const b=e.getBoundingClientRect();return{x:b.x+b.width*.28,y:b.y+b.height*.7}});
   await page.mouse.click(box.x,box.y);await wait(80);assert.notEqual(await page.$eval(selector+' .diagram-world',e=>e.getAttribute('transform')),before);
  }
  await button(page,selector,'适应画布');
 });
 await check(label+' fullscreen opens and Escape restores the canvas',async()=>{
  await button(page,selector,'全屏查看设计图');await page.waitForSelector('.diagram-fullscreen-dialog[open]');await wait(100);
  assert.deepEqual((await geometry(page,'.diagram-fullscreen-dialog')).errors,[]);
  await page.screenshot({path:path.join(out,label.replaceAll(' ','-')+'-fullscreen.png')});
  if(label.includes('mindmap')){
   for(const position of [2,3,1]){
    await page.$eval('.diagram-fullscreen-dialog .diagram-tabs [role="tab"]:nth-child('+position+')',n=>n.click());
    assert.equal(await page.$$eval('.diagram-fullscreen-dialog[open]',ns=>ns.length),1,'switching diagram type must keep fullscreen open');
   }
  }
  await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('.diagram-fullscreen-dialog'));await page.waitForSelector(selector+' .graph-stage');
 });
}
try{
 for(let round=1;round<=2;round++){
  const context=await browser.createBrowserContext();ownedContexts.add(context);const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  await page.setRequestInterception(true);page.on('request',r=>{if(r.url().includes('/api/')&&!['GET','HEAD','OPTIONS'].includes(r.method())){writes.push({url:r.url(),method:r.method()});return r.abort();}void r.continue();});
  await page.setViewport({width:round===1?1520:1280,height:1020});
  const projectId=projects[round-1],saved=await fetch(base+'/api/projects/'+projectId).then(r=>r.json());await page.goto(base+'/?project='+projectId,{waitUntil:'networkidle0'});await page.waitForSelector('.diagram-world');assets.add(await page.$eval('script[src]',e=>new URL(e.src).pathname));
  for(const [name,position] of [['mindmap',1],['framework',2],['flow',3]]){
   await page.$eval('.canvas-tabs [role="tab"]:first-child',e=>e.click());await page.waitForSelector('.diagram-tabs');await page.$eval('.diagram-tabs [role="tab"]:nth-child('+position+')',e=>e.click());
   await check(`R${round} ${name} matches the saved document`,async()=>{
    const actual=await geometry(page,'.overview-page .interactive-diagram');
    if(name==='framework'){
     const modules=saved.convergence.modules.filter(m=>m.state!=='not_applicable'),ids=new Set(modules.map(m=>m.id));
     assert.equal(actual.nodes,modules.length);assert.equal(actual.edges,saved.convergence.module_links.filter(e=>ids.has(e.source)&&ids.has(e.target)).length);
    }
    if(name==='flow'){assert.equal(actual.nodes,saved.document.flow.length);assert.equal(actual.edges,saved.document.flow.length-1);}
    assert.equal(await page.$$eval('.diagram-node-badge',ns=>ns.filter(n=>n.textContent.includes('undefined')).length),0);
   });
   await interact(page,'.overview-page .interactive-diagram',`R${round} ${name}`);
  }
  await page.$eval('.canvas-tabs [role="tab"]:nth-child(2)',e=>e.click());await page.waitForSelector('.full-document .interactive-diagram');
  await check(`R${round} Markdown diagrams render instead of source`,async()=>{assert.equal(await page.$$eval('.full-document pre code.language-mermaid',ns=>ns.length),0);assert.equal(await page.$$eval('.full-document .interactive-diagram',ns=>ns.length),2);});
  await interact(page,'.full-document .interactive-diagram:nth-of-type(1)',`R${round} document-system`);
  // Use an explicit diagram identity, independent of preceding Markdown tags.
  await interact(page,'.full-document .interactive-diagram:has([data-diagram-title="工作流程草案"])',`R${round} document-workflow`);
  await page.$eval('.full-document .interactive-diagram',e=>e.scrollIntoView({block:'center'}));await page.screenshot({path:path.join(out,`R${round}-document-desktop.png`)});
  await page.setViewport({width:round===1?390:320,height:844,isMobile:true,hasTouch:true});await page.waitForSelector('.canvas-tabs');await page.locator('.mobile-switch [role="tab"]:nth-child(2)').click();await page.$eval('.canvas-tabs [role="tab"]:nth-child(2)',e=>e.click());await page.waitForSelector('.full-document .interactive-diagram');
  await check(`R${round} mobile diagram controls fit`,async()=>{assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));for(const r of await page.$$eval('.full-document .interactive-diagram .diagram-toolbar',ns=>ns.map(e=>({w:e.clientWidth,s:e.scrollWidth}))))assert.ok(r.s<=r.w+1);});
  const mobile='.full-document .interactive-diagram:has([data-diagram-title="工作流程草案"])';await page.$eval(mobile,e=>e.scrollIntoView({block:'center'}));await wait(100);
  await check(`R${round} two-finger touch zoom works`,async()=>{
   const box=await page.$eval(mobile+' .graph-stage',e=>{const b=e.getBoundingClientRect();return{x:b.x+b.width/2,y:b.y+b.height/2}}),cdp=await page.createCDPSession();
   const before=await scale(page,mobile);
   await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:box.x-25,y:box.y,id:1},{x:box.x+25,y:box.y,id:2}]});
   await cdp.send('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:box.x-65,y:box.y,id:1},{x:box.x+65,y:box.y,id:2}]});
   await cdp.send('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await wait(150);assert.ok(await scale(page,mobile)>before);await cdp.detach();await button(page,mobile,'适应画布');
  });
  await page.screenshot({path:path.join(out,`R${round}-document-mobile.png`)});await context.close();
 }
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);assert.equal(assets.size,1);
 await fs.writeFile(path.join(out,'report.json'),JSON.stringify({ok:true,base,read_only:true,real_inference:false,rounds:2,browser_assets:[...assets],checks,errors,writes,completed_at:new Date().toISOString()},null,2)+'\n');
 console.log('All diagram interaction checks passed.');
}catch(error){await fs.writeFile(path.join(out,'failure.json'),JSON.stringify({ok:false,error:String(error),checks,errors,writes,at:new Date().toISOString()},null,2)+'\n');throw error;}
finally{await Promise.allSettled([...ownedContexts].map(c=>c.close()));browser.disconnect();}
