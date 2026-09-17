// Usage & budgets: spend, tokens and agent time per project and person, with monthly budgets.
import { $, $$, esc, icon, toast, skeleton } from "../../ui.js";
import { S, navigate } from "../../state.js";
import { orgApi, ORG, can, xicon, avatar, money, currentProject } from "./org.js";

const fmtTok = (n) => { n = Number(n) || 0; return n >= 1e9 ? `${(n / 1e9).toFixed(1)}B` : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n); };
const fmtHours = (s) => { s = Number(s) || 0; return s >= 3600 ? `${(s / 3600).toFixed(1)} h` : `${Math.round(s / 60)} min`; };
const usd = (n, est) => `${est ? "~" : ""}${money(n, Number(n) < 100 ? 2 : 0)}`;
const monthLabel = (m) => { const [y, mo] = m.split("-").map(Number); return new Date(y, mo - 1, 1).toLocaleDateString([], { month: "long", year: "numeric" }); };
const thisMonth = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
const shiftMonth = (m, by) => { const [y, mo] = m.split("-").map(Number); const d = new Date(y, mo - 1 + by, 1); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
const FALLBACK = ["var(--accent)", "var(--blue)", "var(--green)", "var(--purple)", "var(--amber)", "var(--red)"];

function niceMax(v) {
  if (v <= 0) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

let tip = null;
function showTip(e, html) {
  if (!tip) { tip = document.createElement("div"); tip.className = "org-tip"; document.body.appendChild(tip); }
  tip.innerHTML = html;
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = `${Math.max(8, Math.min(e.clientX + 12, innerWidth - w - 8))}px`;
  tip.style.top = `${Math.max(8, e.clientY - h - 12)}px`;
}
function hideTip() { if (tip) tip.hidden = true; }

export function mountUsage(body) {
  let month = thisMonth(), project = currentProject() === "all" ? "" : currentProject(), user = "";
  let data = null, alive = true, users = null, thresholds = [50, 80, 100], orgBudget = 0;
  body.innerHTML = skeleton("cards", 4);

  const colorOf = (id, i) => (ORG.projects.find((p) => p.id === id) || {}).color || FALLBACK[i % FALLBACK.length];

  async function load() {
    try { data = await orgApi.usage({ month, project, user }); } catch (e) { if (alive) body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (can("owner") && !users) {
      try { users = (await orgApi.users()).users || []; } catch { users = []; }
      try { const b = (await orgApi.settings()).budgets || {}; thresholds = b.alert_thresholds || thresholds; orgBudget = b.org_monthly_usd || 0; } catch {}
    }
    if (alive) draw();
  }

  function dailyChart() {
    const days = data.daily || [];
    const projIds = (data.by_project || []).map((p) => p.id);
    const W = 720, H = 220, L = 44, R = 8, T = 10, B = 26;
    const max = niceMax(Math.max(0, ...days.map((d) => d.cost_usd)));
    const bw = (W - L - R) / Math.max(1, days.length);
    const gap = Math.min(2, bw * 0.25);
    const y = (v) => T + (H - T - B) * (1 - v / max);
    const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * max);
    let g = ticks.map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 3}" text-anchor="end">$${v < 10 && max < 10 ? v.toFixed(v ? 2 : 0) : Math.round(v)}</text>`).join("");
    days.forEach((d, i) => {
      const x = L + i * bw + gap / 2, w = Math.max(1, bw - gap);
      let base = 0;
      projIds.forEach((pid, pi) => {
        const v = d.by_project[pid] || 0;
        if (!v) return;
        const y0 = y(base), y1 = y(base + v);
        g += `<rect class="bar" x="${x.toFixed(1)}" y="${y1.toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(0.5, y0 - y1 - (base ? 1 : 0)).toFixed(1)}" rx="${base ? 0 : Math.min(3, w / 2)}" fill="${colorOf(pid, pi)}"/>`;
        base += v;
      });
      g += `<rect x="${(L + i * bw).toFixed(1)}" y="${T}" width="${bw.toFixed(1)}" height="${H - T - B}" fill="transparent" data-day="${i}"/>`;
      if (d.day === 1 || d.day % 5 === 0) g += `<text x="${(x + w / 2).toFixed(1)}" y="${H - 8}" text-anchor="middle">${d.day}</text>`;
    });
    g += `<line class="axis" x1="${L}" x2="${W - R}" y1="${y(0)}" y2="${y(0)}"/>`;
    return `<svg class="org-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily cost by project">${g}</svg>`;
  }

  function monthsChart() {
    const rows = data.months || [];
    if (!rows.length) return `<div class="muted" style="font-size:var(--fs-xs)">No earlier months.</div>`;
    const W = 320, H = 90, B = 16, bw = W / 12;
    const max = niceMax(Math.max(...rows.map((r) => r.cost_usd)));
    return `<svg class="org-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Spend per month">${rows.map((r, i) => {
      const h = Math.max(1, (H - B - 4) * r.cost_usd / max);
      const x = i * bw + 3;
      return `<rect class="bar" data-month="${esc(r.month)}" x="${x}" y="${H - B - h}" width="${bw - 6}" height="${h}" rx="3" fill="${r.month === month ? "var(--accent)" : "var(--border-strong)"}"><title>${esc(monthLabel(r.month))}: ${usd(r.cost_usd)}</title></rect>
        <text x="${x + (bw - 6) / 2}" y="${H - 3}" text-anchor="middle">${esc(new Date(r.month + "-01T00:00").toLocaleDateString([], { month: "narrow" }))}</text>`;
    }).join("")}</svg>`;
  }

  function hbars(rows, labelOf, colorFn) {
    const max = Math.max(0.0001, ...rows.map((r) => r.cost_usd));
    return rows.map((r, i) => `<div class="org-hbar"><span class="truncate" title="${esc(labelOf(r))}">${esc(labelOf(r))}</span>
      <span class="track"><i style="width:${Math.max(1, (r.cost_usd / max) * 100)}%;background:${colorFn(r, i)}"></i></span><span class="num">${usd(r.cost_usd, r.estimated)}</span></div>`).join("");
  }

  function meter(b) {
    const pct = Math.min(100, b.pct), fpct = Math.min(100, b.forecast_pct);
    return `<div class="org-meter ${b.state}">
      <div class="row between"><span class="row" style="gap:6px">${b.kind === "project" ? `<span class="org-proj-dot" style="background:${esc(b.color || "var(--accent)")}"></span>` : xicon(b.kind === "user" ? "user" : "grid", "sm")}<b style="color:var(--text)">${esc(b.label)}</b>${b.hard_cap ? '<span class="badge outline">hard cap</span>' : ""}</span>
        <span>${usd(b.spent_usd)} of ${money(b.budget_usd)} · ${b.pct}%</span></div>
      <span class="org-bar forecast"><i style="width:${pct}%"></i>${fpct > pct ? `<em class="org-forecast-mark" style="left:${fpct}%" title="Forecast ${usd(b.forecast_usd)}"></em>` : ""}</span>
      <div class="row between"><span>${b.state === "over" ? "Over budget" : b.state === "warn" ? "Close to budget" : "On track"}</span><span>Forecast ${usd(b.forecast_usd)} (${b.forecast_pct}%)</span></div></div>`;
  }

  function budgetsCard() {
    if (!can("owner")) return "";
    return `<div class="card"><div class="card-head"><div><h3>Budgets</h3><p class="card-sub">Monthly limits. Alerts fire once per threshold per month; a hard cap stops new tasks in that project.</p></div><button class="btn sm primary" id="bSave">${icon("save")}Save budgets</button></div>
      <div class="card-body stack">
        <div class="grid2"><div class="field"><label>Organisation monthly budget (USD, 0 = none)</label><input type="number" min="0" step="10" id="bOrg" value="${orgBudget}"></div>
          <div class="field"><label>Alert at (% of budget, comma separated)</label><input id="bTh" value="${esc(thresholds.join(", "))}"></div></div>
        <div class="org-table-wrap"><table class="org-table"><thead><tr><th>Project</th><th class="num">Monthly USD</th><th>Hard cap</th></tr></thead><tbody>
          ${ORG.projects.map((p) => `<tr><td><span class="row" style="gap:7px"><span class="org-proj-dot" style="background:${esc(p.color)}"></span>${esc(p.name)}</span></td>
            <td class="num"><input class="input" style="width:110px;text-align:right" type="number" min="0" step="10" data-bp="${esc(p.id)}" value="${(p.budget || {}).monthly_usd || 0}"></td>
            <td><input type="checkbox" class="org-tick" data-bh="${esc(p.id)}" ${(p.budget || {}).hard_cap ? "checked" : ""} aria-label="Hard cap for ${esc(p.name)}"></td></tr>`).join("")}
        </tbody></table></div>
        ${(users || []).length ? `<div class="org-table-wrap"><table class="org-table"><thead><tr><th>Person</th><th class="num">Monthly USD</th></tr></thead><tbody>
          ${users.filter((u) => !u.local || users.length === 1).map((u) => `<tr><td><span class="org-person">${avatar(u, 24)}<span class="min0"><strong class="truncate">${esc(u.name)}</strong><span class="muted">${esc(u.username)}</span></span></span></td>
            <td class="num"><input class="input" style="width:110px;text-align:right" type="number" min="0" step="10" data-bu="${esc(u.username)}" value="${u.budget_monthly_usd || 0}"></td></tr>`).join("")}
        </tbody></table></div>` : ""}
      </div></div>`;
  }

  function draw() {
    const d = data, t = d.total || {};
    const months = [...new Set([...(d.months || []).map((m) => m.month), month, thisMonth()])].sort().reverse();
    const empty = !t.turns;
    const legend = (d.by_project || []).map((p, i) => `<span><i style="background:${colorOf(p.id, i)}"></i>${esc(p.name)}</span>`).join("");
    body.innerHTML = `
      <div class="page-head"><div><h1>Usage &amp; budgets</h1><p>What agent work cost, from every recorded turn. Costs marked ~ are estimated from token counts; subscriptions are not billed per token.</p></div>
        <div class="page-actions org-usage-controls">
          <span class="row" style="gap:4px"><button class="btn sm org-flip" id="mPrev" aria-label="Previous month">${icon("chevron", "sm")}</button>
            <select class="org-select" id="mSel" aria-label="Month">${months.map((m) => `<option value="${m}" ${m === month ? "selected" : ""}>${esc(monthLabel(m))}</option>`).join("")}</select>
            <button class="btn sm" id="mNext" aria-label="Next month" ${month >= thisMonth() ? "disabled" : ""}>${icon("chevron", "sm")}</button></span>
          <select class="org-select" id="pSel" aria-label="Project"><option value="">All projects</option>${ORG.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === project ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select>
          ${user ? `<button class="btn sm" id="uClear">${icon("x")}${esc(user)}</button>` : ""}
        </div></div>
      <div class="org-kpis">
        <div class="org-kpi"><span>Spend in ${esc(monthLabel(month))}</span><b>${usd(t.cost_usd, t.estimated)}</b><small>${t.tasks || 0} tasks</small></div>
        <div class="org-kpi"><span>Forecast for the month</span><b>${usd(d.forecast_usd, t.estimated)}</b><small>at the current daily pace</small></div>
        <div class="org-kpi"><span>Tokens in / out</span><b>${fmtTok(t.input)} / ${fmtTok(t.output)}</b><small>${fmtTok(t.cached)} cached</small></div>
        <div class="org-kpi"><span>Agent time</span><b>${fmtHours(t.seconds)}</b><small>${t.turns || 0} turns</small></div>
      </div>
      ${empty ? `<div class="org-empty"><div class="org-empty-art">${xicon("chart")}</div><h2>No agent work recorded in ${esc(monthLabel(month))}</h2><p>Spend appears here as soon as a task runs a turn. Pick an earlier month, or start a task.</p></div>` : `
      <div class="card"><div class="card-head"><div><h3>Daily cost by project</h3><p class="card-sub">Hover a day for its breakdown.</p></div></div>
        <div class="card-body"><div id="dailyWrap">${dailyChart()}</div><div class="org-legend" style="margin-top:8px">${legend}</div></div></div>`}
      ${d.budgets.length ? `<div class="card"><div class="card-head"><h3>Budgets this month</h3></div><div class="card-body stack" style="gap:14px">${d.budgets.map(meter).join("")}</div></div>` : ""}
      ${empty ? "" : `<div class="org-grid-2">
        <div class="card"><div class="card-head"><h3>By project</h3></div><div class="card-body">${hbars(d.by_project, (r) => r.name, (r, i) => colorOf(r.id, i))}</div></div>
        <div class="card"><div class="card-head"><h3>By agent</h3></div><div class="card-body">${hbars(d.by_agent, (r) => (S.agentMeta[r.agent] || {}).label || r.agent, (r) => `var(--${r.agent}, var(--blue))`)}</div></div>
      </div>
      <div class="org-grid-2">
        <div class="card"><div class="card-head"><h3>By person</h3><span class="muted" style="font-size:var(--fs-xs)">who created the tasks</span></div><div class="card-body org-table-wrap"><table class="org-table">
          <thead><tr><th>Person</th><th class="num">Cost</th><th class="num org-hide-sm">Tokens</th><th class="num org-hide-sm">Time</th><th class="num">Tasks</th></tr></thead><tbody>
          ${d.by_user.map((u) => `<tr class="clickable" data-user="${esc(u.username)}"><td><span class="org-person">${avatar(u, 24)}<span class="min0"><strong class="truncate">${esc(u.name)}</strong><span class="muted">${esc(u.username)}</span></span></span></td>
            <td class="num">${usd(u.cost_usd, u.estimated)}</td><td class="num org-hide-sm">${fmtTok(u.input + u.output)}</td><td class="num org-hide-sm">${fmtHours(u.seconds)}</td><td class="num">${u.tasks}</td></tr>`).join("")}
          </tbody></table></div></div>
        <div class="card"><div class="card-head"><h3>Most expensive tasks</h3></div><div class="card-body stack" style="gap:2px">
          ${d.top_tasks.map((x) => `<a class="org-top-task" href="#/task/${esc(x.task_id)}"><span class="truncate">${x.number ? `<span class="muted">#${esc(x.number)}</span> ` : ""}${esc(x.name || x.task_id)}</span><b>${usd(x.cost_usd)}</b></a>`).join("")}
        </div></div>
      </div>`}
      <div class="card"><div class="card-head"><div><h3>Last 12 months</h3><p class="card-sub">Click a month to open it.</p></div></div><div class="card-body" style="max-width:420px">${monthsChart()}</div></div>
      ${budgetsCard()}`;
    bind();
  }

  function bind() {
    const go = (m) => { month = m; load(); };
    $("#mPrev", body).onclick = () => go(shiftMonth(month, -1));
    $("#mNext", body).onclick = () => month < thisMonth() && go(shiftMonth(month, 1));
    $("#mSel", body).onchange = (e) => go(e.target.value);
    $("#pSel", body).onchange = (e) => { project = e.target.value; load(); };
    $("#uClear", body) && ($("#uClear", body).onclick = () => { user = ""; load(); });
    $$("[data-user]", body).forEach((r) => (r.onclick = () => { user = r.dataset.user; load(); }));
    $$("[data-month]", body).forEach((r) => (r.onclick = () => go(r.dataset.month)));
    const wrap = $("#dailyWrap", body);
    if (wrap) {
      wrap.onmousemove = (e) => {
        const cell = e.target.closest("[data-day]");
        if (!cell) return hideTip();
        const d = data.daily[Number(cell.dataset.day)];
        const rows = (data.by_project || []).filter((p) => d.by_project[p.id]).map((p, i) => `<div><span><i style="display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;background:${colorOf(p.id, i)}"></i>${esc(p.name)}</span><span>${usd(d.by_project[p.id])}</span></div>`).join("");
        const dt = new Date(`${month}-${String(d.day).padStart(2, "0")}T00:00`);
        showTip(e, `<b>${esc(dt.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }))}</b>${rows || '<div class="muted">No spend</div>'}${rows ? `<div style="border-top:1px solid var(--border);margin-top:3px;padding-top:3px"><span>Total</span><span>${usd(d.cost_usd)}</span></div>` : ""}`);
      };
      wrap.onmouseleave = hideTip;
    }
    const save = $("#bSave", body);
    if (save) save.onclick = async () => {
      const th = $("#bTh", body).value.split(",").map((x) => parseInt(x, 10)).filter((x) => x > 0);
      const projects = {}, us = {};
      $$("[data-bp]", body).forEach((i) => { projects[i.dataset.bp] = { monthly_usd: Number(i.value) || 0, hard_cap: $(`[data-bh="${CSS.escape(i.dataset.bp)}"]`, body).checked }; });
      $$("[data-bu]", body).forEach((i) => { us[i.dataset.bu] = Number(i.value) || 0; });
      save.disabled = true;
      try {
        await orgApi.saveBudgets({ org: { org_monthly_usd: Number($("#bOrg", body).value) || 0, alert_thresholds: th.length ? th : [80, 100] }, projects, users: us });
        toast("success", "Budgets saved");
        const { loadMe } = await import("./org.js");
        await loadMe();
        users = null;
        load();
      } catch (e) { toast("error", "Could not save budgets", e.message); save.disabled = false; }
    };
  }

  load();
  const timer = setInterval(() => { if (document.visibilityState === "visible" && !body.querySelector("input:focus, select:focus")) load(); }, 60000);
  return { update() {}, destroy() { alive = false; clearInterval(timer); hideTip(); } };
}
