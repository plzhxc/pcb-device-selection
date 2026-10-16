// 用无头浏览器给界面截图，方便在评审时直接看效果。
// 用法：node tools/screenshot.cjs http://127.0.0.1:8899/ output/ui.png

const { chromium } = require("playwright");

(async () => {
  const url = process.argv[2] || "http://127.0.0.1:8899/";
  const out = process.argv[3] || "output/ui.png";
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: 1680, height: 1150 },
    deviceScaleFactor: 2,
  });
  page.on("console", (msg) => console.log("[page]", msg.type(), msg.text()));
  page.on("pageerror", (err) => console.log("[pageerror]", err.message));
  await page.goto(url, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1500);
  await page.screenshot({ path: out, fullPage: true });
  console.log("已保存截图", out);
  await browser.close();
})().catch((err) => {
  console.error("截图失败：", err);
  process.exit(1);
});
