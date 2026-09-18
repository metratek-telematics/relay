#!/usr/bin/env node
// relay-browse: drive the running app like a user and report what really happened.
//
//   relay-browse <url> <steps.json> [--out <dir>] [--width 1440] [--height 900] [--theme light|dark]
//
// steps.json is a list of steps, run in order; the run stops at the first failing step:
//   {"click": "css selector"}                   click an element (waits for it)
//   {"click": {"x": 640, "y": 360}}              click a point (maps, canvases)
//   {"dblclick": "…"} / {"hover": "…"}
//   {"type": {"into": "css", "text": "…"}}      fill an input
//   {"press": "Enter"}                           a key
//   {"wait": 800}                                milliseconds, or {"wait": "css"} for an element
//   {"expect": {"text": "Saved"}}                text must appear on the page (or in a frame)
//   {"expect": {"selector": "css", "count": 1}}  element count, or {"visible": true}/{"hidden": true}
//   {"expect": {"js": "window.store.point !== null"}}   expression must be truthy
//   {"eval": "JSON.stringify(window.someState)"} record a value in the report
//   {"screenshot": "after-pick"}                 PNG in --out
//   {"frame": "iframe selector"}                 later steps act inside that iframe ({"frame": null} = back to the page)
//
// It prints a JSON report: each step with ok/error, recorded values, window.postMessage traffic (what
// components actually send each other, with the real payload shape), console errors, page errors and
// failed requests. Exit code 1 when a step failed.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const argv = process.argv.slice(2);
const arg = (name, fallback) => { const i = argv.indexOf(`--${name}`); return i > -1 && argv[i + 1] ? argv[i + 1] : fallback; };
const valued = new Set(["--out", "--width", "--height", "--theme"]);
const [url, stepsFile] = argv.filter((a, i) => !a.startsWith("--") && !valued.has(argv[i - 1]));
if (!url || !stepsFile) {
  console.error("usage: relay-browse <url> <steps.json> [--out dir] [--width 1440] [--height 900] [--theme light|dark]");
  process.exit(2);
}
const outDir = arg("out", "relay-browse-out");
fs.mkdirSync(outDir, { recursive: true });
const steps = JSON.parse(fs.readFileSync(stepsFile, "utf8"));

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: Number(arg("width", 1440)), height: Number(arg("height", 900)) },
    colorScheme: arg("theme", "light") === "dark" ? "dark" : "light",
    ignoreHTTPSErrors: true,
  });
  const report = { url, steps: [], values: {}, messages: [], problems: [] };
  page.on("pageerror", (e) => report.problems.push(`page error: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && report.problems.push(`console error: ${m.text()}`));
  page.on("requestfailed", (r) => report.problems.push(`request failed: ${r.url()} (${r.failure()?.errorText})`));
  // Record postMessage traffic in every frame: the real shape of what components send each other.
  await page.exposeFunction("__relayMessage", (m) => { if (report.messages.length < 200) report.messages.push(m); });
  await page.addInitScript(() => {
    const where = window === window.top ? "page" : "frame";
    window.addEventListener("message", (e) => {
      let data = e.data;
      try { data = JSON.parse(JSON.stringify(data)); } catch { data = String(data); }
      try { window.__relayMessage({ to: where, data }); } catch {}
    });
  });
  let scope = page;
  const target = () => scope;
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  } catch (e) {
    report.problems.push(`navigation: ${e.message.split("\n")[0]}`);
  }
  let failed = false;
  for (const [i, step] of steps.entries()) {
    const entry = { n: i + 1, step, ok: true };
    try {
      if ("frame" in step) {
        scope = step.frame ? page.frameLocator(step.frame) : page;
      } else if (step.click !== undefined) {
        if (typeof step.click === "object") await page.mouse.click(step.click.x, step.click.y);
        else await target().locator(step.click).first().click({ timeout: 15000 });
      } else if (step.dblclick) {
        await target().locator(step.dblclick).first().dblclick({ timeout: 15000 });
      } else if (step.hover) {
        await target().locator(step.hover).first().hover({ timeout: 15000 });
      } else if (step.type) {
        await target().locator(step.type.into).first().fill(String(step.type.text), { timeout: 15000 });
      } else if (step.press) {
        await page.keyboard.press(step.press);
      } else if (step.wait !== undefined) {
        if (typeof step.wait === "number") await page.waitForTimeout(step.wait);
        else await target().locator(step.wait).first().waitFor({ timeout: 20000 });
      } else if (step.expect) {
        const x = step.expect;
        if (x.text) await target().getByText(x.text, { exact: false }).first().waitFor({ timeout: 10000 });
        if (x.selector) {
          const loc = target().locator(x.selector);
          if (x.count !== undefined) { const c = await loc.count(); if (c !== x.count) throw new Error(`expected ${x.count} × ${x.selector}, found ${c}`); }
          if (x.visible) await loc.first().waitFor({ state: "visible", timeout: 10000 });
          if (x.hidden) await loc.first().waitFor({ state: "hidden", timeout: 10000 });
        }
        if (x.js) { const v = await page.evaluate(x.js); if (!v) throw new Error(`expected truthy: ${x.js} → ${JSON.stringify(v)}`); }
      } else if (step.eval) {
        entry.value = await page.evaluate(step.eval);
        report.values[`step${i + 1}`] = entry.value;
      } else if (step.screenshot) {
        const file = path.join(outDir, `${String(step.screenshot).replace(/[^\w.-]+/g, "_")}.png`);
        await page.screenshot({ path: file });
        entry.file = file;
      } else {
        throw new Error("unknown step");
      }
    } catch (e) {
      entry.ok = false;
      entry.error = e.message.split("\n")[0];
      failed = true;
    }
    report.steps.push(entry);
    if (failed) break;
  }
  const shot = path.join(outDir, "final.png");
  await page.screenshot({ path: shot }).catch(() => {});
  report.final = shot;
  await browser.close();
  console.log(JSON.stringify(report, null, 2));
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.error(e.message); process.exit(1); });
