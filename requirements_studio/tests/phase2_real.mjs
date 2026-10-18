// Two independently seeded product-owner conversations using the installed Codex CLI.
import {createRequire} from 'node:module';
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';

const root=process.cwd(),out=path.join(root,'.runtime/phase2/verification');await fs.mkdir(out,{recursive:true});
const require=createRequire(path.join(root,'frontend/package.json'));
const profileDir=path.join(out,'real-browser-profile');
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',pipe:true,headless:true,protocolTimeout:1400000,env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},userDataDir:profileDir,args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage']});
const resume=process.argv.includes('--resume');
const rounds=resume?await fs.readFile(path.join(out,'real.json'),'utf8').then(s=>JSON.parse(s).rounds).catch(()=>[]):[];let page,projectId,currentRound;
const getProject=()=>page.evaluate(async id=>(await fetch('/api/projects/'+id)).json(),projectId);
const save=()=>fs.writeFile(path.join(out,'real.json'),JSON.stringify({ok:rounds.length===2,rounds},null,2));
async function send(text){await page.focus('[aria-label="输入硬件需求"]');await page.keyboard.down('Control');await page.keyboard.press('KeyA');await page.keyboard.up('Control');await page.keyboard.press('Backspace');await page.keyboard.sendCharacter(text);assert.equal(await page.$eval('[aria-label="输入硬件需求"]',e=>e.value),text);await page.locator('.composer-buttons .send-button:not(.stop)').click();await page.waitForFunction(()=>document.querySelector('[aria-label="输入硬件需求"]').value==='',{timeout:30000});}
async function idle(){const start=Date.now();while(Date.now()-start<1250000){const p=await getProject();if(['failed','cancelled','interrupted'].includes(p.latest_run?.status))throw new Error(p.latest_run.error);if(!['running','queued'].includes(p.latest_run?.status)&&!p.input_queue.length)return p;await new Promise(r=>setTimeout(r,1500));}throw Error('real conversation timed out');}
function specification(round,seed){const temp=round===1,name=temp?'室内桌面温度显示器':'工作台照度显示器',sensor=temp?'温度传感器模块':'光电传感器模块',unit=temp?'°C':'lx',range=temp?'0～50°C':'0～10000lx',accuracy=temp?'±0.5°C':'读数的±5%',budget=temp?58+seed%7:68+seed%9;return {name,sensor,budget,text:`这是一份虚构的验收测试产品规格，我是产品负责人。产品名称“${name}”，类别是室内消费电子原型；用户为办公室工作人员，放在室内桌面，唯一用途是采集并显示当前${temp?'环境温度':'工作面照度'}，用于日常查看，不作医疗或安全控制。核心功能是传感采集和本地显示，都是Must-have；没有Should-have、Could-have或未来扩展要求。
测量范围${range}，核心精度${accuracy}，显示分辨率${temp?'0.1°C':'1lx'}，每1秒采样和刷新一次。正常上电、断电后重启及复位都应在2秒内显示；测量超范围或传感故障时2秒内显示Err，故障消除后下一次正常采样恢复；不保留历史记录。显示至少包含当前数值及${unit}单位。室内100～1000lx、观看距离50cm、水平及垂直±30°应可辨认，主要字符高度至少6mm。验收使用5个均匀分布的量程点，与已校准参考仪器比较，25±2°C下满足上述精度；异常由测试固件注入失败/越界值，观察2秒内Err及正常恢复。参考仪器选择、校准溯源与具体测试程序由后续测试设计确定，不宣称已实测。
唯一供电为外部USB-C 5V输入，输入允许4.75～5.25V，总工作和峰值电流都不超过200mA，总功耗不超过1W。USB不传数据、不做PD协商。不需要电池、充电、备用电源、多电源切换、低功耗待机、断电记录或历史存储，这些能力及下级参数均不适用。内部3.3V逻辑电源是设计目标；供电效率、纹波、启动内部时序、关断漏电流在满足整机目标前提下由设计阶段定，不要求需求阶段填写芯片能力值。
外形不超过90×60×30mm，重量不超过120g，单台BOM预算不超过${budget}元，按100台估算。工作0～50°C、10～90%RH无凝露，存储-20～60°C、10～95%RH无凝露，室内IP20，外壳温升不超过10°C。无户外、防水、跌落、盐雾、振动、RF发射或气密要求；相应专项不适用。目标设计寿命3年、连续运行720小时无死机；不单设MTBF数值，评估方法由设计阶段确定。无射频、无线、有线业务通信、音频、摄像头、触控、按键、指示灯、执行器或电机，相关模块及所有下级性能参数均不适用。
模块方案按专业规范的功能类别选：传感器与采集类→${sensor}，人机交互类→LCD与OLED显示模块，其他模块→主控与处理模块，电源类→电源转换模块。我确认这四个父类别和功能边界，各为1个物理电路实例；主控可承接多个产品功能，不重复计算物料。具体传感器原理/子类、显示技术、主控类型、调压拓扑及器件与必要外围，全部明确委托设计阶段确定，须满足上述指标和预算，不要在需求阶段强行选某子类或芯片。模块间有采集数据、显示控制和供电关系，实际协议、信号角色细分、引脚与线网由设计阶段确定；只有抽象角色，不需要真实脚号。
制造是100台内部样机，不做产品认证宣称；USB口ESD目标接触±4kV，具体方案和测量程序由设计阶段制定。输入过压、过流、短路、反接及EMI的目标边界在符合USB输入及室内样机用途前提下由设计阶段形成方案，并由后续验证，不把需求文本当作硬件PASS。保留内部SWD烧录与调试、3.3V/GND测试点，真实焊盘、引脚、烧录接口速率由设计阶段确定。PCB材料、层数、铜厚、走线、封装/焊接、可制造/可测试策略在既定尺寸成本内由设计阶段确定；无明确交付日期约束。
这里我明确授权：V3第1～5章其余未逐一给出具体数值的实现参数、器件能力、端口细节、测试方法和可制造性细节，均由设计阶段在上述产品要求和范围内确定，逐字段记录交接边界，不能写成已实现或已验证。产品名称、用途、用户场景、核心功能及以上已定指标则现在确认，不交给后续决定。请沿用先前输入和回答，自动核对已知内容，只有会改变这些产品决定的关键缺口才提问。`};}
try{for(let round=1;round<=2;round++){
  if(rounds.some(r=>r.round===round))continue;
  currentRound=round;const seed=20261015+round,product=specification(round,seed),checks=[],errors=[];
  if(round===2)product.text+='\n照度产品用于桌面阅读和一般办公；固定测量工作台的一个主要作业点，传感器采光面与工作面同向，显示该位置的入射照度。不代表整张桌面的平均值，不要求移动巡测、照度是否达标提示或自动控制灯光。具体固定、开孔和安装结构由设计阶段满足这一测量边界。';
  const check=(name,value)=>{assert.ok(value,name);checks.push(name);console.log(`PASS REAL R${round} ${name}`);};
  const checkpoint=resume?await fs.readFile(path.join(out,`real-progress-${round}.json`),'utf8').then(s=>JSON.parse(s)).catch(()=>null):null;
  page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message));await page.setViewport({width:1440,height:1000});if(checkpoint)await page.evaluateOnNewDocument(id=>localStorage.setItem('requirements-studio:project',id),checkpoint.project_id);await page.goto('http://[::1]:8892/',{waitUntil:'domcontentloaded'});await page.waitForSelector('.new-project');
  let p,preserved;
  if(checkpoint){
    projectId=checkpoint.project_id;p=await getProject();preserved=p.document.requirements.map(r=>r.id);
    check('saved product-owner conversation survives a backend restart',p.messages.filter(m=>m.role==='user').length>=3);
    if(['failed','interrupted','cancelled'].includes(p.latest_run?.status)){
      const previous=p.latest_run.id;await page.waitForSelector('.run-notice .text-button');await page.locator('.run-notice .text-button').click();await page.waitForFunction(async({id,previous})=>(await fetch('/api/projects/'+id).then(r=>r.json())).latest_run.id!==previous,{timeout:20000},{id:projectId,previous});p=await idle();
    }else if(['running','queued'].includes(p.latest_run?.status))p=await idle();
    check('field ledger remains active after recovery',p.convergence.enabled&&p.convergence.fields_total===247);
  }else{
    await page.locator('.new-project').click();await page.waitForFunction(()=>!document.querySelector('[aria-label="输入硬件需求"]').disabled);
    projectId=await page.evaluate(()=>localStorage.getItem('requirements-studio:project'));await fs.writeFile(path.join(out,`real-progress-${round}.json`),JSON.stringify({round,seed,project_id:projectId,stage:'starting'},null,2));
    await send(`我想做一个${product.name}，先帮我梳理用途和需要确认的事情。`);
    await send(`先补充另一条：在室内桌面使用，采用外部USB-C 5V供电，预算按100台估算，不做联网和历史记录。`);
    p=await idle();check('partial queued dialogue preserves both user turns',p.messages.filter(m=>m.role==='user').length===2);check('vague idea remains a draft',p.review.status==='draft');check('field ledger is active',p.convergence.enabled&&p.convergence.fields_total===247);
    preserved=p.document.requirements.map(r=>r.id);
    await send(product.text);p=await idle();
  }
  for(let i=0;i<6&&p.review.status!=='ready';i++){
    const questions=p.document.questions.map(q=>`关于“${q.question}”：请采用我已给出的完整规格和明确委托边界；我再次确认本版不扩展这些范围。`).join('\n');
    const gaps=p.convergence.fields.filter(f=>!['confirmed','not_applicable','design_stage'].includes(f.state)).map(f=>f.label).slice(0,30).join('、');
    await send(`继续收敛本版。${round===1?"补充精度验收决定：温度精度±0.5°C在整个0～50°C测温范围内满足。5个验收点为0、12.5、25、37.5、50°C，使用温控箱让被测传感器与参考仪处于同一稳定环境后对比。前文25±2°C条件撤销，以这条为准；稳定判据和参考仪选择由后续测试设计确定。":"补充照度误差口径：精度为±(5%读数+5lx)，零点允许绝对误差5lx；量程0～10000lx，分辨率1lx。光照验收在25±2°C环境下改变照度，5个量程点0、2500、5000、7500、10000lx，使用已校准参考照度计比对；具体仪器与流程由后续测试设计确定。"} ${questions}\n剩余核对项：${gaps}。产品目标、用户、用途和Must-have要求全部按上一条规格确认；未明确的实现和验收细节我明确委托后续设计，要求满足已定指标与预算；被明确排除的模块及其下级参数不适用。显示、采集、主控、转换电源这四类模块的具体子类型与拓扑也委托设计，不额外增加无线、电池或其他扩展。请先复用已有事实和原话，完整核对字段、功能模块映射与抽象关系，不反问已提供的同一决定。`);
    p=await idle();console.log(`REAL R${round} step ${i+1}: fields ${p.convergence.fields_closed}/247; questions ${p.document.questions.length}; modules ${p.convergence.modules.length}; status ${p.review.status}`);
    await fs.writeFile(path.join(out,`real-progress-${round}.json`),JSON.stringify({round,seed,project_id:projectId,stage:'converging',version:p.document.version,fields:p.convergence.fields_closed,questions:p.document.questions},null,2));
  }
  check('entire field ledger is closed',p.convergence.fields_closed===247);check('all fifty sections converge',p.review.closed===50);check('no open questions or unsettled requirements',!p.document.questions.length&&!p.review.unsettled_requirements.length);check('every required hardware function is mapped',p.convergence.module_gaps.length===0&&p.convergence.module_pending.length===0);check('no supported consistency conflicts',p.convergence.issues.length===0);check('existing requirement identifiers survive',preserved.every(id=>p.document.requirements.some(r=>r.id===id)));
  const catalog=await page.evaluate(()=>fetch('/api/module-catalog').then(r=>r.json()));check('every selected module belongs to the authoritative functional taxonomy',p.convergence.modules.every(m=>catalog.types.some(t=>t.id===m.type_id&&t.name===m.type_name)));check('appropriate sensor category is represented',p.convergence.modules.some(m=>m.type_name===product.sensor));
  await page.locator('.canvas-tabs [role="tab"]:nth-child(4)').click();await page.waitForSelector('.convergence-page');await page.screenshot({path:path.join(out,`real-convergence-${round}.png`)});
  await page.locator('.canvas-tabs [role="tab"]:nth-child(5)').click();await page.waitForSelector('.module-instance');await page.screenshot({path:path.join(out,`real-modules-${round}.png`)});check('canvas shows the same module plan',await page.$$eval('.module-instance',els=>els.length)===p.convergence.modules.length);
  await page.locator('.canvas-tabs [role="tab"]:nth-child(1)').click();await page.locator('.approve-document').click();await page.waitForFunction(()=>document.querySelector('.draft-badge')?.textContent==='已审核',{timeout:20000});p=await getProject();check('approval requires the actual user button',p.review.status==='approved');
  await fs.writeFile(path.join(out,`real-approved-${round}.json`),JSON.stringify(p,null,2));await fs.writeFile(path.join(out,`real-approved-${round}.md`),p.markdown);
  check('no browser runtime errors',errors.length===0);
  rounds.push({round,seed,provider:'real_codex_cli',model:'gpt-6-luna',effort:'xhigh',project_id:projectId,user_turns:p.messages.filter(m=>m.role==='user').length,requirements:p.document.requirements.length,modules:p.convergence.modules.map(m=>({name:m.name,type:m.type_name,category:m.category_name})),fields_closed:p.convergence.fields_closed,approved_version:p.document.version,checks,console_errors:errors});await save();await page.close();
}}
catch(error){await page?.screenshot({path:path.join(out,`real-failure-${currentRound}.png`)}).catch(()=>{});await fs.writeFile(path.join(out,'real-failure.json'),JSON.stringify({round:currentRound,project_id:projectId,error:error.message},null,2));throw error;}
finally{await browser.close();await fs.rm(profileDir,{recursive:true,force:true});}
console.log(JSON.stringify({ok:true,rounds:rounds.map(r=>({round:r.round,checks:r.checks.length,user_turns:r.user_turns}))}));
