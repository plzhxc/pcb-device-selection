// Read-only final visual checks of retained real projects on the published UI.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
const root=process.cwd(),require=createRequire(path.join(root,'frontend/package.json'));
const bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws});
const context=await browser.createBrowserContext(),page=await context.newPage(),errors=[];
const out=path.join(root,'.runtime/phase3/visual');await fs.mkdir(out,{recursive:true});
page.on('pageerror',e=>errors.push(e.message));await page.setCacheEnabled(false);
const base='http://[::1]:8878';
const projects=await fetch(base+'/api/projects').then(r=>r.json());
const selected=[projects.find(p=>p.id==='c885fb0b6c2e4a28aeb35da8dfea6b32'),projects.find(p=>p.title.includes('CASE-10'))];
const checks=[];
try{
 for(const p of selected){
  assert.ok(p,'retained case missing');await page.setViewport({width:1520,height:1020});
  await page.goto(base+'/?project='+p.id,{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.message.user');await page.waitForSelector('.canvas-tabs');
  assert.equal(await page.$eval('.version-label',e=>e.textContent),'v'+p.version);
  for(let i=0;i<5;i++){
   await page.$$eval('.canvas-tabs [role="tab"]',(ns,i)=>ns[i].click(),i);
   await page.waitForFunction(()=>!document.querySelector('.loading-state'));
   assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  }
  await page.$$eval('.canvas-tabs [role="tab"]',ns=>ns[0].click());
  await page.screenshot({path:path.join(out,p.id+'-desktop.png')});
  await page.setViewport({width:390,height:844});
  await page.locator('.mobile-switch [role="tab"]:nth-child(2)').click();
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(out,p.id+'-mobile.png')});
  checks.push({project_id:p.id,title:p.title,version:p.version,views:5,desktop:1520,mobile:390});
 }
 assert.equal(errors.length,0,errors.join('; '));
 const health=await fetch(base+'/api/health').then(r=>r.json());
 const browser_asset=await page.$eval('script[src]',e=>new URL(e.src).pathname);
 await fs.writeFile(path.join(out,'report.json'),JSON.stringify({ok:true,read_only:true,backend_build:health.backend_build,browser_asset,checks,errors},null,2)+'\n');
 console.log('Published UI: 10 retained-case views and desktop/mobile checks passed.');
}finally{await context.close();browser.disconnect();}
