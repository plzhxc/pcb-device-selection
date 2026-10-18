// Node 16 的 crypto 模块没有 getRandomValues，Vite 6 会直接调用它
const crypto = require('crypto');
if (typeof crypto.getRandomValues !== 'function') {
  crypto.getRandomValues = (arr) => crypto.randomFillSync(arr);
}
if (!globalThis.crypto) globalThis.crypto = crypto;
