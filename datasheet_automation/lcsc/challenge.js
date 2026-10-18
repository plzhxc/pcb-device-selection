'use strict';
// Solve LCSC's JS cookie challenges by running the page's own challenge
// scripts inside a minimal browser sandbox. Both the item-detail Aliyun WAF
// (acw_sc__v2) and the search-host RC4 challenge (tws2_<n>) are "the page
// computes a cookie and reloads" -- so we execute their scripts rather than
// reimplementing either algorithm, and return every cookie they set.
//
// Input  (stdin, JSON): {scripts:[...], renderData?, url?, userAgent?}
//   (legacy {s1, s2} also accepted)
// Output (stdout, JSON): {cookieName: value, ...} -- empty object if none.
const vm = require('vm');

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (chunk) => { data += chunk; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

function solve(payload) {
  const href = payload.url || 'https://item.szlcsc.com/';
  const ua = payload.userAgent ||
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36';
  const store = {};
  const doc = {
    getElementById(id) {
      return id === 'renderData' ? { innerHTML: payload.renderData || '' } : null;
    },
    referrer: '',
    get cookie() {
      return Object.entries(store).map(([k, v]) => k + '=' + v).join('; ');
    },
    set cookie(raw) {
      const kv = String(raw).split(';')[0];
      const eq = kv.indexOf('=');
      if (eq < 0) return;
      const k = kv.slice(0, eq).trim();
      const v = kv.slice(eq + 1).trim();
      if (k) store[k] = v;
    },
  };
  const loc = { href, reload() {}, replace() {}, assign() {} };
  const ctx = {
    document: doc,
    location: loc,
    navigator: { userAgent: ua },
    setTimeout() {}, setInterval() {}, clearTimeout() {}, clearInterval() {},
    btoa: (s) => Buffer.from(String(s), 'binary').toString('base64'),
    atob: (s) => Buffer.from(String(s), 'base64').toString('binary'),
  };
  ctx.window = ctx;
  ctx.window.location = loc;
  vm.createContext(ctx);
  const scripts = Array.isArray(payload.scripts)
    ? payload.scripts
    : [payload.s1, payload.s2].filter(Boolean);
  // Per-script timeout caps runaway anti-debug loops; cookies set before the
  // loop are already captured, so a timeout never loses a solved cookie.
  for (const src of scripts) {
    try { vm.runInContext(src, ctx, { timeout: 8000 }); } catch (e) { /* keep going */ }
  }
  return store;
}

// Dual mode: importable as a module (price_chain.js reuses solve() to keep one
// acw solver), and runnable as a CLI (the Python NodeSolver spawns this file and
// pipes the payload on stdin -- that contract is unchanged).
module.exports = { solve };

if (require.main === module) {
  readStdin().then((raw) => {
    process.stdout.write(JSON.stringify(solve(JSON.parse(raw))));
    process.exit(0);
  }).catch((err) => {
    process.stderr.write('acw_solver error: ' + ((err && err.message) || err) + '\n');
    process.exit(4);
  });
}
