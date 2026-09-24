// Task page: one coherent layout. A header with status, team, repositories, pull requests and the primary actions;
// a phase outline on the left; the conversation (or the live theatre, the changes, the checks, the logs) in the
// middle; a rail of collapsible context sections on the right.
import { $, $$, esc, icon, fmtSec, fmtDur, fmtCost, timeAgo, basename, toast, confirm, menu, copyText } from "../ui.js";
import { S, agentLabel, ROLE_LABEL, roleAgent, roleModelText, roleEffortText, roleDetails, statusOf, LIVE, msgStore, taskElapsed, navigate, bus } from "../state.js";
import { api } from "../api.js";
import { Conversation } from "./conversation.js";
import { mountInspector, TABS } from "./inspector.js";
import { openNewTask, openFollowUp } from "./newtask.js";
import { prStatus, prCached, prPill } from "../prstatus.js";
import { openTaskFolder } from "../ide.js";
import { mountTheatre } from "./theatre.js";
import { mountChanges } from "./changes.js";
import { mountMockups } from "./mockups.js";
import { phases, packages, repoChips } from "../live.js";

const hasTab = (k) => TABS.some(([x]) => x === k);
// Design exploration (orchestrator/exploration.py) adds a Mockups view once a task has explored directions.
const hasMockups = (t) => !!(t && t.exploration && (t.exploration.status || (t.exploration.directions || []).length));
const VIEWS = (t) => [
  ["conversation", "Conversation", "message"],
  ["live", "Live", "monitor"],
  ...(hasTab("design") ? [["design", "Design", "layers"]] : []),
  ...(hasMockups(t) ? [["mockups", "Mockups", "image"]] : []),
  ["changes", "Changes", "branch"],
  ["checks", "Checks", "shield"],
  ["logs", "Logs", "terminal"],
];
const CHECK_TABS = ["checks", "review", "timeline"];
const RD_TONE = { succeeded: "green", failed: "red", running: "amber" };
const RD_ICON = { succeeded: "check", failed: "alert", running: "spinner" };
const RD_TRIGGER = { pr_merged: "after PR merge", delivered: "after delivery", manual: "manual" };

// The redeploy record a delivered task carries (orchestrator/redeploy.py), as one meta pill.
function redeployPill(t) {
  const rd = t.redeploy;
  if (!rd) return "";
  const st = String(rd.status || "");
  const exit = rd.exit_code === null || rd.exit_code === undefined ? "" : ` · exit ${esc(rd.exit_code)}`;
  const when = rd.finished_at || rd.started_at;
  const parts = `${esc(RD_TRIGGER[rd.trigger] || rd.trigger || "manual")}${exit}${when ? ` · ${timeAgo(when)}` : ""}`;
  return `<span class="meta-fact redeploy-fact tone-${esc(RD_TONE[st] || "none")}" title="${esc(rd.command || "")}">${icon(RD_ICON[st] || "info", st === "running" ? "sm spin" : "sm")}<span>Redeploy ${esc(st || "unknown")}</span><span class="redeploy-detail muted">${parts}</span></span>`;
}

// The deploy record (orchestrator/deploy.py): the per-repository recipes that ran after the merge,
// shown beside the redeploy pill with one line per target.
const DP_TONE = { succeeded: "green", failed: "red", running: "amber", blocked: "amber", manual: "none", skipped: "none" };
const DP_ICON = { succeeded: "check", failed: "alert", running: "spinner", blocked: "alert", manual: "info", skipped: "info" };
function deployPill(t) {
  const dp = t.deploy;
  if (!dp) return "";
  const st = String(dp.status || "");
  const targets = dp.targets || [];
  const ran = targets.filter((x) => x.status !== "skipped");
  const when = dp.finished_at || dp.started_at;
  const detail = `${ran.length || targets.length} target${(ran.length || targets.length) === 1 ? "" : "s"}${when ? ` · ${timeAgo(when)}` : ""}`;
  const tip = targets.map((x) => `${x.target}: ${x.status}${x.detail ? ` — ${x.detail}` : ""}`).join("\n");
  return `<span class="meta-fact redeploy-fact tone-${esc(DP_TONE[st] || "none")}" title="${esc(tip)}">${icon(DP_ICON[st] || "info", st === "running" ? "sm spin" : "sm")}<span>Deploy ${esc(st || "unknown")}</span><span class="redeploy-detail muted">${esc(detail)}</span></span>`;
}

export function mountTask(main, id) {
  const getTask = () => S.tasks.get(id);
  let t = getTask();
  if (!t) {
    main.innerHTML = `<div class="page"><div class="empty-state">${icon("alert", "lg")}<h3>Task not found</h3><p>It may have been deleted.</p><p><a class="btn sm" href="#/work">${icon("kanban")}Back to Work</a></p></div></div>`;
    return { update() {}, destroy() {} };
  }
  const views = VIEWS(t);
  let view = views.some(([k]) => k === S.route.tab) ? S.route.tab : "conversation";
  const mounted = {};
  const overlayRail = () => matchMedia("(max-width: 1100px)").matches;
  // Below 1100px the rail covers the page, so it starts closed there whatever was remembered.
  let railOpen = overlayRail() ? false : (() => { try { return localStorage.getItem("relay.rail") !== "0"; } catch { return true; } })();

  main.innerHTML = `<div class="tp" id="tp">
    <header class="tp-head">
      <div class="tp-crumbs" id="tpCrumbs"></div>
      <div class="tp-titlebar">
        <h1 class="tp-title" id="tpTitle"></h1>
        <div class="tp-actions" id="tpActions"></div>
      </div>
      <div class="tp-meta" id="tpMeta"></div>
      <nav class="tp-views" role="tablist" aria-label="Task views">${views.map(([k, l, i], n) => `<button type="button" role="tab" id="tv-${k}" aria-controls="tvp-${k}" data-view="${k}" aria-selected="${k === view}" class="${k === view ? "active" : ""}" title="${esc(l)} (${n + 1})">${icon(i, "sm")}<span>${esc(l)}</span><span class="tv-badge" data-vbadge="${k}" hidden></span></button>`).join("")}
        <span class="tp-views-spacer"></span>
        <button type="button" class="btn xs ghost tp-rail-toggle" id="tpRailToggle" aria-pressed="${railOpen}" title="Show or hide details (I)">${icon("panel", "sm")}<span>Details</span></button>
      </nav>
    </header>
    <div class="tp-body ${railOpen ? "" : "rail-hidden"}" id="tpBody">
      <aside class="tp-outline" id="tpOutline" aria-label="Progress"></aside>
      <section class="tp-main">
        <div class="tp-view" id="tvp-conversation" role="tabpanel" aria-labelledby="tv-conversation" ${view === "conversation" ? "" : "hidden"}>
          <div class="convo-pane">
            <div class="convo-head"><span class="convo-filters"><button type="button" class="chip-toggle sm ${S.ui.showTools ? "on" : ""}" id="toggleTools" aria-pressed="${S.ui.showTools}">${icon("terminal", "sm")}Tools</button><button type="button" class="chip-toggle sm ${S.ui.showThinking ? "on" : ""}" id="toggleThinking" aria-pressed="${S.ui.showThinking}">${icon("brain", "sm")}Reasoning</button><button type="button" class="chip-toggle sm ${S.ui.showGit ? "on" : ""}" id="toggleGit" aria-pressed="${S.ui.showGit}">${icon("branch", "sm")}Git</button></span><button class="files-chip" type="button" id="filesChip" hidden></button></div>
            <div class="convo-wrap">
              <div class="convo ${S.ui.showTools ? "" : "hide-tools"} ${S.ui.showThinking ? "" : "hide-thinking"} ${S.ui.showGit ? "" : "hide-git"}" id="convo"></div>
              <button type="button" class="btn sm primary jump-latest" id="jumpLatest" hidden>${icon("chevronDown")}Jump to latest</button>
            </div>
            <div class="composer" id="composer"></div>
          </div>
        </div>
        ${views.filter(([k]) => k !== "conversation").map(([k]) => `<div class="tp-view" id="tvp-${k}" role="tabpanel" aria-labelledby="tv-${k}" ${k === view ? "" : "hidden"}></div>`).join("")}
      </section>
      <aside class="tp-rail" id="tpRail" aria-label="Details"></aside>
    </div>
  </div>`;
  const page = $("#tp", main);

  const convo = new Conversation($("#convo", page), getTask);
  convo.setJumpButton($("#jumpLatest", page));
  const rail = mountInspector($("#tpRail", page), getTask, { tabs: ["overview"] });
  $("#tpRail", page).insertAdjacentHTML("afterbegin", `<div class="rail-head"><h2>Details</h2><button type="button" class="btn xs ghost icon" id="tpRailClose" aria-label="Close details">${icon("x")}</button></div>`);
  $("#tpRailClose", page).onclick = () => { setRail(false); $("#tpRailToggle", page).focus(); };
  page.addEventListener("keydown", (e) => { if (e.key === "Escape" && railOpen && overlayRail()) { setRail(false); $("#tpRailToggle", page).focus(); } });
  document.addEventListener("mousedown", onOutside);
  function onOutside(e) { if (railOpen && overlayRail() && !e.target.closest("#tpRail") && !e.target.closest("#tpRailToggle") && !e.target.closest(".modal-backdrop, .menu-pop")) setRail(false); }
  const railObserver = collapsibleSections($("#tpRail", page));

  const toggle = (btnId, key, cls) => { $(`#${btnId}`, page).onclick = (e) => { S.ui[key] = !S.ui[key]; $("#convo", page).classList.toggle(cls, !S.ui[key]); e.currentTarget.classList.toggle("on", S.ui[key]); e.currentTarget.setAttribute("aria-pressed", S.ui[key]); }; };
  toggle("toggleTools", "showTools", "hide-tools");
  toggle("toggleThinking", "showThinking", "hide-thinking");
  toggle("toggleGit", "showGit", "hide-git");

  function setRail(open) {
    railOpen = open;
    $("#tpBody", page).classList.toggle("rail-hidden", !open);
    $("#tpRailToggle", page).setAttribute("aria-pressed", open);
    if (!overlayRail()) { try { localStorage.setItem("relay.rail", open ? "1" : "0"); } catch {} }
    if (open && overlayRail()) requestAnimationFrame(() => $("#tpRailClose", page)?.focus());
  }
  $("#tpRailToggle", page).onclick = () => setRail(!railOpen);

  // ---------------------------------------------------------------- views
  function showView(k, sub, { push = true } = {}) {
    if (!views.some(([x]) => x === k)) k = "conversation";
    view = k;
    $$("[data-view]", page).forEach((b) => { const on = b.dataset.view === k; b.classList.toggle("active", on); b.setAttribute("aria-selected", on); });
    $$(".tp-view", page).forEach((v) => (v.hidden = v.id !== `tvp-${k}`));
    const hostEl = $(`#tvp-${k}`, page);
    if (k === "live" && !mounted.live) mounted.live = mountTheatre(hostEl, getTask);
    if (k === "changes") { if (!mounted.changes) mounted.changes = mountChanges(hostEl, getTask, sub || "diff"); else if (sub) mounted.changes.show(sub, { push: false }); }
    if (k === "checks") { if (!mounted.checks) mounted.checks = mountInspector(hostEl, getTask, { tabs: CHECK_TABS, initial: sub || "checks", onTab: (x) => push && history.replaceState(null, "", `#/task/${encodeURIComponent(id)}/checks${x === "checks" ? "" : `/${x}`}`) }); else if (sub) mounted.checks.setTab(sub); }
    if (k === "logs") { if (!mounted.logs) mounted.logs = mountInspector(hostEl, getTask, { tabs: ["logs", "sessions"], initial: sub || "logs" }); else if (sub) mounted.logs.setTab(sub); }
    if (k === "design" && !mounted.design) mounted.design = mountInspector(hostEl, getTask, { tabs: ["design"] });
    if (k === "mockups" && !mounted.mockups) mounted.mockups = mountMockups(hostEl, getTask);
    hostEl.classList.toggle("is-panel", k !== "conversation");
    if (push) history.replaceState(null, "", `#/task/${encodeURIComponent(id)}${k === "conversation" ? "" : `/${k}${sub ? `/${sub}` : ""}`}`);
    if (k === "conversation") requestAnimationFrame(() => convo.follow && convo.scrollBottom());
  }
  $$("[data-view]", page).forEach((b) => (b.onclick = () => showView(b.dataset.view)));
  $("#tpBody", page).addEventListener("click", (e) => {
    const b = e.target.closest("[data-open-tab]");
    if (!b) return;
    const tab = b.dataset.openTab;
    const map = { result: ["changes", "try"], changes: ["changes", "diff"], history: ["changes", "commits"], repository: ["changes", "edit"], checks: ["checks", "checks"], review: ["checks", "review"], timeline: ["checks", "timeline"], logs: ["logs", "logs"], sessions: ["logs", "sessions"], design: ["design"] }[tab];
    if (map) showView(map[0], map[1]);
  });

  // ---------------------------------------------------------------- header
  function renderHeader() {
    t = getTask(); if (!t) return;
    const st = statusOf(t);
    const live = LIVE.has(t.status);
    const repos = [{ name: basename(t.repo), pr_url: t.pr_url, pr_number: t.pr_number, primary: true }, ...Object.entries(t.repo_worktrees || {}).map(([name, w]) => ({ name, pr_url: w.pr_url, pr_number: w.pr_number }))];
    $("#tpCrumbs", page).innerHTML = `<a href="#/work">${icon("kanban", "sm")}Work</a><span class="sep" aria-hidden="true">/</span>${t.number ? `<span class="mono">#${esc(t.number)}</span><span class="sep" aria-hidden="true">/</span>` : ""}${repoChips(t, 4)}${t.branch ? `<span class="branch-chip" title="${esc(t.branch)}">${icon("branch", "sm")}<span class="mono">${esc(t.branch)}</span><button type="button" class="icon-btn" data-copy-branch aria-label="Copy branch name">${icon("copy", "sm")}</button></span>` : ""}`;
    $("[data-copy-branch]", page)?.addEventListener("click", () => copyText(t.branch));
    $("#tpTitle", page).innerHTML = `<span class="tp-name" title="Click to rename" data-rename>${esc(t.name)}</span><span class="status-chip lg tone-${esc(st.tone || "none")} ${st.attention ? "attn" : ""}" title="${esc(t.detail || "")}">${live ? '<span class="live-dot sm"></span>' : ""}${esc(st.label)}</span>`;
    const nameEl = $("[data-rename]", page);
    if (nameEl) nameEl.onclick = async () => {
      const next = (window.prompt("Rename task", t.name) || "").trim();
      if (!next || next === t.name) return;
      try { await api.updateTask(t.id, { name: next }); } catch (e) { toast("error", "Could not rename", e.message); }
    };
    const active = live || t.status === "needs_input" || t.status === "paused";
    const paused = t.status === "paused" || t.pause_requested;
    const primary = t.pending?.kind === "design_pick" ? `<button type="button" class="btn sm primary" data-view-go="mockups">${icon("image")}Pick a direction</button>`
      : t.pending ? `<button type="button" class="btn sm primary" data-jump-pending>${icon(t.pending.kind === "question" ? "send" : "check")}${t.pending.kind === "question" ? "Answer" : "Approve…"}</button>`
      : t.status === "done" ? `<a class="btn sm primary" href="#/review/${esc(t.id)}">${icon("eye")}Review cockpit</a>`
      : ["failed", "stopped", "interrupted"].includes(t.status) ? `<button type="button" class="btn sm primary" data-act="retry" title="${t.checkpoint ? "Resume from checkpoint using the same agent sessions" : "Start again"}">${icon("retry")}${t.checkpoint ? "Resume" : "Retry"}</button>`
      : ["queued", "draft"].includes(t.status) ? `<button type="button" class="btn sm primary" data-act="start" title="Start this task now, alongside anything already running">${icon("play")}Start now</button>`
      : live ? `<button type="button" class="btn sm ${view === "live" ? "" : "primary"}" data-view-go="live">${icon("monitor")}Watch live</button>` : "";
    $("#tpActions", page).innerHTML = `
      ${primary}
      ${active ? (paused ? `<button type="button" class="btn sm" data-act="resume">${icon("play")}Resume</button>` : `<button type="button" class="btn sm" data-act="pause" title="Pause after the current agent turn">${icon("pause")}Pause</button>`) : ""}
      ${active ? `<button type="button" class="btn sm danger" data-act="stop">${icon("stop")}Stop</button>` : ""}
      ${t.status === "done" ? `<button type="button" class="btn sm" data-act="followup" title="Start a new task that builds on this branch">${icon("arrowRight")}Follow up</button>` : ""}
      ${t.status === "done" ? `<button type="button" class="btn sm" data-act="redeploy" ${t.redeploy?.status === "running" ? "disabled" : ""} title="Run the redeploy command configured in Settings → Git & GitHub now, whatever the pull request state is">${icon(t.redeploy?.status === "running" ? "spinner" : "retry", t.redeploy?.status === "running" ? "spin" : "")}Redeploy now</button>` : ""}
      <button type="button" class="btn sm icon" data-share title="Copy a read-only status link for stakeholders" aria-label="Share status">${icon("share")}</button>
      <button type="button" class="btn sm icon" id="moreBtn" title="More" aria-label="More actions">${icon("more")}</button>`;
    $$("[data-act]", $("#tpActions", page)).forEach((b) => (b.onclick = () => act(b.dataset.act)));
    $("[data-view-go]", page)?.addEventListener("click", (e) => showView(e.currentTarget.dataset.viewGo));
    $("[data-jump-pending]", page)?.addEventListener("click", () => { showView("conversation"); requestAnimationFrame(() => { const card = $(".qcard.pending", page); card?.scrollIntoView({ block: "center", behavior: "smooth" }); ($("[data-answer]", card || page) || $("#guidance", page))?.focus({ preventScroll: true }); }); });
    $("[data-share]", page).onclick = () => { copyText(`${location.origin}${location.pathname}#/status/${t.id}`); };
    $("#moreBtn", page).onclick = (e) => menu(e.currentTarget, [
      { label: "Open read-only status page", icon: "share", onClick: () => window.open(`#/status/${t.id}`, "_blank", "noopener") },
      { label: "Retry from scratch (new worktree)", icon: "retry", onClick: () => act("retry-fresh"), disabled: active },
      { label: "Duplicate task", icon: "duplicate", onClick: () => act("duplicate") },
      { label: "Edit task settings", icon: "edit", onClick: () => openNewTask({ edit: t }) },
      "-",
      ...openItems(t),
      { label: "Export report (markdown)", icon: "download", onClick: () => { location.href = `/api/tasks/${encodeURIComponent(t.id)}/export`; } },
      { label: "Copy task id", icon: "copy", onClick: () => copyText(t.id) },
      "-",
      { label: t.archived ? "Unarchive" : "Archive", icon: "archive", onClick: () => act(t.archived ? "unarchive" : "archive") },
      { label: "Delete task…", icon: "trash", danger: true, onClick: () => act("delete") },
    ]);

    const p = t.process || {};
    const tot = t.metrics?.total || {};
    const team = ["supervisor", "worker", "reviewer"].filter((r) => roleAgent(t, r)).map((r) => {
      const a = roleAgent(t, r), on = p.state === "running" && p.role === r;
      return `<span class="team-pill ${on ? "on" : ""}" title="${esc(roleDetails(t, [r]))}"><span class="av xs ${esc(a)}"></span><span class="tpl-role">${esc(ROLE_LABEL[r])}</span><span class="tpl-agent">${esc(agentLabel(a))}</span>${on ? '<span class="live-dot sm"></span>' : ""}</span>`;
    }).join(`<span class="team-arrow" aria-hidden="true">${icon("arrowRight", "sm")}</span>`);
    const prs = repos.filter((r) => r.pr_url || r.pr_number);
    $("#tpMeta", page).innerHTML = `<span class="team-flow">${team}</span>
      ${prs.length ? `<span class="meta-group">${prs.map((r) => r.primary ? prPill(t, prCached(t.id)) : `<a class="pr-pill" href="${esc(r.pr_url)}" target="_blank" rel="noopener">${icon("github", "sm")}<span>${esc(r.name)} #${esc(r.pr_number || "")}</span></a>`).join("")}</span>` : ""}
      ${redeployPill(t)}
      ${deployPill(t)}
      <span class="meta-fact" title="Elapsed">${icon("clock", "sm")}<span class="mono" data-tp-elapsed>${fmtSec(taskElapsed(t))}</span></span>
      <span class="meta-fact" title="${tot.estimated ? "Estimated at API-equivalent rates" : "Reported by the CLI"}">${icon("dollar", "sm")}<span class="mono">${fmtCost(tot.cost_usd, tot.estimated)}</span></span>
      ${lineage(t)}
      ${t.github_issue_url ? `<a class="meta-fact" href="${esc(t.github_issue_url)}" target="_blank" rel="noopener">${icon("issue", "sm")}Issue #${esc(t.github_issue_number)}</a>` : ""}
      <span class="meta-fact muted" title="${esc(t.created_at)}">created ${timeAgo(t.created_at)}</span>`;
    const liveBadge = $('[data-vbadge="live"]', page); if (liveBadge) { liveBadge.hidden = !live; liveBadge.className = "tv-badge live-dot sm"; }
    const pendBadge = $('[data-vbadge="conversation"]', page); if (pendBadge) { pendBadge.hidden = !t.pending; pendBadge.className = "tv-badge attn"; pendBadge.textContent = t.pending ? "1" : ""; }
    const chBadge = $('[data-vbadge="changes"]', page); if (chBadge) { chBadge.hidden = !t.changed_count; chBadge.className = "tv-badge"; chBadge.textContent = t.changed_count || ""; }
  }

  function lineage(t) {
    const out = [];
    if (t.follow_up_of) { const p = S.tasks.get(t.follow_up_of); out.push(`<a class="meta-fact" href="#/task/${encodeURIComponent(t.follow_up_of)}" title="${esc(p ? p.name : t.follow_up_of)}">${icon("retry", "sm")}Follow-up of ${esc(p ? (p.number ? `#${p.number}` : p.name) : "a deleted task")}</a>`); }
    const kids = [...S.tasks.values()].filter((x) => x.follow_up_of === t.id).sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
    if (kids.length) out.push(`<a class="meta-fact" href="#/task/${encodeURIComponent(kids[0].id)}" title="${esc(kids.map((k) => k.name).join("\n"))}">${icon("arrowRight", "sm")}Followed up${kids.length > 1 ? ` ×${kids.length}` : ""}</a>`);
    return out.join("");
  }

  function openItems(t) {
    const waiting = !t.worktree;
    const hint = waiting ? " (once the task starts)" : "";
    return [
      { label: `Open worktree in VS Code${hint}`, icon: "code", disabled: waiting, onClick: () => openTaskFolder(t, "vscode") },
      { label: "Open run folder (logs, artifacts)", icon: "file", disabled: !t.run_dir, onClick: () => openTaskFolder(t, "run") },
      { label: "Copy worktree path", icon: "copy", disabled: waiting, onClick: () => copyText(t.worktree) },
    ];
  }

  async function act(a) {
    t = getTask();
    try {
      if (a === "stop") { if (!(await confirm("Stop this task?", "The running agent process is terminated. You can resume later from the checkpoint.", { danger: true, okLabel: "Stop" }))) return; await api.action(t.id, "stop"); toast("info", "Stopping", t.name); }
      else if (a === "pause") { await api.action(t.id, "pause"); toast("info", "Pause requested", "Takes effect after the current agent turn."); }
      else if (a === "resume") { await api.action(t.id, "resume"); toast("success", "Resumed"); }
      else if (a === "retry") { await api.action(t.id, "retry"); toast("success", t.checkpoint ? "Resuming from checkpoint" : "Queued again"); }
      else if (a === "retry-fresh") { if (!(await confirm("Retry from scratch?", "A new worktree and new agent sessions are created. The current worktree is kept on disk.", { okLabel: "Retry fresh" }))) return; await api.action(t.id, "retry", { fresh: true }); }
      else if (a === "queue") { await api.action(t.id, "queue"); toast("success", "Queued"); }
      else if (a === "start") { await api.action(t.id, "start"); toast("success", "Started", "Running alongside any other active tasks."); }
      else if (a === "followup") { openFollowUp(t); }
      else if (a === "redeploy") { const r = await api.action(t.id, "redeploy"); toast(r.status === "succeeded" ? "success" : "error", r.status === "succeeded" ? "Redeploy succeeded" : "Redeploy failed", `exit ${r.exit_code} · ${r.cwd || ""}`); }
      else if (a === "duplicate") { const n = await api.action(t.id, "duplicate"); toast("success", "Duplicated", n.name); navigate(`#/task/${n.id}`); }
      else if (a === "archive") { await api.action(t.id, "archive", { archived: true }); toast("info", "Archived"); }
      else if (a === "unarchive") { await api.action(t.id, "archive", { archived: false }); }
      else if (a === "delete") {
        if (!(await confirm("Delete this task?", "Conversation, artifacts and logs are removed. The git worktree is also removed.", { danger: true, okLabel: "Delete" }))) return;
        await api.deleteTask(t.id, true); toast("info", "Task deleted"); navigate("#/work");
      }
    } catch (e) { toast("error", a === "redeploy" && e.status === 400 ? "Redeploy unavailable" : "Action failed", e.message); }
  }

  // ---------------------------------------------------------------- outline
  function renderOutline() {
    t = getTask(); if (!t) return;
    const ph = phases(t);
    const pk = packages(t);
    const store = S.msgs.get(id);
    const turns = [...new Set((store?.list || []).map((m) => m.turn).filter((x) => x !== undefined && x !== null && x > 0))];
    const done = new Set(t.packages_done || []);
    const pkRows = pk.list.length
      ? pk.list.map((p, i) => `<li class="ol-sub ${done.has(p.id) || t.status === "done" ? "done" : ""}"><button type="button" data-turn="${i + 1}" title="${esc(p.summary || "")}"><span class="ol-sub-dot" aria-hidden="true"></span><span class="mono">${esc(p.id)}</span><span class="truncate">${esc(p.summary || "")}</span>${p.repo ? `<span class="repo-chip xs">${esc(p.repo)}</span>` : ""}</button></li>`).join("")
      : turns.map((n) => `<li class="ol-sub ${n < (t.checkpoint?.turn || 0) || t.status === "done" ? "done" : ""}"><button type="button" data-turn="${n}"><span class="ol-sub-dot" aria-hidden="true"></span><span>Work package ${n}</span></button></li>`).join("");
    const reviewRounds = t.checkpoint?.review_round || (t.review ? t.review.round : 0);
    const events = [...(t.events || [])].slice(-4).reverse();
    $("#tpOutline", page).innerHTML = `
      <h2 class="ol-h">Progress</h2>
      <ol class="ol-list">${ph.steps.map((s) => `<li class="ol-step st-${s.state}">
          <button type="button" class="ol-btn" data-step="${s.key}"><span class="ol-dot" aria-hidden="true">${s.state === "done" ? icon("check", "sm") : s.state === "fail" ? icon("x", "sm") : ""}</span><span class="ol-label">${esc(s.label)}</span>${s.note ? `<span class="ol-note mono">${esc(s.note)}</span>` : ""}</button>
          ${s.key === "build" && pkRows ? `<ol class="ol-subs">${pkRows}</ol>` : ""}
          ${s.key === "review" && reviewRounds ? `<ol class="ol-subs"><li class="ol-sub ${t.review?.verdict === "PASS" ? "done" : ""}"><button type="button" data-step="review"><span class="ol-sub-dot"></span><span>Round ${esc(reviewRounds)}${t.review?.verdict ? ` · ${esc(t.review.verdict)}` : ""}</span></button></li></ol>` : ""}
        </li>`).join("")}</ol>
      <div class="ol-facts">
        <div><span>Elapsed</span><b class="mono" data-tp-elapsed>${fmtSec(taskElapsed(t))}</b></div>
        <div><span>Turns</span><b class="mono">${t.metrics?.total?.turns || 0}</b></div>
        ${t.verification ? `<div><span>Checks</span><b class="tone-${t.verification.ok ? "ok" : "alarm"}">${t.verification.ok ? "passing" : "failing"}</b></div>` : ""}
      </div>
      ${events.length ? `<h2 class="ol-h">Latest</h2><ul class="ol-events">${events.map((e) => `<li><span class="av xs ${esc(roleAgent(t, e.role) || e.role)}"></span><span><b>${esc(e.title)}</b><time>${esc(timeAgo(e.time))}</time></span></li>`).join("")}</ul><button type="button" class="btn xs ghost" data-open-tab="timeline">${icon("activity", "sm")}Full timeline</button>` : ""}`;
    $$("[data-turn]", page).forEach((b) => (b.onclick = () => { showView("conversation"); const d = $(`.m-turn[data-turn="${CSS.escape(b.dataset.turn)}"]`, page); d?.scrollIntoView({ block: "start", behavior: "smooth" }); }));
    $$("[data-step]", $("#tpOutline", page)).forEach((b) => (b.onclick = () => {
      const k = b.dataset.step;
      if (k === "verify") return showView("checks", "checks");
      if (k === "review") return showView("checks", "review");
      if (k === "deliver") return showView("changes", "try");
      if (k === "design" && hasTab("design")) return showView("design");
      if (k === "explore" && views.some(([x]) => x === "mockups")) return showView("mockups");
      showView("conversation");
      const first = k === "plan" ? $(".m-turn", page) : $(`.m-turn[data-turn="1"]`, page);
      first?.scrollIntoView({ block: "start", behavior: "smooth" });
    }));
  }

  // ---------------------------------------------------------------- composer
  function renderComposer() {
    t = getTask(); if (!t) return;
    const host = $("#composer", page);
    const pending = t.pending;
    const answerMode = pending && pending.kind === "question";
    const approvalMode = pending && (pending.kind === "approval" || pending.kind === "design_approval");
    const designApproval = pending && pending.kind === "design_approval";
    const terminal = ["done", "failed", "stopped", "interrupted", "draft"].includes(t.status);
    const draft = host.querySelector("textarea")?.value || "";
    host.classList.toggle("answer-mode", !!answerMode);
    host.classList.toggle("approval-mode", !!approvalMode);
    host.innerHTML = `
      ${answerMode ? `<div class="mode-note">${icon("question", "sm")}Answering ${esc(agentLabel(pending.agent))}'s question. Your message is delivered immediately.</div>` : ""}
      ${approvalMode ? `<div class="mode-note ok">${icon("shield", "sm")}${designApproval ? `The system design waits for your approval.${hasTab("design") ? ' <button type="button" class="btn xs" data-open-tab="design">Read the design</button>' : ""}` : "Delivery waits for your approval. Approve here or in the card above."}</div>` : ""}
      <div class="box">
        <label class="sr-only" for="guidance">${answerMode ? "Your answer" : "Guidance for the team"}</label>
        <textarea id="guidance" rows="1" placeholder="${answerMode ? "Type your answer…" : terminal ? "Add guidance for when this task resumes…" : "Guide the team… e.g. “use the existing DateService instead of a new helper”"}">${esc(draft)}</textarea>
        <div class="bar">
          ${answerMode ? "" : `<select id="gTo" aria-label="Deliver to"><option value="next">Next turn</option><option value="supervisor">Supervisor · ${esc(agentLabel(roleAgent(t, "supervisor")))}</option><option value="worker">Worker · ${esc(agentLabel(roleAgent(t, "worker")))}</option><option value="both">Both</option></select>
          <div class="seg" id="gMode" role="radiogroup" aria-label="When"><button type="button" role="radio" aria-checked="true" data-m="queue" class="active" title="Delivered at the next safe agent boundary">Queue</button><button type="button" role="radio" aria-checked="false" data-m="interrupt" title="Stop the current agent turn now and redirect it">Interrupt now</button></div>`}
          <span class="spacer"></span>
          <span class="composer-hint"><kbd>Ctrl</kbd><kbd>↵</kbd></span>
          ${approvalMode ? `<button type="button" class="btn sm danger" id="rejectBtn">Request changes</button><button type="button" class="btn sm primary" id="approveBtn">${icon("check")}${designApproval ? "Approve design" : "Approve"}</button>` : `<button type="button" class="btn sm primary" id="sendBtn">${icon("send")}${answerMode ? "Answer" : "Send"}</button>`}
        </div>
      </div>`;
    const ta = $("#guidance", host);
    const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(180, ta.scrollHeight) + "px"; };
    ta.addEventListener("input", grow); grow();
    let mode = "queue";
    $$("#gMode button", host).forEach((b) => (b.onclick = () => { mode = b.dataset.m; $$("#gMode button", host).forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-checked", x === b); }); }));
    const send = async () => {
      const text = ta.value.trim();
      if (!text) return;
      const btn = $("#sendBtn", host); if (btn) btn.disabled = true;
      try {
        if (answerMode) { await api.action(t.id, "answer", { id: pending.id, text }); toast("success", "Answer delivered"); }
        else {
          const to = $("#gTo", host)?.value || "next";
          const r = await api.action(t.id, "guidance", { text, to, mode });
          toast(r.applied === "finished" ? "warning" : "success", r.applied === "interrupt" ? "Interrupting the current turn" : r.applied === "answer" ? "Delivered as the answer" : r.applied === "resumed" ? "Task resumed with your message" : r.applied === "finished" ? "Message saved, but this task has finished" : "Guidance queued", r.applied === "interrupt" ? "The agent restarts with your note." : r.applied === "resumed" ? "It continues from where it stopped; the next agent turn gets your message." : r.applied === "finished" ? r.note : r.applied === "next_boundary" ? (r.note ? r.note : `Delivered to the ${to === "next" ? "next agent turn" : to}.`) : "");
        }
        ta.value = ""; grow();
      } catch (e) { toast("error", "Could not send", e.message); }
      finally { if (btn) btn.disabled = false; }
    };
    $("#sendBtn", host) && ($("#sendBtn", host).onclick = send);
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); if (approvalMode) $("#approveBtn", host).click(); else send(); } });
    if (approvalMode) {
      $("#approveBtn", host).onclick = async () => { try { await api.action(t.id, "approve", { note: ta.value.trim() }); toast("success", designApproval ? "Design approved" : "Approved"); } catch (e) { toast("error", "Failed", e.message); } };
      $("#rejectBtn", host).onclick = async () => { const note = ta.value.trim(); if (!note) { toast("warning", "Add a note", "Describe the changes you want."); ta.focus(); return; } try { await api.action(t.id, "reject", { note }); toast("info", "Changes requested"); ta.value = ""; } catch (e) { toast("error", "Failed", e.message); } };
    }
  }

  // ---------------------------------------------------------------- messages
  async function loadMessages() {
    const store = msgStore(id);
    if (!store.loaded) {
      $("#convo", page).innerHTML = `<div class="convo-skel" aria-busy="true">${'<div class="skel-msg"><span class="skel av"></span><span class="skel line"></span><span class="skel line short"></span></div>'.repeat(4)}</div>`;
      try {
        const r = await api.messages(id, 0, 3000);
        store.list = r.messages || [];
        store.byId = new Map(store.list.map((m) => [m.id, m]));
        store.count = r.count || store.list.length;
        store.loaded = true;
      } catch (e) { $("#convo", page).innerHTML = `<div class="empty-state compact">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    }
    if (S.route.id !== id) return;
    convo.render(store.list);
    renderFilesChip(); renderOutline();
    mounted.live?.update("loaded");
  }

  function renderFilesChip() {
    const chip = $("#filesChip", page);
    if (!chip) return;
    const files = convo.filesTouched();
    chip.hidden = !files.length;
    if (!files.length) return;
    chip.innerHTML = `${icon("edit", "sm")}${files.length} file${files.length === 1 ? "" : "s"} edited`;
    chip.title = files.slice(0, 25).join("\n") + (files.length > 25 ? `\n…and ${files.length - 25} more` : "");
    chip.onclick = () => showView("changes", "diff");
  }

  // A task starts exploring after the page mounted: add the Mockups view in place (before Changes).
  function ensureMockupsView() {
    if (views.some(([k]) => k === "mockups") || !hasMockups(getTask())) return;
    const at = views.findIndex(([k]) => k === "changes");
    views.splice(at < 0 ? views.length : at, 0, ["mockups", "Mockups", "image"]);
    const nav = $(".tp-views", page), before = $('[data-view="changes"]', nav);
    const b = document.createElement("button");
    b.type = "button"; b.setAttribute("role", "tab"); b.id = "tv-mockups"; b.dataset.view = "mockups"; b.setAttribute("aria-controls", "tvp-mockups"); b.setAttribute("aria-selected", "false"); b.title = "Mockups";
    b.innerHTML = `${icon("image", "sm")}<span>Mockups</span><span class="tv-badge" data-vbadge="mockups" hidden></span>`;
    b.onclick = () => showView("mockups");
    nav.insertBefore(b, before || $(".tp-views-spacer", nav));
    const panel = document.createElement("div");
    panel.className = "tp-view"; panel.id = "tvp-mockups"; panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", "tv-mockups"); panel.hidden = true;
    $(".tp-main", page).appendChild(panel);
  }

  renderHeader(); renderComposer(); renderOutline(); loadMessages();
  showView(view, S.route.section, { push: false });
  if (t.pr_url || t.pr_number) prStatus(id);
  const offPr = bus.on("pr", (tid) => { if (tid === id) renderHeader(); });
  const prTimer = setInterval(() => { const x = getTask(); const st = prCached(id)?.state; if (x && (x.pr_url || x.pr_number) && st !== "merged" && st !== "closed") prStatus(id); }, 60000);
  const timer = setInterval(() => {
    t = getTask();
    if (t && (LIVE.has(t.status) || t.process?.state === "running")) {
      convo.updateTyping();
      for (const e of $$("[data-tp-elapsed]", page)) e.textContent = fmtSec(taskElapsed(t));
    }
  }, 1000);
  const offRoute = bus.on("route", () => { if (S.route.id === id && S.route.tab && S.route.tab !== view) showView(S.route.tab, S.route.section, { push: false }); });

  return {
    update(reason, payload) {
      t = getTask(); if (!t) return;
      if (reason === "task") {
        ensureMockupsView();
        renderHeader(); renderOutline();
        if (payload?.pendingChanged || payload?.statusChanged) { renderComposer(); convo.refreshQuestions(); }
        convo.updateTyping();
        rail.refresh("task");
        mounted.checks?.refresh("task"); mounted.logs?.refresh("task"); mounted.design?.refresh("task");
        mounted.changes?.update("task");
        mounted.live?.update("task");
        mounted.mockups?.update("task");
      } else if (reason === "message") { convo.append(payload); convo.updateTyping(); renderFilesChip(); mounted.live?.update("message", payload); if (payload.kind === "handoff" || payload.kind === "plan") renderOutline(); }
      else if (reason === "message_update") { convo.patch(payload); convo.updateTyping(); renderFilesChip(); mounted.live?.update("message_update", payload); }
      else if (reason === "process") { convo.updateTyping(); renderHeader(); mounted.live?.update("process"); }
      else if (reason === "event") { mounted.checks?.refresh("event"); renderOutline(); }
      else if (reason === "artifact") { rail.refresh("artifact"); mounted.checks?.refresh("artifact"); }
    },
    key(k, e) {
      const n = Number(k);
      if (n >= 1 && n <= views.length) { e.preventDefault(); showView(views[n - 1][0]); return; }
      if (k === "i") { e.preventDefault(); setRail(!railOpen); }
      if (k === "t") { e.preventDefault(); showView("changes", "try"); }
    },
    command(c) {
      if (c === "guidance") { showView("conversation"); $("#guidance", page)?.focus(); return; }
      act(c);
    },
    destroy() { clearInterval(timer); clearInterval(prTimer); document.removeEventListener("mousedown", onOutside); rail.destroy(); railObserver.disconnect(); for (const m of Object.values(mounted)) m?.destroy?.(); offRoute(); offPr(); },
  };
}

// Cards in the details rail fold under their heading; the choice is remembered per heading.
function collapsibleSections(root) {
  const KEY = "relay.railFolded";
  let folded = new Set();
  try { folded = new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); } catch {}
  const apply = () => {
    for (const card of root.querySelectorAll(".insp-body .card")) {
      const head = card.querySelector(":scope > .card-head");
      const h = head?.querySelector("h3");
      if (!head || !h) continue;
      const name = h.textContent.trim().replace(/\s+/g, " ").slice(0, 40);
      card.classList.toggle("folded", folded.has(name));
      if (head.dataset.fold) { head.setAttribute("aria-expanded", !folded.has(name)); continue; }
      head.dataset.fold = name;
      head.setAttribute("role", "button");
      head.setAttribute("tabindex", "0");
      head.setAttribute("aria-expanded", !folded.has(name));
      const flip = (e) => {
        if (e.target.closest("button, a, input, select, textarea") && e.target.closest("button, a, input, select, textarea") !== head) return;
        folded.has(name) ? folded.delete(name) : folded.add(name);
        try { localStorage.setItem(KEY, JSON.stringify([...folded])); } catch {}
        apply();
      };
      head.addEventListener("click", flip);
      head.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && e.target === head) { e.preventDefault(); flip(e); } });
    }
  };
  const mo = new MutationObserver(() => apply());
  mo.observe(root, { childList: true, subtree: true });
  apply();
  return mo;
}
