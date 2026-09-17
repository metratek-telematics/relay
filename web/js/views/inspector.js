// Inspector tabs: overview, try it, history, timeline, changes, checks, review, repository, logs, sessions.
import { $, $$, el, esc, icon, md, fmtTime, fmtDateTime, fmtDur, fmtNum, fmtCost, timeAgo, diffHtml, copyText, toast, confirm, prompt, debounce } from "../ui.js";
import { S, agentLabel, agentInitial, ROLE_LABEL, roleAgent, roleModel, roleEffort, statusOf, taskElapsed } from "../state.js";
import { api } from "../api.js";
import { prStatus, PR_STATE, REVIEW_LABEL, checksDetail } from "../prstatus.js";
import { packetHtml, blockedHtml } from "../packet.js";
import { openTaskFolder } from "../ide.js";
import { scorecardCard, bindScorecard } from "./success.js";

export const TABS = [
  ["overview", "Overview", "layers"], ["result", "Try it", "play"], ["history", "History", "clock"], ["timeline", "Timeline", "activity"], ["changes", "Changes", "branch"],
  ["checks", "Checks", "shield"], ["review", "Review", "eye"], ["repository", "Repository", "folder"],
  ["logs", "Logs", "terminal"], ["sessions", "Sessions", "cpu"],
];

export function mountInspector(container, getTask) {
  container.innerHTML = `<nav class="tabs" role="tablist">${TABS.map(([k, l, i]) => `<button role="tab" data-tab="${k}">${icon(i, "sm")}${l}<span class="n" data-n="${k}" hidden></span></button>`).join("")}</nav><div class="insp-body" id="inspBody"></div>`;
  const body = $("#inspBody", container);
  const state = { tab: S.ui.inspectorTab || "overview", repo: { open: new Set(), file: null, dirty: false, tree: null }, diffPath: null, logFilter: "", logAgent: "all", logAuto: true, changes: null,
    hist: { id: null, data: null, open: new Set(), diff: null, scroll: 0 }, work: { id: null, data: null, count: -1, at: 0, busy: false } };
  let logTimer = null;

  const setTab = (tab) => {
    state.tab = tab; S.ui.inspectorTab = tab;
    $$(".tabs button", container).forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    body.classList.toggle("flush", tab === "repository");
    render();
  };
  $$(".tabs button", container).forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));

  function counts() {
    const t = getTask();
    const n = { changes: t?.changed_count || 0, timeline: (t?.events || []).length };
    for (const [k, v] of Object.entries(n)) { const s = $(`[data-n="${k}"]`, container); if (s) { s.textContent = v; s.hidden = !v; } }
  }

  async function render() {
    const t = getTask();
    clearInterval(logTimer); logTimer = null;
    if (!t) { body.innerHTML = '<div class="empty small">Select a task.</div>'; return; }
    counts();
    const tab = state.tab;
    try {
      // Awaited so a failed request lands in the catch below instead of leaving
      // the tab on its loading message forever.
      switch (tab) {
        case "overview": return renderOverview(t);
        case "result": return await renderResult(t);
        case "history": return await renderHistory(t);
        case "timeline": return renderTimeline(t);
        case "changes": return await renderChanges(t);
        case "checks": return await renderArtifact(t, "verification", "No verification has run yet.", true);
        case "review": return await renderReview(t);
        case "repository": return await renderRepository(t);
        case "logs": return await renderLogs(t);
        case "sessions": return renderSessions(t);
      }
    } catch (e) {
      if (state.tab === tab) showError(e, TABS.find(([k]) => k === tab)?.[1] || tab);
    }
  }
  function errorText(e, what) {
    if (e.status === 404 || /task not found/i.test(e.message || "")) return "This task no longer exists. It may have been deleted in another window.";
    return `Could not load ${what.toLowerCase()}: ${e.message || e}`;
  }
  function showError(e, what, host = body) {
    host.innerHTML = `<div class="empty small insp-error" role="alert">${icon("alert", "lg")}<p>${esc(errorText(e, what))}</p><p><button class="btn xs" data-retry>${icon("refresh")}Try again</button></p></div>`;
    $("[data-retry]", host).onclick = () => render();
  }

  // ---------------------------------------------------------------- overview
  function renderOverview(t) {
    // Delivered tasks from older builds stored 0 changed files; asking for the file list corrects the count.
    if (t.status === "done" && !(t.diffstat && t.diffstat.files) && t.worktree && !state.statFixed) { state.statFixed = true; api.files(t.id).catch(() => {}); }
    const st = statusOf(t);
    const m = t.metrics?.total || {};
    const plan = t.plan || {};
    const wf = t.workflow || {};
    const roles = ["supervisor", "worker", "reviewer"].filter((r) => roleAgent(t, r));
    const acc = (plan.acceptance || []).map((a) => `<li class="${t.status === "done" ? "ok" : ""}"><i>${icon("check")}</i><span>${esc(a)}</span></li>`).join("");
    const v = t.verification;
    const r = t.review;
    body.innerHTML = `
      <div class="stack" style="gap:14px">
        <div class="stat-row">
          <div class="stat"><b>${esc(st.label)}</b><span>status</span></div>
          <div class="stat"><b>${t.checkpoint?.turn || 0}<small class="muted">/${wf.max_turns || "—"}</small></b><span>work packages</span></div>
          <div class="stat"><b>${fmtDur(taskElapsed(t))}</b><span>elapsed</span></div>
          <div class="stat"><b>${fmtNum((m.input || 0) + (m.output || 0))}</b><span>tokens</span></div>
          <div class="stat" title="${m.estimated ? "Estimated at API-equivalent rates; subscription usage is not billed per token" : "Reported by the CLI"}"><b>${fmtCost(m.cost_usd, m.estimated)}</b><span>${m.estimated ? "est. cost" : "cost"}</span></div>
        </div>
        ${t.error ? `<div class="ecard"><strong>Failure</strong>${esc(t.error)}</div>` : ""}
        ${t.summary ? `<div class="card"><div class="card-head"><h3>Summary</h3></div><div class="card-body md">${md(t.summary)}</div></div>` : ""}
        ${scorecardCard(t)}
        ${workCard(t)}
        <div class="card"><div class="card-head"><h3>Team</h3><span class="badge outline">${esc(wf.preset || "custom")}</span></div><div class="card-body stack">
          ${roles.map((role) => `<div class="row between"><span class="row"><span class="av sm ${esc(roleAgent(t, role))}">${esc(agentInitial(roleAgent(t, role)))}</span><strong>${esc(agentLabel(roleAgent(t, role)))}</strong><span class="muted">${esc(ROLE_LABEL[role])}</span></span><span class="mono muted">${esc(roleModel(t, role) || "default model")}${roleEffort(t, role) ? ` · ${esc(roleEffort(t, role))} effort` : ""}</span></div>`).join("")}
          <div class="kv" style="margin-top:6px"><dt>Verification</dt><dd>${esc(wf.verify_mode || "each_report")}${(t.verify_commands || []).length ? ` · ${t.verify_commands.length} command(s)` : ""}</dd><dt>Approval gate</dt><dd>${wf.approval_before_delivery ? "before delivery" : "off"}</dd><dt>Agent questions</dt><dd>${wf.allow_agent_questions === false ? "disabled" : "allowed"}</dd><dt>Review rounds</dt><dd>${wf.max_review_rounds || "—"}</dd></div>
        </div></div>
        ${plan.plan ? `<div class="card"><div class="card-head"><h3>Plan</h3>${plan.summary ? `<span class="muted truncate" style="max-width:220px">${esc(plan.summary)}</span>` : ""}</div><div class="card-body md">${md(plan.plan)}${packetHtml(plan, { done: t.status === "done" })}</div></div>` : ""}
        <div class="card"><div class="card-head"><h3>Signals</h3></div><div class="card-body stack">
          <div class="row between"><span>Verification</span>${v ? `<span class="badge ${v.ok ? "green" : "red"}">${v.ok ? "passing" : "failing"}</span>` : '<span class="badge">not run</span>'}</div>
          ${(t.blocked_checks || []).length ? `<div><div class="row between"><span>Checks that could not run</span><span class="badge amber">${t.blocked_checks.length}</span></div>${blockedHtml(t.blocked_checks)}</div>` : ""}
          ${t.environment && t.environment.command ? `<div class="row between"><span>Environment</span><span class="badge ${t.environment.ok ? "green" : t.environment.skipped ? "" : "amber"}" title="${esc(t.environment.command)}">${t.environment.ok ? `ready · ${fmtDur(t.environment.duration)}` : t.environment.skipped ? "already prepared" : "setup failed"}</span></div>` : ""}
          <div class="row between"><span>Independent review</span>${r ? `<span class="badge ${r.verdict === "PASS" ? "green" : "red"}">${esc(r.verdict)} · round ${r.round}</span>` : '<span class="badge">—</span>'}</div>
          <div class="row between"><span>Diff</span>${t.diffstat ? `<span class="diffstat">${t.diffstat.files} files <span class="a">+${t.diffstat.insertions}</span> <span class="d">−${t.diffstat.deletions}</span></span>` : '<span class="muted">—</span>'}</div>
          <div class="row between"><span>Branch</span><span class="mono truncate" style="max-width:240px">${esc(t.branch || "—")}</span></div>
          <div class="row between"><span>Pull request</span>${t.pr_url ? `<a href="${esc(t.pr_url)}" target="_blank" rel="noopener">#${esc(t.pr_number || "")} ${icon("external", "sm")}</a>` : '<span class="muted">—</span>'}</div>
        </div></div>
        <div class="card"><div class="card-head"><h3>Request</h3><span class="badge outline">${esc(t.template || "feature")}</span></div><div class="card-body md">${md(t.requirements || "(from issue)")}${t.github_issue_url ? `<p><a href="${esc(t.github_issue_url)}" target="_blank" rel="noopener">Issue #${esc(t.github_issue_number)} ${icon("external", "sm")}</a></p>` : ""}</div></div>
        <div class="row wrap">
          <button class="btn sm" data-open="vscode" ${t.worktree ? "" : "disabled"} title="${t.worktree ? "" : "Available once the task starts"}">${icon("code")}Open in VS Code</button>
          <button class="btn sm" data-open="run" ${t.run_dir ? "" : "disabled"}>${icon("file")}Run folder</button>
          ${t.worktree ? `<button class="btn sm" data-copy-path="${esc(t.worktree)}">${icon("copy")}Copy path</button>` : ""}
          <a class="btn sm" href="/api/tasks/${encodeURIComponent(t.id)}/export" download>${icon("download")}Export report</a>
        </div>
      </div>`;
    $$("[data-open]", body).forEach((b) => (b.onclick = () => openTaskFolder(t, b.dataset.open)));
    $$("[data-copy-path]", body).forEach((b) => (b.onclick = () => copyText(b.dataset.copyPath)));
    bindScorecard(body, t, () => state.tab === "overview" && render());
    loadWork(t);
  }

  // ---------------------------------------------------------------- work chart
  // Wall time per phase comes from the message log; tokens and cost from the per-turn log,
  // which only runs recorded by this build have. Nothing is estimated to fill the gap.
  function workCard(t) {
    if (!t.started_at && !t.message_count) return "";
    const w = state.work.id === t.id ? state.work.data : null;
    return `<div class="card" id="workCard"><div class="card-head"><h3>Where the time went</h3><span class="muted" style="font-size:11px">${w ? `${fmtDur(w.total_seconds)} wall time` : ""}</span></div><div class="card-body">${w ? workChart(w) : workSkeleton(t)}</div></div>`;
  }
  function workSkeleton(t) {
    const cp = t.checkpoint || {};
    const n = Math.max(2, 1 + (cp.turn || 0) + (cp.review_round || 0) + (t.status === "done" ? 1 : 0));
    return `<div class="wchart" aria-busy="true">${Array.from({ length: n }, () => '<div class="wrow skel"><span class="wlabel"></span><span class="wtrack"></span><span class="wval"></span></div>').join("")}</div><div class="wfoot"><span class="wkey">&nbsp;</span></div>`;
  }
  function workChart(w) {
    const segs = w.segments || [];
    if (!segs.length) return '<div class="empty small">No agent turns yet.</div>';
    const max = Math.max(...segs.map((s) => s.seconds), 1);
    const agents = [...new Set(segs.map((s) => s.agent).filter(Boolean))];
    const rows = segs.map((s) => {
      const u = s.usage;
      const who = s.agent ? agentLabel(s.agent) : "Orchestrator";
      const spend = u ? `${fmtNum(u.input + u.output)} tokens · ${fmtCost(u.cost_usd, u.estimated)}${u.estimated ? " (estimated)" : ""}`
        : (w.usage_recorded ? "no agent turns" : "tokens and cost were not recorded per phase for this run");
      const tip = `${s.label} · ${who}\n${fmtDur(s.seconds)} wall time · ${s.tool_calls} tool call${s.tool_calls === 1 ? "" : "s"}\n${spend}`;
      const colour = s.agent ? `var(--${esc(s.agent)}, ${esc((S.agentMeta[s.agent] || {}).color || "var(--accent)")})` : "var(--text-3)";
      return `<div class="wrow" title="${esc(tip)}" aria-label="${esc(tip)}" tabindex="0">
        <span class="wlabel truncate">${esc(s.label)}</span>
        <span class="wtrack"><span class="wfill" style="width:${Math.max(1.5, (s.seconds / max) * 100).toFixed(1)}%;--c:${colour}"></span></span>
        <span class="wval">${fmtDur(s.seconds)}</span>
      </div>`;
    }).join("");
    const legend = agents.map((a) => `<span class="wkey"><i style="--c:var(--${esc(a)}, ${esc((S.agentMeta[a] || {}).color || "var(--accent)")})"></i>${esc(agentLabel(a))}</span>`).join("");
    return `<div class="wchart">${rows}</div><div class="wfoot">${legend}<span class="wkey"><i style="--c:var(--text-3)"></i>Orchestrator</span>${w.usage_recorded ? "" : '<span class="muted wnote">Hover a bar for details. Tokens and cost per phase are recorded for runs from this version on.</span>'}</div>`;
  }
  async function loadWork(t) {
    const w = state.work;
    const count = t.message_count || 0;
    if (w.busy || (w.id === t.id && w.count === count) || (w.id === t.id && Date.now() - w.at < 5000)) return;
    w.busy = true;
    try {
      const data = await api.work(t.id);
      Object.assign(w, { id: t.id, data, count, at: Date.now() });
    } catch (e) {
      Object.assign(w, { id: t.id, data: { segments: [], total_seconds: 0, error: e.message }, count, at: Date.now() });
    } finally { w.busy = false; }
    const card = $("#workCard", body);
    if (state.tab === "overview" && getTask()?.id === t.id && card) card.outerHTML = workCard(getTask());
  }

  // ---------------------------------------------------------------- timeline
  function renderTimeline(t) {
    const ev = [...(t.events || [])].reverse();
    body.innerHTML = ev.length ? `<div class="tl">${ev.map((e) => `<div class="tl-item"><span class="av sm ${esc(roleAgent(t, e.role) || e.role)}">${esc(agentInitial(roleAgent(t, e.role) || e.role))}</span><div><strong>${esc(e.title)}</strong>${e.detail ? `<p>${esc(e.detail)}</p>` : ""}<time>${fmtTime(e.time)} · ${esc(ROLE_LABEL[e.role] || e.role)}</time></div></div>`).join("")}</div>` : '<div class="empty small">No events yet.</div>';
  }

  // ---------------------------------------------------------------- changes
  async function renderChanges(t) {
    if (!t.worktree) { body.innerHTML = '<div class="empty small">The worktree is created when the task starts.</div>'; return; }
    if (state.diffPath) return renderDiff(t, state.diffPath);
    body.innerHTML = '<div class="empty small">Loading changes…</div>';
    const r = await api.files(t.id);
    if (state.tab !== "changes") return;
    const s = r.stat || {};
    body.innerHTML = `<div class="row between" style="margin-bottom:8px"><span class="diffstat">${s.files || 0} files <span class="a">+${s.insertions || 0}</span> <span class="d">−${s.deletions || 0}</span></span><div class="row"><button class="btn xs" id="fullDiff">${icon("eye")}Full diff</button><button class="btn xs" id="refreshFiles">${icon("refresh")}</button></div></div>` +
      ((r.files || []).length ? r.files.map((f) => `<button class="file-row" data-path="${esc(f.path)}"><span class="fst ${esc(f.status)}">${esc(f.status)}</span><span class="fp" title="${esc(f.path)}">${esc(f.path)}</span></button>`).join("") : '<div class="empty small">Working tree is clean.</div>');
    $$("[data-path]", body).forEach((b) => (b.onclick = () => { state.diffPath = b.dataset.path; render(); }));
    $("#fullDiff", body).onclick = () => { state.diffPath = "*"; render(); };
    $("#refreshFiles", body).onclick = () => render();
  }
  async function renderDiff(t, path) {
    body.innerHTML = `<div class="row between" style="margin-bottom:8px"><button class="btn xs" id="backFiles">${icon("chevron")}Back</button><span class="mono truncate">${esc(path === "*" ? "Full diff" : path)}</span><button class="btn xs" id="copyDiff">${icon("copy")}</button></div><div class="empty small">Loading…</div>`;
    $("#backFiles", body).onclick = () => { state.diffPath = null; render(); };
    let r;
    try { r = await api.diff(t.id, path === "*" ? "" : path); }
    catch (e) { if (state.tab === "changes" && state.diffPath === path) { const slot = body.lastElementChild; slot.className = ""; showError(e, "the diff", slot); } return; }
    if (state.tab !== "changes" || state.diffPath !== path) return;
    body.lastElementChild.outerHTML = diffHtml(r.text);
    $("#copyDiff", body).onclick = () => copyText(r.text || "");
  }

  // ---------------------------------------------------------------- history
  async function renderHistory(t) {
    const h = state.hist;
    if (h.id !== t.id) Object.assign(h, { id: t.id, data: null, open: new Set(), diff: null, scroll: 0 });
    if (h.diff) return renderCommitDiff(t);
    if (h.data) drawHistory(t); else body.innerHTML = historySkeleton();
    let r;
    try { r = await api.commits(t.id); }
    catch (e) {
      if (state.tab !== "history" || h.diff) return;
      body.innerHTML = `<div class="empty small">${icon("alert", "lg")}<p>Could not read the branch history: ${esc(e.message)}</p><p><button class="btn xs" id="histRetry">${icon("refresh")}Try again</button></p></div>`;
      $("#histRetry", body).onclick = () => renderHistory(t);
      return;
    }
    if (state.tab !== "history" || h.diff || getTask()?.id !== t.id) return;
    h.data = r;
    drawHistory(t);
  }
  function historySkeleton() {
    return `<div class="stack hist" aria-busy="true"><div class="card hist-sum"><div class="card-body"><div class="stat-row">${'<div class="stat skel"><b>&nbsp;</b><span>&nbsp;</span></div>'.repeat(4)}</div></div></div>
      <ol class="commits">${'<li class="commit skel"><div class="commit-head"><span class="commit-toggle"><span class="commit-main"><span class="commit-subj">&nbsp;</span><span class="commit-meta">&nbsp;</span></span></span></div></li>'.repeat(3)}</ol></div>`;
  }
  function drawHistory(t) {
    const h = state.hist, r = h.data;
    const n = $('[data-n="history"]', container);
    if (n) { n.textContent = r.ok ? r.summary.commits : ""; n.hidden = !(r.ok && r.summary.commits); }
    if (!r.ok) { body.innerHTML = `<div class="empty small">${icon("branch", "lg")}<p>${esc(r.reason)}</p></div>`; return; }
    const s = r.summary;
    const span = s.first ? (s.first === s.last ? fmtDateTime(s.first) : `${fmtDateTime(s.first)} → ${fmtDateTime(s.last)}`) : "";
    const entries = [...(r.uncommitted ? [r.uncommitted] : []), ...r.commits];
    body.innerHTML = `<div class="stack hist">
      <div class="card hist-sum"><div class="card-body stack" style="gap:10px">
        <div class="stat-row">
          <div class="stat"><b>${s.commits}${s.truncated ? "+" : ""}</b><span>commit${s.commits === 1 ? "" : "s"}</span></div>
          <div class="stat"><b>${s.files}</b><span>files touched</span></div>
          <div class="stat"><b class="diffstat"><span class="a">+${fmtNum(s.additions)}</span> <span class="d">−${fmtNum(s.deletions)}</span></b><span>lines</span></div>
          <div class="stat" title="${esc(span)}"><b>${s.first ? esc(timeAgo(s.last)) : "—"}</b><span>last commit</span></div>
        </div>
        <div class="hist-where"><span class="row wrap" style="min-width:0;gap:4px 8px">${icon("branch", "sm")}<code class="mono">${esc(r.branch || "")}</code><span class="muted nowrap">since <code class="mono">${esc(r.base_short)}</code></span></span>
          <span class="row" style="gap:4px">${r.source === "branch" ? '<span class="badge amber" title="The worktree was removed; history is read from the branch">branch only</span>' : ""}<button class="btn xs" id="histRefresh" title="Reload the history">${icon("refresh")}</button></span></div>
        ${span ? `<div class="muted hist-span">${esc(span)}</div>` : ""}
      </div></div>
      ${entries.length ? `<ol class="commits">${entries.map((c) => commitItem(c, h.open.has(c.sha))).join("")}</ol>` : `<div class="empty small">${icon("clock", "lg")}<p>No commits on this branch yet. Relay commits the team's work when it delivers.</p></div>`}
    </div>`;
    $("#histRefresh", body).onclick = () => { h.data = null; renderHistory(t); };
    $$("[data-commit]", body).forEach((b) => (b.onclick = () => {
      const sha = b.dataset.commit;
      h.open.has(sha) ? h.open.delete(sha) : h.open.add(sha);
      const li = b.closest(".commit");
      li.classList.toggle("open", h.open.has(sha));
      b.setAttribute("aria-expanded", h.open.has(sha));
      $(".commit-detail", li).hidden = !h.open.has(sha);
    }));
    $$("[data-copy]", body).forEach((b) => (b.onclick = () => copyText(b.dataset.copy)));
    $$("[data-cfile]", body).forEach((b) => (b.onclick = () => {
      h.scroll = body.scrollTop;
      h.diff = { sha: b.dataset.sha, path: b.dataset.cfile };
      renderCommitDiff(t);
    }));
    if (h.scroll) { body.scrollTop = h.scroll; h.scroll = 0; }
  }
  function commitItem(c, open) {
    const wip = !!c.uncommitted;
    const meta = wip
      ? '<span class="badge amber">not committed</span><span>in the worktree now</span>'
      : `<code class="sha">${esc(c.short)}</code><span class="truncate">${esc(c.author)}</span><time title="${esc(fmtDateTime(c.time))}">${esc(timeAgo(c.time))}</time>`;
    const files = c.files.map((f) => `<button class="hfile" data-cfile="${esc(f.path)}" data-sha="${esc(c.sha)}" title="${esc(f.path)}"><span class="fp">${esc(f.path)}</span><span class="diffstat">${f.binary ? '<span class="muted">binary</span>' : `<span class="a">+${f.additions}</span><span class="d">−${f.deletions}</span>`}</span></button>`).join("");
    return `<li class="commit ${wip ? "wip" : ""} ${open ? "open" : ""}">
      <div class="commit-head">
        <button class="commit-toggle" data-commit="${esc(c.sha)}" aria-expanded="${open}">
          ${icon("chevron", "sm tw")}
          <span class="commit-main"><span class="commit-subj">${esc(c.subject)}</span>
            <span class="commit-meta">${meta}<span class="diffstat">${c.files.length} file${c.files.length === 1 ? "" : "s"} <span class="a">+${c.additions}</span><span class="d">−${c.deletions}</span></span></span></span>
        </button>
        ${wip ? "" : `<button class="btn xs ghost" data-copy="${esc(c.sha)}" title="Copy commit id ${esc(c.sha)}">${icon("copy", "sm")}</button>`}
      </div>
      <div class="commit-detail" ${open ? "" : "hidden"}>
        ${c.body ? `<details class="commit-body"><summary>Full message</summary><pre>${esc(c.body)}</pre></details>` : ""}
        ${files || '<div class="muted" style="font-size:11px;padding:4px 8px">No file changes (an empty or merge commit).</div>'}
        ${c.files.length > 1 ? `<button class="btn xs ghost" data-cfile="" data-sha="${esc(c.sha)}">${icon("eye", "sm")}${wip ? "All uncommitted changes" : "Whole commit"}</button>` : ""}
      </div>
    </li>`;
  }
  async function renderCommitDiff(t) {
    const h = state.hist, { sha, path } = h.diff;
    const c = [...(h.data?.uncommitted ? [h.data.uncommitted] : []), ...(h.data?.commits || [])].find((x) => x.sha === sha);
    const what = sha === "working" ? "Uncommitted" : (c?.short || sha.slice(0, 7));
    body.innerHTML = `<div class="diff-head"><button class="btn xs" id="histBack">${icon("chevron", "sm flip")}History</button><span class="diff-what"><code class="mono">${esc(what)}</code> <span class="mono">${esc(path || (sha === "working" ? "all changes" : "whole commit"))}</span></span><button class="btn xs" id="copyCDiff" title="Copy diff" disabled>${icon("copy")}</button></div>
      ${c && !path ? `<div class="muted" style="margin:-2px 0 8px;font-size:12px">${esc(c.subject)}</div>` : ""}<div class="empty small">Loading diff…</div>`;
    $("#histBack", body).onclick = () => { h.diff = null; body.scrollTop = 0; renderHistory(t); };
    let r;
    try { r = await api.commitDiff(t.id, sha, path); }
    catch (e) { if (state.tab === "history" && h.diff?.sha === sha && h.diff?.path === path) body.lastElementChild.outerHTML = `<div class="empty small">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (state.tab !== "history" || h.diff?.sha !== sha || h.diff?.path !== path) return;
    body.lastElementChild.outerHTML = diffHtml(r.text);
    const copy = $("#copyCDiff", body); copy.disabled = !r.text; copy.onclick = () => copyText(r.text || "");
  }

  // ---------------------------------------------------------------- artifacts
  async function renderArtifact(t, kind, emptyText, checks = false) {
    body.innerHTML = '<div class="empty small">Loading…</div>';
    const r = await api.artifact(t.id, kind);
    if (state.tab !== (checks ? "checks" : kind)) return;
    const v = t.verification;
    const head = checks && v ? `<div class="vcard ${v.ok ? "" : "fail"}" style="margin-bottom:10px"><div class="vh">${icon(v.ok ? "shield" : "alert")}Last verification ${v.ok ? "passed" : "failed"} <span class="muted" style="font-weight:400">· ${timeAgo(v.time)}</span></div><ul>${(v.items || []).map((i) => `<li class="${i.ok ? "ok" : "fail"}">${icon(i.ok ? "check" : "x")}<span class="truncate">${esc(i.command)}</span><small>${i.ok ? "pass" : `exit ${i.rc}`} · ${fmtDur(i.duration)}</small></li>`).join("")}</ul></div>` : "";
    const cmds = checks && (t.verify_commands || []).length ? `<div class="hint" style="margin-bottom:10px">Commands: ${t.verify_commands.map((c) => `<code>${esc(c)}</code>`).join(" · ")}</div>` : (checks ? '<div class="hint" style="margin-bottom:10px">No verification commands detected for this repository. Add some in the task workflow or Settings → Verification.</div>' : "");
    body.innerHTML = head + cmds + (r.text ? `<pre class="pre">${esc(r.text)}</pre>` : `<div class="empty small">${esc(emptyText)}</div>`);
  }
  async function renderReview(t) {
    const r = t.review;
    body.innerHTML = '<div class="empty small">Loading…</div>';
    const a = await api.artifact(t.id, "review");
    if (state.tab !== "review") return;
    const head = r ? `<div class="hcard ${r.verdict === "PASS" ? "done" : "error"}" style="margin-bottom:10px"><div class="hcard-head"><span class="ttl">Review round ${r.round}</span><span class="badge ${r.verdict === "PASS" ? "green" : "red"}">${esc(r.verdict)}</span></div><div class="hcard-body">${esc(r.summary || "")}${(r.findings || []).length ? `<ul class="findings">${r.findings.map((f) => `<li class="${esc(f.severity || "blocking")}"><code>${esc(f.file || "")}</code> ${esc(f.problem || "")}${f.fix ? `<div class="fix">Fix: ${esc(f.fix)}</div>` : ""}</li>`).join("")}</ul>` : ""}</div></div>` : "";
    body.innerHTML = head + (a.text ? `<div class="doc md">${md(a.text)}</div>` : (r ? "" : '<div class="empty small">No independent review yet. The supervisor acts as the gate unless a reviewer is configured.</div>'));
  }

  // ---------------------------------------------------------------- repository
  async function renderRepository(t) {
    if (!t.worktree && !t.repo) { body.innerHTML = '<div class="empty small">No repository.</div>'; return; }
    body.innerHTML = `<div class="repo"><div class="repo-tree" id="repoTree"><div class="empty small">Loading tree…</div></div><div class="repo-editor" id="repoEditor"><div class="empty small" style="padding-top:60px">${icon("file", "lg")}<h3>Repository editor</h3><p>Open any text file in the task worktree, edit it and save. Agents and reviews see your edits immediately.</p></div></div></div>`;
    try { state.repo.tree = await api.repoTree(t.id); } catch (e) { $("#repoTree", body).innerHTML = `<div class="empty small">${esc(e.message)}</div>`; return; }
    let changed = new Set();
    try { changed = new Set(((await api.files(t.id)).files || []).map((f) => f.path)); } catch {}
    state.repo.changed = changed;
    drawTree(t);
    if (state.repo.file) openFile(t, state.repo.file);
  }
  function drawTree(t) {
    const host = $("#repoTree", body);
    if (!host || !state.repo.tree) return;
    const rows = [];
    const walk = (node, depth) => {
      for (const c of node.children || []) {
        const pad = 6 + depth * 12;
        if (c.type === "dir") {
          const open = state.repo.open.has(c.path);
          rows.push(`<button class="tr dir ${open ? "open" : ""}" data-dir="${esc(c.path)}" style="padding-left:${pad}px">${icon("chevron", "tw")}${icon("folder")}<span class="truncate">${esc(c.name)}</span></button>`);
          if (open) walk(c, depth + 1);
        } else {
          rows.push(`<button class="tr ${state.repo.file === c.path ? "active" : ""} ${state.repo.changed?.has(c.path) ? "mod" : ""}" data-file="${esc(c.path)}" style="padding-left:${pad + 14}px">${icon("file")}<span class="truncate">${esc(c.name)}</span></button>`);
        }
      }
    };
    walk(state.repo.tree, 0);
    host.innerHTML = `<div class="row between" style="padding:2px 4px 6px"><strong class="truncate" style="font-size:12px">${esc(state.repo.tree.name)}</strong><button class="btn xs" id="newFileBtn" title="New file">${icon("plus")}</button></div>` + rows.join("");
    $$("[data-dir]", host).forEach((b) => (b.onclick = () => { const p = b.dataset.dir; state.repo.open.has(p) ? state.repo.open.delete(p) : state.repo.open.add(p); drawTree(t); }));
    $$("[data-file]", host).forEach((b) => (b.onclick = () => openFile(t, b.dataset.file)));
    $("#newFileBtn", host).onclick = async () => {
      const p = await prompt("New file", "Path relative to the repository root.", { placeholder: "src/new_module.py" });
      if (!p) return;
      try { await api.saveRepoFile(t.id, p, "", true); toast("success", "File created", p); state.repo.file = p; renderRepository(t); } catch (e) { toast("error", "Could not create", e.message); }
    };
  }
  async function openFile(t, path) {
    if (state.repo.dirty && state.repo.file && state.repo.file !== path) {
      if (!(await confirm("Discard unsaved changes?", `${state.repo.file} has unsaved edits.`, { danger: true, okLabel: "Discard" }))) return;
    }
    state.repo.file = path; state.repo.dirty = false;
    $$("[data-file]", body).forEach((b) => b.classList.toggle("active", b.dataset.file === path));
    const ed = $("#repoEditor", body);
    ed.innerHTML = '<div class="empty small">Loading…</div>';
    let r;
    try { r = await api.repoFile(t.id, path); } catch (e) { ed.innerHTML = `<div class="empty small">${esc(e.message)}</div>`; return; }
    ed.innerHTML = `<div class="repo-bar"><span class="path">${esc(path)}</span><span class="muted">${fmtNum(r.size)} B</span><span class="dirty" id="dirtyFlag" hidden>● unsaved</span><button class="btn xs" id="copyPath">${icon("copy")}</button><button class="btn xs primary" id="saveFile">${icon("save")}Save</button></div>
      <div class="code-editor"><div class="gutter" id="gutter"></div><textarea id="codeTa" spellcheck="false" wrap="off"></textarea></div>`;
    const ta = $("#codeTa", ed), gutter = $("#gutter", ed), flag = $("#dirtyFlag", ed);
    ta.value = r.text;
    const lines = () => { const n = ta.value.split("\n").length; gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n"); };
    lines();
    ta.addEventListener("input", () => { state.repo.dirty = true; flag.hidden = false; lines(); });
    ta.addEventListener("scroll", () => { gutter.scrollTop = ta.scrollTop; });
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Tab") { e.preventDefault(); const s = ta.selectionStart, en = ta.selectionEnd; ta.value = ta.value.slice(0, s) + "  " + ta.value.slice(en); ta.selectionStart = ta.selectionEnd = s + 2; ta.dispatchEvent(new Event("input")); }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
    });
    const save = async () => {
      const btn = $("#saveFile", ed); btn.disabled = true;
      try { await api.saveRepoFile(t.id, path, ta.value); state.repo.dirty = false; flag.hidden = true; toast("success", "Saved", path); state.repo.changed?.add(path); drawTree(t); }
      catch (e) { toast("error", "Save failed", e.message); }
      finally { btn.disabled = false; }
    };
    $("#saveFile", ed).onclick = save;
    $("#copyPath", ed).onclick = () => copyText(path);
  }

  // ---------------------------------------------------------------- logs
  async function renderLogs(t) {
    body.innerHTML = `<div class="row wrap" style="margin-bottom:8px;gap:6px">
        <div class="seg" id="logAgents">${["all", "claude", "codex", "gemini", "verify", "git", "system"].map((a) => `<button data-a="${a}" class="${state.logAgent === a ? "active" : ""}">${a}</button>`).join("")}</div>
        <input class="input" id="logFilter" placeholder="Filter…" style="width:160px;height:28px;padding:0 8px" value="${esc(state.logFilter)}">
        <label class="row" style="gap:4px;font-size:11px;color:var(--text-2)"><input type="checkbox" id="logAuto" ${state.logAuto ? "checked" : ""}>auto-refresh</label>
        <button class="btn xs" id="logCopy">${icon("copy")}</button>
      </div><div class="log-view" id="logView">Loading…</div>`;
    const view = $("#logView", body);
    const load = async () => {
      let r;
      try { r = await api.artifact(t.id, "raw", 250000); }
      catch (e) { if (state.tab === "logs") view.textContent = errorText(e, "the log"); return; }
      if (state.tab !== "logs") return;
      const lines = (r.text || "").split("\n");
      const f = state.logFilter.toLowerCase();
      const out = [];
      for (const l of lines) {
        const m = l.match(/^\[([^\]]+)\]\s?(.*)$/);
        const tag = m ? m[1] : "";
        const txt = m ? m[2] : l;
        const agent = tag.split(":")[0];
        if (state.logAgent !== "all" && agent !== state.logAgent) continue;
        if (f && !l.toLowerCase().includes(f)) continue;
        out.push(`<span class="lt ${esc(agent)}">${esc(tag ? tag.padEnd(14) : "")}</span>${esc(txt)}`);
      }
      const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 60;
      view.innerHTML = out.length ? out.slice(-4000).join("\n") : "(empty)";
      if (atBottom) view.scrollTop = view.scrollHeight;
      view.dataset.raw = r.text || "";
    };
    await load();
    view.scrollTop = view.scrollHeight;
    $$("#logAgents button", body).forEach((b) => (b.onclick = () => { state.logAgent = b.dataset.a; renderLogs(t); }));
    $("#logFilter", body).addEventListener("input", debounce((e) => { state.logFilter = e.target.value; load(); }, 200));
    $("#logAuto", body).onchange = (e) => { state.logAuto = e.target.checked; };
    $("#logCopy", body).onclick = () => copyText(view.dataset.raw || "");
    logTimer = setInterval(() => { if (state.logAuto && state.tab === "logs") load(); }, 3000);
  }

  // ---------------------------------------------------------------- sessions
  // ---------------------------------------------------------------- try it
  async function renderResult(t) {
    body.innerHTML = '<div class="empty small">Loading…</div>';
    const [h, pr] = await Promise.all([api.handoff(t.id), t.pr_number || t.pr_url ? prStatus(t.id) : null]);
    if (state.tab !== "result") return;
    if (pr?.ok && pr.state === "merged") {
      const acc = (h.sections || []).find((s) => s.id === "accept");
      if (acc) Object.assign(acc, { merged: true, text: `Already merged: pull request #${pr.number} was merged ${timeAgo(pr.merged_at) || ""}. There is nothing left to accept.`, commands: [] });
    } else if (pr?.ok && pr.state === "closed") {
      const acc = (h.sections || []).find((s) => s.id === "accept");
      if (acc) acc.text = `Pull request #${pr.number} was closed without merging ${timeAgo(pr.closed_at) || ""}. Reopen it on GitHub first, then: ` + acc.text;
    }
    if (!h.ready) { body.innerHTML = `<div class="empty small">${icon("clock", "lg")}<p>${esc(h.reason)}</p></div>`; return; }
    const cmds = (s) => s.commands.join("\n");
    body.innerHTML = `<div class="stack handoff" style="gap:14px">
      ${t.pr_number || t.pr_url ? `<div class="card pr-card" id="prCard">${prCardInner(t, pr)}</div>` : ""}
      <div class="card"><div class="card-body stack" style="gap:8px">
        <div class="row between wrap"><span class="row" style="min-width:0">${icon("branch")}<code class="mono truncate" title="${esc(h.branch)}">${esc(h.branch)}</code><button class="btn xs ghost" data-copy="${esc(h.branch)}" title="Copy branch name">${icon("copy", "sm")}</button></span>
          ${h.pr_url ? `<a class="btn sm" href="${esc(h.pr_url)}" target="_blank" rel="noopener">${icon("external")}PR #${esc(h.pr_number)}</a>` : `<span class="badge ${h.pushed ? "green" : ""}">${h.pushed ? "pushed" : "on this machine only"}</span>`}</div>
        ${h.diffstat ? `<div class="muted">${esc(h.diffstat)} vs <code>${esc(h.base)}</code></div>` : ""}
        <div class="row wrap muted" style="gap:6px">${icon("folder", "sm")}<code class="mono truncate" title="${esc(h.worktree)}">${esc(h.worktree)}</code><button class="btn xs ghost" data-copy="${esc(h.worktree)}" title="Copy worktree path">${icon("copy", "sm")}</button>${h.ide ? `<a class="btn xs" href="${esc(h.ide)}" target="_blank" rel="noopener">${icon("code", "sm")}Open in VS Code</a>` : ""}${h.worktree_exists ? "" : '<span class="badge amber">worktree removed</span>'}</div>
      </div></div>
      ${h.sections.map((s, i) => `<section class="ho-step">
        <div class="row between"><h3><span class="ho-n ${s.merged ? "ok" : ""}">${s.merged ? icon("check", "sm") : i + 1}</span>${esc(s.title)}</h3>${s.commands.length ? `<button class="btn xs" data-copy-sec="${i}">${icon("copy", "sm")}Copy</button>` : '<span class="badge purple">merged</span>'}</div>
        <p class="muted">${esc(s.text).replace(/`([^`]+)`/g, "<code>$1</code>")}</p>${s.link ? `<p><a class="btn sm" href="${esc(s.link)}" target="_blank" rel="noopener">${icon("external")}${esc(s.link_label || "Open")}</a></p>` : ""}
        ${s.commands.length ? `<pre class="ho-cmds">${s.commands.map((c) => `<span class="ho-line"><span>${esc(c)}</span><button class="btn xs ghost" data-copy="${esc(c)}" title="Copy this line">${icon("copy", "sm")}</button></span>`).join("")}</pre>` : ""}
      </section>`).join("")}
    </div>`;
    $$("[data-copy]", body).forEach((b) => (b.onclick = () => copyText(b.dataset.copy)));
    $$("[data-copy-sec]", body).forEach((b) => (b.onclick = () => copyText(cmds(h.sections[Number(b.dataset.copySec)]))));
    bindPrCard(t);
  }

  function prCardInner(t, pr) {
    const n = esc(t.pr_number || pr?.number || "");
    const refresh = `<button class="btn xs" id="prRefresh" title="Ask GitHub again">${icon("refresh")}Refresh</button>`;
    if (!pr) return `<div class="card-head"><h3 class="row">${icon("github")}Pull request #${n}</h3>${refresh}</div><div class="card-body"><div class="muted">Asking GitHub for the current status…</div></div>`;
    if (!pr.ok) return `<div class="card-head"><h3 class="row">${icon("github")}Pull request #${n}</h3>${refresh}</div><div class="card-body stack" style="gap:6px"><div class="row" style="color:var(--amber);align-items:flex-start">${icon("alert")}<span style="overflow-wrap:anywhere;min-width:0">Could not read the pull request status: ${esc(pr.error || "unknown error")}</span></div>${t.pr_url ? `<a href="${esc(t.pr_url)}" target="_blank" rel="noopener">Open it on GitHub ${icon("external", "sm")}</a>` : ""}</div>`;
    const st = PR_STATE[pr.state] || PR_STATE.open;
    const c = pr.checks || {};
    const review = REVIEW_LABEL[pr.review_decision] || ["No review required", ""];
    const tone = { passing: "green", failing: "red", pending: "amber" }[c.state] || "";
    const mergeable = pr.state === "open" || pr.state === "draft" ? ({ MERGEABLE: "no conflicts", CONFLICTING: "has conflicts" }[pr.mergeable] || "not computed yet") : "";
    const when = pr.state === "merged" ? `merged ${timeAgo(pr.merged_at)}` : pr.state === "closed" ? `closed ${timeAgo(pr.closed_at)}` : "";
    return `<div class="card-head"><h3 class="row" style="min-width:0">${icon("github")}<span class="truncate">Pull request #${n}</span><span class="badge ${esc(st.tone === "muted" ? "outline" : st.tone)}">${esc(st.label)}</span></h3>${refresh}</div>
      <div class="card-body stack" style="gap:8px">
        <a class="pr-title" href="${esc(pr.url)}" target="_blank" rel="noopener">${esc(pr.title || pr.url)} ${icon("external", "sm")}</a>
        <dl class="kv pr-kv">
          <dt>Review</dt><dd><span class="badge ${esc(review[1])}">${esc(review[0])}</span></dd>
          <dt>Checks</dt><dd><span class="badge ${tone}">${esc(c.total ? c.state : "none")}</span> <span class="muted">${esc(checksDetail(c))}</span></dd>
          <dt>Diff</dt><dd><span class="diffstat">${fmtNum(pr.changed_files || 0)} files <span class="a">+${fmtNum(pr.additions || 0)}</span> <span class="d">−${fmtNum(pr.deletions || 0)}</span></span>${mergeable ? ` <span class="muted">· ${esc(mergeable)}</span>` : ""}</dd>
        </dl>
        <div class="muted pr-fetched">${when ? `${esc(when)} · ` : ""}${esc(pr.repo)} · checked ${esc(timeAgo(pr.fetched))}${pr.stale ? ` · last known status, GitHub said: ${esc(pr.error)}` : ""}</div>
      </div>`;
  }
  function bindPrCard(t) {
    const btn = $("#prRefresh", body);
    if (!btn) return;
    btn.onclick = async () => {
      btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Refreshing`;
      const pr = await prStatus(t.id, true);
      if (state.tab !== "result") return;
      // A change to merged rewrites the Accept step too, so redraw the tab rather than only the card.
      if (pr?.ok && pr.state === "merged" && !$(".ho-n.ok", body)) return renderResult(t);
      const card = $("#prCard", body); if (card) { card.innerHTML = prCardInner(t, pr); bindPrCard(t); }
    };
  }

  function renderSessions(t) {
    const sess = t.sessions || {};
    const m = t.metrics || {};
    const roles = ["supervisor", "worker", "reviewer"];
    const ma = m.agents || {};
    body.innerHTML = `<div class="stack">
      <div class="card"><div class="card-head"><h3>Spend by agent</h3><span class="muted" style="font-size:11px">~ = estimated at API-equivalent rates</span></div><div class="card-body stack">
        ${Object.keys(ma).length ? Object.entries(ma).map(([a, d]) => `<div class="row between"><span class="row"><span class="av sm ${esc(a)}">${esc(agentInitial(a))}</span><strong>${esc(agentLabel(a))}</strong><span class="muted">${d.turns} turn${d.turns === 1 ? "" : "s"}</span></span><span class="mono">${fmtNum(d.input)} in · ${fmtNum(d.cached || 0)} cached · ${fmtNum(d.output)} out · <b>${fmtCost(d.cost_usd, d.estimated)}</b></span></div>`).join("") : '<div class="empty small">No turns yet.</div>'}
        <div class="hint">Claude Code reports real cost. Codex and Gemini report tokens only, so their spend is estimated from the pricing table in Settings → Agents. With subscription plans none of this is billed per token; use it for comparison and budgeting.</div>
      </div></div>
      ${roles.filter((r) => roleAgent(t, r)).map((r) => { const s = sess[r] || {}; const a = roleAgent(t, r); const mr = (m.roles || {})[r] || {}; return `<div class="sess">
        <div class="row between"><span class="row"><span class="av sm ${esc(a)}">${esc(agentInitial(a))}</span><strong>${esc(ROLE_LABEL[r])}</strong><span class="muted">${esc(agentLabel(a))}${s.model ? ` · ${esc(s.model)}` : ""}</span></span><span class="badge ${s.id ? "green" : ""}">${s.id ? `${s.turns || 0} turn${s.turns === 1 ? "" : "s"}` : "no session"}</span></div>
        ${s.id ? `<div class="sid"><span class="muted">session</span><code>${esc(s.id)}</code><button title="Copy" data-copy="${esc(s.id)}">${icon("copy", "sm")}</button></div>` : '<div class="muted" style="font-size:11px">Starts when this role first runs. Sessions persist, so the agent remembers the whole task.</div>'}
        <div class="stat-row"><div class="stat"><b>${fmtNum(mr.input || 0)}</b><span>input tok</span></div><div class="stat"><b>${fmtNum(mr.output || 0)}</b><span>output tok</span></div><div class="stat"><b>${fmtCost(mr.cost_usd, mr.estimated)}</b><span>${mr.estimated ? "est. cost" : "cost"}</span></div><div class="stat"><b>${fmtDur(mr.seconds || 0)}</b><span>time</span></div><div class="stat"><b>${mr.tool_calls || 0}</b><span>tool calls</span></div></div>
      </div>`; }).join("")}
      <div class="card"><div class="card-head"><h3>Resume terminal session</h3></div><div class="card-body hint">
        You can attach to any of these sessions in your own terminal to inspect or continue them:<br>
        Claude: <code>claude --resume &lt;session id&gt;</code> · Codex: <code>codex resume &lt;thread id&gt;</code> · Gemini: <code>gemini --resume latest</code> (from the worktree folder).
      </div></div>
    </div>`;
    $$("[data-copy]", body).forEach((b) => (b.onclick = () => copyText(b.dataset.copy)));
  }

  setTab(state.tab);
  return {
    setTab,
    get tab() { return state.tab; },
    refresh(reason) {
      counts();
      if (reason === "event" && state.tab === "timeline") return render();
      if (reason === "artifact" && ["checks", "review", "overview"].includes(state.tab)) return render();
      if (reason === "task" && ["overview", "sessions"].includes(state.tab)) return render();
      if (reason === "task" && state.tab === "result" && getTask()?.status === "done") return render();
      if (reason === "task" && state.tab === "changes" && !state.diffPath) return render();
      if (reason === "force") return render();
    },
    destroy() { clearInterval(logTimer); },
  };
}
