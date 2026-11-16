// 逐帧截图 → ffmpeg 编码。用法：node render.js <html> <out.mp4> [fps]
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const path = require('path');
const ffmpeg = require('@ffmpeg-installer/ffmpeg').path;
(async () => {
  const [html, out, fpsArg] = process.argv.slice(2);
  const fps = parseInt(fpsArg || '30', 10);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  p.on('pageerror', e => console.error('PAGE ERROR', e.message));
  await p.goto('file://' + path.resolve(html) + '?render=1');
  await p.evaluate(() => document.fonts.ready);
  const dur = await p.evaluate(() => window.DURATION);
  const total = Math.round(dur * fps);
  const ff = spawn(ffmpeg, ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-i', '-',
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'medium', '-movflags', '+faststart', out], { stdio: ['pipe', 'inherit', 'inherit'] });
  const t0 = Date.now();
  for (let i = 0; i < total; i++) {
    await p.evaluate(t => window.seek(t), i / fps);
    const buf = await p.screenshot({ type: 'png' });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
    if (i % (fps * 5) === 0) console.log(`frame ${i}/${total}  ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  }
  ff.stdin.end();
  await new Promise(r => ff.on('close', r));
  await b.close();
  console.log('done', out, `${((Date.now() - t0) / 1000).toFixed(0)}s`);
})();
