// Dashboard: KPIs, agent health, usage, recent activity, open PRs.
import { $, $$, esc, icon, fmtDur, fmtNum, fmtCost, timeAgo, fmtTime, toast } from "../ui.js";
import { S, agentLabel, agentInitial, statusOf, navigate, LIVE } from "../state.js";
import { api } from "../api.js";
import { openNewTask } from "./newtask.js";
import { agentHealthRow } from "./agents.js";

export function mountDashboard(main) {
  main.innerHTML = `<div class="page" id="dash"><div class="empty small">Loading…</div></div>`;
  let alive = true;
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
    $("#dash", main).innerHTML = `
      <div class="page-head"><div><h1>Overview</h1><p>Your multi-agent engineering desk · ${esc(new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }))}</p></div>
        <div class="page-actions"><button class="btn" id="dRun">${icon("play")}${d.queue?.running ? "Queue running" : "Run queue"}</button><button class="btn primary" id="dNew">${icon("plus")}New task</button></div></div>
      <div class="kpis">
        <div class="kpi"><div class="lbl">${icon("tasks", "sm")}Tasks</div><div class="val">${d.total}</div><div class="sub">${bs.queued || 0} queued · ${bs.draft || 0} drafts</div></div>
        <div class="kpi"><div class="lbl">${icon("activity", "sm")}Running</div><div class="val">${running}</div><div class="sub">parallel limit ${d.queue?.max_parallel || 1}</div></div>
        <div class="kpi" style="${attention ? "border-color:color-mix(in srgb,var(--amber) 50%,var(--border))" : ""}"><div class="lbl">${icon("bell", "sm")}Needs attention</div><div class="val" style="${attention ? "color:var(--amber)" : ""}">${attention}</div><div class="sub">questions, approvals, paused</div></div>
        <div class="kpi"><div class="lbl">${icon("check", "sm")}Delivered</div><div class="val">${bs.done || 0}</div><div class="sub">${d.success_rate !== null && d.success_rate !== undefined ? `${Math.round(d.success_rate * 100)}% success` : "no finished tasks yet"}</div></div>
        <div class="kpi"><div class="lbl">${icon("clock", "sm")}Avg duration</div><div class="val">${d.avg_duration ? fmtDur(d.avg_duration) : "—"}</div><div class="sub">per delivered task</div></div>
        <div class="kpi" title="Claude reports real cost; Codex and Gemini are estimated at API-equivalent rates. Subscription usage is not billed per token."><div class="lbl">${icon("dollar", "sm")}Spend${d.cost_estimated ? " (est.)" : ""}</div><div class="val">${fmtCost(d.total_cost_usd, d.cost_estimated)}</div><div class="sub">${d.total_turns} agent turns · API-equivalent</div></div>
      </div>
      <div class="dash-grid">
        <div class="stack" style="gap:16px">
          ${attn.length ? `<div class="card"><div class="card-head"><h3>Needs your attention</h3><span class="badge amber">${attn.length}</span></div><div class="card-body stack">${attn.slice(0, 6).map((t) => `<a class="row between" href="#/task/${esc(t.id)}" style="text-decoration:none;color:inherit"><span class="row"><span class="dot warn"></span><strong>${esc(t.name)}</strong></span><span class="muted truncate" style="max-width:55%">${esc(t.pending?.question || t.detail || statusOf(t).label)}</span></a>`).join("")}</div></div>` : ""}
          <div class="card"><div class="card-head"><h3>Recent activity</h3><a href="#/tasks" class="muted" style="font-size:12px">All tasks</a></div><div class="card-body feed">
            ${(d.recent || []).length ? d.recent.map((e) => `<div class="feed-item"><span class="av sm ${esc(e.role === "user" ? "user" : (S.tasks.get(e.task_id)?.workflow?.roles?.[e.role]?.agent || "system"))}">${esc(agentInitial(S.tasks.get(e.task_id)?.workflow?.roles?.[e.role]?.agent || e.role))}</span><div><a href="#/task/${esc(e.task_id)}">${esc(e.task)}</a> · ${esc(e.title)}${e.detail ? `<p>${esc(e.detail)}</p>` : ""}</div><span class="t">${timeAgo(e.time)}</span></div>`).join("") : '<div class="empty small">Nothing yet. Create a task to get started.</div>'}
          </div></div>
          <div class="card"><div class="card-head"><h3>Quick start</h3></div><div class="card-body quick">
            <button id="q1"><strong>${icon("plus")}New task</strong><span>Pick a repository, describe the change, choose who supervises whom.</span></button>
            <button id="q2"><strong>${icon("bot")}Test agents</strong><span>Check Codex, Claude and Gemini are signed in and streaming correctly.</span></button>
            <button id="q3"><strong>${icon("github")}Connect GitHub inbox</strong><span>Auto-queue issues assigned to you or labelled for the agents.</span></button>
            <button id="q4"><strong>${icon("settings")}Workflow defaults</strong><span>Presets, models, verification, approval gates.</span></button>
          </div></div>
        </div>
        <div class="stack" style="gap:16px">
          <div class="card"><div class="card-head"><h3>Agents</h3><a href="#/agents" class="muted" style="font-size:12px">Manage</a></div><div class="card-body agent-health">${["codex", "claude", "gemini"].map((a) => agentHealthRow(a, health[a], { compact: true })).join("")}
            <div class="row" style="font-size:11px;color:var(--text-3);gap:12px;margin-top:4px"><span>${icon(health.git?.ok ? "check" : "x", "sm")} git</span><span>${icon(health.gh?.ok ? "check" : "x", "sm")} gh ${S.github?.login ? `@${esc(S.github.login)}` : ""}</span></div>
          </div></div>
          <div class="card"><div class="card-head"><h3>Usage by agent</h3></div><div class="card-body bars">
            ${Object.keys(agentsTot).length ? Object.entries(agentsTot).map(([a, v]) => `<div class="bar-row"><span class="row"><span class="av sm ${esc(a)}">${esc(agentInitial(a))}</span>${esc(agentLabel(a))}</span><div class="track"><div class="fill" style="width:${Math.round(((v.input || 0) + (v.output || 0)) / maxTok * 100)}%;background:${esc((S.agentMeta[a] || {}).color || "var(--accent)")}"></div></div><span class="v">${fmtNum((v.input || 0) + (v.output || 0))} · ${fmtCost(v.cost_usd, v.estimated)}</span></div>`).join("") : '<div class="empty small">No agent turns recorded yet.</div>'}
          </div></div>
          <div class="card"><div class="card-head"><h3>Open pull requests</h3></div><div class="card-body stack">
            ${(d.prs || []).length ? d.prs.map((p) => `<div class="row between"><a href="${esc(p.url)}" target="_blank" rel="noopener" class="truncate">${icon("external", "sm")} #${esc(p.number || "")} ${esc(p.name)}</a><span class="muted mono" style="font-size:11px">${esc(p.repo || "")}</span></div>`).join("") : '<div class="empty small">Draft PRs appear here after delivery.</div>'}
          </div></div>
        </div>
      </div>`;
    $("#dNew", main).onclick = () => openNewTask();
    $("#q1", main).onclick = () => openNewTask();
    $("#q2", main).onclick = () => navigate("#/agents");
    $("#q3", main).onclick = () => navigate("#/github");
    $("#q4", main).onclick = () => navigate("#/settings/workflow");
    $("#dRun", main).onclick = async () => { try { await api.queueStart(); toast("success", "Queue running"); } catch (e) { toast("error", "Queue", e.message); } };
  }
  render();
  const timer = setInterval(render, 15000);
  return { update(reason) { if (reason === "task" || reason === "agents") render(); }, destroy() { alive = false; clearInterval(timer); } };
}
