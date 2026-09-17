// Learning: is Relay getting better, why, and what to change next.
// One view, self-contained markup (styles in /learning.css) so a redesigned shell can re-home it.
// Data: GET /api/learning (orchestrator/learning_engine.py `view`).
import { $, $$, esc, icon, toast, modal, confirm, timeAgo } from "../ui.js";
import { S } from "../state.js";
import { api } from "../api.js";
import { teamLabel } from "./advice.js";

const pct = (x) => (x === null || x === undefined ? "—" : `${Math.round(x * 100)}%`);
const num = (x) => (x === null || x === undefined ? "—" : Math.round(x));
const money = (x) => (x === null || x === undefined ? "—" : `$${Number(x).toFixed(2)}`);
const tone = (score) => (score === null || score === undefined ? "" : score >= 80 ? "green" : score >= 60 ? "amber" : "red");
const weekLabel = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
const VERDICT = { helps: ["green", "helps"], hurts: ["red", "hurts"], no_effect: ["amber", "no effect"], unproven: ["", "unproven"] };
const KIND_ICON = { system_packages: "cpu", lesson: "brain", rule_tweak: "docs", optional_check: "check", setting: "settings", team_default: "bot" };
const teamFromKey = (key) => {
  const names = ["supervisor", "worker", "reviewer"], roles = {};
  (key || "").split(">").slice(0, 3).forEach((part, i) => { let effort = ""; if (part.includes("@")) [part, effort] = [part.slice(0, part.lastIndexOf("@")), part.slice(part.lastIndexOf("@") + 1)]; const [agent, ...m] = part.split(":"); roles[names[i]] = { agent, model: m.join(":"), effort }; });
  return roles;
};

export function mountLearning(main) {
  main.innerHTML = `<div class="page lx-page" id="lxPage"><div class="empty small">Loading…</div></div>`;
  const page = $("#lxPage", main);
  let data = null, alive = true, busy = false;

  async function load() {
    if (busy) return;
    busy = true;
    try { data = await api.learning(); if (alive) draw(); }
    catch (e) { if (alive) page.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; }
    finally { busy = false; }
  }

  function bars(rows) {
    if (!rows.length) return '<div class="chart-empty">Nothing recorded yet.</div>';
    return `<div class="lx-rows">${rows.map((r) => `<div class="lx-row" title="${esc(r.key)}"><span class="truncate">${esc(r.label)}</span>
      <span class="lx-track" aria-hidden="true"><i class="${tone(r.avg_score)}" style="width:${Math.max(2, r.avg_score || 0)}%"></i></span>
      <span class="lx-v"><b>${num(r.avg_score)}</b> <span class="muted">avg · ${pct(r.success_rate)} success · n ${r.n} · ${money(r.median_cost)}</span></span></div>`).join("")}</div>`;
  }

  function trend() {
    const w = data.trend;
    return `<div class="lx-weeks">${w.map((b) => { const tip = b.n ? `Week of ${weekLabel(b.week)}: ${pct(b.success_rate)} success, avg score ${num(b.avg_score)}, ${b.n} run(s)` : `Week of ${weekLabel(b.week)}: nothing finished`;
      return `<div class="lx-wk" title="${esc(tip)}" aria-label="${esc(tip)}" tabindex="0"><span class="lx-wk-v">${b.n ? pct(b.success_rate) : ""}</span>
        <span class="lx-wk-col"><i class="${b.n ? tone((b.success_rate || 0) * 100) : "none"}" style="height:${b.n ? Math.max(4, (b.success_rate || 0) * 100) : 0}%"></i></span>
        <span class="lx-wk-l">${esc(weekLabel(b.week))}</span></div>`; }).join("")}</div>`;
  }

  function proposalCard(p) {
    const open = p.status === "proposed";
    const imp = p.impact;
    return `<li class="lx-prop ${esc(p.status)}" data-id="${esc(p.id)}">
      <span class="lx-prop-ic">${icon(KIND_ICON[p.kind] || "sparkles", "sm")}</span>
      <div class="lx-prop-main"><div class="lx-prop-title"><strong>${esc(p.title)}</strong><span class="badge outline">${esc(p.cause_label || p.cause)}</span>${p.confidence ? `<span class="muted">${pct(p.confidence)} confidence</span>` : ""}</div>
        <div class="muted">${esc(p.detail || "")}</div>
        ${p.text ? `<blockquote class="lx-quote">${esc(p.text)}</blockquote>` : ""}
        ${(p.evidence || []).length ? `<div class="lx-evidence">${icon("eye", "sm")}<span>${esc(p.evidence.join(" · "))}</span></div>` : ""}
        <div class="lx-prop-meta muted">${esc(p.repo_label || p.repo || "")}${p.task_id ? ` · from <a href="#/task/${esc(p.task_id)}">${esc(p.task_name || p.task_id)}</a>` : ""}${(p.task_ids || []).length > 1 ? ` · seen in ${p.task_ids.length} runs` : ""} · ${esc(timeAgo(p.created_at))}
          ${p.status === "applied" ? ` · <b>applied</b> ${esc(timeAgo(p.applied_at))}: ${esc(p.applied_result || "")}` : ""}${p.status === "dismissed" ? " · dismissed" : ""}${p.recurred_after_apply ? ` · <span class="tone-red">recurred ${p.recurred_after_apply}× after applying</span>` : ""}</div>
        ${imp && (imp.before.n || imp.after.n) ? `<div class="lx-impact" title="Clean: delivered, scored 60 or more, and every required acceptance criterion proven">Runs here before: <b>${pct(imp.before.clean_rate)}</b> clean, ${num(imp.before.avg_score)} avg, ${imp.before.blocked_avg ?? "—"} blocked checks (n ${imp.before.n}) → after: <b>${pct(imp.after.clean_rate)}</b> clean, ${num(imp.after.avg_score)} avg, ${imp.after.blocked_avg ?? "—"} blocked checks (n ${imp.after.n})</div>` : ""}
      </div>
      ${open ? `<div class="lx-prop-act"><button class="btn sm primary" data-apply="${esc(p.id)}">${icon("zap")}Apply</button><button class="btn sm ghost" data-dismiss="${esc(p.id)}">Dismiss</button></div>` : ""}
    </li>`;
  }

  function lessonsCard() {
    const L = data.lessons;
    const rows = L.lessons;
    const flagged = rows.filter((r) => r.retire && r.enabled !== false).length;
    const high = L.queue.filter((q) => q.high_confidence);
    return `<div class="card"><div class="card-head"><div><h3>Lesson effectiveness</h3><p class="card-sub">Runs in the lesson's scope with it in the prompts vs without · a verdict needs ${esc(data.settings.effect_min_tasks)} runs with it</p></div>
        ${flagged ? `<span class="badge red">${flagged} flagged for retirement</span>` : ""}</div>
      <div class="card-body">${rows.length ? `<div class="lx-scroll"><table class="lx-table"><thead><tr><th>Lesson</th><th>Category</th><th>With</th><th>Without</th><th>Δ score</th><th>Verdict</th><th></th></tr></thead><tbody>
        ${rows.map((r) => { const [t, l] = VERDICT[r.verdict] || VERDICT.unproven; const w = r.with || {}, wo = r.without || {};
          return `<tr class="${r.enabled === false ? "off" : ""}"><td class="lx-lesson"><span>${esc(r.text)}</span><span class="muted">${r.scope === "global" ? "all repositories" : esc((r.repo || "").split("/").pop())}${r.auto_approved ? " · auto-approved" : ""}${r.source === "autopsy" ? " · from an autopsy" : ""}${r.retired_at ? " · retired" : ""}</span></td>
            <td>${esc(L.categories[r.category] || r.category)}</td>
            <td>${w.n ? `${num(w.avg_score)} <span class="muted">n ${w.n}</span>` : '<span class="muted">n 0</span>'}</td>
            <td>${wo.n ? `${num(wo.avg_score)} <span class="muted">n ${wo.n}</span>` : '<span class="muted">n 0</span>'}</td>
            <td>${r.delta === null || r.delta === undefined ? "—" : `<b class="tone-${r.delta >= 0 ? "green" : "red"}">${r.delta > 0 ? "+" : ""}${r.delta}</b>`}${r.first_pass_delta !== undefined && r.first_pass_delta !== null ? `<div class="muted">first pass ${r.first_pass_delta > 0 ? "+" : ""}${Math.round(r.first_pass_delta * 100)} pts</div>` : ""}</td>
            <td><span class="badge ${t}">${l}</span></td>
            <td>${r.enabled !== false && (r.retire || r.verdict !== "helps") ? `<button class="btn xs ${r.retire ? "danger" : "ghost"}" data-retire="${esc(r.id)}">Retire</button>` : ""}</td></tr>`; }).join("")}
        </tbody></table></div>` : '<div class="chart-empty">No approved lessons yet.</div>'}
        ${high.length ? `<div class="lx-high">${icon("sparkles", "sm")}<span><b>${high.length}</b> proposed lesson${high.length === 1 ? " is" : "s are"} backed by ${esc(data.settings.auto_approve_min_tasks)}+ tasks${data.settings.auto_approve_lessons ? " and will be approved automatically" : ""}: ${high.slice(0, 3).map((q) => `“${esc(q.text)}”`).join(" · ")}</span><a class="btn xs" href="#/knowledge/lessons">Review</a></div>` : ""}
      </div></div>`;
  }

  function playbooksCard() {
    const pbs = data.playbooks;
    const repos = data.by_repo.filter((r) => !pbs.some((p) => p.repo === r.key));
    return `<div class="card"><div class="card-head"><div><h3>Playbooks</h3><p class="card-sub">Per repository, injected into the supervisor's planning prompt · seeded from the system map, environment, lessons and outcomes · refreshed by a cheap agent turn · editable</p></div></div>
      <div class="card-body">${pbs.length ? `<div class="lx-pbs">${pbs.map((pb) => { const secs = Object.entries(pb.sections || {}).filter(([, s]) => (s.text || "").trim());
        return `<div class="lx-pb"><div class="lx-pb-head"><strong class="truncate" title="${esc(pb.repo)}">${esc(pb.repo_label || pb.repo)}</strong>
            <span class="muted">${secs.length}/5 sections · ${esc((pb.evidence || {}).runs || 0)} runs${pb.agent_refresh ? ` · agent ${esc(timeAgo(pb.agent_refresh.time))}` : ""}${pb.seed_at ? ` · seeded ${esc(timeAgo(pb.seed_at))}` : ""}</span>
            <span class="row" style="gap:4px"><button class="btn xs" data-pb-open="${esc(pb.repo)}">${icon("edit")}Open</button><button class="btn xs ghost" data-pb-seed="${esc(pb.repo)}" title="Rebuild the sections nobody edited">${icon("refresh")}Re-seed</button><button class="btn xs ghost" data-pb-agent="${esc(pb.repo)}" title="One cheap agent turn">${icon("sparkles")}Refresh</button></span></div>
          ${secs.length ? `<div class="lx-pb-preview">${secs.slice(0, 2).map(([k, s]) => `<div><span class="field-label">${esc(pb.labels[k])}</span><pre>${esc(s.text.split("\n").slice(0, 3).join("\n"))}</pre></div>`).join("")}</div>` : '<div class="muted">Empty until tasks run here.</div>'}
        </div>`; }).join("")}</div>` : '<div class="chart-empty">No playbooks yet. They are seeded when a task runs on a repository.</div>'}
        ${repos.length ? `<div class="row wrap lx-seed">${repos.slice(0, 6).map((r) => `<button class="btn xs" data-pb-seed="${esc(r.key)}">${icon("plus")}Seed ${esc(r.label)}</button>`).join("")}</div>` : ""}
      </div></div>`;
  }

  function calibrationCard() {
    const c = data.calibration, rec = c.recommendation;
    const recent = data.recent_predictions;
    return `<div class="card"><div class="card-head"><div><h3>Recommendations and calibration</h3><p class="card-sub">Each task's pre-flight forecast compared with how it went</p></div>
        ${c.brier !== null ? `<span class="badge ${c.brier <= c.baseline_brier ? "green" : "amber"}" title="Brier score: mean squared error of the predicted failure chance (0 is perfect); baseline always predicts the average">Brier ${c.brier} · baseline ${c.baseline_brier}</span>` : ""}</div>
      <div class="card-body stack" style="gap:14px">
        ${c.levels.length ? `<div class="lx-scroll"><table class="lx-table"><thead><tr><th>Predicted risk</th><th>Runs</th><th>Predicted failure</th><th>Actual failure</th></tr></thead><tbody>${c.levels.map((l) => `<tr><td><span class="badge ${{ low: "green", medium: "amber", high: "red" }[l.level]}">${esc(l.level)}</span></td><td>${l.n}</td><td>${pct(l.predicted)}</td><td><b>${pct(l.actual)}</b></td></tr>`).join("")}</tbody></table></div>`
          : `<div class="chart-empty">${c.predictions ? "" : "No finished task had a forecast yet. Tasks created from now on are forecast when they are created."}</div>`}
        ${rec ? `<div class="lx-followed">Recommended team used: <b>${rec.followed}</b> run(s), ${num(rec.followed_avg)} avg · another team: <b>${rec.ignored}</b> run(s), ${num(rec.ignored_avg)} avg</div>` : ""}
        ${recent.length ? `<div class="lx-scroll"><table class="lx-table"><thead><tr><th>Task</th><th>Risk</th><th>Recommended</th><th>Team used</th><th>Score</th></tr></thead><tbody>${recent.slice(0, 12).map((r) => `<tr>
          <td class="truncate"><a href="#/task/${esc(r.task_id)}">${esc(r.name)}</a></td>
          <td>${r.risk ? `<span class="badge ${{ low: "green", medium: "amber", high: "red" }[r.risk]}">${esc(r.risk)} ${pct(r.p_fail)}</span>` : "—"}</td>
          <td>${r.recommended ? esc(teamLabel(teamFromKey(r.recommended))) : "—"}</td>
          <td>${r.team_pick ? `${esc(teamLabel(teamFromKey(r.team_pick.team_key)))} <span class="badge ${r.team_pick.explored ? "purple" : "blue"}">${r.team_pick.explored ? "explored" : "auto-picked"}</span>` : r.chosen ? `${esc(teamLabel(teamFromKey(r.chosen)))}${r.followed ? ' <span class="badge green">recommended</span>' : ""}` : "—"}</td>
          <td>${r.score === null || r.score === undefined ? `<span class="muted">${esc(r.status)}</span>` : `<b class="tone-${tone(r.score)}">${r.score}</b>`}</td></tr>`).join("")}</tbody></table></div>` : ""}
      </div></div>`;
  }

  function settingsCard() {
    const s = data.settings;
    const sw = (k, label, help) => `<div class="field inline lx-set"><div><label>${esc(label)}</label><div class="help">${esc(help)}</div></div><span class="switch ${s[k] ? "on" : ""}" data-set="${k}" role="switch" aria-checked="${!!s[k]}" tabindex="0"></span></div>`;
    return `<details class="card lx-settings"><summary class="card-head"><h3>${icon("settings", "sm")} Learning settings</h3><span class="muted">auto-pick ${s.auto_pick_team ? "on" : "off"} · lessons ${esc(s.lessons_selection)} · auto-approve ${s.auto_approve_lessons ? "on" : "off"}</span></summary>
      <div class="card-body lx-set-grid">
        ${sw("auto_pick_team", "Autopilot picks the team", "For queued tasks whose team nobody chose by hand. Pinned repository teams win.")}
        <div class="field"><label for="lxMode">Recommendation mode</label><select id="lxMode">${[["best", "Best"], ["balanced", "Balanced"], ["cheapest_good_enough", "Cheapest good enough"]].map(([k, l]) => `<option value="${k}" ${s.recommend_mode === k ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="field"><label for="lxEps">Exploration (share of auto-picks)</label><input id="lxEps" type="number" min="0" max="50" step="1" value="${Math.round((s.explore_rate || 0) * 100)}"><div class="help">Percent. Never used for tasks marked critical or urgent.</div></div>
        ${sw("risk_check", "Pre-flight risk check", "Risk, mitigations and clarifying questions in the New task wizard.")}
        <div class="field"><label for="lxSel">Lessons in prompts</label><select id="lxSel"><option value="relevant" ${s.lessons_selection === "relevant" ? "selected" : ""}>Only relevant lessons</option><option value="all" ${s.lessons_selection === "all" ? "selected" : ""}>All approved lessons</option></select></div>
        ${sw("auto_approve_lessons", "Auto-approve well-supported lessons", "Approve a proposed lesson once enough separate tasks proposed it. Off: they are only marked.")}
        <div class="field"><label for="lxMin">Tasks needed to auto-approve</label><input id="lxMin" type="number" min="2" max="20" value="${esc(s.auto_approve_min_tasks)}"></div>
        ${sw("playbooks_inject", "Playbooks in planning prompts", "The repository playbook goes to the supervisor's kickoff.")}
        ${sw("playbook_agent_refresh", "Agent refreshes playbooks", "One cheap turn per repository at most every " + s.playbook_refresh_hours + " h, after two new successful runs.")}
      </div></details>`;
  }

  function draw() {
    const o = data.overall, c = data.calibration;
    const open = data.proposals.filter((p) => p.status === "proposed");
    const done = data.proposals.filter((p) => p.status !== "proposed");
    page.innerHTML = `
      <div class="page-head"><div><h1>Learning</h1><p>Every finished run is recorded with its team, request, prompts, judge events and outcome. Relay uses that to recommend teams, forecast risk, choose lessons, keep playbooks and propose fixes when something fails.</p></div>
        <div class="page-actions"><button class="btn" id="lxBackfill" title="Record outcomes for scored runs that have none">${icon("refresh")}Backfill</button></div></div>
      <div class="lx-kpis">
        <div class="lx-kpi"><b>${data.records}</b><span>runs recorded</span></div>
        <div class="lx-kpi"><b>${pct(o.success_rate)}</b><span>success</span></div>
        <div class="lx-kpi" title="Delivered, scored 60 or more, and every required acceptance criterion proven"><b>${pct(o.clean_rate)}</b><span>clean (all criteria proven)</span></div>
        <div class="lx-kpi"><b class="tone-${tone(o.avg_score)}">${num(o.avg_score)}</b><span>avg score</span></div>
        <div class="lx-kpi"><b>${pct(o.first_pass)}</b><span>verification first pass</span></div>
        <div class="lx-kpi"><b class="${open.length ? "tone-amber" : ""}">${open.length}</b><span>open proposals</span></div>
        <div class="lx-kpi"><b>${c.brier === null ? "—" : c.brier}</b><span>forecast Brier${c.predictions ? ` (n ${c.predictions})` : ""}</span></div>
      </div>
      <div class="card lx-props-card"><div class="card-head"><div><h3>Improvement proposals</h3><p class="card-sub">From autopsies of failed and low-scoring runs · Apply makes the change</p></div><span class="badge ${open.length ? "amber" : ""}">${open.length} open</span></div>
        <div class="card-body">${open.length ? `<ul class="lx-props">${open.map(proposalCard).join("")}</ul>` : `<div class="empty small">${icon("check", "lg")}<p>No open proposals. When a run fails or scores low, Relay finds the root cause and proposes one concrete change here.</p></div>`}
          ${done.length ? `<details class="lx-done"><summary>${done.length} applied or dismissed</summary><ul class="lx-props">${done.slice(0, 30).map(proposalCard).join("")}</ul></details>` : ""}</div></div>
      <div class="card"><div class="card-head"><div><h3>Success by week</h3><p class="card-sub">Share of runs delivered and not rejected or reverted, last 12 weeks</p></div></div><div class="card-body">${trend()}</div></div>
      <div class="lx-grid">
        <div class="card"><div class="card-head"><h3>By repository</h3></div><div class="card-body">${bars(data.by_repo.map((r) => ({ ...r, label: String(r.label || r.key).split("/").pop() })))}</div></div>
        <div class="card"><div class="card-head"><h3>By team</h3></div><div class="card-body">${bars(data.by_team)}</div></div>
        <div class="card"><div class="card-head"><h3>By task type</h3></div><div class="card-body">${bars(data.by_type)}</div></div>
        <div class="card"><div class="card-head"><h3>Root causes</h3><span class="muted">autopsies</span></div><div class="card-body">${bars(data.causes)}</div></div>
      </div>
      ${lessonsCard()}
      ${playbooksCard()}
      ${calibrationCard()}
      ${settingsCard()}`;
    bind();
  }

  const saveSetting = async (patch) => {
    try { await api.saveSettings({ learning: patch }); toast("success", "Learning setting saved"); load(); } catch (e) { toast("error", "Could not save", e.message); }
  };

  function bind() {
    $("#lxBackfill", page).onclick = async (e) => { e.target.disabled = true; try { const r = await api.learningBackfill(); toast("success", `${r.added} outcome record(s) added`); load(); } catch (err) { toast("error", "Backfill", err.message); } };
    $$("[data-apply]", page).forEach((b) => (b.onclick = async () => {
      b.disabled = true; b.innerHTML = `${icon("spinner", "spin")}Applying`;
      try { const r = await api.learningProposal(b.dataset.apply, "apply"); toast("success", "Applied", r.proposal.applied_result || ""); load(); }
      catch (e) { toast("error", "Could not apply", e.message); b.disabled = false; b.innerHTML = `${icon("zap")}Apply`; }
    }));
    $$("[data-dismiss]", page).forEach((b) => (b.onclick = async () => { try { await api.learningProposal(b.dataset.dismiss, "dismiss"); load(); } catch (e) { toast("error", "Dismiss", e.message); } }));
    $$("[data-retire]", page).forEach((b) => (b.onclick = async () => {
      if (!(await confirm("Retire this lesson?", "It stops being added to prompts. You can switch it back on under Lessons.", { okLabel: "Retire" }))) return;
      try { await api.learningRetire(b.dataset.retire, "no measurable benefit"); toast("success", "Lesson retired"); load(); } catch (e) { toast("error", "Retire", e.message); }
    }));
    $$("[data-pb-seed]", page).forEach((b) => (b.onclick = async () => { b.disabled = true; try { await api.playbookSeed(b.dataset.pbSeed); toast("success", "Playbook seeded"); load(); } catch (e) { toast("error", "Seed", e.message); b.disabled = false; } }));
    $$("[data-pb-agent]", page).forEach((b) => (b.onclick = async () => { b.disabled = true; try { await api.playbookRefresh(b.dataset.pbAgent); toast("info", "Refreshing the playbook", "A cheap agent turn runs in the background."); } catch (e) { toast("error", "Refresh", e.message); b.disabled = false; } }));
    $$("[data-pb-open]", page).forEach((b) => (b.onclick = () => openPlaybook(data.playbooks.find((p) => p.repo === b.dataset.pbOpen))));
    $$("[data-set]", page).forEach((s) => { const flip = () => saveSetting({ [s.dataset.set]: !s.classList.contains("on") }); s.onclick = flip; s.onkeydown = (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } }; });
    $("#lxMode", page).onchange = (e) => saveSetting({ recommend_mode: e.target.value });
    $("#lxSel", page).onchange = (e) => saveSetting({ lessons_selection: e.target.value });
    $("#lxEps", page).onchange = (e) => saveSetting({ explore_rate: Math.max(0, Math.min(50, Number(e.target.value) || 0)) / 100 });
    $("#lxMin", page).onchange = (e) => saveSetting({ auto_approve_min_tasks: Math.max(2, Number(e.target.value) || 3) });
  }

  function openPlaybook(pb) {
    if (!pb) return;
    const m = modal(`<h2>Playbook · ${esc(pb.repo_label || pb.repo)}</h2><p class="hint">Edited sections are kept as you wrote them; new evidence is offered as a suggestion next to them.</p>
      <div class="lx-pb-edit">${Object.entries(pb.labels).map(([k, l]) => { const s = pb.sections[k] || {};
        return `<div class="field"><div class="row between wrap field-label"><label for="pb_${k}">${esc(l)}</label><span class="muted">${s.edited ? "edited by you" : esc(s.source || "")}</span></div>
          <textarea id="pb_${k}" rows="5" data-sec="${k}">${esc(s.text || "")}</textarea>
          ${s.suggestion ? `<div class="lx-suggest"><span class="muted">Suggested (${esc(s.suggestion_source || "")}):</span><pre>${esc(s.suggestion)}</pre><button class="btn xs" data-accept="${k}">Use suggestion</button></div>` : ""}</div>`; }).join("")}</div>
      <div class="modal-actions"><button class="btn" data-close>Close</button><button class="btn primary" id="pbSave">${icon("save")}Save changes</button></div>`, { wide: true });
    $$("[data-accept]", m.body).forEach((b) => (b.onclick = async () => { try { await api.playbookEdit({ repo: pb.repo, section: b.dataset.accept, accept_suggestion: true }); toast("success", "Suggestion used"); m.close(); load(); } catch (e) { toast("error", "Playbook", e.message); } }));
    $("#pbSave", m.body).onclick = async () => {
      try {
        for (const ta of $$("[data-sec]", m.body)) {
          const k = ta.dataset.sec; if ((pb.sections[k]?.text || "") === ta.value) continue;
          await api.playbookEdit({ repo: pb.repo, section: k, text: ta.value });
        }
        toast("success", "Playbook saved"); m.close(); load();
      } catch (e) { toast("error", "Playbook", e.message); }
    };
  }

  load();
  return { update(reason) { if (reason === "learning" || reason === "lessons") load(); }, destroy() { alive = false; } };
}
