#!/usr/bin/env node
// relay-screenshot: let an agent look at the page it is building.
//
//   relay-screenshot <url> <out.png> [--width 1440] [--height 900] [--theme light|dark] [--full] [--wait 1500]
//
// Prints console errors and failed requests so a visual check also catches broken pages.
const { chromium } = require("playwright-core");

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 && process.argv[i + 1] && !process.argv[i + 1].startsWith("--") ? process.argv[i + 1] : fallback;
}

(async () => {
  const valued = new Set(["--width", "--height", "--theme", "--wait"]);
  const argv = process.argv.slice(2);
  const [url, out] = argv.filter((a, i) => !a.startsWith("--") && !valued.has(argv[i - 1]));
  if (!url || !out) {
    console.error("usage: relay-screenshot <url> <out.png> [--width 1440] [--height 900] [--theme light|dark] [--full] [--wait 1500]");
    process.exit(2);
  }
  const theme = arg("theme", "light");
  const browser = await chromium.launch();
  const page = await browser.newPage({
    viewport: { width: Number(arg("width", 1440)), height: Number(arg("height", 900)) },
    colorScheme: theme === "dark" ? "dark" : "light",
    ignoreHTTPSErrors: true,
  });
  const problems = [];
  page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && problems.push(`console error: ${m.text()}`));
  page.on("requestfailed", (r) => problems.push(`request failed: ${r.url()} (${r.failure()?.errorText})`));
  try {
    await page.goto(url, { waitUntil: "networkidle", timeout: 60000 });
  } catch (e) {
    problems.push(`navigation: ${e.message.split("\n")[0]}`);
  }
  await page.waitForTimeout(Number(arg("wait", 1500)));
  await page.screenshot({ path: out, fullPage: process.argv.includes("--full") });
  await browser.close();
  console.log(`saved ${out} (${theme}, ${arg("width", 1440)}x${arg("height", 900)})`);
  for (const p of problems.slice(0, 30)) console.log(p);
  if (problems.length > 30) console.log(`…and ${problems.length - 30} more`);
})().catch((e) => { console.error(e.message); process.exit(1); });
