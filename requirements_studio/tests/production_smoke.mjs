// Read-only release check against the existing workspace. All mutations are blocked.
import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const root=process.cwd(),out=path.join(root,'.runtime/verification/ui-audit');
const require=createRequire(path.join(root,'frontend/package.json'));
const before=JSON.parse(await fs.readFile(path.join(out,'production-before.json'),'utf8'));
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',pipe:true,headless:true,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},userDataDir:path.join(out,'production-read-only-profile'),args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage']});
const rounds=[];
try {
  for(let round=1;round<=2;round++) {
    const page=await browser.newPage(),errors=[],mutations=[],checks=[];
    const check=(name,value)=>{assert.ok(value,name);checks.push(name);console.log(`PASS PROD R${round} ${name}`);};
    page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
    await page.setRequestInterception(true);
    page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method())){mutations.push(r.method()+' '+r.url());void r.abort();}else void r.continue();});
    await page.setViewport({width:1440,height:1000});
    await page.evaluateOnNewDocument(()=>localStorage.setItem('requirements-studio:project','63af06c7b778467687ad6d0639e58a14'));
    await page.goto('http://[::1]:8878/',{waitUntil:'domcontentloaded'});
    await page.waitForSelector('.requirements-list');
    check('fixed model label remains correct',await page.$eval('.model-label',el=>el.textContent.includes('gpt6-luna-xhigh')));
    check('full V3 coverage panel exists',await page.$eval('[aria-label="V3 章节覆盖"]',el=>el.getAttribute('aria-valuemax')==='50'));
    const stored=await page.evaluate(async()=>{
      const rows=await fetch('/api/projects').then(r=>r.json());
      return Promise.all(rows.map(r=>fetch('/api/projects/'+r.id).then(r=>r.json())));
    });
    check('all original project versions and message counts remain intact',before.projects.every(p=>stored.some(s=>s.id===p.id&&s.document.version===p.version&&s.messages.length===p.messages)));
    const original=stored.find(p=>p.id==='63af06c7b778467687ad6d0639e58a14');
    check('original ten requirements including Android remain available',original.document.requirements.length===10&&JSON.stringify(original.document).includes('Android'));
    for(const [index,label] of [[2,'需求说明书'],[3,'变更记录'],[1,'设计总览']]) {
      await page.locator(`.canvas-tabs [role="tab"]:nth-child(${index})`).click();
      check(`${label} tab is usable`,await page.$eval(`.canvas-tabs [role="tab"][aria-selected="true"]`,(el,text)=>el.textContent.includes(text),label));
    }
    if(await page.$eval('.sidebar',el=>el.getBoundingClientRect().width<200)) {
      await page.locator('.toggle-sidebar').click();
      await page.waitForFunction(()=>document.querySelector('.sidebar').getBoundingClientRect().width>200);
    }
    await page.locator('[data-project-id="d323263c5026476ca9df0e25bf5da5c1"]').click();
    await page.waitForSelector('[data-question-id="accuracy"]');
    check('legacy accuracy question derives its performance branch',await page.$eval('[data-question-id="accuracy"] .question-meta',el=>el.textContent.includes('性能指标')));
    await page.screenshot({path:path.join(out,`production-desktop-round-${round}.png`)});
    await page.setViewport({width:390,height:844});
    await page.locator('.mobile-switch [role="tab"]:nth-child(2)').click();
    await page.waitForFunction(()=>document.querySelector('.canvas-panel')?.getBoundingClientRect().width>300);
    check('mobile canvas has no page overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    await page.screenshot({path:path.join(out,`production-mobile-round-${round}.png`)});
    check('no runtime or console errors',errors.length===0);
    check('no data-changing request was attempted',mutations.length===0);
    rounds.push({round,provider:'production_read_only',checks,console_errors:errors,mutations});
    await fs.writeFile(path.join(out,'production-ui.json'),JSON.stringify({ok:true,rounds},null,2));
    await page.close();
  }
} finally {
  await browser.close();
  await fs.rm(path.join(out,'production-read-only-profile'),{recursive:true,force:true});
}
console.log(JSON.stringify({ok:true,rounds:rounds.map(r=>({round:r.round,checks:r.checks.length}))}));
