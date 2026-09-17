// Work: one board for everything to do. Tasks in every state, GitHub issues and the queue, in the columns a change
// moves through: Ideas → Queued → Running → Needs you → Review → Done. Drag to queue, reorder or unqueue; select
// several for bulk actions.
import { $, $$, esc, icon, toast, timeAgo, fmtDur, throttle, basename, confirm } from "../ui.js";
import { S, navigate, statusOf, LIVE, queuedInOrder, agentLabel } from "../state.js";
import { api } from "../api.js";
import { openNewTask, defaultWorkflow } from "./newtask.js";
import { prCached } from "../prstatus.js";
import { activityHtml, teamHtml, repoChips, reposOf, phases } from "../live.js";
import { scoreRing } from "./mission.js";

const COLUMNS = [
  { id: "ideas", label: "Ideas", icon: "sparkles", hint: "Drafts and GitHub issues. Drag one to Queued to line it up." },
  { id: "queued", label: "Queued", icon: "list", hint: "The queue is empty. Drag an idea here to line it up." },
  { id: "running", label: "Running", icon: "activity", hint: "No agent team at work. Queued tasks start when the queue runs, or start one now." },
  { id: "needs", label: "Needs you", icon: "inbox", hint: "Nothing needs you. Questions, approvals and failed runs land here." },
  { id: "review", label: "Review", icon: "eye", hint: "Nothing to review. Delivered tasks wait here until their pull requests merge." },
  { id: "done", label: "Done", icon: "checkCircle", hint: "Merged, closed and stopped work collects here." },
];
const REVIEW_DAYS = 14;

function prStateOf(t) { return prCached(t.id)?.state || t.scorecard?.pr?.state || (t.pr_url ? "open" : null); }

export function columnOf(t) {
  if (t.status === "draft") return "ideas";
  if (t.status === "queued") return "queued";
  if (LIVE.has(t.status)) return "running";
  if (t.status === "needs_input" || t.status === "paused" || t.status === "interrupted" || t.status === "failed") return "needs";
  if (t.status === "done") {
    const st = prStateOf(t);
    if (st === "merged" || st === "closed") return "done";
    if (t.pr_url || t.pr_number) return "review";
    const age = (Date.now() - Date.parse(t.finished_at || t.updated_at || "")) / 86400000;
    return age < REVIEW_DAYS ? "review" : "done";
  }
  return "done";
}

export function mountWork(main, tab) {
  const ui = { q: "", repo: "", issues: true, archived: false, col: "running" };
  try { Object.assign(ui, JSON.parse(localStorage.getItem("relay.work") || "{}")); } catch {}
  if (tab === "issues") { ui.issues = true; ui.col = "ideas"; }
  let alive = true, issues = null, issueErrors = [], issuesLoading = false;
  const selected = new Set();
  const save = () => { try { localStorage.setItem("relay.work", JSON.stringify({ repo: ui.repo, issues: ui.issues, archived: ui.archived, col: ui.col })); } catch {} };

  main.innerHTML = `<div class="page work" id="work">
    <header class="page-header">
      <div class="ph-title"><div class="eyebrow">Work</div><h1>Everything to do, in one place</h1><p class="ph-sub" id="wkSub">&nbsp;</p></div>
      <div class="ph-actions"><button class="btn primary" type="button" id="wkNew">${icon("plus")}New task</button></div>
    </header>
    <div class="toolbar" role="toolbar" aria-label="Filter the board">
      <label class="search-field">${icon("search")}<input id="workSearch" type="search" placeholder="Filter by name, #number, repository or tag" value="${esc(ui.q)}" autocomplete="off" aria-label="Filter tasks"><kbd>/</kbd></label>
      <select id="wkRepo" class="select" aria-label="Repository"></select>
      <button type="button" class="chip-toggle ${ui.issues ? "on" : ""}" id="wkIssues" aria-pressed="${ui.issues}">${icon("github", "sm")}GitHub issues</button>
      <button type="button" class="chip-toggle ${ui.archived ? "on" : ""}" id="wkArchived" aria-pressed="${ui.archived}">${icon("archive", "sm")}Archived</button>
      <span class="toolbar-spacer"></span>
      <span class="muted small" id="wkIssueNote"></span>
    </div>
    <div class="selbar" id="wkSel" hidden></div>
    <nav class="col-switch seg" id="wkSwitch" aria-label="Column"></nav>
    <div class="board" id="wkBoard" role="list"></div>
  </div>`;
  const page = $("#work", main);

  // ---------------------------------------------------------------- data
  function visibleTasks() {
    const q = ui.q.trim().toLowerCase();
    return [...S.tasks.values()].filter((t) => {
      if (!!t.archived !== ui.archived) return false;
      if (ui.repo && !reposOf(t).includes(ui.repo)) return false;
      if (q && !`${t.name} #${t.number || ""} ${reposOf(t).join(" ")} ${(t.tags || []).join(" ")} ${t.github_repo || ""}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }
  function visibleIssues() {
    if (!ui.issues || !issues || ui.archived) return [];
    const q = ui.q.trim().toLowerCase();
    const tasked = new Set([...S.tasks.values()].map((t) => (t.github_issue_key || "").toLowerCase()).filter(Boolean));
    return issues.filter((x) => {
      if (tasked.has(`${x.repo}#${x.number}`.toLowerCase())) return false;
      if (ui.repo && basename(x.local_path || x.repo) !== ui.repo && x.repo.split("/").pop() !== ui.repo) return false;
      if (q && !`${x.title} #${x.number} ${x.repo}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }
  async function loadIssues(force = false) {
    if (!ui.issues || issuesLoading) return;
    issuesLoading = true;
    $("#wkIssueNote", page).textContent = "Loading issues…";
    try { const r = await api.issues({ state: "open", force }); issues = r.issues || []; issueErrors = r.errors || []; }
    catch (e) { issues = []; issueErrors = [{ repo: "GitHub", error: e.message }]; }
    finally { issuesLoading = false; }
    if (alive) draw();
  }

  // ---------------------------------------------------------------- render
  function taskCard(t, col, i) {
    const st = statusOf(t);
    const sel = selected.has(t.id);
    const cost = t.metrics?.total?.cost_usd;
    let body = "";
    if (col === "running") {
      const pk = phases(t).packages;
      body = `<div class="wc-activity">${activityHtml(t, { compact: true })}</div>${pk.total ? `<div class="wc-progress" title="${pk.done} of ${pk.total} work packages"><span class="pk-bar"><i style="width:${Math.round((pk.done / pk.total) * 100)}%"></i></span><span class="mono">${esc(phases(t).steps.find((s) => s.state === "cur")?.label || st.label)}</span></div>` : ""}`;
    } else if (col === "needs") {
      const text = t.pending?.question || (t.pending?.kind === "approval" ? "Waiting for your approval to deliver" : t.pending?.kind === "design_approval" ? "Design waiting for your approval" : "") || t.error || t.detail || "";
      body = `<p class="wc-note ${t.status === "failed" ? "err" : "attn"}">${esc(String(text).slice(0, 160))}</p>`;
    } else if (col === "review" || (col === "done" && t.status === "done")) {
      const pr = prStateOf(t);
      body = `<div class="wc-review">${t.scorecard ? scoreRing(t.scorecard.score, 30) : ""}${t.pr_url ? `<a class="pr-chip state-${esc(pr || "open")}" href="${esc(t.pr_url)}" target="_blank" rel="noopener">${icon("github", "sm")}#${esc(t.pr_number || "")}<span>${esc(pr || "open")}</span></a>` : '<span class="pr-chip state-none">branch only</span>'}${Object.values(t.repo_worktrees || {}).filter((w) => w.pr_url).length ? `<span class="muted small">+${Object.values(t.repo_worktrees).filter((w) => w.pr_url).length} PR</span>` : ""}</div>`;
    } else if (col === "queued") {
      body = t.waiting?.text ? `<p class="wc-note">${icon(t.waiting.kind === "limits" ? "gauge" : t.waiting.kind === "dependency" || t.waiting.kind === "chain" ? "layers" : "clock", "sm")}${esc(t.waiting.text)}</p>` : "";
    }
    const action = col === "needs" ? (t.pending ? `<a class="btn xs primary" href="#/home/needs">${icon("send", "sm")}Answer</a>` : ["failed", "interrupted", "stopped"].includes(t.status) ? `<button type="button" class="btn xs" data-act="retry">${icon("retry", "sm")}Retry</button>` : t.status === "paused" ? `<button type="button" class="btn xs" data-act="resume">${icon("play", "sm")}Resume</button>` : "")
      : col === "review" ? `<a class="btn xs" href="#/review/${esc(t.id)}">${icon("eye", "sm")}Review</a>`
      : col === "ideas" ? `<button type="button" class="btn xs" data-act="queue">${icon("list", "sm")}Queue</button>`
      : col === "queued" ? `<button type="button" class="btn xs ghost" data-act="start" title="Start now">${icon("play", "sm")}</button>` : "";
    return `<article class="wcard ${sel ? "selected" : ""} ${col === "running" ? "is-live" : ""}" role="listitem" tabindex="0" draggable="true" data-id="${esc(t.id)}" data-col="${col}" aria-label="${esc(t.name)}, ${esc(st.label)}">
      <div class="wc-top"><input type="checkbox" class="wc-check" ${sel ? "checked" : ""} aria-label="Select ${esc(t.name)}">${col === "queued" ? `<span class="wc-pos mono" title="Position in the queue">${i + 1}</span>` : ""}${t.number ? `<span class="wc-num mono">#${esc(t.number)}</span>` : ""}<span class="status-chip tone-${esc(st.tone || "none")}">${col === "running" ? '<span class="live-dot sm"></span>' : ""}${esc(st.label)}</span>${t.priority && t.priority !== "normal" ? `<span class="badge ${t.priority === "urgent" ? "red" : t.priority === "high" ? "amber" : ""}">${esc(t.priority)}</span>` : ""}</div>
      <a class="wc-title" href="#/task/${esc(t.id)}" draggable="false">${esc(t.name)}</a>
      ${body}
      <div class="wc-foot">${repoChips(t, 2)}<span class="wc-spacer"></span>${teamHtml(t, { size: "xs" })}</div>
      <div class="wc-foot2"><span class="muted">${esc(timeAgo(t.updated_at))}</span>${cost ? `<span class="mono muted">$${Number(cost).toFixed(2)}</span>` : ""}<span class="wc-spacer"></span>${action}</div>
    </article>`;
  }
  const issueKey = (x) => `issue:${x.repo}#${x.number}`;
  function issueCard(x) {
    const sel = selected.has(issueKey(x));
    return `<article class="wcard is-issue ${sel ? "selected" : ""}" role="listitem" tabindex="0" draggable="true" data-issue="${esc(issueKey(x))}" data-col="ideas" aria-label="Issue ${esc(x.repo)} #${x.number}: ${esc(x.title)}">
      <div class="wc-top"><input type="checkbox" class="wc-check" ${sel ? "checked" : ""} aria-label="Select issue #${x.number}"><span class="wc-num mono">${icon("github", "sm")}#${esc(x.number)}</span><span class="status-chip tone-none">Issue</span></div>
      <a class="wc-title" href="${esc(x.url)}" target="_blank" rel="noopener" draggable="false">${esc(x.title)}</a>
      ${x.labels?.length ? `<div class="wc-labels">${x.labels.slice(0, 4).map((l) => `<span class="iss-label"${/^[0-9a-f]{6}$/i.test(l.color || "") ? ` style="--lc:#${l.color}"` : ""}>${esc(l.name)}</span>`).join("")}</div>` : ""}
      <div class="wc-foot"><span class="repo-chips"><span class="repo-chip">${icon("folder", "sm")}${esc(x.repo.split("/").pop())}</span></span><span class="wc-spacer"></span>${x.assignees?.length ? `<span class="muted small">@${esc(x.assignees[0])}</span>` : ""}</div>
      <div class="wc-foot2"><span class="muted">${esc(timeAgo(x.updated))}</span><span class="wc-spacer"></span><button type="button" class="btn xs" data-issue-act="draft" title="Create a draft task">${icon("plus", "sm")}Task</button></div>
    </article>`;
  }

  function draw() {
    if (!alive) return;
    const rows = visibleTasks();
    const byCol = Object.fromEntries(COLUMNS.map((c) => [c.id, []]));
    for (const t of rows) byCol[columnOf(t)].push(t);
    byCol.queued = queuedInOrder(byCol.queued);
    byCol.running.sort((a, b) => (a.started_at || "").localeCompare(b.started_at || ""));
    const newest = (a, b) => (b.updated_at || "").localeCompare(a.updated_at || "");
    for (const k of ["ideas", "needs", "review", "done"]) byCol[k].sort(newest);
    const iss = visibleIssues();
    S.workOrder = COLUMNS.flatMap((c) => byCol[c.id].map((t) => t.id));

    // Repository filter options follow the tasks that exist.
    const repos = [...new Set([...S.tasks.values()].flatMap(reposOf))].sort();
    $("#wkRepo", page).innerHTML = `<option value="">All repositories</option>${repos.map((r) => `<option ${r === ui.repo ? "selected" : ""}>${esc(r)}</option>`).join("")}`;
    const total = rows.length;
    $("#wkSub", page).textContent = `${total} task${total === 1 ? "" : "s"}${iss.length ? ` and ${iss.length} open issue${iss.length === 1 ? "" : "s"}` : ""}${ui.repo ? ` in ${ui.repo}` : ""}${ui.archived ? " · archived" : ""}`;
    $("#wkIssueNote", page).innerHTML = ui.issues && issueErrors.length ? `${icon("alert", "sm")} Some issues could not be listed` : ui.issues && issues === null ? "Loading issues…" : "";
    $("#wkIssueNote", page).title = issueErrors.map((e) => `${e.repo}: ${e.error}`).join("\n");

    const counts = Object.fromEntries(COLUMNS.map((c) => [c.id, byCol[c.id].length + (c.id === "ideas" ? iss.length : 0)]));
    $("#wkSwitch", page).innerHTML = COLUMNS.map((c) => `<button type="button" data-col-pick="${c.id}" class="${ui.col === c.id ? "active" : ""}" aria-pressed="${ui.col === c.id}">${esc(c.label)} <span class="n">${counts[c.id]}</span></button>`).join("");
    $$("[data-col-pick]", page).forEach((b) => (b.onclick = () => { ui.col = b.dataset.colPick; save(); draw(); }));

    const scroll = new Map($$(".wcol-body", page).map((x) => [x.parentElement.dataset.col, x.scrollTop]));
    $("#wkBoard", page).innerHTML = COLUMNS.map((c) => {
      const cards = byCol[c.id].map((t, i) => taskCard(t, c.id, i)).join("") + (c.id === "ideas" ? iss.map(issueCard).join("") : "");
      return `<section class="wcol ${ui.col === c.id ? "active" : ""} col-${c.id}" data-col="${c.id}" aria-label="${esc(c.label)}">
        <header class="wcol-head"><span class="wcol-ic">${icon(c.icon, "sm")}</span><h2>${esc(c.label)}</h2><span class="wcol-n">${counts[c.id]}</span>${c.id === "ideas" ? `<button type="button" class="btn xs ghost icon" data-new-draft aria-label="New task" title="New task">${icon("plus")}</button>` : ""}</header>
        <div class="wcol-body" data-drop="${c.id}">${cards || `<div class="wcol-empty">${esc(c.hint)}</div>`}</div>
      </section>`;
    }).join("");
    for (const [col, top] of scroll) { const b = $(`.wcol[data-col="${col}"] .wcol-body`, page); if (b) b.scrollTop = top; }
    bindBoard(byCol, iss);
    drawSel(iss);
  }

  function drawSel(iss) {
    const bar = $("#wkSel", page);
    for (const k of [...selected]) { if (k.startsWith("issue:") ? !iss.some((x) => issueKey(x) === k) : !S.tasks.has(k)) selected.delete(k); }
    if (!selected.size) { bar.hidden = true; return; }
    const tasks = [...selected].filter((k) => !k.startsWith("issue:")).map((id) => S.tasks.get(id));
    const issuesSel = [...selected].filter((k) => k.startsWith("issue:"));
    const can = (pred) => tasks.some(pred);
    bar.hidden = false;
    bar.innerHTML = `<span class="sel-count"><b>${selected.size}</b> selected</span>
      ${can((t) => t.status === "draft") ? `<button type="button" class="btn sm" data-bulk="queue">${icon("list")}Queue</button>` : ""}
      ${can((t) => ["draft", "queued", "failed", "stopped", "interrupted"].includes(t.status)) ? `<button type="button" class="btn sm" data-bulk="start">${icon("play")}Start now</button>` : ""}
      ${can((t) => ["failed", "stopped", "interrupted"].includes(t.status)) ? `<button type="button" class="btn sm" data-bulk="retry">${icon("retry")}Retry</button>` : ""}
      ${can((t) => LIVE.has(t.status) || t.status === "needs_input" || t.status === "paused") ? `<button type="button" class="btn sm danger" data-bulk="stop">${icon("stop")}Stop</button>` : ""}
      ${can((t) => t.status === "queued") ? `<button type="button" class="btn sm" data-bulk="unqueue">${icon("unqueue")}Unqueue</button>` : ""}
      ${tasks.length ? `<button type="button" class="btn sm" data-bulk="archive">${icon("archive")}${ui.archived ? "Unarchive" : "Archive"}</button>` : ""}
      ${issuesSel.length ? `<button type="button" class="btn sm primary" data-bulk="issues">${icon("sparkles")}Queue ${issuesSel.length} issue${issuesSel.length === 1 ? "" : "s"} in order</button>` : ""}
      <span class="toolbar-spacer"></span><button type="button" class="btn sm ghost" data-bulk="clear">Clear</button>`;
    $$("[data-bulk]", bar).forEach((b) => (b.onclick = () => bulk(b.dataset.bulk, tasks, issuesSel, iss)));
  }

  async function bulk(kind, tasks, issuesSel, iss) {
    if (kind === "clear") { selected.clear(); draw(); return; }
    if (kind === "issues") { await createFromIssues(issuesSel.map((k) => iss.find((x) => issueKey(x) === k)).filter(Boolean), "sequential"); return; }
    const pick = { queue: (t) => t.status === "draft", start: (t) => ["draft", "queued", "failed", "stopped", "interrupted"].includes(t.status), retry: (t) => ["failed", "stopped", "interrupted"].includes(t.status),
      stop: (t) => LIVE.has(t.status) || t.status === "needs_input" || t.status === "paused", unqueue: (t) => t.status === "queued", archive: () => true }[kind];
    const targets = tasks.filter(pick);
    if (kind === "stop" && !(await confirm(`Stop ${targets.length} task${targets.length === 1 ? "" : "s"}?`, "Running agents are terminated; each can resume later from its checkpoint.", { danger: true, okLabel: "Stop" }))) return;
    let ok = 0;
    for (const t of targets) {
      try {
        if (kind === "archive") await api.action(t.id, "archive", { archived: !ui.archived });
        else await api.action(t.id, kind);
        ok++;
      } catch (e) { toast("error", `${t.name}: could not ${kind}`, e.message); }
    }
    if (ok) toast("success", `${ok} task${ok === 1 ? "" : "s"}: ${kind === "archive" && ui.archived ? "unarchived" : { queue: "queued", start: "started", retry: "retried", stop: "stopping", unqueue: "unqueued", archive: "archived" }[kind]}`);
    selected.clear(); draw();
  }

  async function createFromIssues(list, mode) {
    if (!list.length) return;
    try {
      const res = await api.createIssueTasks({ issues: list.map((x) => ({ repo: x.repo, number: x.number })), mode, stop_on_failure: false, template: "feature", priority: "normal", workflow: defaultWorkflow(), comment: false, label: "", related_repos: true });
      if (res.created?.length) toast("success", `${res.created.length} task${res.created.length === 1 ? "" : "s"} ${mode === "draft" ? "created as drafts" : "queued"}`, res.created.map((c) => c.name).slice(0, 3).join(" · "));
      for (const p of [...(res.skipped || []), ...(res.errors || [])]) toast("warning", String(p.issue || "Issue"), p.reason || p.error || "");
      for (const x of list) selected.delete(issueKey(x));
      loadIssues(true);
    } catch (e) { toast("error", "Could not create tasks from issues", e.message); }
  }

  // ---------------------------------------------------------------- interaction
  function bindBoard(byCol, iss) {
    const board = $("#wkBoard", page);
    $("[data-new-draft]", board)?.addEventListener("click", () => openNewTask());
    $$(".wcard", board).forEach((card) => {
      const id = card.dataset.id, ik = card.dataset.issue;
      const key = id || ik;
      $(".wc-check", card).onchange = (e) => { e.target.checked ? selected.add(key) : selected.delete(key); card.classList.toggle("selected", e.target.checked); drawSel(iss); };
      card.addEventListener("click", (e) => {
        if (e.target.closest("a, button, input")) return;
        if (e.shiftKey || e.metaKey || e.ctrlKey || selected.size) { const cb = $(".wc-check", card); cb.checked = !cb.checked; cb.onchange({ target: cb }); return; }
        if (id) navigate(`#/task/${id}`); else window.open(iss.find((x) => issueKey(x) === ik)?.url, "_blank", "noopener");
      });
      card.addEventListener("keydown", (e) => {
        if (e.target !== card) return;
        if (e.key === "Enter") { e.preventDefault(); card.click(); }
        if (e.key === "x" || e.key === " ") { e.preventDefault(); const cb = $(".wc-check", card); cb.checked = !cb.checked; cb.onchange({ target: cb }); }
        if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); const sib = e.key === "ArrowDown" ? card.nextElementSibling : card.previousElementSibling; sib?.focus?.(); }
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); const col = card.closest(".wcol"); const next = (e.key === "ArrowRight" ? col.nextElementSibling : col.previousElementSibling); $(".wcard", next || col)?.focus(); }
      });
      $$("[data-act]", card).forEach((b) => (b.onclick = async () => {
        b.disabled = true;
        try { await api.action(id, b.dataset.act); toast("success", { queue: "Queued", start: "Started", retry: "Retrying", resume: "Resumed" }[b.dataset.act] || "Done", S.tasks.get(id)?.name || ""); }
        catch (err) { toast("error", "Could not do that", err.message); b.disabled = false; }
      }));
      $("[data-issue-act]", card)?.addEventListener("click", () => createFromIssues([iss.find((x) => issueKey(x) === ik)], "draft"));
      card.addEventListener("dragstart", (e) => { card.classList.add("dragging"); board.classList.add("is-dragging"); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", key); } catch {} });
      card.addEventListener("dragend", () => { card.classList.remove("dragging"); board.classList.remove("is-dragging"); $$(".drop-over", board).forEach((x) => x.classList.remove("drop-over")); });
    });
    $$("[data-drop]", board).forEach((zone) => {
      zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drop-over"); });
      zone.addEventListener("dragleave", (e) => { if (!zone.contains(e.relatedTarget)) zone.classList.remove("drop-over"); });
      zone.addEventListener("drop", async (e) => {
        e.preventDefault(); zone.classList.remove("drop-over");
        const key = e.dataTransfer.getData("text/plain") || $(".wcard.dragging", board)?.dataset.id || $(".wcard.dragging", board)?.dataset.issue;
        const to = zone.dataset.drop;
        if (!key) return;
        if (key.startsWith("issue:")) {
          const x = iss.find((i) => issueKey(i) === key);
          if (to === "queued") return createFromIssues([x], "parallel");
          if (to === "ideas") return;
          return toast("info", "Queue an issue first", "Drop it on Queued to create and queue its task.");
        }
        const t = S.tasks.get(key);
        if (!t) return;
        const from = columnOf(t);
        try {
          if (from === "ideas" && to === "queued") { await api.action(t.id, "queue"); toast("success", "Queued", t.name); }
          else if (from === "queued" && to === "ideas") { await api.action(t.id, "unqueue"); toast("info", "Back to ideas", t.name); }
          else if (from === "queued" && to === "queued") {
            const target = e.target.closest(".wcard");
            const order = byCol.queued.map((x) => x.id);
            const a = order.indexOf(t.id), b = target ? order.indexOf(target.dataset.id) : order.length - 1;
            if (a >= 0 && b >= 0 && a !== b) { for (let k = 0; k < Math.abs(b - a); k++) { const r = await api.moveInQueue(t.id, b < a ? "up" : "down"); if (r.task) S.tasks.set(r.task.id, { ...S.tasks.get(r.task.id), ...r.task }); } draw(); }
          } else if ((from === "ideas" || from === "queued" || from === "needs") && to === "running") { await api.action(t.id, from === "needs" ? "retry" : "start"); toast("success", "Started", t.name); }
          else if (from !== to) toast("info", "Relay moves it there by itself", `${COLUMNS.find((c) => c.id === to).label} follows the task's progress.`);
        } catch (err) { toast("error", "Could not move it", err.message); }
      });
    });
  }

  $("#wkNew", page).onclick = () => openNewTask();
  $("#workSearch", page).addEventListener("input", (e) => { ui.q = e.target.value; draw(); });
  $("#wkRepo", page).onchange = (e) => { ui.repo = e.target.value; save(); draw(); };
  $("#wkIssues", page).onclick = (e) => { ui.issues = !ui.issues; e.currentTarget.classList.toggle("on", ui.issues); e.currentTarget.setAttribute("aria-pressed", ui.issues); save(); if (ui.issues && issues === null) loadIssues(); draw(); };
  $("#wkArchived", page).onclick = (e) => { ui.archived = !ui.archived; e.currentTarget.classList.toggle("on", ui.archived); e.currentTarget.setAttribute("aria-pressed", ui.archived); selected.clear(); save(); draw(); };

  draw();
  if (ui.issues) loadIssues();
  const soon = throttle(() => {
    if ($(".wcard.dragging", page)) return;
    const focusKey = document.activeElement?.closest?.(".wcard") && (document.activeElement.closest(".wcard").dataset.id || document.activeElement.closest(".wcard").dataset.issue);
    draw();
    if (focusKey) $(`.wcard[data-id="${CSS.escape(focusKey)}"], .wcard[data-issue="${CSS.escape(focusKey)}"]`, page)?.focus({ preventScroll: true });
  }, 600);
  const tick = setInterval(() => { for (const b of $$(".wcard.is-live .wc-activity", page)) { const t = S.tasks.get(b.closest(".wcard").dataset.id); if (t) b.innerHTML = activityHtml(t, { compact: true }); } }, 2000);

  return {
    update(reason) { if (["task", "autopilot", "queue"].includes(reason)) soon(); if (reason === "activity" || reason === "process") { /* the tick repaints activity lines */ } },
    destroy() { alive = false; clearInterval(tick); },
  };
}
