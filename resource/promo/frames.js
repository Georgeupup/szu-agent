// 用法：node frames.js <html> <outdir> t1 t2 ...  —— 截若干关键帧
const { chromium } = require('playwright');
const path = require('path'), fs = require('fs');
(async () => {
  const [html, out, ...ts] = process.argv.slice(2);
  fs.mkdirSync(out, { recursive: true });
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  p.on('pageerror', e => console.error('PAGE ERROR', e.message));
  await p.goto('file://' + path.resolve(html) + '?render=1');
  await p.evaluate(() => document.fonts.ready);
  for (const t of ts) {
    await p.evaluate(t => window.seek(t), parseFloat(t));
    await p.screenshot({ path: path.join(out, `f_${t}.png`) });
  }
  await b.close();
})();
