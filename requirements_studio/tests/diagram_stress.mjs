// Deliberately long labels and dense graphs, intercepted in the browser only.
// Existing databases, user messages and model calls are never modified.
import assert from 'node:assert/strict';import fs from 'node:fs/promises';import path from 'node:path';import{createRequire}from'node:module';
const root=process.cwd(),require=createRequire(path.join(root,'frontend/package.json'));
const out='.runtime/phase3/diagram-stress';await fs.mkdir(out,{recursive:true});
const base='http://[::1]:8896',project=await fetch(base+'/api/projects/3ea063afa8734afaaad4de1563085919').then(r=>r.json());
const bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws});const context=await browser.createBrowserContext(),page=await context.newPage();
const errors=[],writes=[],checks=[];page.on('pageerror',e=>errors.push(e.message));
const clone=structuredClone(project);clone.id='diagram-stress-fixture';clone.title=clone.document.title='长标题与多模块画布布局验收';
const cat=['overview','functions','performance','manufacturing','architecture','selection'];
clone.document.requirements=Array.from({length:90},(_,i)=>({...project.document.requirements[0],id:'REQ-'+String(i+1).padStart(3,'0'),name:'第'+(i+1)+'项需求：'+(i%3?'长文字标题验证完整自动换行':'VeryLongUnbrokenEnglishWordWithoutSpaces'.repeat(4)),value:'完整内容不会写入数据库。'.repeat(30),category:cat[i%6],state:'stated'}));
clone.document.modules=Array.from({length:30},(_,i)=>({...project.convergence.modules[0],id:'MOD-'+String(i+1).padStart(3,'0'),name:i===0?'第一模块 '+'MixedLatin0123456789'.repeat(20)+' 中文完整名称验证'.repeat(10):'第'+(i+1)+'模块 '+(i%3?'自动换行与连线验证':'中文长名称'.repeat(8)),type_name:'规范类别完整名称（需要完整显示且不能被截断）',purpose:'模块用途的完整解释。'.repeat(35),boundary:'完整边界。'.repeat(30),state:'confirmed',requirement_ids:['REQ-'+String(i+1).padStart(3,'0')]}));
clone.document.flow=Array.from({length:18},(_,i)=>({label:'步骤'+(i+1)+'：'+(i%4?'上电、自检、采集、处理、输出与异常恢复验证':'这是很长的流程标题，必须完整换行'.repeat(12)),requirement_ids:['REQ-'+String(i+1).padStart(3,'0')]}));
clone.convergence.modules=clone.document.modules;clone.convergence.module_links=Array.from({length:30},(_,i)=>({id:'edge-'+i,source:'MOD-'+String(i+1).padStart(3,'0'),target:'MOD-'+String((i+1)%30+1).padStart(3,'0'),kind:i%2?'data':'power'}));
const clean=s=>s.replace(/["<>\[\]{}\n\r`]/g,' ');
clone.markdown='# 图形测试（浏览器内受控数据）\n\n## 模块关系草案\n\n```mermaid\nflowchart LR\n'+clone.document.modules.map(m=>`  ${m.id.replaceAll('-','_')}["${clean(m.name)}"]`).join('\n')+'\n'+clone.convergence.module_links.map(e=>`  ${e.source.replaceAll('-','_')} -->|数据| ${e.target.replaceAll('-','_')}`).join('\n')+'\n```\n\n## 工作流程草案\n\n```mermaid\nflowchart LR\n'+clone.document.flow.map((s,i)=>`  Step${i}["${clean(s.label)}"]`+(i?`\n  Step${i-1} --> Step${i}`:'')).join('\n')+'\n```';
await page.setRequestInterception(true);page.on('request',r=>{
 const url=new URL(r.url());if(url.pathname.startsWith('/api/')&&!['GET','HEAD'].includes(r.method())){writes.push(r.url());return r.abort();}
 if(url.pathname==='/api/projects')return r.respond({status:200,contentType:'application/json',body:JSON.stringify([{id:clone.id,title:clone.title,version:clone.document.version,requirement_count:90,updated_at:clone.updated_at}])});
 if(url.pathname==='/api/projects/'+clone.id)return r.respond({status:200,contentType:'application/json',body:JSON.stringify(clone)});
 return r.continue();
});
async function validate(label){
 await page.waitForSelector('.diagram-world');await new Promise(r=>setTimeout(r,200));
 const result=await page.$$eval('.interactive-diagram',roots=>roots.map(root=>{const errors=[];for(const node of root.querySelectorAll('[data-diagram-node]')){const w=+node.getAttribute('data-node-width'),h=+node.getAttribute('data-node-height');for(const text of node.querySelectorAll('text')){const b=text.getBBox();if(b.x<6||b.y<2||b.x+b.width>w-5||b.y+b.height>h-3)errors.push({text:text.textContent,box:[b.x,b.y,b.width,b.height],size:[w,h]});}}return{title:root.querySelector('[data-diagram-title]')?.getAttribute('data-diagram-title'),nodes:root.querySelectorAll('[data-diagram-node]').length,errors}}));
 for(const r of result)assert.deepEqual(r.errors,[],label+' '+r.title);
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.equal(await page.$$eval('.diagram-node img,.diagram-node script',ns=>ns.length),0);
 checks.push({label,graphs:result});console.log('PASS',label);
}
try{
 for(let round=1;round<=2;round++){
  await page.setViewport({width:round===1?1520:390,height:1020,isMobile:round===2,hasTouch:round===2});
  await page.goto(base+'/?project='+clone.id,{waitUntil:'networkidle0'});await page.waitForSelector('.diagram-world');
  if(round===2)await page.locator('.mobile-switch [role="tab"]:nth-child(2)').click();
  await validate('R'+round+' 90-requirement collapsed mindmap');
  await page.$eval('[data-diagram-node="category-functions"]',n=>n.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true})));
  await validate('R'+round+' expanded branch');
  assert.equal(await page.$$eval('[data-diagram-node^="more-"]',ns=>ns.length),5);
  await page.$eval('.diagram-tabs [role="tab"]:nth-child(2)',e=>e.click());await validate('R'+round+' 30-module long-label framework');
  assert.equal(await page.$$eval('.diagram-node',ns=>ns.length),30);
  await page.$eval('.diagram-tabs [role="tab"]:nth-child(3)',e=>e.click());await validate('R'+round+' 18-step long-label folded flow');
  assert.equal(await page.$$eval('.diagram-node',ns=>ns.length),18);
  await page.$eval('.canvas-tabs [role="tab"]:nth-child(2)',e=>e.click());await page.waitForSelector('.full-document .interactive-diagram');await validate('R'+round+' both Markdown diagrams');
  assert.equal(await page.$$eval('.full-document .interactive-diagram',ns=>ns.length),2);
  assert.equal(await page.$$eval('.full-document pre code',ns=>ns.length),0);
  assert.equal(await page.$$eval('.diagram-node-badge',ns=>ns.filter(n=>n.textContent.includes('undefined')).length),0);
  if(round===1){await page.$eval('.full-document .interactive-diagram:last-of-type',e=>e.scrollIntoView({block:'center'}));await page.screenshot({path:out+'/long-flow.png'});}
 }
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
 await fs.writeFile(out+'/report.json',JSON.stringify({ok:true,controlled_browser_data_only:true,real_inference:false,rounds:2,checks,errors,writes,at:new Date().toISOString()},null,2)+'\n');
}finally{await context.close();browser.disconnect();}
