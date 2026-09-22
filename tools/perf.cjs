#!/usr/bin/env node
// relay-perf: measure how a web page really performs while a user drives it, with the numbers DevTools and
// Lighthouse give, so a performance change is judged by evidence instead of by a screenshot.
//
//   relay-perf run <url> [steps.json] [--out dir] [--throttle 1,4] [--network slow4g|fast3g] [--width 1440] [--height 900]
//                  [--settle 3000] [--repeat 1] [--no-lighthouse] [--lighthouse-preset desktop|mobile] [--label name]
//                  [--serve "npx vite --host 127.0.0.1 --port {port} --strictPort"]   start the app first; <url> may then be a path
//                  [--har data.har [--har-url "**/rpc/**"]]   record the scenario's responses once (file missing), then replay them
//                                                            in every run, so before/after compare the same live data (default:
//                                                            only requests to other origins; the app's own code is never replayed)
//   relay-perf compare <before/perf.json> <after/perf.json> [--out compare.json] [--md compare.md]
//   relay-perf assert <perf.json> "4x.fps_p5>=45" "4x.pan.long_task_max_ms<=100" [--baseline before/perf.json "4x.tbt_ms<=-50%"]
//
// `run` loads the page once per CPU profile (1x = this machine, 4x/6x = Emulation.setCPUThrottlingRate, an older device),
// runs the steps while recording a Chrome performance trace with the V8 sampling profiler, then reports:
//   frame rate (requestAnimationFrame in the page and every iframe: avg / p5 FPS, % late frames), long tasks (>50 ms,
//   count / p95 / max, Total Blocking Time) with the functions inside them, scripting / rendering / painting / GC time,
//   forced synchronous layouts, JS heap after GC at start and end (leaks), DOM nodes and listeners, busy time of every
//   thread (main, workers, compositor, GPU: who is using the cores), the top 15 functions by self time with file:line
//   (source-mapped when the server serves maps), network (requests, bytes, slow/big resources, polling, websocket rate),
//   and Lighthouse (score, LCP, TBT, CLS, opportunities) when it is installed.
// Output in --out: perf.json (summary: machine-comparable), PERF_REPORT.md, trace-<profile>.json.gz (open it in Chrome
// DevTools > Performance > Load profile), profile-<profile>.cpuprofile, lighthouse.report.{json,html}.
//
// Steps (same format as relay-browse, plus interaction steps):
//   {"goto": "url"}  {"wait": 800 | "css"}  {"click": "css" | {"x":..,"y":..}}  {"hover": "css"}  {"press": "Enter"}
//   {"type": {"into": "css", "text": "…"}}  {"frame": "iframe css" | null}  {"eval": "js"}  {"screenshot": "name"}
//   {"expect": {"js": "…"} | {"text": "…"} | {"selector": "css", "count": n}}
//   {"drag": {"from": [x, y], "to": [x, y], "steps": 30, "duration": 800}}   (alias "pan") mouse drag, e.g. pan a map
//   {"wheel": {"x": 720, "y": 450, "deltaY": -300, "repeat": 3, "interval": 400}}   mouse wheel, e.g. zoom a map
//   {"frames": 3000}   just watch for that long (animations, live updates)
//   {"phase": "pan"}   measure the following steps as a named phase until the next phase ({"phase": null} ends it)
// Without a steps file a default map scenario runs (~20 s): pan in four directions twice, zoom in and out, idle.
"use strict";
const fs = require("fs");
const path = require("path");
const zlib = require("zlib");
const { spawnSync } = require("child_process");
const A = require("./perf_analyze.cjs");

const CATEGORIES = [
  "-*", "devtools.timeline", "disabled-by-default-devtools.timeline", "disabled-by-default-devtools.timeline.frame",
  "disabled-by-default-devtools.timeline.stack", "v8.execute", "disabled-by-default-v8.cpu_profiler", "blink.user_timing",
  "loading", "latencyInfo", "toplevel", "blink.console", "v8", "disabled-by-default-v8.gc", "__metadata",
];
// No GPU in the container: WebGL (map renderers) runs on SwiftShader, so GPU-bound numbers are pessimistic.
// RELAY_PERF_CHROME_ARGS adds flags, e.g. "--disable-web-security" when a local dev origin is refused by CORS
// on a production tile server that allows the real origin.
const BROWSER_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
  ...String(process.env.RELAY_PERF_CHROME_ARGS || "").split(/\s+/).filter(Boolean)];
const NETWORK = {
  slow4g: { latency: 150, downloadThroughput: (1.6 * 1024 * 1024) / 8, uploadThroughput: (750 * 1024) / 8 },
  fast3g: { latency: 562.5, downloadThroughput: (1.44 * 1024 * 1024) / 8, uploadThroughput: (675 * 1024) / 8 },
};

// ---------------------------------------------------------------- args
const argv = process.argv.slice(2);
const VALUED = new Set(["--out", "--throttle", "--network", "--width", "--height", "--settle", "--repeat", "--lighthouse-preset",
  "--label", "--md", "--baseline", "--theme", "--timeout", "--serve", "--serve-timeout", "--har", "--har-url"]);
function arg(name, fallback) { const i = argv.indexOf(`--${name}`); return i > -1 && argv[i + 1] !== undefined ? argv[i + 1] : fallback; }
const flag = (name) => argv.includes(`--${name}`);
const positional = argv.filter((a, i) => !a.startsWith("--") && !VALUED.has(argv[i - 1]));

function usage() {
  console.error(fs.readFileSync(__filename, "utf8").split("\n").slice(1, 31).map((l) => l.replace(/^\/\/ ?/, "")).join("\n"));
  process.exit(2);
}

// ---------------------------------------------------------------- default scenario
function defaultSteps(w, h) {
  const cx = Math.round(w / 2), cy = Math.round(h / 2), dx = Math.round(w * 0.3), dy = Math.round(h * 0.3);
  const drag = (x1, y1, x2, y2) => ({ drag: { from: [x1, y1], to: [x2, y2], steps: 40, duration: 1000 } });
  const pans = [drag(cx + dx, cy, cx - dx, cy), drag(cx, cy + dy, cx, cy - dy), drag(cx - dx, cy, cx + dx, cy), drag(cx, cy - dy, cx, cy + dy)];
  return [
    { phase: "pan" }, ...pans, { wait: 300 }, ...pans,
    { phase: "zoom" },
    { wheel: { x: cx, y: cy, deltaY: -240, repeat: 4, interval: 700 } },
    { wait: 800 },
    { wheel: { x: cx, y: cy, deltaY: 240, repeat: 4, interval: 700 } },
    { phase: "idle" }, { frames: 3000 },
    { phase: null },
  ];
}

// ---------------------------------------------------------------- in-page probes
// Runs in the page and in every iframe before their scripts: frame times and web-vitals style entries.
function initProbe() {
  const st = (window.__relayPerf = window.__relayPerf || { raf: [], lcp: 0, cls: 0, events: [], longtasks: 0 });
  const origin = () => performance.timeOrigin;
  // An iframe's initial about:blank window is reused by its first navigation and loses pending rAF callbacks,
  // so the loop restarts on DOMContentLoaded/load and whenever it has gone quiet (one loop at a time).
  let gen = 0, last = 0;
  const start = () => {
    const g = ++gen;
    const tick = (t) => { if (g !== gen) return; last = Date.now(); if (st.raf.length < 200000) st.raf.push(origin() + t); requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  };
  start();
  addEventListener("DOMContentLoaded", start);
  addEventListener("load", start);
  setInterval(() => { if (Date.now() - last > 1500 && document.visibilityState === "visible") start(); }, 1000);
  const observe = (type, fn, extra) => {
    try { new PerformanceObserver((l) => l.getEntries().forEach(fn)).observe({ type, buffered: true, ...(extra || {}) }); } catch {}
  };
  observe("largest-contentful-paint", (e) => { st.lcp = Math.max(st.lcp, e.startTime); });
  observe("layout-shift", (e) => { if (!e.hadRecentInput) st.cls += e.value; });
  observe("event", (e) => { if (st.events.length < 5000) st.events.push({ n: e.name, d: e.duration, id: e.interactionId || 0, t: origin() + e.startTime }); }, { durationThreshold: 16 });
  observe("longtask", () => { st.longtasks++; });
}

async function frameProbe(frame) {
  try {
    return await frame.evaluate(() => {
      const st = window.__relayPerf || {};
      const nav = performance.getEntriesByType("navigation")[0];
      const fcp = performance.getEntriesByName("first-contentful-paint")[0];
      const scripts = new Set([location.href, ...[...document.scripts].map((s) => s.src).filter(Boolean),
        ...performance.getEntriesByType("resource").filter((r) => r.initiatorType === "script" || r.name.endsWith(".js")).map((r) => r.name)]);
      return { url: location.href, raf: st.raf || [], lcp: st.lcp || 0, cls: st.cls || 0, events: st.events || [],
               fcp: fcp ? fcp.startTime : null, dcl: nav ? nav.domContentLoadedEventEnd : null, load: nav ? nav.loadEventEnd : null,
               origin: performance.timeOrigin, scripts: [...scripts], nodes: document.getElementsByTagName("*").length };
    });
  } catch { return null; }
}

// ---------------------------------------------------------------- steps
async function runSteps(page, steps, ctx) {
  let scope = page;
  const report = [];
  for (const [i, step] of steps.entries()) {
    const entry = { n: i + 1, step, ok: true };
    const t0 = Date.now();
    try {
      if ("phase" in step) {
        await ctx.phase(step.phase);
      } else if ("frame" in step) {
        scope = step.frame ? page.frameLocator(step.frame) : page;
      } else if (step.goto) {
        await page.goto(step.goto, { waitUntil: "load", timeout: 120000 });
      } else if (step.drag || step.pan) {
        const d = step.drag || step.pan;
        const n = Math.max(2, Number(d.steps || 30)), dur = Number(d.duration ?? 800);
        await page.mouse.move(d.from[0], d.from[1]);
        await page.mouse.down();
        const start = Date.now();
        for (let k = 1; k <= n; k++) {
          await page.mouse.move(d.from[0] + ((d.to[0] - d.from[0]) * k) / n, d.from[1] + ((d.to[1] - d.from[1]) * k) / n);
          const due = start + (dur * k) / n - Date.now();
          if (due > 0) await page.waitForTimeout(due);
        }
        await page.mouse.up();
      } else if (step.wheel) {
        const w = step.wheel;
        await page.mouse.move(w.x ?? 720, w.y ?? 450);
        for (let k = 0; k < Number(w.repeat || 1); k++) {
          await page.mouse.wheel(Number(w.deltaX || 0), Number(w.deltaY ?? -240));
          await page.waitForTimeout(Number(w.interval ?? 400));
        }
      } else if (step.frames !== undefined) {
        await page.waitForTimeout(Number(step.frames));
      } else if (step.click !== undefined) {
        if (typeof step.click === "object") await page.mouse.click(step.click.x, step.click.y);
        else await scope.locator(step.click).first().click({ timeout: 15000 });
      } else if (step.dblclick) {
        await scope.locator(step.dblclick).first().dblclick({ timeout: 15000 });
      } else if (step.hover) {
        await scope.locator(step.hover).first().hover({ timeout: 15000 });
      } else if (step.type) {
        await scope.locator(step.type.into).first().fill(String(step.type.text), { timeout: 15000 });
      } else if (step.press) {
        await page.keyboard.press(step.press);
      } else if (step.wait !== undefined) {
        if (typeof step.wait === "number") await page.waitForTimeout(step.wait);
        else await scope.locator(step.wait).first().waitFor({ timeout: 30000 });
      } else if (step.expect) {
        const x = step.expect;
        if (x.text) await scope.getByText(x.text, { exact: false }).first().waitFor({ timeout: 15000 });
        if (x.selector && x.count !== undefined) { const c = await scope.locator(x.selector).count(); if (c !== x.count) throw new Error(`expected ${x.count} × ${x.selector}, found ${c}`); }
        if (x.js) { const v = await page.evaluate(x.js); if (!v) throw new Error(`expected truthy: ${x.js}`); }
      } else if (step.eval) {
        entry.value = await page.evaluate(step.eval);
      } else if (step.screenshot) {
        const file = path.join(ctx.outDir, `${ctx.profile}-${String(step.screenshot).replace(/[^\w.-]+/g, "_")}.png`);
        await page.screenshot({ path: file });
        entry.file = file;
      } else {
        throw new Error("unknown step");
      }
    } catch (e) {
      entry.ok = false;
      entry.error = e.message.split("\n")[0];
    }
    entry.ms = Date.now() - t0;
    report.push(entry);
    if (!entry.ok) break;
  }
  return report;
}

// ---------------------------------------------------------------- one profile run
async function runProfile(browser, url, steps, opts) {
  const { rate, outDir, label } = opts;
  const context = await browser.newContext({ viewport: { width: opts.width, height: opts.height }, ignoreHTTPSErrors: true,
                                             colorScheme: opts.theme === "dark" ? "dark" : "light" });
  await context.addInitScript(initProbe);
  // Same data for every run: replay recorded responses (anything not recorded still goes to the network).
  if (opts.har) await context.routeFromHAR(opts.har, { notFound: "fallback", url: opts.harUrl || undefined });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Performance.enable", { timeDomain: "timeTicks" }).catch(() => cdp.send("Performance.enable"));
  if (rate > 1) await cdp.send("Emulation.setCPUThrottlingRate", { rate });
  if (opts.network && NETWORK[opts.network]) {
    await cdp.send("Network.enable");
    await cdp.send("Network.emulateNetworkConditions", { offline: false, ...NETWORK[opts.network] });
  }
  // Workers get the same CPU throttle where Chrome allows it.
  page.on("worker", async (w) => {
    try { const s = await context.newCDPSession(w); if (rate > 1) await s.send("Emulation.setCPUThrottlingRate", { rate }); } catch {}
  });

  // Network, from every frame and worker of the page.
  const requests = [], sockets = [], problems = [];
  const reqStart = new Map();
  context.on("request", (r) => reqStart.set(r, Date.now()));
  const finish = async (r, failed) => {
    const t0 = reqStart.get(r) || Date.now();
    let bytes = 0, status = 0, frame = "";
    try { const s = await r.sizes(); bytes = s.responseBodySize + s.responseHeadersSize; } catch {}
    try { status = (await r.response())?.status() || 0; } catch {}
    try { frame = r.frame().url(); } catch { frame = "(worker)"; }
    requests.push({ url: r.url(), method: r.method(), type: r.resourceType(), start: t0, ms: Date.now() - t0, bytes, status, failed: !!failed, frame });
  };
  context.on("requestfinished", (r) => { finish(r, false); });
  context.on("requestfailed", (r) => { finish(r, true); problems.push(`request failed: ${A.shortUrl(r.url())} (${r.failure()?.errorText})`); });
  page.on("pageerror", (e) => problems.push(`page error: ${e.message.split("\n")[0]}`));
  page.on("console", (m) => { if (m.type() === "error" && problems.length < 60) problems.push(`console error: ${m.text().slice(0, 200)}`); });
  page.on("websocket", (ws) => {
    const s = { url: ws.url(), rx: [], tx: [] };
    sockets.push(s);
    const size = (p) => (typeof p === "string" ? Buffer.byteLength(p) : p?.length || 0);
    ws.on("framereceived", (f) => { if (s.rx.length < 200000) s.rx.push([Date.now(), size(f.payload)]); });
    ws.on("framesent", (f) => { if (s.tx.length < 200000) s.tx.push([Date.now(), size(f.payload)]); });
  });

  const epochMark = (name) => page.evaluate((n) => { performance.mark(n); return performance.timeOrigin + performance.now(); }, name);
  // The page's JS profile comes from the Profiler domain: the trace's own sampler drops most main-thread samples
  // when the thread is saturated (exactly the case we measure). Workers keep the trace sampler.
  await cdp.send("Profiler.enable");
  await cdp.send("Profiler.setSamplingInterval", { interval: 500 });
  await browser.startTracing(page, { categories: CATEGORIES });
  await cdp.send("Profiler.start");
  const navStartEpoch = Date.now();
  let navError = null;
  try { await page.goto(url, { waitUntil: "load", timeout: Number(opts.timeout) }); }
  catch (e) { navError = e.message.split("\n")[0]; problems.push(`navigation: ${navError}`); }
  await page.waitForTimeout(Number(opts.settle));

  const metrics = async () => {
    try { const r = await cdp.send("Performance.getMetrics"); return Object.fromEntries(r.metrics.map((m) => [m.name, m.value])); }
    catch { return {}; }
  };
  const heapAfterGc = async () => {
    try { await cdp.send("HeapProfiler.collectGarbage"); const h = await cdp.send("Runtime.getHeapUsage"); return h.usedSize / 1048576; }
    catch { return null; }
  };
  const heapStart = await heapAfterGc();
  const mStart = await metrics();
  const series = [];
  const sampler = setInterval(async () => {
    const m = await metrics();
    if (m.JSHeapUsedSize) series.push({ t: Date.now(), heap_mb: m.JSHeapUsedSize / 1048576, nodes: m.Nodes, listeners: m.JSEventListeners });
  }, 1000);

  // Phases, in epoch ms (page clock) and as trace marks.
  const phases = [];
  let current = null;
  const ctx = {
    outDir, profile: label,
    async phase(name) {
      const t = await epochMark(`relay:phase:${current ? current.name : "_"}:end`);
      if (current) { current.end = t; phases.push(current); current = null; }
      if (name) { current = { name: String(name), start: await epochMark(`relay:phase:${name}:start`) }; }
    },
  };
  const scenarioStart = await epochMark("relay:scenario:start");
  const stepReport = await runSteps(page, steps, ctx);
  if (current) await ctx.phase(null);
  const scenarioEnd = await epochMark("relay:scenario:end");
  clearInterval(sampler);
  const mEnd = await metrics();
  const heapEnd = await heapAfterGc();

  const probes = [];
  for (const f of page.frames()) { const p = await frameProbe(f); if (p) probes.push({ ...p, main: f === page.mainFrame() }); }
  await page.screenshot({ path: path.join(outDir, `${label}-final.png`) }).catch(() => {});
  const cpuProfile = (await cdp.send("Profiler.stop").catch(() => ({}))).profile || null;
  const traceBuf = await browser.stopTracing();

  // ---------------- analyse the trace
  const events = JSON.parse(traceBuf.toString("utf8"));
  const traceEvents = Array.isArray(events) ? events : events.traceEvents;
  const model = A.buildModel(traceEvents);
  const mainT = A.mainThreads(model, url)[0];
  const mainProfile = cpuProfile && mainT ? A.fromCpuProfile(cpuProfile, mainT.pid, mainT.tid) : null;
  // Put the full main-thread profile into the saved trace so DevTools shows the JS flame chart.
  const saved = mainProfile ? [...traceEvents.filter((e) => !((e.name === "Profile" || e.name === "ProfileChunk") && e.pid === mainT.pid && model.profileOwner?.get(`${e.pid}:${e.id}`) === mainT.tid)),
    ...A.profileTraceEvents(cpuProfile, mainT.pid, mainT.tid)] : traceEvents;
  fs.writeFileSync(path.join(outDir, `trace-${label}.json.gz`), zlib.gzipSync(JSON.stringify({ traceEvents: saved })));
  const markTs = (name) => model.marks.find((m) => m.name === name)?.ts;
  const sTs = markTs("relay:scenario:start"), eTs = markTs("relay:scenario:end");
  let offsetUs = null; // trace µs = epoch ms * 1000 + offset
  if (sTs && eTs) offsetUs = ((sTs - scenarioStart * 1000) + (eTs - scenarioEnd * 1000)) / 2;
  const toTrace = (epoch) => epoch * 1000 + offsetUs;
  const mainProbe = probes.find((p) => p.main) || probes[0] || {};
  const windows = {
    scenario: offsetUs !== null ? [sTs, eTs] : [model.minTs, model.minTs + (scenarioEnd - navStartEpoch) * 1000],
    load: offsetUs !== null && mainProbe.origin ? [toTrace(mainProbe.origin), sTs] : null,
    phases: Object.fromEntries(phases.filter((p) => p.end > p.start).map((p) => [p.name, [toTrace(p.start), toTrace(p.end)]])),
  };
  const { res, prof } = A.analyze(model, { windows, pageUrl: url, mainProfile });
  if (cpuProfile) fs.writeFileSync(path.join(outDir, `profile-${label}.cpuprofile`), JSON.stringify(cpuProfile));

  // Frames: rAF in every document; the worst frame is what the user sees stutter.
  const fpsFor = (a, b) => {
    const per = probes.filter((p) => p.raf.length > 2).map((p) => ({ frame: p.main ? "page" : A.shortUrl(p.url), ...A.fpsStats(p.raf, a, b) }));
    const worst = per.filter((x) => x.fps_p5 !== null).sort((x, y) => x.fps_p5 - y.fps_p5)[0] || per[0] || A.fpsStats([], a, b);
    return { worst, per };
  };
  const fps = fpsFor(scenarioStart, scenarioEnd);
  const phaseFps = Object.fromEntries(phases.map((p) => [p.name, fpsFor(p.start, p.end).worst]));

  // Interactions (Event Timing): the INP-style number for this scenario.
  const byInteraction = new Map();
  for (const p of probes) for (const e of p.events) if (e.id && e.t >= scenarioStart) byInteraction.set(`${p.url}:${e.id}`, Math.max(byInteraction.get(`${p.url}:${e.id}`) || 0, e.d));
  const inter = [...byInteraction.values()].sort((a, b) => a - b);

  // Network during the scenario.
  await new Promise((r) => setTimeout(r, 200));
  const inScenario = requests.filter((r) => r.start >= scenarioStart - 50 && r.start <= scenarioEnd);
  const spanS = (scenarioEnd - scenarioStart) / 1000;
  const groups = new Map();
  for (const r of inScenario) {
    let k = r.url;
    try { const u = new URL(r.url); k = `${r.method} ${u.origin}${u.pathname}`; } catch {}
    const g = groups.get(k) || { endpoint: k, n: 0, bytes: 0, starts: [] };
    g.n++; g.bytes += r.bytes; g.starts.push(r.start);
    groups.set(k, g);
  }
  const polling = [...groups.values()].filter((g) => g.n >= 3 && !/\.(png|jpe?g|webp|pbf|mvt|avif)$/i.test(g.endpoint)).map((g) => {
    // Tile loaders fire in bursts: count bursts (gap > 300 ms) and the time between them.
    const s = g.starts.sort((a, b) => a - b);
    const bursts = [s[0]];
    for (let i = 1; i < s.length; i++) if (s[i] - s[i - 1] > 300) bursts.push(s[i]);
    const iv = bursts.slice(1).map((x, i) => x - bursts[i]);
    return { endpoint: A.shortUrl(g.endpoint.split(" ")[1]), method: g.endpoint.split(" ")[0], count: g.n, bursts: bursts.length,
             per_burst: A.round(g.n / bursts.length), every_ms: iv.length ? Math.round(A.median(iv)) : null, kb: A.round(g.bytes / 1024) };
  }).sort((a, b) => b.count - a.count).slice(0, 8);
  const wsScen = sockets.map((s) => {
    const rx = s.rx.filter(([t]) => t >= scenarioStart && t <= scenarioEnd), tx = s.tx.filter(([t]) => t >= scenarioStart && t <= scenarioEnd);
    return { url: A.shortUrl(s.url), rx_msgs: rx.length, rx_kb: A.round(rx.reduce((a, [, b]) => a + b, 0) / 1024), tx_msgs: tx.length,
             msgs_per_s: A.round(rx.length / spanS), kb_per_s: A.round(rx.reduce((a, [, b]) => a + b, 0) / 1024 / spanS) };
  });
  const network = {
    requests_total: requests.length, transfer_kb_total: A.round(requests.reduce((a, r) => a + r.bytes, 0) / 1024),
    requests_during_scenario: inScenario.length, transfer_kb_during_scenario: A.round(inScenario.reduce((a, r) => a + r.bytes, 0) / 1024),
    failed: requests.filter((r) => r.failed || r.status >= 400).length,
    biggest: [...requests].sort((a, b) => b.bytes - a.bytes).slice(0, 8).map((r) => ({ url: A.shortUrl(r.url), type: r.type, kb: A.round(r.bytes / 1024), ms: r.ms })),
    slowest: [...requests].sort((a, b) => b.ms - a.ms).slice(0, 8).map((r) => ({ url: A.shortUrl(r.url), type: r.type, kb: A.round(r.bytes / 1024), ms: r.ms })),
    polling, websockets: wsScen,
  };

  // Heap trend while interacting (MB per minute, least squares).
  let slope = null;
  if (series.length >= 8) {
    const t0 = series[0].t, xs = series.map((s) => (s.t - t0) / 60000), ys = series.map((s) => s.heap_mb);
    const mx = xs.reduce((a, b) => a + b) / xs.length, my = ys.reduce((a, b) => a + b) / ys.length;
    const num = xs.reduce((a, x, i) => a + (x - mx) * (ys[i] - my), 0), den = xs.reduce((a, x) => a + (x - mx) ** 2, 0);
    slope = den ? num / den : null;
  }

  // Which document loaded which script: attribute hot functions to the page or an iframe.
  const scriptFrame = new Map();
  for (const p of probes) for (const s of p.scripts) if (!scriptFrame.has(s) || !p.main) scriptFrame.set(s, p.main ? "page" : `iframe ${A.shortUrl(p.url)}`);
  const resolve = A.makeSourceMapper(async (u) => { const r = await context.request.get(u, { timeout: 10000 }); return r.ok() ? r.text() : ""; });
  const annotate = async (list) => {
    for (const f of list || []) {
      if (f.url) f.frame = scriptFrame.get(f.url) || (f.url.includes("/") ? undefined : undefined);
      const m = f.url ? await resolve(f.url, f.line, f.column).catch(() => null) : null;
      f.where = m ? `${m.source}:${m.line}` : `${A.shortUrl(f.url) || "(native)"}:${f.line}`;
      if (m && m.name && m.name !== f.function && f.function.length <= 3) f.function = `${f.function} (${m.name})`;
    }
  };
  await annotate(res.scenario?.hot_functions);
  await annotate(res.scenario?.hot_by_total);
  await annotate(res.load?.hot_functions);
  for (const t of res.scenario?.top_long_tasks || []) { await annotate(t.hot); await annotate(t.hot_total); }
  for (const t of res.load?.top_long_tasks || []) { await annotate(t.hot); await annotate(t.hot_total); }
  for (const ph of Object.values(res.phases || {})) await annotate(ph.hot_functions);
  for (const w of res.worker_hot || []) await annotate(w.top);
  await annotate(res.scenario?.forced_layouts?.top);

  await context.close();

  const sc = res.scenario || {};
  const bd = sc.breakdown_ms || {};
  const summary = {
    fps_avg: fps.worst.fps_avg, fps_p5: fps.worst.fps_p5, jank_pct: fps.worst.jank_pct, jank33_pct: fps.worst.jank33_pct, janky_time_pct: fps.worst.janky_time_pct,
    frame_p95_ms: fps.worst.frame_p95_ms, frame_max_ms: fps.worst.frame_max_ms,
    long_tasks: sc.long_tasks, long_tasks_over_100ms: sc.long_tasks_over_100ms, long_task_p95_ms: sc.long_task_p95_ms,
    long_task_max_ms: sc.long_task_max_ms, tbt_ms: sc.tbt_ms, main_busy_pct: sc.main_busy_pct, cores_used: res.cores_used,
    scripting_ms: bd.scripting, rendering_ms: bd.rendering, painting_ms: bd.painting, gc_ms: bd.gc, gc_count: sc.gc?.count,
    forced_layouts: sc.forced_layouts?.count, inp_ms: inter.length ? A.round(A.percentile(inter, 98)) : null,
    heap_start_mb: A.round(heapStart), heap_end_mb: A.round(heapEnd), heap_growth_mb: heapStart !== null && heapEnd !== null ? A.round(heapEnd - heapStart) : null,
    heap_peak_mb: series.length ? A.round(Math.max(...series.map((s) => s.heap_mb))) : null, heap_slope_mb_per_min: A.round(slope),
    dom_nodes: mEnd.Nodes ?? null, dom_nodes_growth: mEnd.Nodes !== undefined && mStart.Nodes !== undefined ? mEnd.Nodes - mStart.Nodes : null,
    listeners: mEnd.JSEventListeners ?? null, listeners_growth: mEnd.JSEventListeners !== undefined && mStart.JSEventListeners !== undefined ? mEnd.JSEventListeners - mStart.JSEventListeners : null,
    requests: inScenario.length, transfer_kb: network.transfer_kb_during_scenario,
    ws_msgs_per_s: wsScen.length ? A.round(wsScen.reduce((a, s) => a + s.msgs_per_s, 0)) : 0,
    ws_kb_per_s: wsScen.length ? A.round(wsScen.reduce((a, s) => a + s.kb_per_s, 0)) : 0,
    load_ms: mainProbe.load ? A.round(mainProbe.load) : null, fcp_ms: mainProbe.fcp ? A.round(mainProbe.fcp) : null,
    lcp_ms: mainProbe.lcp ? A.round(mainProbe.lcp) : null, cls: A.round(probes.reduce((a, p) => a + (p.cls || 0), 0), 3),
    load_tbt_ms: res.load?.tbt_ms ?? null, load_long_task_max_ms: res.load?.long_task_max_ms ?? null,
    phases: Object.fromEntries(Object.entries(res.phases || {}).map(([n, p]) => [n, {
      fps_avg: phaseFps[n]?.fps_avg ?? null, fps_p5: phaseFps[n]?.fps_p5 ?? null, jank_pct: phaseFps[n]?.jank_pct ?? null, janky_time_pct: phaseFps[n]?.janky_time_pct ?? null,
      long_tasks: p.long_tasks, long_task_max_ms: p.long_task_max_ms, tbt_ms: p.tbt_ms, main_busy_pct: p.main_busy_pct }])),
  };
  return {
    profile: label, cpu_throttle: rate, network: opts.network || "none", navigation_error: navError,
    steps: stepReport, steps_ok: stepReport.every((s) => s.ok), summary, detail: res, fps_per_frame: fps.per, network_detail: network,
    heap_series: series.map((s) => ({ t: A.round((s.t - scenarioStart) / 1000), heap_mb: A.round(s.heap_mb), nodes: s.nodes })),
    problems: [...new Set(problems)].slice(0, 30), files: { trace: `trace-${label}.json.gz`, cpuprofile: prof ? `profile-${label}.cpuprofile` : null, screenshot: `${label}-final.png` },
  };
}

// ---------------------------------------------------------------- Lighthouse
function lighthouseBin() {
  if (process.env.RELAY_LIGHTHOUSE && fs.existsSync(process.env.RELAY_LIGHTHOUSE)) return process.env.RELAY_LIGHTHOUSE;
  for (const p of ["/opt/lighthouse/node_modules/.bin/lighthouse", path.join(__dirname, "node_modules/.bin/lighthouse")]) if (fs.existsSync(p)) return p;
  const w = spawnSync("sh", ["-c", "command -v lighthouse"], { encoding: "utf8" });
  return w.status === 0 ? w.stdout.trim() : null;
}

async function runLighthouse(chromium, url, outDir, preset) {
  const bin = lighthouseBin();
  if (!bin) return { skipped: "lighthouse is not installed (npm i -g lighthouse, or set RELAY_LIGHTHOUSE)" };
  const port = 9300 + Math.floor(Math.random() * 500);
  const browser = await chromium.launch({ args: [...BROWSER_ARGS, `--remote-debugging-port=${port}`] });
  try {
    const args = [url, `--port=${port}`, "--output=json", "--output=html", `--output-path=${path.join(outDir, "lighthouse")}`,
      "--only-categories=performance", "--quiet", "--max-wait-for-load=45000", "--disable-full-page-screenshot"];
    if (preset === "desktop") args.push("--preset=desktop");
    const r = await new Promise((resolve) => {
      const { spawn } = require("child_process");
      const p = spawn(bin, args, { stdio: ["ignore", "pipe", "pipe"] });
      let err = "";
      p.stderr.on("data", (d) => { err += d; });
      const timer = setTimeout(() => p.kill("SIGKILL"), 240000);
      p.on("close", (code) => { clearTimeout(timer); resolve({ code, err }); });
    });
    const file = path.join(outDir, "lighthouse.report.json");
    if (!fs.existsSync(file)) return { error: `lighthouse failed (exit ${r.code}): ${r.err.trim().split("\n").slice(-2).join(" ").slice(0, 300)}` };
    const lhr = JSON.parse(fs.readFileSync(file, "utf8"));
    const au = lhr.audits || {};
    const num = (k) => (au[k] && typeof au[k].numericValue === "number" ? A.round(au[k].numericValue, 1) : null);
    const items = (k, n, f) => ((au[k]?.details?.items) || []).slice(0, n).map(f);
    const opportunities = Object.values(au).filter((a) => a.details?.type === "opportunity" && (a.details.overallSavingsMs > 0 || a.details.overallSavingsBytes > 0))
      .sort((x, y) => (y.details.overallSavingsMs || 0) - (x.details.overallSavingsMs || 0)).slice(0, 8)
      .map((a) => ({ id: a.id, title: a.title, savings_ms: A.round(a.details.overallSavingsMs || 0, 0), savings_kb: A.round((a.details.overallSavingsBytes || 0) / 1024, 0) }));
    const diagnostics = ["mainthread-work-breakdown", "bootup-time", "long-tasks", "dom-size", "third-party-summary", "total-byte-weight",
      "unused-javascript", "legacy-javascript", "duplicated-javascript", "non-composited-animations", "unsized-images", "render-blocking-resources", "uses-long-cache-ttl"]
      .filter((k) => au[k]).map((k) => ({ id: k, title: au[k].title, score: au[k].score, value: au[k].displayValue || "" }));
    return {
      preset, lighthouse_version: lhr.lighthouseVersion, score: lhr.categories?.performance?.score !== null && lhr.categories?.performance?.score !== undefined ? Math.round(lhr.categories.performance.score * 100) : null,
      fcp_ms: num("first-contentful-paint"), lcp_ms: num("largest-contentful-paint"), tbt_ms: num("total-blocking-time"),
      cls: au["cumulative-layout-shift"]?.numericValue ?? null, si_ms: num("speed-index"), tti_ms: num("interactive"),
      max_potential_fid_ms: num("max-potential-fid"), bootup_ms: num("bootup-time"), mainthread_ms: num("mainthread-work-breakdown"),
      dom_size: num("dom-size"), total_kb: au["total-byte-weight"] ? A.round(au["total-byte-weight"].numericValue / 1024, 0) : null,
      bootup_top: items("bootup-time", 6, (i) => ({ url: A.shortUrl(i.url), total_ms: A.round(i.total, 0), scripting_ms: A.round(i.scripting, 0), parse_ms: A.round(i.scriptParseCompile, 0) })),
      mainthread_top: items("mainthread-work-breakdown", 7, (i) => ({ group: i.groupLabel || i.group, ms: A.round(i.duration, 0) })),
      long_tasks_top: items("long-tasks", 5, (i) => ({ url: A.shortUrl(i.url), start_ms: A.round(i.startTime, 0), ms: A.round(i.duration, 0) })),
      unused_js_top: items("unused-javascript", 5, (i) => ({ url: A.shortUrl(i.url), wasted_kb: A.round((i.wastedBytes || 0) / 1024, 0), total_kb: A.round((i.totalBytes || 0) / 1024, 0) })),
      opportunities, diagnostics, report_html: "lighthouse.report.html", report_json: "lighthouse.report.json",
      runtime_error: lhr.runtimeError?.message || null,
    };
  } finally {
    await browser.close().catch(() => {});
  }
}

// ---------------------------------------------------------------- findings: plain-language hints
function findings(out) {
  const f = [];
  for (const p of out.profiles) {
    const s = p.summary, d = p.detail?.scenario || {};
    const tag = `[${p.profile}]`;
    if (s.fps_p5 !== null && s.fps_p5 < 30) f.push(`${tag} frame rate collapses during interaction: p5 ${s.fps_p5} FPS, avg ${s.fps_avg}, ${s.jank_pct}% frames late (>20 ms).`);
    if (s.long_task_max_ms > 100) f.push(`${tag} ${s.long_tasks_over_100ms} task(s) over 100 ms during interaction, worst ${s.long_task_max_ms} ms (TBT ${s.tbt_ms} ms): input is blocked that long.`);
    if (s.main_busy_pct > 60) f.push(`${tag} main thread busy ${s.main_busy_pct}% of the interaction: little headroom for input and frames.`);
    const top = (d.hot_functions || [])[0];
    if (top && top.self_pct >= 5) f.push(`${tag} hottest function: ${top.function} (${top.where}) — ${top.self_pct}% of the main thread by itself.`);
    const tot = (d.hot_by_total || []).find((x) => x.total_pct >= 20 && x.function !== "(anonymous)");
    if (tot) f.push(`${tag} ${tot.function} (${tot.where}) and what it calls take ${tot.total_pct}% of the main thread.`);
    if (d.forced_layouts?.count > 20) f.push(`${tag} ${d.forced_layouts.count} forced synchronous layouts (${d.forced_layouts.total_ms} ms); layout thrash in ${d.forced_layouts.thrash_tasks} task(s).`);
    if (s.gc_ms > 0.1 * (d.window_ms || 1)) f.push(`${tag} garbage collection ${s.gc_ms} ms (${s.gc_count} GCs): lots of short-lived allocation per frame.`);
    if (s.heap_growth_mb > 10) f.push(`${tag} JS heap after GC grew ${s.heap_growth_mb} MB during the scenario: possible leak.`);
    if (s.cores_used > 1.5) f.push(`${tag} ${s.cores_used} cores busy on average during interaction (see threads).`);
    if (s.dom_nodes > 5000) f.push(`${tag} ${s.dom_nodes} DOM nodes.`);
    if (s.ws_msgs_per_s > 20) f.push(`${tag} ${s.ws_msgs_per_s} websocket messages/s — batch updates to animation frames.`);
    for (const pl of (p.network_detail?.polling || []).filter((x) => x.count >= 20)) f.push(`${tag} ${pl.count} × ${pl.method} ${pl.endpoint} during interaction (${pl.bursts} bursts, ~${pl.per_burst} per burst${pl.every_ms ? ", every ~" + pl.every_ms + " ms" : ""}).`);
  }
  if (out.lighthouse?.score !== undefined && out.lighthouse?.score !== null && out.lighthouse.score < 50) f.push(`Lighthouse performance score ${out.lighthouse.score}.`);
  return f;
}

// ---------------------------------------------------------------- markdown
function fmt(v, unit = "") { return v === null || v === undefined ? "–" : `${v}${unit}`; }
const SUMMARY_ROWS = [
  ["fps_avg", "FPS avg (interaction)"], ["fps_p5", "FPS p5 (interaction)"], ["jank_pct", "% frames late (>20 ms)"], ["jank33_pct", "% frames >36 ms"], ["janky_time_pct", "% time in late frames"],
  ["frame_max_ms", "worst frame ms"], ["long_tasks", "long tasks (>50 ms)"], ["long_tasks_over_100ms", "long tasks >100 ms"], ["long_task_p95_ms", "long task p95 ms"],
  ["long_task_max_ms", "long task max ms"], ["tbt_ms", "TBT ms (interaction)"], ["main_busy_pct", "main thread busy %"], ["cores_used", "cores busy (all threads)"],
  ["scripting_ms", "scripting ms"], ["rendering_ms", "rendering ms"], ["painting_ms", "painting ms"], ["gc_ms", "GC ms"], ["gc_count", "GC count"],
  ["forced_layouts", "forced sync layouts"], ["inp_ms", "slowest interaction ms (INP-style)"], ["heap_start_mb", "heap after GC, start MB"],
  ["heap_end_mb", "heap after GC, end MB"], ["heap_growth_mb", "heap growth MB"], ["heap_peak_mb", "heap peak MB"], ["dom_nodes", "DOM nodes"],
  ["listeners", "event listeners"], ["requests", "requests (interaction)"], ["transfer_kb", "KB transferred (interaction)"], ["ws_msgs_per_s", "websocket msgs/s"],
  ["load_ms", "load event ms"], ["fcp_ms", "FCP ms"], ["lcp_ms", "LCP ms"], ["cls", "CLS"], ["load_tbt_ms", "TBT during load ms"],
];

function markdown(out) {
  const P = out.profiles;
  const L = [];
  L.push(`# Performance report`, "", `- URL: ${out.url}`, `- When: ${out.time}`, `- Profiles: ${P.map((p) => `${p.profile} (CPU ×${p.cpu_throttle}${p.network !== "none" ? ", " + p.network : ""})`).join(", ")}`,
    `- Scenario: ${out.scenario_source}; ${P[0]?.steps.length || 0} steps${P.some((p) => !p.steps_ok) ? " — **a step failed, numbers cover only the steps that ran**" : ""}`, "");
  if (out.findings.length) { L.push("## Findings", "", ...out.findings.map((x) => `- ${x}`), ""); }
  L.push("## Summary", "", `| metric | ${P.map((p) => p.profile).join(" | ")} |`, `|---|${P.map(() => "---:").join("|")}|`);
  for (const [k, label] of SUMMARY_ROWS) L.push(`| ${label} | ${P.map((p) => fmt(p.summary[k])).join(" | ")} |`);
  L.push("");
  const phaseNames = [...new Set(P.flatMap((p) => Object.keys(p.summary.phases || {})))];
  if (phaseNames.length) {
    L.push("### Phases", "", "| phase | profile | FPS avg | FPS p5 | % late | long tasks | max task ms | TBT ms | main busy % |", "|---|---|---:|---:|---:|---:|---:|---:|---:|");
    for (const n of phaseNames) for (const p of P) { const x = p.summary.phases[n]; if (x) L.push(`| ${n} | ${p.profile} | ${fmt(x.fps_avg)} | ${fmt(x.fps_p5)} | ${fmt(x.jank_pct)} | ${fmt(x.long_tasks)} | ${fmt(x.long_task_max_ms)} | ${fmt(x.tbt_ms)} | ${fmt(x.main_busy_pct)} |`); }
    L.push("");
  }
  for (const p of P) {
    const d = p.detail?.scenario;
    if (!d) continue;
    L.push(`## ${p.profile}: where the main thread goes during interaction`, "");
    L.push(`Window ${d.window_ms} ms; busy ${d.main_busy_pct}%. Breakdown (self time): ${Object.entries(d.breakdown_ms).map(([k, v]) => `${k} ${v} ms`).join(", ")}.`);
    if (p.detail.scripting_by_frame?.length) L.push(`Script time by document: ${p.detail.scripting_by_frame.map((x) => `${x.frame} ${x.ms} ms (${x.pct}%)`).join("; ")}.`);
    L.push("", "### Top functions by self time", "", "| # | function | where | frame | self ms | self % | total ms |", "|---:|---|---|---|---:|---:|---:|");
    (d.hot_functions || []).forEach((f, i) => L.push(`| ${i + 1} | \`${f.function}\` | ${f.where} | ${f.frame || ""} | ${f.self_ms} | ${f.self_pct} | ${f.total_ms} |`));
    if (d.hot_by_total?.length) {
      L.push("", "### Top call paths by total time (the function and everything it calls)", "", "| function | where | total ms | total % |", "|---|---|---:|---:|");
      d.hot_by_total.forEach((f) => L.push(`| \`${f.function}\` | ${f.where} | ${f.total_ms} | ${f.total_pct} |`));
    }
    if (d.top_long_tasks?.length) {
      L.push("", "### Longest tasks", "", "| at ms | duration | inside | hottest functions |", "|---:|---:|---|---|");
      for (const t of d.top_long_tasks.slice(0, 8)) {
        L.push(`| ${t.start_ms} | ${t.dur_ms} ms | ${(t.inside || []).map((x) => `${x.what} ${x.ms}ms${x.n > 1 ? " ×" + x.n : ""}`).join("; ")} | ${(t.hot || []).map((h) => `\`${h.function}\` ${h.where} ${h.self_ms}ms`).join("; ")} |`);
      }
    }
    const fl = d.forced_layouts;
    if (fl?.count) L.push("", `Forced synchronous layouts: ${fl.count} (${fl.total_ms} ms), thrashing tasks: ${fl.thrash_tasks}. ${fl.top.map((x) => `${x.function} ${x.where || ""} ×${x.count}`).join("; ")}`);
    L.push("", `GC: ${d.gc.count} (${d.gc.major} major), ${d.gc.total_ms} ms. Heap after GC ${fmt(p.summary.heap_start_mb)} → ${fmt(p.summary.heap_end_mb)} MB, peak ${fmt(p.summary.heap_peak_mb)} MB, trend ${fmt(p.summary.heap_slope_mb_per_min)} MB/min. DOM nodes ${fmt(p.summary.dom_nodes)} (${fmt(p.summary.dom_nodes_growth)} change), listeners ${fmt(p.summary.listeners)} (${fmt(p.summary.listeners_growth)} change).`);
    if (p.detail.threads?.length) {
      L.push("", "### Threads (busy during interaction)", "", "| process | thread | busy ms | busy % | CPU ms |", "|---|---|---:|---:|---:|");
      p.detail.threads.slice(0, 10).forEach((t) => L.push(`| ${t.process} | ${t.thread}${t.is_main ? " (main)" : ""} | ${t.busy_ms} | ${t.busy_pct} | ${fmt(t.cpu_ms)} |`));
      L.push("", `Cores busy on average: ${p.detail.cores_used}.`);
    }
    for (const w of p.detail.worker_hot || []) L.push("", `Worker ${w.thread} (${w.busy_ms} ms JS): ${w.top.map((f) => `\`${f.function}\` ${f.where} ${f.self_ms}ms`).join("; ")}`);
    if (p.detail.load) {
      const l = p.detail.load;
      L.push("", `### ${p.profile}: page load`, "", `Load window ${l.window_ms} ms, main busy ${l.main_busy_pct}%, ${l.long_tasks} long tasks (max ${l.long_task_max_ms} ms, TBT ${l.tbt_ms} ms).`);
      if (l.hot_functions?.length) L.push(`Hot during load: ${l.hot_functions.slice(0, 6).map((f) => `\`${f.function}\` ${f.where} ${f.self_ms}ms`).join("; ")}`);
    }
    const n = p.network_detail;
    L.push("", `### ${p.profile}: network`, "", `${n.requests_total} requests, ${n.transfer_kb_total} KB total; during interaction ${n.requests_during_scenario} requests, ${n.transfer_kb_during_scenario} KB; ${n.failed} failed/4xx+.`);
    if (n.polling.length) L.push(`Repeated requests during interaction: ${n.polling.map((x) => `${x.method} ${x.endpoint} ×${x.count} in ${x.bursts} burst(s)${x.every_ms ? ", a burst every ~" + x.every_ms + " ms" : ""}`).join("; ")}`);
    if (n.websockets.length) L.push(`Websockets: ${n.websockets.map((w) => `${w.url} ${w.msgs_per_s} msg/s ${w.kb_per_s} KB/s`).join("; ")}`);
    L.push(`Biggest: ${n.biggest.slice(0, 5).map((r) => `${r.url} ${r.kb} KB`).join("; ")}`, `Slowest: ${n.slowest.slice(0, 5).map((r) => `${r.url} ${r.ms} ms`).join("; ")}`);
    if (p.problems.length) L.push("", `Problems seen: ${p.problems.slice(0, 8).join(" | ")}`);
    L.push("");
  }
  const lh = out.lighthouse;
  if (lh) {
    L.push("## Lighthouse", "");
    if (lh.skipped || lh.error) L.push(lh.skipped || lh.error, "");
    else {
      L.push(`Preset ${lh.preset}, Lighthouse ${lh.lighthouse_version}. **Performance ${fmt(lh.score)}** · FCP ${fmt(lh.fcp_ms)} ms · LCP ${fmt(lh.lcp_ms)} ms · TBT ${fmt(lh.tbt_ms)} ms · CLS ${fmt(A.round(lh.cls, 3))} · SI ${fmt(lh.si_ms)} ms · TTI ${fmt(lh.tti_ms)} ms · max potential FID ${fmt(lh.max_potential_fid_ms)} ms`);
      if (lh.runtime_error) L.push(`Runtime error: ${lh.runtime_error}`);
      if (lh.mainthread_top.length) L.push("", `Main-thread work: ${lh.mainthread_top.map((x) => `${x.group} ${x.ms} ms`).join(", ")}`);
      if (lh.bootup_top.length) L.push(`JS execution: ${lh.bootup_top.map((x) => `${x.url} ${x.total_ms} ms`).join("; ")}`);
      if (lh.opportunities.length) L.push(`Opportunities: ${lh.opportunities.map((o) => `${o.title} (${o.savings_ms} ms${o.savings_kb ? ", " + o.savings_kb + " KB" : ""})`).join("; ")}`);
      if (lh.unused_js_top.length) L.push(`Unused JS: ${lh.unused_js_top.map((u) => `${u.url} ${u.wasted_kb}/${u.total_kb} KB`).join("; ")}`);
      L.push(`Diagnostics: ${lh.diagnostics.filter((d) => d.score !== null && d.score < 0.9).map((d) => `${d.title}${d.value ? " (" + d.value + ")" : ""}`).join("; ") || "all fine"}`, "");
    }
  }
  L.push("## Files", "", ...P.map((p) => `- ${p.profile}: ${p.files.trace} (Chrome DevTools > Performance > Load profile), ${p.files.cpuprofile || "no cpuprofile"}, ${p.files.screenshot}`));
  if (lh && lh.report_html) L.push(`- Lighthouse: ${lh.report_html}`);
  L.push("- Machine-readable: perf.json (`summary`), compare with `relay-perf compare before/perf.json after/perf.json`.", "");
  return L.join("\n");
}

// ---------------------------------------------------------------- compare / assert
// direction: -1 lower is better, +1 higher is better. rel = relative change that counts, abs = minimum absolute change.
const METRICS = {
  fps_avg: [1, 0.1, 3], fps_p5: [1, 0.2, 3], jank_pct: [-1, 0.25, 4], jank33_pct: [-1, 0.25, 3], janky_time_pct: [-1, 0.25, 8], frame_max_ms: [-1, 0.3, 50],
  long_tasks: [-1, 0.25, 3], long_tasks_over_100ms: [-1, 0.25, 2], long_task_p95_ms: [-1, 0.25, 40], long_task_max_ms: [-1, 0.3, 50],
  tbt_ms: [-1, 0.25, 200], main_busy_pct: [-1, 0.15, 8], cores_used: [-1, 0.2, 0.3], scripting_ms: [-1, 0.2, 300], rendering_ms: [-1, 0.25, 80],
  painting_ms: [-1, 0.25, 100], gc_ms: [-1, 0.3, 50], forced_layouts: [-1, 0.3, 5], inp_ms: [-1, 0.3, 50], heap_growth_mb: [-1, 0.5, 15],
  heap_end_mb: [-1, 0.2, 20], dom_nodes: [-1, 0.1, 200], listeners: [-1, 0.2, 50], requests: [-1, 0.25, 10], transfer_kb: [-1, 0.25, 300],
  ws_msgs_per_s: [-1, 0.25, 5], load_ms: [-1, 0.2, 300], lcp_ms: [-1, 0.2, 300], cls: [-1, 0.25, 0.05], load_tbt_ms: [-1, 0.25, 150],
  "lighthouse.score": [1, 0.08, 8], "lighthouse.tbt_ms": [-1, 0.25, 100], "lighthouse.lcp_ms": [-1, 0.2, 300], "lighthouse.cls": [-1, 0.25, 0.05],
  "lighthouse.bootup_ms": [-1, 0.2, 200], "lighthouse.mainthread_ms": [-1, 0.2, 300],
};
// The metrics a change is judged by, over the whole interaction (phase windows are short and noisy: they are
// reported and can be targets, but do not decide the verdict alone). Heap and request counts depend on live data;
// make them targets when the task is about memory or requests.
const KEY = new Set(["fps_avg", "fps_p5", "long_task_max_ms", "tbt_ms", "main_busy_pct", "scripting_ms", "inp_ms", "lighthouse.score", "lighthouse.tbt_ms"]);

function flatten(perf) {
  const out = {};
  for (const [prof, s] of Object.entries(perf.summary || {})) {
    if (prof === "lighthouse") { for (const [k, v] of Object.entries(s || {})) if (typeof v === "number") out[`lighthouse.${k}`] = v; continue; }
    for (const [k, v] of Object.entries(s || {})) {
      if (k === "phases") { for (const [ph, m] of Object.entries(v || {})) for (const [k2, v2] of Object.entries(m)) if (typeof v2 === "number") out[`${prof}.${ph}.${k2}`] = v2; }
      else if (typeof v === "number") out[`${prof}.${k}`] = v;
    }
  }
  return out;
}
function metricDef(key) { const base = key.startsWith("lighthouse.") ? key : key.split(".").pop(); return [METRICS[base], base]; }

function compare(before, after) {
  const b = flatten(before), a = flatten(after);
  const rows = [];
  for (const key of Object.keys({ ...b, ...a })) {
    const [def, base] = metricDef(key);
    if (!def || !(key in b) || !(key in a)) continue;
    const [dir, rel, abs] = def;
    const d = a[key] - b[key];
    const pct = b[key] ? (d / Math.abs(b[key])) * 100 : null;
    const big = Math.abs(d) >= abs && (b[key] === 0 || Math.abs(d) / Math.abs(b[key] || 1) >= rel);
    const verdict = !big ? "unchanged" : d * dir > 0 ? "improved" : "regressed";
    rows.push({ metric: key, before: b[key], after: a[key], delta: A.round(d, 2), pct: pct === null ? null : A.round(pct), verdict, key: KEY.has(base) && (key.startsWith("lighthouse.") || key.split(".").length === 2) });
  }
  const keyRows = rows.filter((r) => r.key);
  const regressed = keyRows.filter((r) => r.verdict === "regressed");
  const improved = keyRows.filter((r) => r.verdict === "improved");
  const verdict = regressed.length ? "regressed" : improved.length ? "improved" : "unchanged";
  const warnings = [];
  if (before.scenario_hash && after.scenario_hash && before.scenario_hash !== after.scenario_hash)
    warnings.push("the two runs used different scenarios (URL, steps, viewport or throttling differ): totals are not comparable");
  return { verdict, comparable: !warnings.length, warnings, improved: improved.map((r) => r.metric), regressed: regressed.map((r) => r.metric), rows,
           before: { url: before.url, time: before.time, label: before.label }, after: { url: after.url, time: after.time, label: after.label } };
}
function compareMarkdown(c) {
  const L = [`# Performance comparison: **${c.verdict}**`, "", `before: ${c.before.label || c.before.url} (${c.before.time})  →  after: ${c.after.label || c.after.url} (${c.after.time})`, ""];
  for (const w of c.warnings || []) L.push(`**Warning:** ${w}`);
  if (c.improved.length) L.push(`Improved: ${c.improved.join(", ")}`);
  if (c.regressed.length) L.push(`Regressed: ${c.regressed.join(", ")}`);
  L.push("", "| metric | before | after | Δ | Δ% | verdict |", "|---|---:|---:|---:|---:|---|");
  const order = { regressed: 0, improved: 1, unchanged: 2 };
  for (const r of [...c.rows].sort((x, y) => order[x.verdict] - order[y.verdict] || Number(y.key) - Number(x.key))) {
    L.push(`| ${r.key ? "**" + r.metric + "**" : r.metric} | ${r.before} | ${r.after} | ${r.delta > 0 ? "+" : ""}${r.delta} | ${r.pct === null ? "–" : (r.pct > 0 ? "+" : "") + r.pct + "%"} | ${r.verdict} |`);
  }
  return L.join("\n") + "\n";
}

// "4x.fps_p5>=45", "4x.pan.long_task_max_ms<100", with --baseline: "4x.tbt_ms<=-50%" (relative to the baseline value).
function assertAll(perf, exprs, baseline) {
  const now = flatten(perf), base = baseline ? flatten(baseline) : {};
  const results = [];
  for (const expr of exprs) {
    const m = String(expr).replace(/\s+/g, "").match(/^([\w.]+)(>=|<=|>|<|==)(-?[\d.]+)(%?)$/);
    if (!m) { results.push({ expr, ok: false, error: "cannot parse; use metric>=number or metric<=-50% with --baseline" }); continue; }
    const [, key, op, numS, pctS] = m;
    const v = now[key];
    if (v === undefined || v === null) { results.push({ expr, ok: false, error: `no value for ${key} (have: ${Object.keys(now).slice(0, 12).join(", ")}…)` }); continue; }
    let target = Number(numS);
    if (pctS) {
      if (!(key in base)) { results.push({ expr, ok: false, error: `relative target needs --baseline with ${key}` }); continue; }
      target = base[key] * (1 + target / 100);
    }
    const ok = { ">=": v >= target, "<=": v <= target, ">": v > target, "<": v < target, "==": v === target }[op];
    results.push({ expr, ok, value: v, target: A.round(target, 2), baseline: pctS ? base[key] : undefined });
  }
  return { ok: results.every((r) => r.ok), results };
}

// ---------------------------------------------------------------- serving the app under test
// --serve starts the app (dev server or preview) on a free port, waits until it answers, and stops the whole
// process group afterwards, so a measurement is one reproducible command.
async function freePort() {
  const net = require("net");
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => { const { port } = srv.address(); srv.close(() => resolve(port)); });
    srv.on("error", reject);
  });
}
async function startServer(cmdTemplate, timeoutMs) {
  const { spawn } = require("child_process");
  const port = await freePort();
  const cmd = cmdTemplate.replace(/\{port\}/g, String(port));
  process.stderr.write(`relay-perf: starting the app: ${cmd}\n`);
  const child = spawn("bash", ["-c", cmd], { detached: true, stdio: ["ignore", "pipe", "pipe"], env: { ...process.env, PORT: String(port), BROWSER: "none" } });
  let tail = "";
  const keep = (d) => { tail = (tail + d).slice(-3000); };
  child.stdout.on("data", keep); child.stderr.on("data", keep);
  let exited = null;
  child.on("exit", (code) => { exited = code; });
  const stop = () => { try { process.kill(-child.pid, "SIGTERM"); } catch {} setTimeout(() => { try { process.kill(-child.pid, "SIGKILL"); } catch {} }, 3000).unref(); };
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (exited !== null) throw new Error(`the serve command exited (${exited}) before the app answered:\n${tail.trim().split("\n").slice(-12).join("\n")}`);
    try { const r = await fetch(`http://127.0.0.1:${port}/`, { signal: AbortSignal.timeout(3000) }); if (r.status) return { port, stop }; } catch {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  stop();
  throw new Error(`the app did not answer on port ${port} within ${Math.round(timeoutMs / 1000)} s:\n${tail.trim().split("\n").slice(-12).join("\n")}`);
}

// ---------------------------------------------------------------- main
async function cmdRun() {
  let [url, stepsFile] = positional.slice(1);
  if (!url) usage();
  let server = null;
  if (arg("serve", "")) {
    server = await startServer(arg("serve"), Number(arg("serve-timeout", 240)) * 1000);
    process.on("exit", () => server.stop());
    url = url.replace(/\{port\}/g, String(server.port));
    if (url.startsWith("/")) url = `http://127.0.0.1:${server.port}${url}`;
  }
  const width = Number(arg("width", 1440)), height = Number(arg("height", 900));
  const steps = stepsFile ? JSON.parse(fs.readFileSync(stepsFile, "utf8")) : defaultSteps(width, height);
  const outDir = path.resolve(arg("out", "relay-perf-out"));
  fs.mkdirSync(outDir, { recursive: true });
  const rates = String(arg("throttle", "1,4")).split(",").map((x) => Number(String(x).replace(/x$/i, ""))).filter((x) => x >= 1);
  const repeat = Math.max(1, Number(arg("repeat", 1)));
  const { chromium } = require("playwright-core");
  const browser = await chromium.launch({ args: [...BROWSER_ARGS, "--enable-precise-memory-info"] });
  const out = { tool: "relay-perf", version: 1, url, label: arg("label", ""), time: new Date().toISOString(),
                scenario_source: stepsFile ? path.basename(stepsFile) : "default map scenario (pan ×8, zoom in/out, idle)",
                viewport: { width, height }, profiles: [], summary: {},
                scenario_hash: require("crypto").createHash("sha1").update(JSON.stringify({ url: url.replace(/:\/\/(127\.0\.0\.1|localhost):\d+/, "://local"), steps, width, height, rates })).digest("hex").slice(0, 12) };
  const harFile = arg("har", "") ? path.resolve(arg("har")) : "";
  // Only data from other origins is recorded/replayed by default: the app's own code must always be the code under test.
  let harUrl = arg("har-url", "") || undefined;
  if (harFile && !harUrl) {
    try { harUrl = new RegExp("^(?!" + new URL(url).origin.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")https?://"); } catch { harUrl = undefined; }
  }
  try {
    if (harFile && !fs.existsSync(harFile)) {
      // Record the data once (a pass that is not measured), so every profile and every later run sees the same data.
      process.stderr.write(`relay-perf: recording the scenario's network data to ${harFile}…\n`);
      const ctx = await browser.newContext({ viewport: { width, height }, ignoreHTTPSErrors: true });
      await ctx.routeFromHAR(harFile, { update: true, updateContent: "embed", url: harUrl });
      const pg = await ctx.newPage();
      try {
        await pg.goto(url, { waitUntil: "load", timeout: Number(arg("timeout", 120000)) });
        await pg.waitForTimeout(Number(arg("settle", 3000)));
        await runSteps(pg, steps, { outDir, profile: "record", phase: async () => {} });
      } catch (e) { process.stderr.write(`relay-perf: recording: ${e.message.split("\n")[0]}\n`); }
      await ctx.close();
      out.har = { file: harFile, recorded: true };
    } else if (harFile) out.har = { file: harFile, recorded: false };
    for (const rate of rates) {
      const runs = [];
      for (let k = 0; k < repeat; k++) {
        const label = `${rate}x${repeat > 1 ? "-" + (k + 1) : ""}`;
        process.stderr.write(`relay-perf: profiling ${label} (CPU ×${rate})…\n`);
        runs.push(await runProfile(browser, url, steps, { rate, outDir, label, width, height, settle: arg("settle", 3000),
                                                         network: arg("network", ""), timeout: arg("timeout", 120000), theme: arg("theme", "light"),
                                                         har: harFile && fs.existsSync(harFile) ? harFile : "", harUrl }));
      }
      // Several runs: keep the median run (by p5 FPS, then TBT) so one noisy run does not decide.
      runs.sort((x, y) => (x.summary.fps_p5 ?? 0) - (y.summary.fps_p5 ?? 0) || (y.summary.tbt_ms ?? 0) - (x.summary.tbt_ms ?? 0));
      const chosen = runs[Math.floor(runs.length / 2)];
      chosen.profile = `${rate}x`;
      if (repeat > 1) chosen.repeats = runs.map((r) => ({ fps_p5: r.summary.fps_p5, tbt_ms: r.summary.tbt_ms, long_task_max_ms: r.summary.long_task_max_ms }));
      out.profiles.push(chosen);
      out.summary[`${rate}x`] = chosen.summary;
    }
  } finally {
    await browser.close().catch(() => {});
  }
  if (!flag("no-lighthouse")) {
    process.stderr.write("relay-perf: running Lighthouse…\n");
    out.lighthouse = await runLighthouse(require("playwright-core").chromium, url, outDir, arg("lighthouse-preset", "desktop")).catch((e) => ({ error: e.message }));
    if (out.lighthouse && !out.lighthouse.skipped && !out.lighthouse.error) {
      out.summary.lighthouse = Object.fromEntries(["score", "fcp_ms", "lcp_ms", "tbt_ms", "cls", "si_ms", "tti_ms", "max_potential_fid_ms", "bootup_ms", "mainthread_ms"].map((k) => [k, out.lighthouse[k]]));
    }
  }
  out.findings = findings(out);
  fs.writeFileSync(path.join(outDir, "perf.json"), JSON.stringify(out, null, 1));
  fs.writeFileSync(path.join(outDir, "PERF_REPORT.md"), markdown(out));
  // Console: the short version; the details are in the files.
  print(JSON.stringify({ out: outDir, report: path.join(outDir, "PERF_REPORT.md"), summary: Object.fromEntries(Object.entries(out.summary).map(([k, v]) => [k, (() => { const { phases, ...rest } = v; return phases ? { ...rest, phases } : rest; })()])),
                               findings: out.findings, steps_ok: out.profiles.every((p) => p.steps_ok),
                               top_functions: (out.profiles[out.profiles.length - 1]?.detail?.scenario?.hot_functions || []).slice(0, 8).map((f) => `${f.self_pct}% ${f.function} ${f.where}`) }, null, 2));
  if (server) server.stop();
  process.exit(out.profiles.every((p) => p.steps_ok && !p.navigation_error) ? 0 : 1);
}

function readJson(f) { return JSON.parse(fs.readFileSync(f, "utf8")); }
// Synchronous: console.log to a pipe is asynchronous and process.exit() would cut a large report short.
function print(text) { fs.writeSync(1, text.endsWith("\n") ? text : text + "\n"); }

(async () => {
  const cmd = positional[0];
  if (cmd === "compare") {
    const [bf, af] = positional.slice(1);
    if (!bf || !af) usage();
    const c = compare(readJson(bf), readJson(af));
    if (arg("out", "")) fs.writeFileSync(arg("out"), JSON.stringify(c, null, 1));
    const md = compareMarkdown(c);
    if (arg("md", "")) fs.writeFileSync(arg("md"), md);
    print(flag("json") ? JSON.stringify(c, null, 1) : md);
    process.exit(c.verdict === "regressed" ? 1 : 0);
  } else if (cmd === "assert") {
    const file = positional[1];
    if (!file) usage();
    const r = assertAll(readJson(file), positional.slice(2), arg("baseline", "") ? readJson(arg("baseline")) : null);
    print(JSON.stringify(r, null, 1));
    process.exit(r.ok ? 0 : 1);
  } else if (cmd === "run") {
    await cmdRun();
  } else if (cmd && /^(https?:|file:|\/)/.test(cmd)) {
    positional.unshift("run");
    await cmdRun();
  } else usage();
})().catch((e) => { console.error(`relay-perf: ${e.stack || e.message}`); process.exit(1); });

module.exports = { compare, assertAll, flatten, markdown, findings };
