// Read-only release verification. User projects must never become test fixtures.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const root=process.cwd(),out=path.join(root,'.runtime/phase2/verification');
const before=JSON.parse(await fs.readFile(path.join(out,'production-before.json'),'utf8'));
const target=before.projects.find(p=>p.requirements>0);
assert.ok(target,'an existing populated project is required for the read-only check');
const expectedScript=(await fs.readFile(path.join(root,'.runtime/phase2-dist/index.html'),'utf8')).match(/src="(\/assets\/[^"]+\.js)"/)[1];
const require=createRequire(path.join(root,'frontend/package.json')),profile=path.join(out,'production-browser-profile');
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',pipe:true,headless:true,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},userDataDir:profile,args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage']});
const rounds=[];
try{for(let round=1;round<=2;round++){
  const page=await browser.newPage(),checks=[],errors=[],mutations=[];
  const check=(name,condition)=>{assert.ok(condition,name);checks.push(name);console.log(`PASS RELEASE R${round} ${name}`);};
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  await page.setRequestInterception(true);
  page.on('request',r=>{if(!['GET','HEAD','OPTIONS'].includes(r.method())){mutations.push(r.method()+' '+r.url());void r.abort();}else void r.continue();});
  await page.setViewport({width:1440,height:1000});
  await page.evaluateOnNewDocument(id=>localStorage.setItem('requirements-studio:project',id),target.id);
  await page.goto('http://[::1]:8878/',{waitUntil:'domcontentloaded'});
  await page.waitForSelector('.canvas-tabs');
  check('published UI uses the verified build',await page.evaluate(script=>[...document.scripts].some(s=>s.getAttribute('src')===script),expectedScript));
  check('fixed Codex model label is visible',await page.$eval('.model-label',e=>e.textContent.includes('gpt6-luna-xhigh')));
  check('all five canvas views are present',await page.$$eval('.canvas-tabs [role="tab"]',e=>e.length)===5);
  const stored=await page.evaluate(async()=>{const rows=await fetch('/api/projects').then(r=>r.json());return Promise.all(rows.map(p=>fetch('/api/projects/'+p.id).then(r=>r.json())));});
  check('all existing project versions, messages and requirement counts survive',before.projects.every(p=>stored.some(s=>s.id===p.id&&s.document.version===p.version&&s.messages.length===p.messages&&s.document.requirements.length===p.requirements)));
  const android=before.projects.filter(p=>p.has_android);
  check('existing Android content remains available',android.every(p=>JSON.stringify(stored.find(s=>s.id===p.id).document).includes('Android')));
  await page.locator('.canvas-tabs [role="tab"]:nth-child(4)').click();
  await page.waitForSelector('.convergence-page');
  check('convergence stages are usable',await page.$$eval('.convergence-stages>button',e=>e.length)===5);
  check('complete field ledger is displayed',await page.$eval('.convergence-metrics',e=>e.textContent.includes('247')));
  const current=stored.find(p=>p.id===target.id);
  check('legacy documents show an explicit migration boundary',current.convergence.enabled||await page.$eval('.convergence-board',e=>e.textContent.includes('下一轮分析')));
  await page.locator('.canvas-tabs [role="tab"]:nth-child(5)').click();
  await page.waitForSelector('.catalog-tree>details');
  check('all sixty parent categories are browsable',await page.$$eval('.catalog-tree>details',e=>e.length)===60);
  check('category names lead the module selector',await page.$$eval('.catalog-categories>button',e=>e.length)===11);
  const type=await page.evaluate(async()=>{const c=await fetch('/api/module-catalog').then(r=>r.json());return c.types.find(t=>t.level==='parent');});
  await page.locator('[aria-label="搜索模块类别"]').fill(type.name);
  await page.locator(`[data-type-id="${type.id}"]`).click();
  await page.waitForFunction(name=>document.querySelector('.module-spec-detail h2')?.textContent===name,{},type.name);
  check('functional category details are readable',await page.$eval('.module-spec-detail',e=>e.textContent.includes('功能边界')));
  await page.locator('[aria-label="关闭模块规范详情"]').click();
  await page.locator('[aria-label="搜索模块类别"]').fill('');
  await page.screenshot({path:path.join(out,`production-modules-${round}.png`)});
  await page.locator('.canvas-tabs [role="tab"]:nth-child(2)').click();
  await page.waitForSelector('.document-review-banner');
  check('full document includes a review status and next action',await page.$eval('.document-review-banner',e=>e.textContent.length>30&&!!e.querySelector('button')));
  for(const [index,label] of [[3,'变更记录'],[1,'设计总览']]){
    await page.locator(`.canvas-tabs [role="tab"]:nth-child(${index})`).click();
    check(`${label} remains available`,await page.$eval('.canvas-tabs [aria-selected="true"]',(e,label)=>e.textContent.includes(label),label));
  }
  await page.setViewport({width:390,height:844});
  await page.locator('.mobile-switch [role="tab"]:nth-child(2)').click();
  await page.waitForFunction(()=>document.querySelector('.canvas-panel')?.getBoundingClientRect().width>300);
  await page.locator('.canvas-tabs [role="tab"]:nth-child(5)').click();
  check('mobile module canvas fits the screen',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:path.join(out,`production-mobile-${round}.png`)});
  check('no browser runtime or console errors',errors.length===0);
  check('no data-changing requests were attempted',mutations.length===0);
  rounds.push({round,provider:'production_read_only',script_asset:expectedScript,checks,console_errors:errors,mutations});
  await fs.writeFile(path.join(out,'production-ui.json'),JSON.stringify({ok:rounds.length===2,rounds},null,2));
  await page.close();
}}
finally{await browser.close();await fs.rm(profile,{recursive:true,force:true});}
console.log(JSON.stringify({ok:true,rounds:rounds.map(r=>({round:r.round,checks:r.checks.length}))}));
