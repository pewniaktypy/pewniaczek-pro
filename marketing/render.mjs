import { chromium } from '/opt/node-tools/node_modules/playwright/index.mjs';
import { spawn } from 'node:child_process';
import path from 'node:path';

const dir = path.dirname(new URL(import.meta.url).pathname);
const [,, out = path.join(dir, 'out.mp4'), fromArg, toArg] = process.argv;
const FPS = 30, DUR = 30;
const from = fromArg ? Math.round(+fromArg * FPS) : 0;
const to = toArg ? Math.round(+toArg * FPS) : FPS * DUR;

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
await page.goto('file://' + path.join(dir, 'scene.html'));
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(500);

if (out.endsWith('.png')) {
  await page.evaluate(t => window.render(t), +fromArg);
  await page.screenshot({ path: out });
} else {
  const ff = spawn('ffmpeg', ['-y', '-f', 'image2pipe', '-framerate', String(FPS), '-i', '-',
    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'medium', '-movflags', '+faststart', out],
    { stdio: ['pipe', 'ignore', 'inherit'] });
  for (let f = from; f < to; f++) {
    await page.evaluate(t => window.render(t), f / FPS);
    const buf = await page.screenshot({ type: 'jpeg', quality: 92 });
    if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
  }
  ff.stdin.end();
  await new Promise(r => ff.on('close', r));
}
await browser.close();
