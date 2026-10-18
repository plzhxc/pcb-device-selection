import { createRequire } from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const require = createRequire(new URL('../frontend/package.json', import.meta.url));
const puppeteer = require('puppeteer-core');
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = process.env.STUDIO_TEST_OUTPUT ?? path.join(root, '.runtime', 'verification');
const assets = process.env.STUDIO_TEST_ASSETS ?? path.resolve(root, '../docs/assets');
await fs.mkdir(output, { recursive: true });
await fs.mkdir(assets, { recursive: true });
const base = process.env.STUDIO_TEST_URL ?? 'http://[::1]:8880';
const executablePath = process.env.STUDIO_CHROME ?? '/usr/bin/chromium-browser';
const browser = await puppeteer.launch({ executablePath, headless: true, pipe: true, env: {...process.env, LANG:'zh_CN.utf8', LC_ALL:'zh_CN.utf8'}, args: ['--no-sandbox', '--disable-dev-shm-usage', '--no-proxy-server', '--disable-background-networking'], userDataDir: path.join(output, 'browser-profile') });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 1000, deviceScaleFactor: 1 });
const errors = [], checks = [];
let realUI = null;
page.on('pageerror', error => errors.push(error.message));
page.on('console', message => { if(message.type()==='error') errors.push(message.text()); });
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
const check = (label, result) => { assert.ok(result,label); checks.push(label); console.log(`PASS ${label}`); };
async function click(selector) { await page.locator(selector).click(); }
async function project() { return page.evaluate(async()=>{const id=localStorage.getItem('requirements-studio:project');return fetch(`/api/projects/${id}`).then(r=>r.json());}); }
async function waitReady() { await page.waitForFunction(()=>!!document.querySelector('.composer textarea') && !document.querySelector('.loading-state'),{timeout:30000}); }
async function noOverflow(label) { check(label,await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)); }
async function clickTab(text) { await page.$$eval('.canvas-tabs button',(buttons,text)=>buttons.find(b=>b.textContent.includes(text)).click(),text); }

try {
  await page.goto(base,{waitUntil:'domcontentloaded'}); await waitReady();
  await click('.new-project'); await page.waitForSelector('.chat-empty');
  await noOverflow('desktop empty layout has no page overflow');
  await page.screenshot({path:path.join(assets,'需求工作台-空白项目.png')});
  await click('.starter-prompts button');
  check('starter inserts editable input',await page.$eval('textarea',el=>el.value.includes('蓝牙温度记录器')));
  await page.focus('.composer textarea');
  await page.keyboard.down('Shift');await page.keyboard.press('Enter');await page.keyboard.up('Shift');
  check('Shift+Enter inserts newline',await page.$eval('.composer textarea',el=>el.value.includes('\n')));
  let sent=0;page.on('request',request=>{if(request.method()==='POST'&&request.url().endsWith('/messages'))sent++;});
  await page.$eval('.composer textarea',el=>el.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',code:'Enter',isComposing:true,bubbles:true})));
  await sleep(200);check('Chinese composition Enter does not send',sent===0);
  await page.reload({waitUntil:'domcontentloaded'});await waitReady();
  check('unsent draft restores after refresh',await page.$eval('.composer textarea',el=>el.value.includes('蓝牙温度记录器')));
  await click('.sample-entry');await page.waitForSelector('.example-notice');await page.waitForSelector('.requirements-list');
  await page.screenshot({path:path.join(assets,'需求工作台-桌面总览.png')});
  const sample=await project();check('example is visibly labelled and stored independently',sample.is_example&&sample.document.requirements.length===9);
  await click('.diagram-tabs button:nth-child(2)');
  check('functional framework renders',await page.$eval('.diagram-viewport svg',svg=>svg.getAttribute('aria-label')==='功能框架草案'));
  await page.screenshot({path:path.join(assets,'需求工作台-功能框架.png')});
  await click('.diagram-tabs button:nth-child(3)');
  check('workflow renders saved steps',(await page.$$('.graph-node')).length===sample.document.flow.length);
  await click('[aria-label="放大设计图"]');check('diagram zoom works',await page.$eval('.zoom-tools span',el=>el.textContent==='125%'));
  await click('[aria-label="重置设计图缩放"]');
  await click('.graph-node.interactive');await page.waitForSelector('.context-chip');
  check('diagram node binds a concrete requirement',await page.$eval('.context-chip',el=>el.textContent.includes('REQ-')));
  await click('[aria-label="取消需求引用"]');
  await click('.requirement-main');await page.waitForSelector('dialog[open]');
  await click('dialog textarea');await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.type('室内办公环境（验收修改）');
  await click('dialog .button.primary');await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
  await page.waitForFunction(()=>document.querySelector('.requirements-list').textContent.includes('室内办公环境（验收修改）'));
  let saved=await project();check('manual edit saves a new revision and source',saved.document.version===2&&saved.document.requirements[0].source_type==='manual');
  check('save notification does not intercept interaction',await page.$eval('.toast',el=>getComputedStyle(el).pointerEvents==='none'));
  check('manual summary follows edited facts',saved.document.summary.includes('室内办公环境（验收修改）'));
  await page.reload({waitUntil:'domcontentloaded'});await waitReady();
  saved=await project();check('project and edits persist across refresh',saved.document.version===2&&saved.document.requirements[0].value==='室内办公环境（验收修改）');
  await clickTab('需求说明书');await page.waitForSelector('.full-document .markdown h1');
  check('full document includes edited content',await page.$eval('.full-document',el=>el.textContent.includes('室内办公环境（验收修改）')));
  await page.screenshot({path:path.join(assets,'需求工作台-需求说明书.png')});
  await clickTab('变更记录');await page.waitForSelector('.revision');check('revision history shows both versions',(await page.$$('.revision')).length===2);
  const downloads=path.join(output,`downloads-${Date.now()}`);await fs.mkdir(downloads,{recursive:true});
  const cdp=await page.createCDPSession();await cdp.send('Page.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
  await click('.export-menu>button');await click('.dropdown a:first-of-type');
  for(let i=0;i<50;i++){if((await fs.readdir(downloads)).some(f=>f.endsWith('.md')))break;await sleep(100);}
  const files=await fs.readdir(downloads);check('Markdown export actually downloads a file',files.some(f=>f.endsWith('.md')));
  const md=await fs.readFile(path.join(downloads,files.find(f=>f.endsWith('.md'))),'utf8');check('downloaded Markdown matches saved version',md.includes('版本：v2')&&md.includes('室内办公环境（验收修改）'));
  await click('.export-menu>button');await click('.dropdown a:nth-of-type(2)');
  for(let i=0;i<50;i++){if((await fs.readdir(downloads)).some(f=>f.endsWith('.json')))break;await sleep(100);}
  const jsonFile=(await fs.readdir(downloads)).find(f=>f.endsWith('.json'));
  check('JSON export actually downloads a file',!!jsonFile);
  const json=JSON.parse(await fs.readFile(path.join(downloads,jsonFile),'utf8'));
  check('JSON export matches canonical requirement data',json.document.version===2&&json.document.requirements[0].value==='室内办公环境（验收修改）');
  await clickTab('设计总览');
  await page.type('.project-search input','不存在的验收项目');
  check('project search filters the list',(await page.$$('.project-item')).length===0);
  await click('[aria-label="清除搜索"]');
  await click('.canvas-toggle');check('canvas collapse gives space to conversation',await page.$eval('.canvas-panel',el=>getComputedStyle(el).display==='none'));
  await click('.canvas-toggle');
  await click('[aria-label="全屏查看画布"]');check('canvas focus mode works',await page.$eval('.chat-panel',el=>getComputedStyle(el).display==='none'));
  await click('[aria-label="恢复对话与画布"]');
  const widthBefore=await page.$eval('.chat-panel',el=>el.getBoundingClientRect().width);
  await page.focus('.resize-handle');await page.keyboard.press('ArrowRight');
  check('keyboard adjusts split-pane width',(await page.$eval('.chat-panel',el=>el.getBoundingClientRect().width))>widthBefore);
  await page.keyboard.press('ArrowLeft');
  await page.setViewport({width:1100,height:800});await sleep(100);
  const beforeDrag=await page.$eval('.chat-panel',el=>el.getBoundingClientRect().width);
  let handle=await page.$('.resize-handle'),box=await handle.boundingBox();
  await page.mouse.move(box.x+.5,box.y+200);await page.mouse.down();await page.mouse.move(box.x-39.5,box.y+200,{steps:5});await page.mouse.up();
  check('pointer resizing works on a narrow notebook layout',beforeDrag-(await page.$eval('.chat-panel',el=>el.getBoundingClientRect().width))>30);
  box=await handle.boundingBox();await page.mouse.move(box.x+.5,box.y+200);await page.mouse.down();await page.mouse.move(box.x+40.5,box.y+200,{steps:5});await page.mouse.up();
  check('pointer release restores normal page interaction',await page.evaluate(()=>!document.body.classList.contains('resizing')));
  await page.$eval('.canvas-scroll',el=>el.scrollTop=0);
  await page.setViewport({width:1280,height:800});await sleep(150);await noOverflow('1280px notebook layout fits');
  await page.setViewport({width:390,height:844});await sleep(150);await noOverflow('390px mobile chat fits');
  await page.screenshot({path:path.join(assets,'需求工作台-手机对话.png')});
  await click('.mobile-switch button:nth-child(2)');await noOverflow('390px mobile canvas fits');
  await page.screenshot({path:path.join(assets,'需求工作台-手机画布.png')});
  await page.setViewport({width:320,height:740});await sleep(100);await noOverflow('320px narrow canvas fits');
  await page.setViewport({width:768,height:1024});await sleep(100);await noOverflow('tablet layout fits');
  await page.setViewport({width:1440,height:1000});await sleep(150);
  if(!await page.$eval('.sidebar',el=>el.getBoundingClientRect().width>100))await click('.toggle-sidebar');
  await page.$eval('.canvas-scroll',el=>el.scrollTop=0);
  if(process.argv.includes('--capture-only')) {
    check('browser console is free of errors',errors.length===0);
  } else {
    // A real model turn initiated by the visible UI, including mid-run refresh.
    await click('.new-project');await page.waitForSelector('.chat-empty');
    await page.type('.composer textarea','我需要一个USB供电的室内温度显示器，预算50元，必须显示当前温度。精度暂不确定。');
    await click('[aria-label="发送需求"]');await page.waitForSelector('[aria-label="停止分析"]');
    const running=await project();await page.reload({waitUntil:'domcontentloaded'});await waitReady();
    check('refresh restores active background analysis',(await project()).latest_run.id===running.latest_run.id);
    await click(`.project-item[data-project-id="${sample.id}"]`);await page.waitForSelector('.requirements-list');
    await page.waitForFunction(async id=>{const p=await fetch(`/api/projects/${id}`).then(r=>r.json());return p.latest_run.status!=='queued'&&p.latest_run.status!=='running';},{timeout:620000,polling:1000},running.id);
    check('background completion does not replace the selected project',(await project()).id===sample.id);
    await click(`.project-item[data-project-id="${running.id}"]`);
    await page.waitForFunction(()=>!!document.querySelector('.requirements-list')&&!document.querySelector('[aria-label="停止分析"]'),{timeout:620000,polling:1000});
    const real=await project();check('real Codex UI turn produces persisted requirements',real.latest_run.status==='completed'&&real.document.requirements.length>0&&real.messages.at(-1).role==='assistant');
    check('returning to a background project updates sidebar metadata',await page.$eval(`.project-item[data-project-id="${running.id}"]`,el=>el.textContent.includes('v1')));
    if(real.document.questions.length){
      if(await page.$('.question-option'))await click('.question-option');
      else await page.type('.custom-answer','使用普通室内监测精度即可，请保留未明确的参数。');
      await click('.question-footer .button.primary');
      await page.waitForFunction(()=>document.querySelector('.version-label')?.textContent==='v2'&&!document.querySelector('[aria-label="停止分析"]'),{timeout:620000,polling:1000});
      const followup=await project();
      check('question-card answers complete a real second UI turn',followup.document.version===2&&followup.messages.length===4);
      check('second UI turn retains previous requirement identities',real.document.requirements.every(old=>followup.document.requirements.some(r=>r.id===old.id)));
      realUI={project_id:followup.id,version:followup.document.version,requirements:followup.document.requirements.length,run_id:followup.latest_run.id};
    }else throw new Error('Expected the deliberately incomplete real scenario to produce a question card.');
    await page.screenshot({path:path.join(assets,'需求工作台-真实Codex对话.png')});
    // Cancellation must not turn a partial CLI result into a document revision.
    await click('.new-project');await page.waitForSelector('.chat-empty');
    await page.type('.composer textarea','我想设计一个带显示屏的便携气象站，请帮我逐步梳理需求。');
    await click('[aria-label="发送需求"]');await page.waitForSelector('[aria-label="停止分析"]');
    await click('[aria-label="停止分析"]');await page.waitForSelector('.run-notice');
    const cancelled=await project();check('stop preserves previous saved document',cancelled.latest_run.status==='cancelled'&&cancelled.document.version===0);
    check('cancelled turn offers retry',await page.$eval('.run-notice',el=>el.textContent.includes('重试本轮')));
    check('browser console is free of errors',errors.length===0);
  }
  await fs.writeFile(path.join(output,'browser.json'),JSON.stringify({ok:true,provider:process.env.STUDIO_TEST_PROVIDER??'real_codex_cli',base_url:base,checks,console_errors:errors,real_ui:realUI,screenshots_dir:assets},null,2));
  console.log(JSON.stringify({ok:true,checks:checks.length,screenshots:assets}));
} catch(error) {
  await page.screenshot({path:path.join(output,'browser-failure.png')}).catch(()=>{});
  await fs.writeFile(path.join(output,'browser-failure.json'),JSON.stringify({error:String(error),checks,console_errors:errors},null,2));
  throw error;
} finally { await browser.close(); }
