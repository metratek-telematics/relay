#!/usr/bin/env node
// Browser connector worker: run by Relay (never by an agent) with the job as JSON on stdin, so the
// credentials in it never appear in a process list. Prints one JSON line with the result.
//
// job: { base_url, login_steps:[{action,selector,value,path,ms}], username, password, goto, actions:[…],
//        screenshot, text_selector, width, height, theme, wait }
const { chromium } = require("playwright-core");

function readStdin() {
  return new Promise((resolve) => {
    let s = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => (s += c));
    process.stdin.on("end", () => resolve(s));
  });
}

(async () => {
  const job = JSON.parse(await readStdin());
  const origin = new URL(job.base_url).origin;
  const fill = (v) => String(v ?? "").replace(/\{\{\s*username\s*\}\}/g, job.username || "").replace(/\{\{\s*password\s*\}\}/g, job.password || "");
  const url = (p) => job.base_url + (String(p || "/").startsWith("/") ? p : `/${p}`);
  const out = { ok: false, problems: [] };
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: job.width, height: job.height }, colorScheme: job.theme === "dark" ? "dark" : "light", ignoreHTTPSErrors: true });
    page.on("pageerror", (e) => out.problems.push(`page error: ${e.message}`));
    page.on("console", (m) => m.type() === "error" && out.problems.push(`console error: ${m.text()}`));
    page.on("requestfailed", (r) => out.problems.push(`request failed: ${r.url()} (${r.failure()?.errorText})`));
    page.on("response", (r) => r.status() >= 400 && r.url().startsWith(origin) && out.problems.push(`HTTP ${r.status()}: ${r.url()}`));

    const step = async (s) => {
      if (s.action === "goto") return page.goto(url(s.path), { waitUntil: "networkidle", timeout: 45000 });
      if (s.action === "fill") return page.fill(s.selector, fill(s.value), { timeout: 15000 });
      if (s.action === "click") return page.click(s.selector, { timeout: 15000 });
      if (s.action === "press") return page.press(s.selector, s.value || "Enter", { timeout: 15000 });
      if (s.action === "wait") return page.waitForTimeout(Math.min(Number(s.ms) || 1000, 15000));
      if (s.action === "waitfor") return page.waitForSelector(s.selector, { timeout: 20000 });
    };
    if ((job.login_steps || []).length) {
      try {
        for (const s of job.login_steps) await step(s);
        out.login = "done";
      } catch (e) {
        out.login = `failed: ${String(e.message).split("\n")[0]}`;
      }
    }
    let resp = null;
    try {
      resp = await page.goto(url(job.goto), { waitUntil: "networkidle", timeout: 45000 });
    } catch (e) {
      out.problems.push(`navigation: ${String(e.message).split("\n")[0]}`);
    }
    for (const a of job.actions || []) {
      try { await step(a); } catch (e) { out.problems.push(`${a.action} ${a.selector || a.path || ""}: ${String(e.message).split("\n")[0]}`); }
    }
    if (job.wait) await page.waitForTimeout(job.wait);
    // Navigation must stay on the connector's site: a redirect elsewhere is reported, not followed further.
    if (!page.url().startsWith(origin)) out.problems.push(`left the connector's site: ${page.url()}`);
    out.url = page.url();
    out.status = resp ? resp.status() : null;
    out.title = await page.title().catch(() => "");
    if (job.text_selector) out.text = await page.innerText(job.text_selector, { timeout: 5000 }).catch((e) => `(no text: ${String(e.message).split("\n")[0]})`);
    if (job.screenshot) out.screenshot = (await page.screenshot({ fullPage: false })).toString("base64");
    out.ok = !!resp && resp.status() < 400 && !String(out.login || "").startsWith("failed");
  } catch (e) {
    out.error = String(e.message).split("\n")[0];
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(out) + "\n");
})().catch((e) => { process.stdout.write(JSON.stringify({ ok: false, error: e.message, problems: [] }) + "\n"); });
