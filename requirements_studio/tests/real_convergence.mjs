import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const root=process.cwd(),out=path.join(root,'.runtime/verification/ui-audit');
const require=createRequire(path.join(root,'frontend/package.json'));
const profiles=JSON.parse(await fs.readFile(path.join(out,'real-profiles.json'),'utf8'));
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',protocolTimeout:1400000,headless:true,pipe:true,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},userDataDir:path.join(out,'real-convergence-profile'),args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage','--disable-background-networking']});
const resumeProject=process.argv.find(arg=>arg.startsWith('--resume-project='))?.split('=')[1];
const resumeRound=Number(process.argv.find(arg=>arg.startsWith('--resume-round='))?.split('=')[1]??1);
const reports=resumeProject&&resumeRound>1?JSON.parse(await fs.readFile(path.join(out,'real-convergence.json'),'utf8')).rounds.filter(r=>r.round<resumeRound):[];let page,round,checks,errors;
const check=(name,value)=>{assert.ok(value,name);checks.push(name);console.log(`PASS REAL R${round} ${name}`);};
const project=()=>page.evaluate(async()=>fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json()));
async function send(text){await page.focus('.composer textarea');await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.press('Backspace');await page.keyboard.sendCharacter(text);assert.equal(await page.$eval('.composer textarea',el=>el.value),text,'pasted text must match the complete user input');await page.locator('[aria-label="发送需求"], [aria-label="加入待处理补充"]').click();await page.waitForSelector('[aria-label="停止分析"]',{timeout:30000});}
async function settle(){await page.waitForFunction(async()=>{const p=await fetch('/api/projects/'+localStorage.getItem('requirements-studio:project')).then(r=>r.json());return !['queued','running'].includes(p.latest_run?.status)&&!p.input_queue.length;},{timeout:1250000,polling:2000});await page.waitForFunction(()=>!document.querySelector('[aria-label="停止分析"]'),{timeout:30000});const p=await project();assert.equal(p.latest_run.status,'completed',p.latest_run.error);console.log(`REAL R${round} v${p.document.version}, coverage ${p.review.closed}/50, ${p.document.questions.length} questions, ${p.review.unsettled_requirements.length} unsettled`,JSON.stringify(p.document.questions.map(q=>q.question)));return p;}

try{
for(const profile of profiles){
  if(resumeProject&&profile.round<resumeRound)continue;
  round=profile.round;checks=[];errors=[];page=await browser.newPage();await page.setViewport({width:1440,height:1000});
  page.on('pageerror',e=>errors.push(e.message));page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  if(resumeProject&&round===resumeRound)await page.evaluateOnNewDocument(id=>localStorage.setItem('requirements-studio:project',id),resumeProject);
  await page.goto('http://[::1]:8880/',{waitUntil:'domcontentloaded'});await page.waitForFunction(()=>!!document.querySelector('.composer textarea')&&!document.querySelector('.loading-state'));
  if(await page.$eval('.sidebar',el=>el.getBoundingClientRect().width<50))await page.locator('.toggle-sidebar').click();
  let complete;
  if(resumeProject&&round===resumeRound){
    const previous=JSON.parse(await fs.readFile(path.join(out,'real-convergence-failure.json'),'utf8'));
    checks=previous.checks;
    complete=await settle();
  }else{
  await page.locator('.new-project').click();await page.waitForSelector('.chat-empty');
  await send(`我想做一个${round===1?'办公室桌面':'工位'}温度显示器，用USB-C 5V供电，只显示当前温度，预算${round===1?50:65}元。精度和测量范围还没想好，请逐步帮我完成整份需求说明书。`);
  const first=await settle();check('incomplete idea produces real questions and blocks approval',first.document.questions.length>0&&first.review.status==='draft');
  const originalQuestions=first.document.questions.map(q=>q.id);
  const question=first.document.questions[0];
  const answer=/预算|成本/.test(question.question)?'预算指单台BOM成本，按100台估算。':/范围/.test(question.question)&&/精度|准确|误差/.test(question.question)?'测温范围0～50°C，精度±0.5°C。':/精度|准确|误差/.test(question.question)?'测温精度需要±0.5°C。':/范围/.test(question.question)?'测温范围需要0～50°C。':/显示|交互/.test(question.question)?'只显示当前温度和°C单位，每秒刷新，超量程或故障显示Err。':'产品名称为办公室桌面温度显示器，用于办公室日常监测。';
  await page.locator('.question-card .custom-answer').fill(answer);await page.locator('.question-footer .primary').click();await page.waitForSelector('[aria-label="停止分析"]');
  await send('补充另一分支：USB-C仅供5V电源，不需要USB数据通信。先保留尚未回答的问题。');
  check('user can queue a different branch during a real model call',(await project()).input_queue.length>0);
  const partial=await settle();
  const answeredTopics=/预算|成本/.test(question.question)?['预算','成本']:/范围/.test(question.question)&&/精度|准确|误差/.test(question.question)?['范围','精度','准确','误差']:/精度|准确|误差/.test(question.question)?['精度','准确','误差']:/范围/.test(question.question)?['范围']:/显示|交互/.test(question.question)?['显示','单位','刷新','异常']:['名称','场景'];
  const unanswered=first.document.questions.slice(1).filter(q=>!answeredTopics.some(topic=>q.question.includes(topic)));
  check('partial answer preserves other unanswered branches',unanswered.length>0&&unanswered.every(q=>partial.document.questions.some(current=>current.id===q.id)));
  check('history and requirement IDs persist across turns',first.document.requirements.every(old=>partial.document.requirements.some(r=>r.id===old.id)));
  await send(profile.complete_spec);complete=await settle();
  }
  for(let attempt=0;attempt<3&&complete.review.status!=='ready';attempt++){
    if(complete.document.questions.length&&!complete.document.questions_paused){
      if(await page.$('.question-expand'))await page.locator('.question-expand').click();
      for(const q of complete.document.questions){
        const answer=/720|72小时|老化/.test(q.question)?'720小时连续无死机是性能设计目标，72小时是本版实际老化验收时长，本版老化不延长到720小时。':/组合|温点/.test(q.question)?'4.75V和5.25V分别与0、25、50°C三个温点组合，合计6种组合均进行功能验收。':/反接/.test(q.question)?'USB-C输入保护需求包含反接保护，具体实现由设计阶段确定，后续安排验证，不声明现在已经测试。':/可读|照度|光线/.test(q.question)?'按办公室正常室内照明，在显示正前方0.5米处人工目视判定，温度数值与°C单位必须清晰可辨。':/触发|注入|故障/.test(q.question)?'由测试固件注入传感器通信失败及低于0°C/高于50°C的越界值，2秒内显示Err；解除注入后下一次正常采样恢复温度显示。':/预算|成本/.test(q.question)?`单台BOM不超过${round===1?50:65}元，按100台估算。`:/精度|误差/.test(q.question)?'0～50°C全范围绝对误差±0.5°C；内部实现细节由设计阶段决定，仍需满足本指标。':/范围/.test(q.question)?'测温范围0～50°C，环境与存储范围按完整规格，不增加其他测量范围。':/显示|小数|摄氏/.test(q.question)?'只显示摄氏温度和°C单位，一位小数、0.1°C分辨率，每秒刷新；其余显示实现细节明确交由设计阶段决定。':'这项内部实现细节明确交由设计阶段确定，必须满足此前完整规格中功能、性能、成本和验收边界；不追加新的用户指标。';
        await page.locator(`[data-question-id="${q.id}"] .custom-answer`).fill(answer);
      }
      await page.locator('.question-footer .primary').click();await page.waitForSelector('[aria-label="停止分析"]');complete=await settle();
    }else{
      const gaps=complete.review.gaps.map(g=>g.section+' '+g.title).join('、');
      await send(`继续核对${gaps}。未列出的内部实现细节由后续设计确定，必须满足完整规格的功能、性能、成本与验收约束；如果有真正遗漏的用户指标请提问，不要伪造完成。`);complete=await settle();
    }
  }
  check('all 50 sections converge through real conversation',complete.review.closed===50&&complete.review.gaps.length===0);
  check('no pending question or unsettled requirement is hidden',complete.document.questions.length===0&&complete.review.unsettled_requirements.length===0&&complete.review.status==='ready');
  const users=Object.fromEntries(complete.messages.filter(m=>m.role==='user').map(m=>[m.id,m.content]));
  check('all closed coverage has literal user evidence',complete.document.coverage.every(c=>c.source_quote&&users[c.source_message_id]?.includes(c.source_quote)));
  const exported=await page.evaluate(async id=>fetch(`/api/projects/${id}/export/json`).then(r=>r.json()),complete.id);
  check('export matches the converged document',JSON.stringify(exported.document)===JSON.stringify(complete.document));
  await page.locator('.approve-document').click();await page.waitForFunction(()=>document.querySelector('.draft-badge')?.textContent==='已审核');
  const approved=await project();check('only the user review action marks the document approved',approved.review.status==='approved'&&approved.document.version===complete.document.version+1);
  await page.screenshot({path:path.join(out,`real-converged-round-${round}.png`)});
  await send(`修订：单台BOM预算改为${round===1?60:75}元，其他需求与适用范围保持，先暂停提问。`);const revised=await settle();
  check('changing an old answer invalidates the previous approval',revised.review.status!=='approved'&&revised.document.requirements.some(r=>r.state==='stated'&&r.value.includes(round===1?'60':'75')));
  check('real browser session has no runtime errors',errors.length===0);
  reports.push({round,provider:'real_codex_cli',model:'gpt-6-luna',effort:'xhigh',project_id:complete.id,checks,console_errors:errors,converged_version:complete.document.version,approved_version:approved.document.version,requirements:complete.document.requirements.length,coverage_closed:complete.review.closed,conversation_user_turns:complete.messages.filter(m=>m.role==='user').length,converged_document:complete.document,approved_document:approved.document,revised_document:revised.document});
  await fs.writeFile(path.join(out,'real-convergence.json'),JSON.stringify({ok:true,rounds:reports},null,2));await page.close();
}
}catch(error){await page?.screenshot({path:path.join(out,`real-convergence-failure-r${round}.png`)}).catch(()=>{});await fs.writeFile(path.join(out,'real-convergence-failure.json'),JSON.stringify({round,error:String(error),checks,console_errors:errors},null,2));throw error;}
finally{await browser.close();}
console.log(JSON.stringify({ok:true,rounds:reports.map(r=>({round:r.round,checks:r.checks.length,coverage:r.coverage_closed,user_turns:r.conversation_user_turns}))}));
