// Improve retained acceptance-project names through the actual UI, then export
// the resulting version again. Never edits requirement facts or model receipts.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {spawn} from 'node:child_process';
import {createHash} from 'node:crypto';

const root=process.cwd(),args=process.argv.slice(2),position=args.indexOf('--suites');
assert.ok(position>=0,'Pass completed suite names with --suites a,b');
const suites=args[position+1].split(',');assert.ok(suites.every(s=>/^[A-Za-z0-9_-]+$/.test(s)));
const catalog=JSON.parse(await fs.readFile('tests/phase4_case_catalog.json','utf8'));
const names=Object.fromEntries(catalog.map(c=>[c.id,c.title.replace(/\[(?:已验证|未验证)\]|【(?:已验证|未验证)】/g,'').trim()]));
const require=createRequire(path.join(root,'frontend/package.json'));
const bridge=JSON.parse(await fs.readFile('.runtime/phase3/browser-bridge.json','utf8'));
const browser=await require('puppeteer-core').connect({browserWSEndpoint:bridge.ws});
const context=await browser.createBrowserContext(),page=await context.newPage(),errors=[],records=[];
page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1520,height:1020});
const semantics=document=>{const copy=structuredClone(document);for(const key of ['title','title_locked','changes','changed_ids','version','updated_at'])delete copy[key];return copy;};
const digest=value=>createHash('sha256').update(value).digest('hex');
async function saveJson(file,value){
 const temporary=file+'.display-'+process.pid+'.tmp',handle=await fs.open(temporary,'wx');
 try{await handle.writeFile(JSON.stringify(value,null,2)+'\n');await handle.sync();}finally{await handle.close();}
 await fs.rename(temporary,file);
}
async function get(id){return page.evaluate(async id=>{const response=await fetch('/api/projects/'+id);if(!response.ok)throw Error('Read failed '+response.status);return response.json();},id);}
try{
 for(const suite of suites){
  const folder=path.join(root,'.runtime/phase4',suite),indexPath=path.join(folder,'index.json');
  const index=JSON.parse(await fs.readFile(indexPath,'utf8'));
  assert.ok(index.cases.length&&index.cases.every(c=>c.status==='approved'),'Finish every case before changing display names.');
  const rounds=index.cases.map(c=>c.round),first=Math.min(...rounds),last=Math.max(...rounds),cases=[...new Set(index.cases.map(c=>c.case_id))];
  for(let round=first;round<=last;round++)for(const id of cases)assert.ok(index.cases.some(c=>c.case_id===id&&c.round===round),'Export must not create an untested case.');
  const backup=path.join(root,'.runtime/phase3/rename-history',suite);await fs.mkdir(backup,{recursive:true});
  for(const entry of index.cases){
   await page.goto(index.base+'/?project='+entry.project_id,{waitUntil:'domcontentloaded'});
   await page.waitForSelector('.project-title-button');await page.waitForFunction(()=>!document.querySelector('.loading-state'));
   const before=await get(entry.project_id);
   assert.equal(before.review.status,'approved');assert.ok(!before.analysis_pending&&!before.input_queue.length&&!before.domain.active_jobs.length);
   assert.ok(!['running','queued'].includes(before.latest_run?.status));
   const title=`${entry.case_id} · R${entry.round} · ${names[entry.case_id]}`.slice(0,100);
   const saved=[];
   for(const suffix of ['.json.gz','.json','-handoff.zip','-transcript.md','-oracle.json']){
    const source=path.join(folder,entry.key+suffix),target=path.join(backup,entry.key+suffix);
    try{const raw=await fs.readFile(source);try{await fs.writeFile(target,raw,{flag:'wx'});}catch(e){if(e.code!=='EEXIST')throw e;}saved.push({file:path.relative(root,target),sha256:digest(await fs.readFile(target))});}catch(e){if(e.code!=='ENOENT')throw e;}
   }
   const entryBackup=path.join(backup,entry.key+'-entry.json');try{await fs.writeFile(entryBackup,JSON.stringify(entry,null,2)+'\n',{flag:'wx'});}catch(e){if(e.code!=='EEXIST')throw e;}
   if(before.title!==title){
    await page.locator('.project-title-button').click();await page.waitForSelector('.dialog input');
    await page.$eval('.dialog input',(element,value)=>{Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(element,value);element.dispatchEvent(new Event('input',{bubbles:true}));},title);
    await page.locator('.dialog-actions .primary').click();
    await page.waitForFunction(async({id,title})=>(await fetch('/api/projects/'+id).then(r=>r.json())).title===title,{timeout:30000},{id:entry.project_id,title});
   }
   const after=await get(entry.project_id);
   assert.deepEqual(semantics(after.document),semantics(before.document));
   assert.equal(after.domain_revision,before.domain_revision);assert.equal(after.review.status,'approved');assert.deepEqual(after.messages,before.messages);
   entry.initial_title??=entry.title;entry.title=title;
   entry.actions.push({kind:'ui_display_name_review',old_title:before.title,title,at:new Date().toISOString()});
   records.push({suite,key:entry.key,project_id:entry.project_id,old_title:before.title,title,version_before:before.document.version,version_after:after.document.version,semantic_content_unchanged:true,backups:saved});
   await saveJson(indexPath,index);
  }
  // Existing real-user driver uses the approved UI export dialog. All requested
  // entries already exist, so this makes no model request or approval decision.
  // Keep this CDP connection responsive: a synchronous child can deadlock
  // target attachment when both clients share the same browser.
  const exported=await new Promise((resolve,reject)=>{
   const child=spawn('node',[path.join(root,'tests/phase4_real.mjs'),'--base',index.base,'--suite',suite,'--cases',cases.join(','),'--round-start',String(first),'--rounds',String(last),'--mode','export','--screenshots','0'],{cwd:root,stdio:['ignore','pipe','pipe']});
   child.stdout.on('data',chunk=>process.stdout.write(chunk));child.stderr.on('data',chunk=>process.stderr.write(chunk));
   child.once('error',reject);child.once('close',(code,signal)=>resolve({code,signal}));
  });
  assert.equal(exported.code,0,'UI re-export failed: '+exported.signal);
 }
 assert.equal(errors.length,0,errors.join('; '));
 const out=path.join(root,'.runtime/phase3/case-display-names.json');
 let previous=[];try{previous=JSON.parse(await fs.readFile(out,'utf8')).records;}catch{}
 const merged=new Map(previous.map(r=>[r.project_id,r]));for(const record of records)merged.set(record.project_id,record);
 await saveJson(out,{ok:true,ui_only:true,model_calls:0,errors,records:[...merged.values()]});
 console.log('Renamed and re-exported '+records.length+' retained UI projects; requirement facts unchanged.');
}finally{await context.close();browser.disconnect();}
