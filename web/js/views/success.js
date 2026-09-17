// Success: the dashboard's weekly success section and the scorecard card on a task page.
// Numbers come from orchestrator/scorecard.py; the formula is spelled out there and on every card.
import { $, esc, icon, md, fmtDur, fmtCost, timeAgo, toast, modal } from "../ui.js";
import { agentLabel } from "../state.js";
import { api } from "../api.js";

const pct = (n) => (n === null || n === undefined ? "—" : `${Math.round(n * 100)}%`);
export const scoreTone = (n) => (n === null || n === undefined ? "" : n >= 80 ? "green" : n >= 50 ? "amber" : "red");
const weekLabel = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };

function rateRows(rows, empty) {
  if (!rows.length) return `<div class="chart-empty">${esc(empty)}</div>`;
  return rows.map((r) => `<div class="repo-bar" title="${esc(r.key)}">
      <span class="name truncate">${esc(r.label)}</span>
      <span class="track" aria-hidden="true"><i class="ch-done" style="width:${r.success_rate * 100}%"></i><i class="ch-failed" style="width:${(1 - r.success_rate) * 100}%"></i></span>
      <span class="v">${pct(r.success_rate)} success <span class="muted">· ${r.success} of ${r.finished} · avg score ${Math.round(r.avg_score)}${r.cost_usd ? ` · ${fmtCost(r.cost_usd)}` : ""}</span></span></div>`).join("");
}

// ---------------------------------------------------------------------------- dashboard
export function successSection(s) {
  if (!s) return "";
  const weeks = s.weeks || [];
  const fails = s.failures || [];
  const maxFail = Math.max(1, ...fails.map((f) => f.count));
  const prs = s.prs || {};
  const pending = s.lessons_pending || 0;
  const head = `<div class="section-head"><div><h2>Success</h2><p class="card-sub">Scored after every task · last ${s.window_weeks} weeks · ${s.finished} finished</p></div>
      <a class="btn sm ${pending ? "primary" : ""}" href="#/lessons">${icon("brain")}Lessons${pending ? ` <span class="pill-count">${pending} to review</span>` : ""}</a></div>`;
  if (!s.finished) return `${head}<div class="card success-empty"><div class="card-body chart-empty">No task has finished in the last ${s.window_weeks} weeks. Each finished task gets a scorecard and a short retrospective, and the results collect here.</div></div>`;
  const weekBars = weeks.map((w) => {
    const h = w.success_rate === null ? 0 : Math.max(3, w.success_rate * 100);
    const tip = w.finished ? `Week of ${weekLabel(w.week)}: ${pct(w.success_rate)} success (${w.success} of ${w.finished}), average score ${Math.round(w.avg_score)}` : `Week of ${weekLabel(w.week)}: nothing finished`;
    return `<div class="wk" title="${esc(tip)}" aria-label="${esc(tip)}" tabindex="0">
        <span class="wk-val">${w.finished ? pct(w.success_rate) : ""}</span>
        <span class="wk-col"><i class="${w.finished ? scoreTone(w.success_rate * 100) : "none"}" style="height:${h}%"></i></span>
        <span class="wk-lbl">${esc(weekLabel(w.week))}</span></div>`;
  }).join("");
  return `${head}<div class="insights success">
    <div class="card"><div class="card-head"><div><h3>Success rate by week</h3><p class="card-sub">Delivered and not rejected, of all finished tasks</p></div>
        <div class="score-kpis"><span><b>${pct(s.success_rate)}</b>success</span><span><b class="tone-${scoreTone(s.avg_score)}">${s.avg_score === null ? "—" : Math.round(s.avg_score)}</b>avg score</span></div></div>
      <div class="card-body"><div class="wk-bars">${weekBars}</div>
        <div class="pr-outcomes muted">${prs.total ? `${prs.total} pull request${prs.total === 1 ? "" : "s"}: <b>${prs.merged}</b> merged · <b>${prs.open}</b> open · <b>${prs.closed}</b> closed unmerged${prs.human_fixed ? ` · <b>${prs.human_fixed}</b> needed human commits` : ""}` : "No pull requests in this window."}</div></div></div>
    <div class="card"><div class="card-head"><div><h3>Why tasks fail</h3><p class="card-sub">Failed and stopped tasks by cause</p></div></div><div class="card-body">
      ${fails.length ? `<div class="fail-rows">${fails.map((f) => `<div class="fail-row"><span class="truncate">${esc(f.label)}</span><span class="track" aria-hidden="true"><i style="width:${(f.count / maxFail) * 100}%"></i></span><b>${f.count}</b></div>`).join("")}</div>` : '<div class="chart-empty">Nothing failed in this window.</div>'}
      <a class="lessons-link ${pending ? "has" : ""}" href="#/lessons">${icon("brain", "sm")}<span>${pending ? `<b>${pending}</b> proposed lesson${pending === 1 ? "" : "s"} awaiting your review` : "No lessons awaiting review"}</span><span class="muted">${s.lessons_approved || 0} approved</span></a>
    </div></div>
    <div class="card"><div class="card-head"><div><h3>By repository</h3><p class="card-sub">Success rate and average score</p></div></div><div class="card-body repo-bars">${rateRows(s.by_repo || [], "No repositories yet.")}</div></div>
    <div class="card"><div class="card-head"><div><h3>By team</h3><p class="card-sub">Supervisor → worker → reviewer</p></div></div><div class="card-body repo-bars">${rateRows(s.by_pairing || [], "No teams yet.")}</div></div>
  </div>`;
}

// ---------------------------------------------------------------------------- task page
const FINISHED = new Set(["done", "failed", "stopped"]);
const yes = (v) => (v === null || v === undefined ? "—" : v ? "passed" : "failed");

export function scorecardCard(t) {
  const c = t.scorecard;
  if (!c || !FINISHED.has(t.status) || c.finished_at !== t.finished_at) return "";
  const h = c.human || {}, tr = c.agent_trouble || {}, v = c.verification || {};
  const humans = [[h.questions, "question", "questions"], [h.guidance, "guidance message", "guidance messages"], [h.interrupts, "interrupt", "interrupts"],
    [h.rejections, "rejected approval", "rejected approvals"], [h.file_edits, "file edit", "file edits"], [h.retries, "retry", "retries"]]
    .filter(([n]) => n).map(([n, one, many]) => `${n} ${n === 1 ? one : many}`);
  const trouble = (tr.failures || 0) + (tr.timeouts || 0) + (tr.envelope_nudges || 0);
  const pr = c.pr;
  const prText = pr ? `${esc(pr.state)}${pr.human_commits ? ` · ${pr.human_commits} human commit${pr.human_commits === 1 ? "" : "s"} after delivery` : ""}${pr.checked_at ? ` <span class="muted">· checked ${esc(timeAgo(pr.checked_at))}</span>` : ' <span class="muted">· not checked yet</span>'}${pr.error ? ` <span class="muted">· ${esc(pr.error)}</span>` : ""}` : "";
  return `<div class="card scorecard" id="scoreCard"><div class="card-head"><h3>Scorecard</h3><button class="btn xs" id="scoreRefresh" title="Recompute, and ask GitHub about the pull request">${icon("refresh")}Refresh</button></div>
    <div class="card-body stack" style="gap:12px">
      <div class="score-top"><span class="score-ring tone-${scoreTone(c.score)}" style="--p:${c.score}"><b>${c.score}</b></span>
        <div class="score-what"><strong>${esc(c.outcome_label)}</strong>
          <span class="row wrap" style="gap:6px"><span class="badge ${c.success ? "green" : "red"}">${c.success ? "success" : "not a success"}</span>${c.failure_label && c.outcome === "failed" ? `<span class="badge outline">${esc(c.failure_label)}</span>` : ""}</span></div></div>
      <div class="stat-row">
        <div class="stat"><b>${c.duration_seconds ? fmtDur(c.duration_seconds) : "—"}</b><span>duration</span></div>
        <div class="stat"><b>${c.work_packages}</b><span>work packages</span></div>
        <div class="stat"><b>${c.revisions}</b><span>revisions</span></div>
        <div class="stat"><b>${c.review_rounds}</b><span>review rounds</span></div>
        <div class="stat"><b>${fmtCost(c.cost_usd, c.cost_estimated)}</b><span>${c.cost_estimated ? "est. cost" : "cost"}</span></div>
      </div>
      <dl class="kv">
        <dt>Team</dt><dd>${Object.entries(c.team || {}).map(([r, x]) => `${esc(agentLabel(x.agent))}${x.model ? ` <span class="muted mono">${esc(x.model)}</span>` : ""} <span class="muted">${esc(r)}</span>`).join("<br>") || "—"}</dd>
        <dt>Verification</dt><dd>${v.runs ? `first run ${yes(v.first_ok)}, last run ${yes(v.final_ok)} · ${v.runs} run${v.runs === 1 ? "" : "s"}` : "not run"}</dd>
        <dt>Humans</dt><dd>${humans.length ? esc(humans.join(" · ")) : "no intervention"}</dd>
        <dt>Agent trouble</dt><dd>${trouble ? `${tr.failures || 0} failed turn(s) · ${tr.timeouts || 0} timeout(s) · ${tr.envelope_nudges || 0} missing envelope(s)` : "none"}</dd>
        ${pr ? `<dt>Pull request</dt><dd>${prText}</dd>` : ""}
        ${c.lessons_used ? `<dt>Lessons used</dt><dd>${c.lessons_used} approved lesson${c.lessons_used === 1 ? "" : "s"} in the prompts</dd>` : ""}
      </dl>
      <details class="score-parts"><summary>How the score was calculated</summary><ul>${(c.score_parts || []).map((p) => `<li><span>${esc(p.label)}</span><b class="${p.points < 0 ? "neg" : "pos"}">${p.points > 0 ? "+" : ""}${p.points}</b></li>`).join("")}<li class="sum"><span>Score (0 to 100)</span><b>${c.score}</b></li></ul></details>
      ${retroBlock(t)}
    </div></div>`;
}

function retroBlock(t) {
  const r = t.retro;
  const run = `<button class="btn xs" id="retroRun">${icon("sparkles")}${r?.status === "done" || r?.status === "failed" ? "Run again" : "Run retrospective"}</button>`;
  if (!r) return `<div class="retro"><div class="row between"><strong>Retrospective</strong>${run}</div><p class="muted">None yet for this run.</p></div>`;
  if (r.status === "queued" || r.status === "running") return `<div class="retro"><div class="row between"><strong>Retrospective</strong><span class="badge blue">${icon("spinner", "spin")}${esc(r.status)}</span></div><p class="muted">${r.agent ? `${esc(agentLabel(r.agent))} is looking back at this run.` : "Waiting for its turn."} It never holds up other work.</p></div>`;
  if (r.status === "failed") return `<div class="retro"><div class="row between"><strong>Retrospective</strong>${run}</div><p class="muted">Could not run: ${esc(r.error || "unknown error")}</p></div>`;
  const list = (title, rows) => (rows || []).length ? `<div><div class="field-label">${esc(title)}</div><ul class="retro-list">${rows.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : "";
  const n = (r.lessons_proposed || []).length;
  return `<div class="retro"><div class="row between wrap"><strong>Retrospective</strong><span class="row" style="gap:6px"><button class="btn xs" id="retroOpen">${icon("docs")}RETRO.md</button>${run}</span></div>
    <p class="muted">${esc(agentLabel(r.agent))}${r.model ? ` · ${esc(r.model)}` : ""}${r.effort ? ` · ${esc(r.effort)} effort` : ""} · ${esc(timeAgo(r.finished || r.time))}${r.cost_usd ? ` · ${fmtCost(r.cost_usd, r.estimated)}` : ""}</p>
    ${list("What went well", r.what_went_well)}${list("What went wrong", r.what_went_wrong)}${list("Root causes", r.root_causes)}
    <a class="lessons-link ${n ? "has" : ""}" href="#/lessons">${icon("brain", "sm")}<span>${n ? `<b>${n}</b> lesson${n === 1 ? "" : "s"} proposed for review` : "No new lessons proposed"}</span>${icon("chevron", "sm")}</a></div>`;
}

export function bindScorecard(host, t, rerender) {
  const refresh = $("#scoreRefresh", host);
  if (refresh) refresh.onclick = async () => {
    refresh.disabled = true; refresh.innerHTML = `${icon("spinner", "spin")}Refreshing`;
    try { await api.refreshScorecard(t.id); toast("success", "Scorecard updated"); } catch (e) { toast("error", "Could not refresh", e.message); }
    finally { rerender && rerender(); }
  };
  const run = $("#retroRun", host);
  if (run) run.onclick = async () => {
    run.disabled = true;
    try { await api.runRetro(t.id); toast("info", "Retrospective queued", "It runs in the background with a cheap agent turn."); } catch (e) { toast("error", "Retrospective", e.message); run.disabled = false; }
  };
  const open = $("#retroOpen", host);
  if (open) open.onclick = async () => {
    try {
      const a = await api.artifact(t.id, "retro");
      modal(`<h2>Retrospective</h2><div class="doc md retro-doc">${md(a.text || "(empty)")}</div><div class="modal-actions"><a class="btn" href="#/lessons" data-close>${icon("brain")}Review lessons</a><button class="btn primary" data-close>Close</button></div>`, { wide: true });
    } catch (e) { toast("error", "Could not open RETRO.md", e.message); }
  };
}
