// Screenshot the sim2d viewer at ~8 fps for DURATION seconds into frames/.
const puppeteer = require('puppeteer-core');
const path = require('path');

const DURATION_S = parseFloat(process.env.DURATION_S || '60');
const FPS = parseFloat(process.env.FPS || '8');
const OUT = path.join(__dirname, 'frames');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    headless: 'new',
    args: ['--no-sandbox', '--hide-scrollbars', '--force-device-scale-factor=2',
           '--disable-background-timer-throttling',
           '--disable-backgrounding-occluded-windows', '--disable-renderer-backgrounding'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1120, height: 640, deviceScaleFactor: 2 });
  await page.goto('http://localhost:9092', { waitUntil: 'networkidle2' });
  await new Promise(r => setTimeout(r, 800));

  const interval = 1000 / FPS;
  const total = Math.round(DURATION_S * FPS);
  console.log(`recording ${total} frames at ${FPS} fps...`);
  const t0 = Date.now();
  for (let i = 0; i < total; i++) {
    const target = t0 + i * interval;
    const wait = target - Date.now();
    if (wait > 0) await new Promise(r => setTimeout(r, wait));
    await page.screenshot({ path: path.join(OUT, `f${String(i).padStart(4, '0')}.png`) });
  }
  await browser.close();
  console.log('done');
})();
