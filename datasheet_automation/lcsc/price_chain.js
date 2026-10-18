'use strict';
// Fetch a product's full RMB price ladder from LCSC's signed endpoints, sized
// for a PRODUCTION crawler that runs repeatedly -- not a one-shot probe.
//
// The costly, WAF-guarded step is POST /cid/verify. The frontend calls it ONCE,
// caches the returned cvt in localStorage, and reuses it across every product's
// GET /product/stock/price/sign; it only re-verifies when the cvt is rejected
// and it backs off on RATE_LIMIT. We mirror that with a persistent cvt cache
// file: with a valid cached cvt we skip cid/verify entirely and do only the
// lenient GET, so POST volume stays near zero and the IP stays clean.
//
// Signing wasm+glue are fetched at runtime (the version stamp rotates); the
// acw_sc__v2 challenge is solved by the platform's own challenge.js.
//
// Usage : node price_chain.js <productId>
// Output: stdout JSON -- {ok:true, payload:<decrypted price object>}
//         | {ok:false, blocked:true, reason}  (cid/verify WAF-blocked)
//         | {ok:false, reason}                (any other failure)
// Exit code is 0 unless the process crashes, so a block is a parseable result.
const http2 = require('http2');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { solve: solveAcwChallenge } = require('./challenge.js');

const AUTH = 'https://item.szlcsc.com';
const GLUE_URL =
  'https://static.szlcsc.com/ecp/assets/newWeb/common/pkg/rust_wasm_simple.min.js';
const UA =
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36';

// cvt is global per environment (the frontend keys it by env, not by product),
// so one cached token serves every product until the server rejects it.
const WORKLIB_ROOT = path.resolve(__dirname, '..', '..', '..');
const TEMP_ROOT = path.resolve(WORKLIB_ROOT, '07_TEMP');
const DATASHEET_TEMP_ROOT = path.resolve(TEMP_ROOT, 'datasheet_automation');
const configuredCvtCache = process.env.PCBAGENT_CVT_CACHE ||
  path.resolve(DATASHEET_TEMP_ROOT, 'cache', 'lcsc-cvt.json');
const CVT_CACHE = path.resolve(configuredCvtCache);
if (CVT_CACHE !== DATASHEET_TEMP_ROOT &&
    !CVT_CACHE.startsWith(DATASHEET_TEMP_ROOT + path.sep)) {
  throw new Error(
    'PCBAGENT_CVT_CACHE must stay inside Worklib/07_TEMP/datasheet_automation'
  );
}
const CVT_TTL_MS = Number(process.env.PCBAGENT_CVT_TTL_MS || 30 * 60 * 1000);
const BACKOFF_MS = Number(process.env.PCBAGENT_PRICE_BACKOFF_MS || 30000);
const CODE = { NONCE_EXPIRE: 100103, NO_CID: 110101, RATE_LIMIT: 110102, NO_CVT: 110201, BAD_CVT: 110202 };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function httpsGet(url, binary) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers: { 'user-agent': UA } }, (res) => {
      if (res.statusCode !== 200) { res.resume(); reject(new Error(`GET ${url} -> ${res.statusCode}`)); return; }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve(binary ? Buffer.concat(chunks) : Buffer.concat(chunks).toString('utf8')));
    }).on('error', reject);
  });
}

// -- persistent cvt cache (mirrors the frontend's localStorage reuse) ---------
function readCvt() {
  try {
    const c = JSON.parse(fs.readFileSync(CVT_CACHE, 'utf8'));
    if (c && c.cvt && Date.now() - (c.ts || 0) < CVT_TTL_MS) return c.cvt;
  } catch { /* no/invalid cache */ }
  return null;
}
function writeCvt(cvt) {
  try {
    fs.mkdirSync(path.dirname(CVT_CACHE), { recursive: true });
    fs.writeFileSync(CVT_CACHE, JSON.stringify({ cvt, ts: Date.now() }));
  } catch { /* cache is best-effort */ }
}
function clearCvt() { try { fs.unlinkSync(CVT_CACHE); } catch { /* already gone */ } }

// -- signer: LCSC's own wasm loaded at runtime --------------------------------
async function loadSigner() {
  const glueSrc = await httpsGet(GLUE_URL, false);
  const stamp = glueSrc.match(/_bg_\d+\.wasm/);
  if (!stamp) throw new Error('cannot find wasm stamp in glue');
  const wasmUrl = GLUE_URL.replace(/\.min\.js$/, '').replace(/[^/]*$/, '') + 'rust_wasm_simple' + stamp[0];
  const wasmBytes = await httpsGet(wasmUrl, true);
  const _log = console.log;
  console.log = () => {};
  (0, eval)(glueSrc);
  const wb = globalThis.wasm_bindgen;
  wb.initSync({ module: new WebAssembly.Module(wasmBytes) });
  console.log = _log;
  return {
    genCid: () => wb.generate_cid(),
    nonce: () => wb.generate_nonce(),
    signOver: (obj) => wb.generate_sign(Object.entries(obj)),
    decrypt: (s) => wb.decrypt_data(s),
  };
}

// -- h2 session with a cookie jar ---------------------------------------------
function makeSession() {
  const session = http2.connect(AUTH);
  session.on('error', () => {});
  const jar = {};
  const cookie = () => Object.entries(jar).map(([k, v]) => `${k}=${v}`).join('; ');
  function absorb(sc) {
    if (!sc) return;
    for (const c of [].concat(sc)) {
      const kv = c.split(';')[0], eq = kv.indexOf('=');
      if (eq > 0) jar[kv.slice(0, eq).trim()] = kv.slice(eq + 1).trim();
    }
  }
  function req(method, p, extra, body) {
    return new Promise((resolve, reject) => {
      const headers = { ':method': method, ':path': p, 'user-agent': UA, ...extra };
      const ck = cookie(); if (ck) headers['cookie'] = ck;
      if (body != null) headers['content-length'] = Buffer.byteLength(body);
      const r = session.request(headers);
      let status = null, data = '';
      r.on('response', (h) => { status = h[':status']; absorb(h['set-cookie']); });
      r.setEncoding('utf8');
      r.on('data', (c) => { data += c; });
      r.on('end', () => resolve({ status, body: data }));
      r.on('error', reject);
      if (body != null) r.write(body);
      r.end();
    });
  }
  return { jar, req, close: () => session.close() };
}

function solveAcw(html, url) {
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  const rd = html.match(/<textarea[^>]*id="renderData"[^>]*>([\s\S]*?)<\/textarea>/);
  return solveAcwChallenge({ scripts, renderData: rd ? rd[1] : '', url, userAgent: UA });
}

async function ensureDetail(s, path, url) {
  // GET detail, solving the acw challenge until __NEXT_DATA__ (max 3 rounds).
  let r = await s.req('GET', path, {
    accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'accept-language': 'zh-CN,zh;q=0.9',
  });
  for (let i = 0; i < 3 && !r.body.includes('__NEXT_DATA__'); i++) {
    if (!r.body.slice(0, 1200).includes('arg1=')) break;
    Object.assign(s.jar, solveAcw(r.body, url));
    r = await s.req('GET', path, { accept: 'text/html,*/*;q=0.8' });
  }
}

// POST /cid/verify -> cvt. The only WAF-guarded call; done once then cached.
async function acquireCvt(s, signer, url) {
  const cid = signer.genCid();
  const q = `cid=${cid}&nonce=${signer.nonce()}&sign=${encodeURIComponent(signer.signOver({ cid }))}`;
  const rv = await s.req('POST', `/cid/verify?${q}`, {
    accept: 'application/json, text/plain, */*', 'content-type': 'application/json',
    origin: AUTH, referer: url, 'x-requested-with': 'XMLHttpRequest',
    'sec-fetch-site': 'same-origin', 'sec-fetch-mode': 'cors', 'sec-fetch-dest': 'empty',
  }, '{}');
  let body; try { body = JSON.parse(rv.body); } catch { body = null; }
  if (body && body.code === 200 && body.result) return { cvt: body.result };
  return { blocked: rv.status === 403, reason: `cid/verify ${rv.status}` };
}

// GET /product/stock/price/sign -> {code, payload?}. Lenient endpoint (GET).
async function fetchPrice(s, signer, productId, cvt, url) {
  const nonce = signer.nonce();
  const sign = signer.signOver({ productId, cvt });
  const q = `productId=${productId}&cvt=${encodeURIComponent(cvt)}&nonce=${nonce}&sign=${encodeURIComponent(sign)}`;
  const rp = await s.req('GET', `/product/stock/price/sign?${q}`, {
    accept: 'application/json, text/plain, */*', referer: url, 'x-requested-with': 'XMLHttpRequest',
  });
  let body; try { body = JSON.parse(rp.body); } catch { body = null; }
  if (!body) return { code: -1, reason: `price/sign ${rp.status}` };
  if (body.code !== 200) return { code: body.code, waitTime: body.result };
  let show = (body.result || {}).productShowResultSignStr || '';
  if (typeof show === 'string' && show.startsWith('lcsc_encrypt_v1:')) show = signer.decrypt(show);
  return { code: 200, payload: typeof show === 'string' ? JSON.parse(show) : show };
}

async function run(productId) {
  const signer = await loadSigner();
  const s = makeSession();
  const path = `/${productId}.html`, url = AUTH + path;
  try {
    await ensureDetail(s, path, url);
    let cvt = readCvt();

    // Up to 2 rounds: reuse cached cvt; on cvt-reject re-verify once; on
    // rate-limit back off once; on WAF-block on cid/verify, stop (return block).
    for (let attempt = 0; attempt < 2; attempt++) {
      if (!cvt) {
        const got = await acquireCvt(s, signer, url);
        if (!got.cvt) return { ok: false, blocked: !!got.blocked, reason: got.reason };
        cvt = got.cvt; writeCvt(cvt);
      }
      const pr = await fetchPrice(s, signer, productId, cvt, url);
      if (pr.code === 200) return { ok: true, payload: pr.payload };
      if (pr.code === CODE.NO_CVT || pr.code === CODE.BAD_CVT || pr.code === CODE.NONCE_EXPIRE) {
        clearCvt(); cvt = null; continue;              // stale cvt -> re-verify once
      }
      if (pr.code === CODE.RATE_LIMIT) {
        await sleep(Number(pr.waitTime) || BACKOFF_MS); continue;  // back off, retry once
      }
      return { ok: false, reason: `price/sign code ${pr.code}` };
    }
    return { ok: false, reason: 'price/sign not resolved after retry' };
  } finally {
    s.close();
  }
}

const productId = process.argv[2];
if (!productId) { process.stderr.write('usage: node price_chain.js <productId>\n'); process.exit(2); }
run(productId)
  .then((out) => { process.stdout.write(JSON.stringify(out)); process.exit(0); })
  .catch((err) => { process.stdout.write(JSON.stringify({ ok: false, reason: String((err && err.message) || err) })); process.exit(0); });
