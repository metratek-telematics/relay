// Dashboard: KPIs, 14-day insights, agent health, usage, recent activity, open PRs.
import { $, $$, esc, icon, fmtDur, fmtNum, fmtCost, timeAgo, toast, basename } from "../ui.js";
import { S, agentLabel, agentInitial, statusOf, navigate, LIVE } from "../state.js";
import { api } from "../api.js";
import { openNewTask } from "./newtask.js";
import { agentHealthRow } from "./agents.js";

const OUTCOMES = [["done", "Delivered"], ["failed", "Failed"], ["stopped", "Stopped"]];
const AGENT_ORDER = ["codex", "claude", "gemini"];
const SVG_NS = 'xmlns="http://www.w3.org/2000/svg"';

// ---------------------------------------------------------------------------- chart helpers
const dayOf = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d); };
const dayLong = (iso) => dayOf(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
const niceMax = (v) => { if (v <= 0) return 1; const p = 10 ** Math.floor(Math.log10(v)); return [1, 2, 2.5, 5, 10].map((m) => m * p).find((m) => m >= v); };
const money = (n) => (n >= 100 ? `$${Math.round(n)}` : n >= 10 ? `$${n.toFixed(0)}` : `$${n.toFixed(n >= 1 ? 1 : 2)}`);
const pct = (n) => `${Math.round(n * 100)}%`;

// A column whose top corners are rounded and whose base sits square on the axis.
function column(x, y, w, h, rounded) {
  if (h <= 0) return "";
  const r = rounded ? Math.min(4, w / 2, h) : 0;
  return `M${x},${y + h}V${y + r}${r ? `Q${x},${y} ${x + r},${y}` : ""}H${x + w - r}${r ? `Q${x + w},${y} ${x + w},${y + r}` : ""}V${y + h}Z`;
}

// Stacked daily columns. `stacks` lists the segments bottom-up; every day gets a
// full-height, focusable hit band so a zero day still answers on hover.
function stackedColumns(width, days, stacks, fmtAxis, label) {
  const H = 170, top = 10, bottom = 22, left = 38, right = 6;
  const plotW = Math.max(60, width - left - right), plotH = H - top - bottom;
  const totals = days.map((d) => stacks.reduce((a, s) => a + (s.value(d) || 0), 0));
  // Formatting rounds (whole counts, cents): widen the scale until the half and full gridlines read differently.
  let max = niceMax(Math.max(...totals));
  while (fmtAxis(max / 2) === fmtAxis(max) && max < 1e9) max *= 2;
  const band = plotW / days.length;
  const bw = Math.max(4, Math.min(24, band * 0.62));
  const every = band < 22 ? 3 : band < 34 ? 2 : 1;
  const y = (v) => top + plotH - (v / max) * plotH;
  const grid = [0, 0.5, 1].map((f) => `<line class="ch-grid" x1="${left}" x2="${left + plotW}" y1="${y(max * f)}" y2="${y(max * f)}"/><text class="ch-axis" x="${left - 6}" y="${y(max * f) + 3.5}" text-anchor="end">${esc(fmtAxis(max * f))}</text>`).join("");
  const cols = days.map((d, i) => {
    const x = left + i * band + (band - bw) / 2;
    let base = 0;
    const visible = stacks.filter((s) => (s.value(d) || 0) > 0);
    const segs = visible.map((s, j) => {
      const v = s.value(d);
      const y1 = y(base + v), y0 = y(base);
      base += v;
      // A 2px surface gap between segments; the topmost carries the rounded end.
      const gap = j ? 2 : 0;
      return `<path class="${s.cls}" d="${column(x, y1, bw, Math.max(0, y0 - y1 - gap), j === visible.length - 1)}"/>`;
    }).join("");
    const tick = (days.length - 1 - i) % every === 0 ? `<text class="ch-axis" x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${dayOf(d.date).getDate()}</text>` : "";
    return `<g class="ch-col">${segs}${tick}<rect class="ch-hit" x="${left + i * band}" y="${top}" width="${band}" height="${plotH}" data-i="${i}" tabindex="0" aria-label="${esc(label(d))}"/></g>`;
  }).join("");
  return `<svg ${SVG_NS} class="chart-svg" width="${width}" height="${H}" viewBox="0 0 ${width} ${H}" role="img">${grid}${cols}<line class="ch-base" x1="${left}" x2="${left + plotW}" y1="${top + plotH}" y2="${top + plotH}"/></svg>`;
}

// One daily series as a 2px line with end dots; days without data break the line.
function trendLine(width, days, value, max, fmtAxis, label) {
  const H = 96, top = 8, bottom = 8, left = 38, right = 8;
  const plotW = Math.max(60, width - left - right), plotH = H - top - bottom;
  const band = plotW / days.length;
  const x = (i) => left + band * i + band / 2;
  const y = (v) => top + plotH - (v / max) * plotH;
  let path = "", pen = false;
  days.forEach((d, i) => {
    const v = value(d);
    if (v === null || v === undefined) { pen = false; return; }
    path += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  const dots = days.map((d, i) => { const v = value(d); return v === null || v === undefined ? "" : `<circle class="ch-dot" cx="${x(i)}" cy="${y(v)}" r="3.5"/>`; }).join("");
  const grid = [0, 1].map((f) => `<line class="ch-grid" x1="${left}" x2="${left + plotW}" y1="${y(max * f)}" y2="${y(max * f)}"/><text class="ch-axis" x="${left - 6}" y="${y(max * f) + 3.5}" text-anchor="end">${esc(fmtAxis(max * f))}</text>`).join("");
  const hits = days.map((d, i) => `<rect class="ch-hit" x="${left + i * band}" y="0" width="${band}" height="${H}" data-i="${i}" tabindex="0" aria-label="${esc(label(d))}"/>`).join("");
  return `<svg ${SVG_NS} class="chart-svg" width="${width}" height="${H}" viewBox="0 0 ${width} ${H}" role="img">${grid}<path class="ch-line" d="${path}"/>${dots}${hits}</svg>`;
}

// Charts are drawn at the card's real pixel width so text stays 11px on a phone
// instead of shrinking with a scaled viewBox; a resize redraws them.
function mountChart(host, draw, tip) {
  const paint = () => {
    const w = Math.floor(host.clientWidth);
    if (!w || w === host._w) return;
    host._w = w;
    host.innerHTML = draw(w) + '<div class="chart-tip" hidden></div>';
  };
  const show = (hit) => {
    const box = $(".chart-tip", host);
    if (!box || !hit) return;
    box.innerHTML = tip(Number(hit.dataset.i));
    box.hidden = false;
    const hr = hit.getBoundingClientRect(), cr = host.getBoundingClientRect();
    const left = Math.max(0, Math.min(host.clientWidth - box.offsetWidth, hr.left - cr.left + hr.width / 2 - box.offsetWidth / 2));
    box.style.left = `${left}px`;
    box.style.top = `${Math.max(0, hr.top - cr.top - box.offsetHeight - 6)}px`;
    $$(".ch-hit.on", host).forEach((h) => h.classList.remove("on"));
    hit.classList.add("on");
  };
  const hide = () => { const box = $(".chart-tip", host); if (box) box.hidden = true; $$(".ch-hit.on", host).forEach((h) => h.classList.remove("on")); };
  host.addEventListener("pointermove", (e) => { const hit = e.target.closest?.(".ch-hit"); hit ? show(hit) : hide(); });
  host.addEventListener("pointerleave", hide);
  host.addEventListener("focusin", (e) => show(e.target.closest?.(".ch-hit")));
  host.addEventListener("focusout", hide);
  const ro = new ResizeObserver(paint);
  ro.observe(host);
  paint();
  return ro;
}

function delta(cur, prev, { better, fmt }) {
  if (cur === null || cur === undefined || prev === null || prev === undefined) return '<span class="delta muted">no earlier data to compare</span>';
  const diff = cur - prev;
  if (Math.abs(diff) < 1e-9) return '<span class="delta muted">same as the 7 days before</span>';
  const good = better === "up" ? diff > 0 : diff < 0;
  return `<span class="delta ${good ? "good" : "bad"}">${diff > 0 ? "▲" : "▼"} ${esc(fmt(Math.abs(diff)))}</span><span class="muted"> vs the 7 days before</span>`;
}

export function mountDashboard(main) {
  main.innerHTML = `<div class="page" id="dash"><div class="empty small">Loading…</div></div>`;
  let alive = true;
  let observers = [];
  async function render() {
    let d;
    try { d = await api.dashboard(); } catch (e) { if (alive) $("#dash", main).innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (!alive) return;
    const bs = d.by_status || {};
    const attention = (bs.needs_input || 0) + (bs.paused || 0) + (bs.interrupted || 0);
    const running = Object.entries(bs).filter(([k]) => LIVE.has(k)).reduce((a, [, v]) => a + v, 0);
    const agentsTot = d.agents || {};
    const maxTok = Math.max(1, ...Object.values(agentsTot).map((a) => (a.input || 0) + (a.output || 0)));
    const health = S.agents || {};
    const tasks = [...S.tasks.values()].filter((t) => !t.archived);
    const attn = tasks.filter((t) => statusOf(t).attention).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    const empty = !d.total;
    const ins = d.insights || { days: [], top_repos: [] };
    const days = ins.days || [];
    const finished14 = days.reduce((a, x) => a + x.done + x.failed + x.stopped, 0);
    const cost14 = days.reduce((a, x) => a + (x.cost_usd || 0), 0);
    const agentsUsed = [...AGENT_ORDER, ...Object.keys(agentsTot).filter((a) => !AGENT_ORDER.includes(a))].filter((a) => days.some((x) => (x.cost_by_agent || {})[a]));
    const cur = ins.current || {}, prev = ins.previous || {};
    const scroll = $("#dash", main).scrollTop;
    observers.forEach((o) => o.disconnect());
    observers = [];

    const legend = (items) => `<div class="chart-legend">${items.map(([cls, l]) => `<span><i class="sw ${cls}"></i>${esc(l)}</span>`).join("")}</div>`;
    const readyAgents = AGENT_ORDER.filter((a) => health[a]?.ok);
    const prompts = (S.config.saved_prompts || []).slice(0, 4);

    const welcome = `<div class="card welcome"><div class="card-body">
        <div class="welcome-head"><span class="welcome-ic">${icon("sparkles", "lg")}</span><div><h2>Set up your first task</h2><p class="hint">Relay pairs two coding agents on one change: one plans and checks, the other writes the code, in an isolated branch of your repository. Charts of outcomes and spend appear here once tasks finish.</p></div></div>
        <ol class="setup-steps">
          <li class="${readyAgents.length >= 2 ? "done" : ""}"><span class="step-ic">${icon(readyAgents.length >= 2 ? "check" : "bot")}</span><div><strong>Sign in to at least two agents</strong><span>${readyAgents.length ? `${esc(readyAgents.map(agentLabel).join(" and "))} ${readyAgents.length === 1 ? "is" : "are"} ready.` : "None are ready yet."} Check them on the Agents page.</span></div><button class="btn sm" data-go="#/agents">${icon("bot")}Agents</button></li>
          <li><span class="step-ic">${icon("folder")}</span><div><strong>Describe a change in a repository</strong><span>Pick a local git folder or clone one from GitHub, then write what you want once.</span></div><button class="btn sm primary" data-new>${icon("plus")}New task</button></li>
          <li class="${S.github?.login ? "done" : ""}"><span class="step-ic">${icon(S.github?.login ? "check" : "github")}</span><div><strong>Optional: watch GitHub issues</strong><span>${S.github?.login ? `Signed in as @${esc(S.github.login)}.` : "Issues assigned to you or labelled for the agents can be queued automatically."}</span></div><button class="btn sm" data-go="#/github">${icon("github")}GitHub inbox</button></li>
        </ol>
        ${prompts.length ? `<div class="field-label" style="margin-top:14px">Or start from a saved prompt</div><div class="templ">${prompts.map((p) => `<button type="button" class="chip prompt-chip" data-prompt="${esc(p.id)}" title="${esc(p.text.slice(0, 300))}">${icon("message", "sm")}<span class="truncate">${esc(p.name)}</span></button>`).join("")}</div>` : ""}
      </div></div>`;

    const insights = empty ? "" : `<div class="insights">
        <div class="card chart-card"><div class="card-head"><div><h3>Finished per day</h3><p class="card-sub">Last 14 days · ${finished14} finished</p></div>${legend(OUTCOMES.map(([k, l]) => [`ch-${k}`, l]))}</div>
          <div class="card-body">${finished14 ? '<div class="chart" id="chOutcomes"></div>' : '<div class="chart-empty">No tasks finished in the last 14 days.</div>'}
          ${finished14 ? `<details class="chart-table"><summary>Show as a table</summary><div class="md-table"><table><thead><tr><th>Day</th><th>Delivered</th><th>Failed</th><th>Stopped</th><th>Success</th><th>Median time</th><th>Spend</th></tr></thead><tbody>${days.slice().reverse().map((x) => `<tr><td>${esc(dayLong(x.date))}</td><td>${x.done}</td><td>${x.failed}</td><td>${x.stopped}</td><td>${x.success_rate === null ? "—" : pct(x.success_rate)}</td><td>${x.median_duration ? fmtDur(x.median_duration) : "—"}</td><td>${x.cost_usd ? fmtCost(x.cost_usd) : "—"}</td></tr>`).join("")}</tbody></table></div></details>` : ""}</div></div>
        <div class="card chart-card" title="Claude reports real cost; Codex and Gemini are estimated at API-equivalent rates."><div class="card-head"><div><h3>Spend per day</h3><p class="card-sub">Last 14 days · ${fmtCost(cost14, d.cost_estimated)}${d.cost_estimated ? " est." : ""}</p></div>${legend(agentsUsed.map((a) => [`ch-agent ${esc(a)}`, agentLabel(a)]))}</div>
          <div class="card-body">${cost14 ? '<div class="chart" id="chCost"></div>' : '<div class="chart-empty">No agent spend recorded in the last 14 days.</div>'}</div></div>
        <div class="card"><div class="card-head"><div><h3>Trends</h3><p class="card-sub">Last 7 days against the 7 before · lines show 14 days</p></div></div><div class="card-body trend-grid">
          <div class="trend"><div class="trend-head"><span class="lbl">Success rate</span><b>${cur.success_rate === null || cur.success_rate === undefined ? "—" : pct(cur.success_rate)}</b></div>
            <div class="trend-sub">${delta(cur.success_rate, prev.success_rate, { better: "up", fmt: (v) => `${Math.round(v * 100)} pts` })}</div>
            ${finished14 ? '<div class="chart" id="chRate"></div>' : ""}</div>
          <div class="trend"><div class="trend-head"><span class="lbl">Median time to deliver</span><b>${cur.median_duration ? fmtDur(cur.median_duration) : "—"}</b></div>
            <div class="trend-sub">${delta(cur.median_duration, prev.median_duration, { better: "down", fmt: fmtDur })}</div>
            ${days.some((x) => x.median_duration) ? '<div class="chart" id="chDuration"></div>' : ""}</div>
        </div></div>
        <div class="card"><div class="card-head"><div><h3>Top repositories</h3><p class="card-sub">Tasks active in the last 14 days</p></div></div><div class="card-body repo-bars">
          ${(ins.top_repos || []).length ? (() => { const top = Math.max(...ins.top_repos.map((r) => r.tasks)); return ins.top_repos.map((r) => `<div class="repo-bar" title="${esc(r.path || r.repo)}">
            <span class="name truncate">${esc(r.repo.includes("/") && !r.repo.startsWith("/") ? r.repo : basename(r.repo))}</span>
            <span class="track" aria-hidden="true"><i class="ch-done" style="width:${(r.done / top) * 100}%"></i><i class="ch-failed" style="width:${(r.failed / top) * 100}%"></i><i class="ch-other" style="width:${((r.tasks - r.done - r.failed) / top) * 100}%"></i></span>
            <span class="v">${r.tasks} <span class="muted">· ${r.done} delivered${r.failed ? ` · ${r.failed} failed or stopped` : ""}</span></span></div>`).join(""); })() : '<div class="chart-empty">No tasks in the last 14 days.</div>'}
        </div></div>
      </div>`;

    $("#dash", main).innerHTML = `
      <div class="page-head"><div><h1>Overview</h1><p>Your multi-agent engineering desk · ${esc(new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }))}</p></div>
        <div class="page-actions"><button class="btn" id="dRun">${icon("play")}${d.queue?.running ? "Queue running" : "Run queue"}</button><button class="btn primary" id="dNew">${icon("plus")}New task</button></div></div>
      <div class="kpis">
        <div class="kpi"><div class="lbl">${icon("tasks", "sm")}Tasks</div><div class="val">${d.total}</div><div class="sub">${bs.queued || 0} queued · ${bs.draft || 0} drafts</div></div>
        <div class="kpi"><div class="lbl">${icon("activity", "sm")}Running</div><div class="val">${running}</div><div class="sub">parallel limit ${d.queue?.max_parallel || 1}</div></div>
        <div class="kpi" style="${attention ? "border-color:color-mix(in srgb,var(--amber) 50%,var(--border))" : ""}"><div class="lbl">${icon("bell", "sm")}Needs attention</div><div class="val" style="${attention ? "color:var(--amber)" : ""}">${attention}</div><div class="sub">questions, approvals, paused</div></div>
        <div class="kpi"><div class="lbl">${icon("check", "sm")}Delivered</div><div class="val">${bs.done || 0}</div><div class="sub">${d.success_rate !== null && d.success_rate !== undefined ? `${Math.round(d.success_rate * 100)}% success` : "no finished tasks yet"}</div></div>
        <div class="kpi"><div class="lbl">${icon("clock", "sm")}Median duration</div><div class="val">${d.median_duration ? fmtDur(d.median_duration) : "—"}</div><div class="sub">${d.avg_duration ? `per delivered task · mean ${fmtDur(d.avg_duration)}` : "per delivered task"}</div></div>
        <div class="kpi" title="Claude reports real cost; Codex and Gemini are estimated at API-equivalent rates. Subscription usage is not billed per token."><div class="lbl">${icon("dollar", "sm")}Spend${d.cost_estimated ? " (est.)" : ""}</div><div class="val">${fmtCost(d.total_cost_usd, d.cost_estimated)}</div><div class="sub">${d.total_turns} agent turns · API-equivalent</div></div>
      </div>
      ${empty ? welcome : insights}
      <div class="dash-grid ${empty ? "solo" : ""}">
        <div class="stack" style="gap:16px">
          ${attn.length ? `<div class="card"><div class="card-head"><h3>Needs your attention</h3><span class="badge amber">${attn.length}</span></div><div class="card-body stack">${attn.slice(0, 6).map((t) => `<a class="attn-row" href="#/task/${esc(t.id)}"><span class="dot warn"></span><strong class="truncate">${esc(t.name)}</strong><span class="muted truncate">${esc(t.pending?.question || t.detail || statusOf(t).label)}</span></a>`).join("")}</div></div>` : ""}
          ${empty ? "" : `<div class="card"><div class="card-head"><h3>Recent activity</h3><a href="#/tasks" class="muted" style="font-size:12px">All tasks</a></div><div class="card-body feed">
            ${(d.recent || []).length ? d.recent.map((e) => `<div class="feed-item"><span class="av sm ${esc(e.role === "user" ? "user" : (S.tasks.get(e.task_id)?.workflow?.roles?.[e.role]?.agent || "system"))}">${esc(agentInitial(S.tasks.get(e.task_id)?.workflow?.roles?.[e.role]?.agent || e.role))}</span><div><a href="#/task/${esc(e.task_id)}">${esc(e.task)}</a> · ${esc(e.title)}${e.detail ? `<p>${esc(e.detail)}</p>` : ""}</div><span class="t">${timeAgo(e.time)}</span></div>`).join("") : '<div class="empty small">Nothing yet.</div>'}
          </div></div>
          <div class="card"><div class="card-head"><h3>Quick start</h3></div><div class="card-body quick">
            <button id="q1"><strong>${icon("plus")}New task</strong><span>Pick a repository, describe the change, choose who supervises whom.</span></button>
            <button id="q2"><strong>${icon("bot")}Test agents</strong><span>Check Codex, Claude and Gemini are signed in and streaming correctly.</span></button>
            <button id="q3"><strong>${icon("github")}Connect GitHub inbox</strong><span>Auto-queue issues assigned to you or labelled for the agents.</span></button>
            <button id="q4"><strong>${icon("settings")}Workflow defaults</strong><span>Presets, models, verification, approval gates.</span></button>
          </div></div>`}
        </div>
        <div class="stack" style="gap:16px">
          <div class="card"><div class="card-head"><h3>Agents</h3><a href="#/agents" class="muted" style="font-size:12px">Manage</a></div><div class="card-body agent-health">${AGENT_ORDER.map((a) => agentHealthRow(a, health[a], { compact: true })).join("")}
            <div class="row" style="font-size:11px;color:var(--text-3);gap:12px;margin-top:4px"><span>${icon(health.git?.ok ? "check" : "x", "sm")} git</span><span>${icon(health.gh?.ok ? "check" : "x", "sm")} gh ${S.github?.login ? `@${esc(S.github.login)}` : ""}</span></div>
          </div></div>
          ${empty ? "" : `<div class="card"><div class="card-head"><h3>Usage by agent</h3></div><div class="card-body bars">
            ${Object.keys(agentsTot).length ? Object.entries(agentsTot).map(([a, v]) => `<div class="bar-row"><span class="row"><span class="av sm ${esc(a)}">${esc(agentInitial(a))}</span>${esc(agentLabel(a))}</span><div class="track"><div class="fill" style="width:${Math.round(((v.input || 0) + (v.output || 0)) / maxTok * 100)}%;background:${esc((S.agentMeta[a] || {}).color || "var(--accent)")}"></div></div><span class="v">${fmtNum((v.input || 0) + (v.output || 0))} · ${fmtCost(v.cost_usd, v.estimated)}</span></div>`).join("") : '<div class="empty small">No agent turns recorded yet.</div>'}
          </div></div>
          <div class="card"><div class="card-head"><h3>Open pull requests</h3></div><div class="card-body stack">
            ${(d.prs || []).length ? d.prs.map((p) => `<div class="pr-row"><a href="${esc(p.url)}" target="_blank" rel="noopener" class="truncate" title="${esc(p.name)}">${icon("external", "sm")} #${esc(p.number || "")} ${esc(p.name)}</a><span class="muted mono truncate">${esc(p.repo || "")}</span></div>`).join("") : '<div class="empty small">Draft PRs appear here after delivery.</div>'}
          </div></div>`}
        </div>
      </div>`;

    const outcomeTip = (x) => `<strong>${esc(dayLong(x.date))}</strong>${OUTCOMES.map(([k, l]) => `<span class="tip-row"><i class="sw ch-${k}"></i><b>${x[k]}</b> ${esc(l.toLowerCase())}</span>`).join("")}`;
    const host = (id) => $(`#${id}`, main);
    if (host("chOutcomes")) observers.push(mountChart(host("chOutcomes"),
      (w) => stackedColumns(w, days, OUTCOMES.map(([k]) => ({ cls: `ch-${k}`, value: (x) => x[k] })), (v) => String(Math.round(v)),
        (x) => `${dayLong(x.date)}: ${x.done} delivered, ${x.failed} failed, ${x.stopped} stopped`),
      (i) => outcomeTip(days[i])));
    if (host("chCost")) observers.push(mountChart(host("chCost"),
      (w) => stackedColumns(w, days, agentsUsed.map((a) => ({ cls: `ch-agent ${a}`, value: (x) => (x.cost_by_agent || {})[a] || 0 })), money,
        (x) => `${dayLong(x.date)}: ${fmtCost(x.cost_usd)} spent`),
      (i) => { const x = days[i]; return `<strong>${esc(dayLong(x.date))}</strong><span class="tip-row"><b>${x.cost_usd ? fmtCost(x.cost_usd) : "$0"}</b> total</span>${agentsUsed.filter((a) => (x.cost_by_agent || {})[a]).map((a) => `<span class="tip-row"><i class="sw ch-agent ${esc(a)}"></i><b>${fmtCost(x.cost_by_agent[a])}</b> ${esc(agentLabel(a))}</span>`).join("")}`; }));
    if (host("chRate")) observers.push(mountChart(host("chRate"),
      (w) => trendLine(w, days, (x) => x.success_rate, 1, pct, (x) => `${dayLong(x.date)}: ${x.success_rate === null ? "nothing finished" : `${pct(x.success_rate)} success`}`),
      (i) => { const x = days[i]; const n = x.done + x.failed + x.stopped; return `<strong>${esc(dayLong(x.date))}</strong><span class="tip-row">${n ? `<b>${pct(x.success_rate)}</b> success · ${x.done} of ${n}` : "Nothing finished"}</span>`; }));
    const durMax = niceMax(Math.max(0, ...days.map((x) => (x.median_duration || 0) / 60)));
    if (host("chDuration")) observers.push(mountChart(host("chDuration"),
      (w) => trendLine(w, days, (x) => (x.median_duration ? x.median_duration / 60 : null), durMax, (v) => `${Math.round(v)}m`, (x) => `${dayLong(x.date)}: ${x.median_duration ? fmtDur(x.median_duration) : "no deliveries"}`),
      (i) => { const x = days[i]; return `<strong>${esc(dayLong(x.date))}</strong><span class="tip-row">${x.median_duration ? `<b>${fmtDur(x.median_duration)}</b> median · ${x.done} delivered` : "No deliveries"}</span>`; }));

    $("#dash", main).scrollTop = scroll;
    $("#dNew", main).onclick = () => openNewTask();
    $("#q1", main) && ($("#q1", main).onclick = () => openNewTask());
    $("#q2", main) && ($("#q2", main).onclick = () => navigate("#/agents"));
    $("#q3", main) && ($("#q3", main).onclick = () => navigate("#/github"));
    $("#q4", main) && ($("#q4", main).onclick = () => navigate("#/settings/workflow"));
    $$("[data-go]", main).forEach((b) => (b.onclick = () => navigate(b.dataset.go)));
    $$("[data-new]", main).forEach((b) => (b.onclick = () => openNewTask()));
    $$("[data-prompt]", main).forEach((b) => (b.onclick = () => { const p = prompts.find((x) => x.id === b.dataset.prompt); if (p) openNewTask({ requirements: p.text }); }));
    $("#dRun", main).onclick = async () => { try { await api.queueStart(); toast("success", "Queue running"); } catch (e) { toast("error", "Queue", e.message); } };
  }
  render();
  const timer = setInterval(render, 15000);
  return { update(reason) { if (reason === "task" || reason === "agents") render(); }, destroy() { alive = false; clearInterval(timer); observers.forEach((o) => o.disconnect()); } };
}
