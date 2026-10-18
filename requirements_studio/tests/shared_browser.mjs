// One temporary Chromium process, isolated incognito contexts in each worker.
// GPFS preallocates several MiB for each browser SQLite file, so avoid N profiles.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
const root=process.cwd(),out=path.join(root,'.runtime/phase3');await fs.mkdir(out,{recursive:true});
const require=createRequire(path.join(root,'frontend/package.json'));
const profile=path.join(out,'shared-browser-'+process.pid);
const browser=await require('puppeteer-core').launch({executablePath:'/usr/bin/chromium-browser',headless:true,userDataDir:profile,
 env:{...process.env,LANG:'zh_CN.utf8',LC_ALL:'zh_CN.utf8'},args:['--no-sandbox','--no-proxy-server','--disable-dev-shm-usage',
 '--incognito','--disable-gpu','--disable-gpu-shader-disk-cache','--disk-cache-size=1','--media-cache-size=1','--disable-component-update']});
const state=path.join(out,'browser-bridge.json');
await fs.writeFile(state,JSON.stringify({pid:process.pid,browser_pid:browser.process().pid,ws:browser.wsEndpoint(),profile,created_at:new Date().toISOString()},null,2)+'\n');
console.log('Shared test browser ready; contexts remain isolated.');
let ending=false;
async function close(){if(ending)return;ending=true;await browser.close();await fs.rm(profile,{recursive:true,force:true});await fs.rm(state,{force:true});process.exit(0);}
process.on('SIGTERM',()=>void close());process.on('SIGINT',()=>void close());
browser.on('disconnected',()=>void close());
await new Promise(()=>{});
