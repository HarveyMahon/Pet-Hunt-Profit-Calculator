// Loads the site in a headless browser with the freshly updated bosses.json and
// checks every page renders without errors or broken numbers.
// Run by the weekly price update before it commits: node check_page.js
// Exits 1 (and prints what broke) if anything looks wrong.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const ROOT = __dirname;
const TYPES = { '.html': 'text/html; charset=utf-8', '.json': 'application/json', '.js': 'text/javascript' };
const BAD = /\bNaN\b|\bundefined\b|\bInfinity\b|\[object Object\]|\b1\/0\b/;

function serve() {
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
    const file = path.join(ROOT, rel);
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { 'Content-Type': TYPES[path.extname(file)] || 'application/octet-stream' });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise(r => server.listen(0, '127.0.0.1', () => r(server)));
}

(async () => {
  const data = JSON.parse(fs.readFileSync(path.join(ROOT, 'bosses.json'), 'utf8'));
  const problems = [];

  // The data itself: missing numbers show up as 0 on the page rather than NaN, so check them here
  const num = v => typeof v === 'number' && Number.isFinite(v);
  for (const [name, it] of Object.entries(data.items || {}))
    if (!it.derivedFrom && !num(it.price)) problems.push(`Item "${name}" has no valid price (${JSON.stringify(it.price)}).`);
  for (const b of data.bosses || []) {
    // Raids and Doom have no fixed rate (it depends on their settings), so only check fixed rates
    if (b.petRate != null && !(num(b.petRate) && b.petRate > 0)) problems.push(`${b.name}: pet rate is not a positive number.`);
    for (const d of b.drops || []) if (!data.items[d.item]) problems.push(`${b.name}: drop "${d.item}" has no item entry.`);
  }
  if (!(data.bosses || []).length) problems.push('bosses.json has no bosses.');
  const server = await serve();
  const url = `http://127.0.0.1:${server.address().port}/index.html`;
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    page.on('pageerror', e => problems.push(`Script error: ${e.message}`));
    // Fonts come from Google; don't let the page wait on them
    await page.route(/fonts\.(googleapis|gstatic)\.com/, r => r.abort());
    await page.goto(url);
    await page.waitForSelector('#overview tbody tr', { timeout: 15000 });

    if (await page.evaluate(() => document.documentElement.dataset.source) !== 'live')
      problems.push('The page could not read bosses.json and fell back to its built-in copy.');

    const expected = data.bosses.length + (data.skillingPets || []).length;
    const rows = await page.$$eval('#overview tbody tr[data-id]', r => r.length);
    if (rows !== expected) problems.push(`Summary shows ${rows} rows, expected ${expected}.`);
    const summary = await page.$eval('#overview', t => t.innerText);
    if (BAD.test(summary)) problems.push('Summary table contains NaN/undefined.');

    // Every boss and miscellaneous breakdown
    for (const b of data.bosses) {
      const text = await page.evaluate(id => {
        const sel = document.getElementById('boss');
        sel.value = id; sel.dispatchEvent(new Event('change'));
        return document.getElementById('summary').innerText + '\n' + document.getElementById('drops').innerText;
      }, b.id);
      if (BAD.test(text)) problems.push(`${b.name}: breakdown contains NaN, undefined or a 1/0 drop rate.`);
      if (!text.trim()) problems.push(`${b.name}: breakdown is empty.`);
    }
    // Every skilling pet page
    for (const p of data.skillingPets || []) {
      const res = await page.evaluate(id => {
        const sel = document.getElementById('skill-pet');
        sel.value = id; sel.dispatchEvent(new Event('change'));
        const t = document.getElementById('skill-table');
        return { text: document.getElementById('skill-summary').innerText + '\n' + t.innerText, rows: t.tBodies[0].rows.length };
      }, p.id);
      if (BAD.test(res.text)) problems.push(`${p.name}: skilling page contains NaN/undefined.`);
      if (!res.rows) problems.push(`${p.name}: no methods listed.`);
    }
  } catch (e) {
    problems.push(`Check crashed: ${e.message}`);
  } finally {
    await browser.close();
    server.close();
  }

  if (problems.length) {
    console.error(`Page check FAILED (${problems.length} problem${problems.length > 1 ? 's' : ''}):`);
    problems.forEach(p => console.error(' - ' + p));
    process.exit(1);
  }
  console.log('Page check passed: summary, every boss breakdown and every skilling pet rendered cleanly.');
})();
