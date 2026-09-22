// relay-perf analysis: turn a Chrome trace (Tracing domain, with the V8 sampling profiler) plus in-page
// frame samples into numbers an agent can act on. Used by tools/perf.cjs; no browser needed here, so it can
// also re-analyse a saved trace:  node tools/perf_analyze.cjs trace.json.gz
"use strict";
const fs = require("fs");
const zlib = require("zlib");

const LONG_TASK_MS = 50;

// ---------------------------------------------------------------- event classification (DevTools-like)
const SCRIPTING = new Set([
  "FunctionCall", "EvaluateScript", "v8.compile", "v8.compileModule", "v8.evaluateModule", "v8.produceCache",
  "v8.produceModuleCache", "TimerFire", "TimerInstall", "TimerRemove", "EventDispatch", "FireAnimationFrame",
  "RequestAnimationFrame", "CancelAnimationFrame", "FireIdleCallback", "XHRReadyStateChange", "XHRLoad",
  "RunMicrotasks", "v8.run", "V8.Execute", "v8.callFunction", "v8.newInstance", "CompileScript", "CompileCode",
  "OptimizeCode", "CacheScript", "WebSocketReceiveHandshakeResponse", "ParseScriptOnBackground", "V8.DeserializeContext",
  "v8.parseOnBackground", "v8.deserializeOnBackground", "V8.CompileLazy", "BackgroundCompile", "StreamingCompileTask",
]);
const RENDERING = new Set([
  "Layout", "UpdateLayoutTree", "RecalculateStyles", "UpdateLayerTree", "UpdateLayer", "HitTest", "PrePaint",
  "Layerize", "ScheduleStyleRecalculation", "InvalidateLayout", "ParseAuthorStyleSheet", "ComputeIntersections",
  "IntersectionObserverController::computeIntersections", "LayoutShift", "ScrollLayer", "UpdateCounters",
]);
const PAINTING = new Set([
  "Paint", "PaintImage", "PaintSetup", "RasterTask", "Rasterize", "CompositeLayers", "Commit", "Decode Image",
  "ImageDecodeTask", "Decode LazyPixelRef", "Draw LazyPixelRef", "GPUTask", "Canvas2DLayerBridge::flush",
  "DrawFrame", "BeginMainThreadFrame", "ActivateLayerTree", "DrawLazyPixelRef", "ResizeImage",
]);
const LOADING = new Set(["ParseHTML", "ResourceSendRequest", "ResourceReceiveResponse", "ResourceFinish", "ResourceReceivedData", "ResourceWillSendRequest", "ResourceChangePriority"]);
const GC_RE = /^(MinorGC|MajorGC|V8\.GC|V8\.GC_|BlinkGC\.|CppGC\.|ThreadState::|V8\.CppGC|V8\.Scavenge|V8\.MarkCompact)/;

function category(name) {
  if (GC_RE.test(name)) return "gc";
  if (SCRIPTING.has(name) || /^(v8\.|V8\.)/.test(name)) return "scripting";
  if (RENDERING.has(name)) return "rendering";
  if (PAINTING.has(name)) return "painting";
  if (LOADING.has(name)) return "loading";
  return "other";
}
const SCRIPT_PARENT = new Set(["FunctionCall", "EvaluateScript", "TimerFire", "EventDispatch", "FireAnimationFrame",
  "FireIdleCallback", "RunMicrotasks", "XHRReadyStateChange", "XHRLoad", "v8.callFunction", "V8.Execute", "v8.run", "v8.evaluateModule"]);

// ---------------------------------------------------------------- helpers
const round = (x, d = 1) => (x === null || x === undefined || !Number.isFinite(x) ? null : Math.round(x * 10 ** d) / 10 ** d);
function percentile(sorted, p) {
  if (!sorted.length) return null;
  const i = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[i];
}
function median(xs) { const s = [...xs].sort((a, b) => a - b); return s.length ? s[Math.floor(s.length / 2)] : null; }
function shortUrl(u) {
  if (!u) return "";
  try { const x = new URL(u); return (x.pathname + (x.search.length > 1 && x.search.length < 40 ? x.search : "")).replace(/^\/@fs\//, "/"); }
  catch { return u.length > 90 ? "…" + u.slice(-88) : u; }
}
function loadTrace(file) {
  let buf = fs.readFileSync(file);
  if (file.endsWith(".gz")) buf = zlib.gunzipSync(buf);
  const data = JSON.parse(buf.toString("utf8"));
  return Array.isArray(data) ? data : data.traceEvents;
}

// ---------------------------------------------------------------- trace model
function buildModel(events) {
  const threads = new Map(); // key pid:tid -> {pid, tid, name, slices:[]}
  const procNames = new Map();
  const marks = [];
  const frames = new Map(); // frame id -> {url, pid, parent}
  const profiles = new Map(); // pid:id -> {pid, tid, startTime, nodes: Map, samples: [], timeDeltas: []}
  const open = new Map(); // B/E stacks per thread
  const profileOwner = new Map(); // pid:id -> profiled thread
  const thr = (pid, tid) => {
    const k = `${pid}:${tid}`;
    let t = threads.get(k);
    if (!t) { t = { key: k, pid, tid, name: "", slices: [] }; threads.set(k, t); }
    return t;
  };
  let minTs = Infinity;
  for (const e of events) {
    if (e.ph === "M") {
      if (e.name === "thread_name") thr(e.pid, e.tid).name = e.args?.name || "";
      else if (e.name === "process_name") procNames.set(e.pid, e.args?.name || "");
      continue;
    }
    if (typeof e.ts === "number" && e.ts > 0 && e.ph !== "P") minTs = Math.min(minTs, e.ts);
    if (e.name === "TracingStartedInBrowser" && e.args?.data?.frames) {
      for (const f of e.args.data.frames) frames.set(f.frame, { url: f.url, pid: f.processId, parent: f.parent || null });
    } else if ((e.name === "FrameCommittedInBrowser" || e.name === "CommitLoad") && e.args?.data?.frame) {
      const d = e.args.data;
      const prev = frames.get(d.frame) || {};
      frames.set(d.frame, { url: d.url || prev.url, pid: d.processId || prev.pid || e.pid, parent: d.parent || prev.parent || null });
    }
    if (e.cat && e.cat.includes("blink.user_timing") && typeof e.name === "string" && e.name.startsWith("relay:")) {
      marks.push({ name: e.name, ts: e.ts, pid: e.pid });
    }
    if (e.name === "Profile" && e.ph === "P") {
      profileOwner.set(`${e.pid}:${e.id}`, e.tid);
      profiles.set(`${e.pid}:${e.id}`, { pid: e.pid, tid: e.tid, startTime: e.args?.data?.startTime ?? e.ts, nodes: new Map(), samples: [], timeDeltas: [] });
      continue;
    }
    if (e.name === "ProfileChunk" && e.ph === "P") {
      const p = profiles.get(`${e.pid}:${e.id}`);
      if (!p) continue;
      const cp = e.args?.data?.cpuProfile || {};
      for (const n of cp.nodes || []) p.nodes.set(n.id, n);
      for (const s of cp.samples || []) p.samples.push(s);
      for (const d of e.args?.data?.timeDeltas || []) p.timeDeltas.push(d);
      continue;
    }
    if (e.ph === "X" && typeof e.dur === "number") {
      thr(e.pid, e.tid).slices.push({ name: e.name, ts: e.ts, dur: e.dur, tdur: e.tdur, args: e.args });
    } else if (e.ph === "B") {
      const k = `${e.pid}:${e.tid}`;
      if (!open.has(k)) open.set(k, []);
      open.get(k).push(e);
    } else if (e.ph === "E") {
      const st = open.get(`${e.pid}:${e.tid}`);
      if (st && st.length) {
        const b = st.pop();
        thr(e.pid, e.tid).slices.push({ name: b.name, ts: b.ts, dur: e.ts - b.ts, tdur: (e.tts && b.tts) ? e.tts - b.tts : undefined,
                                         args: { ...(b.args || {}), ...(e.args || {}) } });
      }
    }
  }
  for (const t of threads.values()) {
    t.process = procNames.get(t.pid) || "";
    t.slices.sort((a, b) => a.ts - b.ts || b.dur - a.dur);
    // Nesting: parent, depth, self time.
    const stack = [];
    for (const s of t.slices) {
      while (stack.length && stack[stack.length - 1].ts + stack[stack.length - 1].dur <= s.ts) stack.pop();
      const parent = stack[stack.length - 1] || null;
      s.parent = parent;
      s.depth = stack.length;
      s.self = s.dur;
      if (parent) parent.self -= Math.min(s.dur, parent.self);
      stack.push(s);
    }
  }
  return { threads, frames, marks, profiles, profileOwner, minTs };
}

// Profiles from the trace -> per-thread sample lists with timestamps.
function profileSamples(p) {
  let t = p.startTime;
  const out = [];
  for (let i = 0; i < p.samples.length; i++) {
    t += p.timeDeltas[i] || 0;
    out.push({ ts: t, node: p.samples[i] });
  }
  out.sort((a, b) => a.ts - b.ts);
  for (let i = 0; i < out.length; i++) out[i].dur = i + 1 < out.length ? Math.min(out[i + 1].ts - out[i].ts, 20000) : 0;
  return out;
}

// A Profiler-domain .cpuprofile -> the internal profile shape used for trace profiles.
function fromCpuProfile(cp, pid, tid) {
  const nodes = new Map();
  for (const n of cp.nodes || []) nodes.set(n.id, { id: n.id, callFrame: n.callFrame });
  for (const n of cp.nodes || []) for (const c of n.children || []) if (nodes.has(c)) nodes.get(c).parent = n.id;
  return { pid, tid, startTime: cp.startTime, nodes, samples: cp.samples || [], timeDeltas: cp.timeDeltas || [] };
}
// Trace events carrying a .cpuprofile, so DevTools draws it on that thread.
function profileTraceEvents(cp, pid, tid) {
  const id = "0x7e1a";
  const nodes = (cp.nodes || []).map((n) => ({ id: n.id, callFrame: n.callFrame }));
  const parent = new Map();
  for (const n of cp.nodes || []) for (const c of n.children || []) parent.set(c, n.id);
  for (const n of nodes) if (parent.has(n.id)) n.parent = parent.get(n.id);
  const out = [{ name: "Profile", ph: "P", cat: "disabled-by-default-v8.cpu_profiler", id, pid, tid, ts: cp.startTime, args: { data: { startTime: cp.startTime } } }];
  const CH = 20000;
  for (let i = 0; i < Math.max(1, (cp.samples || []).length); i += CH) {
    out.push({ name: "ProfileChunk", ph: "P", cat: "disabled-by-default-v8.cpu_profiler", id, pid, tid, ts: cp.startTime,
               args: { data: { cpuProfile: { nodes: i === 0 ? nodes : [], samples: (cp.samples || []).slice(i, i + CH) }, timeDeltas: (cp.timeDeltas || []).slice(i, i + CH) } } });
  }
  return out;
}

// DevTools-loadable .cpuprofile for one thread.
function toCpuProfile(p) {
  const nodes = [...p.nodes.values()].map((n) => ({ id: n.id, callFrame: n.callFrame, hitCount: 0, children: [] }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (const n of p.nodes.values()) if (n.parent !== undefined && byId.has(n.parent)) byId.get(n.parent).children.push(n.id);
  const samples = profileSamples(p);
  for (const s of samples) { const n = byId.get(s.node); if (n) n.hitCount++; }
  const deltas = samples.map((s, i) => (i === 0 ? s.ts - p.startTime : s.ts - samples[i - 1].ts));
  return { nodes, startTime: p.startTime, endTime: samples.length ? samples[samples.length - 1].ts : p.startTime,
           samples: samples.map((s) => s.node), timeDeltas: deltas };
}

const SPECIAL = new Set(["(root)", "(program)", "(idle)", "(garbage collector)"]);
function fnKey(cf) { return `${cf.functionName || "(anonymous)"}@${cf.url}:${cf.lineNumber}:${cf.columnNumber}`; }

// Self/total time per function in [a,b] for one profile.
function hotFunctions(p, a, b, samples) {
  samples = samples || profileSamples(p);
  const self = new Map(), total = new Map(), meta = new Map();
  let windowMs = 0, idle = 0, program = 0, gc = 0;
  for (const s of samples) {
    if (s.ts < a || s.ts > b || !s.dur) continue;
    const n = p.nodes.get(s.node);
    if (!n) continue;
    const ms = s.dur / 1000;
    windowMs += ms;
    const fn = n.callFrame.functionName;
    if (fn === "(idle)") { idle += ms; continue; }
    if (fn === "(program)") { program += ms; continue; }
    if (fn === "(garbage collector)") { gc += ms; continue; }
    const k = fnKey(n.callFrame);
    self.set(k, (self.get(k) || 0) + ms);
    meta.set(k, n.callFrame);
    const seen = new Set();
    for (let cur = n; cur; cur = cur.parent !== undefined ? p.nodes.get(cur.parent) : null) {
      if (SPECIAL.has(cur.callFrame.functionName)) continue;
      const ck = fnKey(cur.callFrame);
      if (seen.has(ck)) continue;
      seen.add(ck);
      meta.set(ck, cur.callFrame);
      total.set(ck, (total.get(ck) || 0) + ms);
    }
  }
  return { self, total, meta, windowMs, idle, program, gc };
}

function topFunctions(h, spanMs, n = 15) {
  return [...h.self.entries()].sort((x, y) => y[1] - x[1]).slice(0, n).map(([k, ms]) => {
    const cf = h.meta.get(k);
    return { function: cf.functionName || "(anonymous)", url: cf.url, line: cf.lineNumber + 1, column: cf.columnNumber + 1,
             self_ms: round(ms), total_ms: round(h.total.get(k) || ms), self_pct: round((ms / spanMs) * 100),
             total_pct: round(((h.total.get(k) || ms) / spanMs) * 100) };
  });
}
function topByTotal(h, spanMs, n = 10) {
  return [...h.total.entries()].sort((x, y) => y[1] - x[1]).filter(([k]) => !!h.meta.get(k)?.url).slice(0, n).map(([k, ms]) => {
    const cf = h.meta.get(k);
    return { function: cf.functionName || "(anonymous)", url: cf.url, line: cf.lineNumber + 1, column: cf.columnNumber + 1,
             total_ms: round(ms), total_pct: round((ms / spanMs) * 100), self_ms: round(h.self.get(k) || 0) };
  });
}

// Union of busy intervals of top-level slices in [a,b] (µs) -> ms, and thread CPU time where available.
function busy(t, a, b) {
  let busyUs = 0, cpuUs = 0, end = -Infinity;
  for (const s of t.slices) {
    if (s.depth !== 0) continue;
    const s0 = Math.max(s.ts, a), s1 = Math.min(s.ts + s.dur, b);
    if (s1 <= s0) continue;
    const from = Math.max(s0, end);
    if (s1 > from) busyUs += s1 - from;
    end = Math.max(end, s1);
    if (typeof s.tdur === "number") cpuUs += s.tdur * ((s1 - s0) / Math.max(1, s.dur));
  }
  return { busy_ms: busyUs / 1000, cpu_ms: cpuUs / 1000 };
}

function frameUrlFor(model, frameId) { return model.frames.get(frameId)?.url || ""; }

// Main thread(s): CrRendererMain of each process that hosts one of the page's frames.
function mainThreads(model, pageUrl) {
  const pids = new Set();
  for (const f of model.frames.values()) if (f.pid && (!pageUrl || f.url?.startsWith("http") || f.url?.startsWith("file"))) pids.add(f.pid);
  const mains = [...model.threads.values()].filter((t) => t.name === "CrRendererMain" && (pids.size === 0 || pids.has(t.pid)));
  mains.sort((x, y) => y.slices.length - x.slices.length);
  return mains;
}

function breakdown(t, a, b) {
  const out = { scripting: 0, rendering: 0, painting: 0, gc: 0, loading: 0, other: 0 };
  for (const s of t.slices) {
    if (s.ts < a || s.ts > b) continue;
    out[category(s.name)] += Math.max(0, s.self) / 1000;
  }
  for (const k of Object.keys(out)) out[k] = round(out[k]);
  return out;
}

function stackTop(s) {
  const st = s.args?.beginData?.stackTrace || s.args?.data?.stackTrace;
  if (Array.isArray(st) && st.length) {
    const f = st[0];
    return { function: f.functionName || "(anonymous)", url: f.url, line: (f.lineNumber ?? 0) + 1, column: (f.columnNumber ?? 0) + 1 };
  }
  return null;
}

function forcedLayouts(t, a, b) {
  const items = [];
  for (const s of t.slices) {
    if (s.ts < a || s.ts > b || (s.name !== "Layout" && s.name !== "UpdateLayoutTree")) continue;
    let p = s.parent, forced = false;
    while (p) { if (SCRIPT_PARENT.has(p.name)) { forced = true; break; } p = p.parent; }
    if (forced) items.push(s);
  }
  const by = new Map();
  for (const s of items) {
    const top = stackTop(s);
    const k = top ? `${top.function}@${top.url}:${top.line}` : "(no stack)";
    const e = by.get(k) || { where: top, count: 0, ms: 0 };
    e.count++; e.ms += s.dur / 1000;
    by.set(k, e);
  }
  // Layout thrash: several forced layouts inside one top-level task.
  const perTask = new Map();
  for (const s of items) { let r = s; while (r.parent) r = r.parent; perTask.set(r, (perTask.get(r) || 0) + 1); }
  return {
    count: items.length, total_ms: round(items.reduce((x, s) => x + s.dur, 0) / 1000),
    thrash_tasks: [...perTask.values()].filter((n) => n >= 3).length,
    top: [...by.values()].sort((x, y) => y.ms - x.ms).slice(0, 5).map((e) => ({ ...(e.where || { function: "(no stack)" }), count: e.count, ms: round(e.ms) })),
  };
}

function gcStats(t, a, b) {
  let count = 0, ms = 0, major = 0, minor = 0;
  for (const s of t.slices) {
    if (s.ts < a || s.ts > b) continue;
    if (s.name !== "MinorGC" && s.name !== "MajorGC" && s.name !== "V8.GC_MC_BACKGROUND_MARKING" && !/^V8\.GC(Scavenger|FinalizeMC|Compactor)$/.test(s.name)) continue;
    let p = s.parent, nested = false;
    while (p) { if (GC_RE.test(p.name)) { nested = true; break; } p = p.parent; }
    if (nested) continue;
    count++; ms += s.dur / 1000;
    if (s.name === "MajorGC" || s.name.includes("MC")) major++; else minor++;
  }
  return { count, total_ms: round(ms), major, minor };
}

// Long tasks with what ran inside them.
function longTasks(model, t, a, b, prof, samples) {
  const tasks = t.slices.filter((s) => s.depth === 0 && s.ts >= a && s.ts <= b && s.dur / 1000 > LONG_TASK_MS);
  const out = tasks.map((s) => ({ start_ms: round((s.ts - a) / 1000), dur_ms: round(s.dur / 1000), slice: s }));
  // Children lookup: slices whose root is the task.
  const children = new Map(out.map((o) => [o.slice, []]));
  for (const x of t.slices) {
    if (x.depth === 0 || x.ts < a || x.ts > b) continue;
    let r = x; while (r.parent) r = r.parent;
    if (children.has(r)) children.get(r).push(x);
  }
  for (const o of out) {
    const kids = children.get(o.slice) || [];
    const byKind = new Map();
    for (const k of kids) {
      if (k.depth > 3) continue;
      let label = k.name;
      const d = k.args?.data || k.args?.beginData || {};
      if (k.name === "FunctionCall" && (d.url || d.functionName)) label = `FunctionCall ${d.functionName || ""} ${shortUrl(d.url)}:${(d.lineNumber ?? 0) + 1}`.replace(/\s+/g, " ");
      else if (k.name === "EvaluateScript" && d.url) label = `EvaluateScript ${shortUrl(d.url)}`;
      else if (k.name === "TimerFire" || k.name === "FireAnimationFrame" || k.name === "EventDispatch") label = `${k.name}${d.type ? " " + d.type : ""}${d.frame ? " [" + shortUrl(frameUrlFor(model, d.frame)) + "]" : ""}`;
      else if (!["Layout", "UpdateLayoutTree", "Paint", "MinorGC", "MajorGC", "ParseHTML", "v8.compile", "RunMicrotasks", "XHRLoad", "XHRReadyStateChange", "PrePaint", "Layerize", "HitTest", "v8.parseOnBackground"].includes(k.name)) continue;
      const e = byKind.get(label) || { what: label, ms: 0, n: 0 };
      e.ms += k.dur / 1000; e.n++;
      byKind.set(label, e);
    }
    o.inside = [...byKind.values()].sort((x, y) => y.ms - x.ms).slice(0, 4).map((e) => ({ what: e.what, ms: round(e.ms), n: e.n }));
    if (prof) {
      const h = hotFunctions(prof, o.slice.ts, o.slice.ts + o.slice.dur, samples);
      o.hot = topFunctions(h, o.slice.dur / 1000, 3).map((f) => ({ function: f.function, url: f.url, line: f.line, column: f.column, self_ms: f.self_ms }));
      o.hot_total = topByTotal(h, o.slice.dur / 1000, 3).map((f) => ({ function: f.function, url: f.url, line: f.line, column: f.column, total_ms: f.total_ms }));
    }
    delete o.slice;
  }
  return out;
}

function longTaskStats(t, a, b) {
  const durs = t.slices.filter((s) => s.depth === 0 && s.ts >= a && s.ts <= b && s.dur / 1000 > LONG_TASK_MS).map((s) => s.dur / 1000).sort((x, y) => x - y);
  return {
    long_tasks: durs.length,
    long_task_total_ms: round(durs.reduce((x, y) => x + y, 0)),
    long_task_p95_ms: round(percentile(durs, 95)),
    long_task_max_ms: round(durs.length ? durs[durs.length - 1] : 0),
    long_tasks_over_100ms: durs.filter((d) => d > 100).length,
    tbt_ms: round(durs.reduce((x, d) => x + Math.max(0, d - LONG_TASK_MS), 0)),
  };
}

// Frames from requestAnimationFrame timestamps (epoch ms) inside [a,b] (epoch ms).
function fpsStats(times, a, b) {
  const ts = times.filter((x) => x >= a && x <= b).sort((x, y) => x - y);
  if (ts.length < 3) return { frames: ts.length, fps_avg: null, fps_p5: null, jank_pct: null, jank33_pct: null, janky_time_pct: null, frame_p95_ms: null, frame_max_ms: null };
  const iv = [];
  for (let i = 1; i < ts.length; i++) iv.push(ts[i] - ts[i - 1]);
  // Frames that never happened count too: the gap to the first frame and after the last one.
  const lead = ts[0] - a, tail = b - ts[ts.length - 1];
  if (lead > 34) iv.push(lead);
  if (tail > 34) iv.push(tail);
  const sorted = [...iv].sort((x, y) => x - y);
  const span = (b - a) / 1000;
  const totalMs = iv.reduce((x, y) => x + y, 0);
  // p5 FPS is time-weighted: the frame rate the user sees during the worst 5% of the interaction time.
  // (Counting frames would hide a 300 ms freeze behind hundreds of fast frames.)
  let acc = 0, worst5 = sorted[sorted.length - 1];
  for (let i = sorted.length - 1; i >= 0; i--) { acc += sorted[i]; if (acc >= totalMs * 0.05) { worst5 = sorted[i]; break; } }
  return {
    frames: ts.length,
    fps_avg: round(ts.length / span),
    fps_p5: round(Math.min(1000 / worst5, 1000 / median(iv))),
    // 16.7 ms is one vsync at 60 Hz; allow jitter so a frame that is merely on time is not "late".
    jank_pct: round((iv.filter((x) => x > 20).length / iv.length) * 100),
    jank33_pct: round((iv.filter((x) => x > 36).length / iv.length) * 100),
    janky_time_pct: round((iv.filter((x) => x > 20).reduce((x, y) => x + y, 0) / totalMs) * 100),
    frame_p95_ms: round(percentile(sorted, 95)), frame_max_ms: round(sorted[sorted.length - 1]),
  };
}

// ---------------------------------------------------------------- source maps (minimal VLQ decoder)
const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const B64MAP = Object.fromEntries([...B64].map((c, i) => [c, i]));
function decodeMappings(str) {
  const lines = [];
  let src = 0, oline = 0, ocol = 0, name = 0;
  for (const lineStr of str.split(";")) {
    const segs = [];
    let col = 0;
    if (lineStr) for (const seg of lineStr.split(",")) {
      const vals = [];
      let v = 0, shift = 0;
      for (const ch of seg) {
        const d = B64MAP[ch];
        if (d === undefined) break;
        v += (d & 31) << shift;
        if (d & 32) shift += 5;
        else { vals.push(v & 1 ? -(v >>> 1) : v >>> 1); v = 0; shift = 0; }
      }
      if (!vals.length) continue;
      col += vals[0];
      if (vals.length >= 4) {
        src += vals[1]; oline += vals[2]; ocol += vals[3];
        if (vals.length >= 5) { name += vals[4]; segs.push([col, src, oline, ocol, name]); } else segs.push([col, src, oline, ocol]);
      }
    }
    lines.push(segs);
  }
  return lines;
}
function makeSourceMapper(fetchText) {
  const cache = new Map();
  async function mapFor(url) {
    if (cache.has(url)) return cache.get(url);
    let m = null;
    try {
      const text = await fetchText(url);
      const all = [...String(text || "").matchAll(/[#@]\s*sourceMappingURL=([^\s'"]+)/g)];
      const ref = all.length ? all[all.length - 1][1] : null;
      if (ref) {
        let json;
        if (ref.startsWith("data:")) {
          const body = ref.slice(ref.indexOf(",") + 1);
          json = ref.includes(";base64") ? Buffer.from(body, "base64").toString("utf8") : decodeURIComponent(body);
        } else json = await fetchText(new URL(ref, url).href);
        const sm = JSON.parse(json);
        m = { sm, lines: decodeMappings(sm.mappings || "") };
      }
    } catch { m = null; }
    cache.set(url, m);
    return m;
  }
  return async function resolve(url, line1, col1) {
    if (!url || !/^https?:/.test(url)) return null;
    const m = await mapFor(url);
    if (!m) return null;
    const segs = m.lines[line1 - 1];
    if (!segs || !segs.length) return null;
    let best = segs[0];
    for (const s of segs) { if (s[0] <= col1 - 1) best = s; else break; }
    let source = m.sm.sources?.[best[1]] || "";
    if (m.sm.sourceRoot) source = m.sm.sourceRoot.replace(/\/?$/, "/") + source;
    // Minified names: the original name recorded at (or just after) the function's start.
    let name = null;
    for (const sg of segs) { if (sg[0] >= col1 - 1 && sg.length === 5) { name = m.sm.names?.[sg[4]] || null; break; } if (sg[0] > col1 + 40) break; }
    return { source: source.replace(/^(\.\.\/)+/, "").replace(/^\/@fs/, ""), line: best[2] + 1, column: best[3] + 1, name };
  };
}

// ---------------------------------------------------------------- full analysis
// windows: {load:[a,b], scenario:[a,b], phases:{name:[a,b]}} in trace µs.
function analyze(model, { windows, pageUrl, mainProfile }) {
  const mains = mainThreads(model, pageUrl);
  const main = mains[0];
  const profByThread = new Map();
  for (const p of model.profiles.values()) {
    const k = `${p.pid}:${p.tid}`;
    const cur = profByThread.get(k);
    if (!cur || p.samples.length > cur.samples.length) profByThread.set(k, p);
  }
  const res = { main_thread: main ? { pid: main.pid, tid: main.tid } : null, other_renderer_mains: mains.slice(1).map((t) => ({ pid: t.pid, tid: t.tid })) };
  if (!main) { res.error = "no renderer main thread found in the trace"; return res; }
  const prof = mainProfile || profByThread.get(main.key);
  const samples = prof ? profileSamples(prof) : [];
  const [sa, sb] = windows.scenario;
  const spanMs = (sb - sa) / 1000;

  // Scenario window (the interaction).
  const lt = longTaskStats(main, sa, sb);
  const bz = busy(main, sa, sb);
  res.scenario = {
    window_ms: round(spanMs), ...lt,
    main_busy_ms: round(bz.busy_ms), main_busy_pct: round((bz.busy_ms / spanMs) * 100),
    breakdown_ms: breakdown(main, sa, sb),
    forced_layouts: forcedLayouts(main, sa, sb),
    gc: gcStats(main, sa, sb),
  };
  if (prof) {
    const h = hotFunctions(prof, sa, sb, samples);
    res.scenario.hot_functions = topFunctions(h, spanMs, 15);
    res.scenario.hot_by_total = topByTotal(h, spanMs, 10);
    res.scenario.profile_ms = { sampled: round(h.windowMs), idle: round(h.idle), program: round(h.program), gc: round(h.gc) };
  }
  res.scenario.top_long_tasks = longTasks(model, main, sa, sb, prof, samples).sort((x, y) => y.dur_ms - x.dur_ms).slice(0, 10);

  // Load window (navigation until the scenario starts).
  if (windows.load) {
    const [la, lb] = windows.load;
    const lms = (lb - la) / 1000;
    res.load = { window_ms: round(lms), ...longTaskStats(main, la, lb), breakdown_ms: breakdown(main, la, lb) };
    const lbz = busy(main, la, lb);
    res.load.main_busy_pct = round((lbz.busy_ms / lms) * 100);
    if (prof) res.load.hot_functions = topFunctions(hotFunctions(prof, la, lb, samples), lms, 8);
    res.load.top_long_tasks = longTasks(model, main, la, lb, prof, samples).sort((x, y) => y.dur_ms - x.dur_ms).slice(0, 5);
  }

  // Phases.
  res.phases = {};
  for (const [name, [a, b]] of Object.entries(windows.phases || {})) {
    const ms = (b - a) / 1000;
    const pb = busy(main, a, b);
    res.phases[name] = { window_ms: round(ms), ...longTaskStats(main, a, b), main_busy_pct: round((pb.busy_ms / ms) * 100) };
    if (prof) res.phases[name].hot_functions = topFunctions(hotFunctions(prof, a, b, samples), ms, 5);
  }

  // Every thread with work in the scenario: who is using the cores.
  const threads = [];
  let cpuSum = 0;
  for (const t of model.threads.values()) {
    const b2 = busy(t, sa, sb);
    if (b2.busy_ms < 1) continue;
    cpuSum += b2.cpu_ms || b2.busy_ms;
    threads.push({ process: t.process, thread: t.name || `tid ${t.tid}`, pid: t.pid, tid: t.tid, busy_ms: round(b2.busy_ms),
                   busy_pct: round((b2.busy_ms / spanMs) * 100), cpu_ms: b2.cpu_ms ? round(b2.cpu_ms) : null,
                   is_main: t === main || mains.includes(t) });
  }
  threads.sort((x, y) => y.busy_ms - x.busy_ms);
  res.threads = threads.slice(0, 14);
  res.cores_used = round(cpuSum / spanMs, 2);

  // JS on other threads (workers): what they run.
  res.worker_hot = [];
  for (const t of model.threads.values()) {
    if (t === main || !/Worker|worker/.test(t.name) || !profByThread.has(t.key)) continue;
    const p = profByThread.get(t.key);
    const h = hotFunctions(p, sa, sb);
    if (h.windowMs - h.idle < 5) continue;
    res.worker_hot.push({ thread: t.name, pid: t.pid, tid: t.tid, busy_ms: round(h.windowMs - h.idle - h.program), top: topFunctions(h, spanMs, 5) });
  }

  // Scripting attributed to frames (page vs iframe), from top-level script entries.
  const byFrame = new Map();
  for (const s of main.slices) {
    if (s.ts < sa || s.ts > sb || !SCRIPT_PARENT.has(s.name)) continue;
    let p = s.parent, nested = false;
    while (p) { if (SCRIPT_PARENT.has(p.name)) { nested = true; break; } p = p.parent; }
    if (nested) continue;
    const f = s.args?.data?.frame;
    const url = shortUrl(frameUrlFor(model, f)) || "(unknown frame)";
    byFrame.set(url, (byFrame.get(url) || 0) + s.dur / 1000);
  }
  res.scripting_by_frame = [...byFrame.entries()].sort((x, y) => y[1] - x[1]).slice(0, 6).map(([frame, ms]) => ({ frame, ms: round(ms), pct: round((ms / spanMs) * 100) }));
  res.frames = [...model.frames.entries()].map(([id, f]) => ({ id, url: shortUrl(f.url), pid: f.pid, parent: f.parent })).slice(0, 12);
  return { res, prof, main };
}

module.exports = {
  loadTrace, buildModel, analyze, toCpuProfile, fromCpuProfile, profileTraceEvents, mainThreads, fpsStats, makeSourceMapper, round, percentile, median, shortUrl,
  profileSamples, hotFunctions, topFunctions, category, decodeMappings, LONG_TASK_MS,
};

if (require.main === module) {
  const file = process.argv[2];
  if (!file) { console.error("usage: node perf_analyze.cjs trace.json[.gz]"); process.exit(2); }
  const model = buildModel(loadTrace(file));
  const ts = model.marks.filter((m) => m.name === "relay:scenario:start")[0]?.ts;
  const te = model.marks.filter((m) => m.name === "relay:scenario:end")[0]?.ts;
  let a = ts, b = te;
  if (!a || !b) {
    let lo = Infinity, hi = 0;
    for (const t of model.threads.values()) for (const s of t.slices) { lo = Math.min(lo, s.ts); hi = Math.max(hi, s.ts + s.dur); }
    a = lo; b = hi;
  }
  const { res } = analyze(model, { windows: { scenario: [a, b] } });
  console.log(JSON.stringify(res, null, 2));
}
