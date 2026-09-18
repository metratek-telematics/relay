#!/usr/bin/env node
// Render design-exploration mockups (orchestrator/exploration.py) to PNGs: every direction at every size and theme,
// in one browser, each page time-bounded, so a broken mockup never stalls a task.
//
//   node render_mockups.cjs job.json
//   job = {"items":[{"id":"A","html":"/abs/A/index.html","out_dir":"/abs/A/shots"}],
//          "sizes":[{"name":"desktop","width":1440,"height":900}], "themes":["light","dark"],
//          "page_timeout_ms":20000, "max_height":2400}
//
// Writes <out_dir>/<size>-<theme>.png and prints one JSON line per shot.
const fs = require("fs");
const path = require("path");
const { pathToFileURL } = require("url");
const { chromium } = require("playwright-core");

(async () => {
  const job = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
  const pageTimeout = Number(job.page_timeout_ms || 20000);
  const maxHeight = Number(job.max_height || 2400);
  const browser = await chromium.launch({ args: ["--disable-gpu", "--no-sandbox"] });
  try {
    for (const item of job.items || []) {
      fs.mkdirSync(item.out_dir, { recursive: true });
      for (const size of job.sizes || []) {
        for (const theme of job.themes || ["light"]) {
          const out = path.join(item.out_dir, `${size.name}-${theme}.png`);
          const started = Date.now();
          const problems = [];
          const ctx = await browser.newContext({ viewport: { width: size.width, height: size.height }, colorScheme: theme === "dark" ? "dark" : "light",
            reducedMotion: "reduce", deviceScaleFactor: 1 });
          const page = await ctx.newPage();
          page.setDefaultTimeout(pageTimeout);
          page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
          // Mockups are local files; anything remote (fonts) gets a short leash.
          await page.route(/^https?:/, async (route) => {
            const u = route.request().url();
            if (/fonts\.(googleapis|gstatic)\.com/.test(u)) return route.continue().catch(() => {});
            problems.push(`blocked remote request: ${u.slice(0, 120)}`);
            return route.abort().catch(() => {});
          });
          try {
            await page.goto(pathToFileURL(item.html).href, { waitUntil: "load", timeout: pageTimeout });
            await page.evaluate((th) => { document.documentElement.setAttribute("data-theme", th); document.documentElement.style.colorScheme = th; }, theme);
            await page.waitForTimeout(400);
            const h = await page.evaluate(() => Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0));
            const height = Math.max(size.height, Math.min(maxHeight, h || size.height));
            await page.screenshot({ path: out, fullPage: true, clip: { x: 0, y: 0, width: size.width, height }, timeout: pageTimeout });
            const hscroll = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
            if (hscroll) problems.push("horizontal scroll");
            console.log(JSON.stringify({ id: item.id, shot: `${size.name}-${theme}`, ok: true, path: out, ms: Date.now() - started, problems: problems.slice(0, 5) }));
          } catch (e) {
            console.log(JSON.stringify({ id: item.id, shot: `${size.name}-${theme}`, ok: false, error: String(e.message || e).split("\n")[0], problems: problems.slice(0, 5) }));
          } finally {
            await ctx.close().catch(() => {});
          }
        }
      }
    }
  } finally {
    await browser.close().catch(() => {});
  }
})().catch((e) => { console.error(e.message || String(e)); process.exit(1); });
