// Repositories: local repository status, the worktrees Relay created, and a branch graph per repository.
import { $, $$, esc, icon, toast, confirm, modal, menu, timeAgo, copyText, debounce } from "../ui.js";
import { S, statusOf, navigate } from "../state.js";
import { api } from "../api.js";
import { openNewTask } from "./newtask.js";
import { openRepoEnv } from "./repoenv.js";
import { renderSystemMap } from "./systemmap.js";

const TABS = [["list", "Repositories", "folder"], ["worktrees", "Worktrees", "layers"], ["graph", "Branch graph", "branch"], ["system", "System map", "globe"]];
const FILTERS = [["all", "All"], ["orphaned", "Orphaned"], ["uncommitted", "Uncommitted"], ["merged", "Merged"], ["missing", "Missing"]];
// Sizes survive re-renders and tab switches; walking a worktree with node_modules is the slow part of this page.
const sizes = new Map();

const fmtBytes = (n) => {
  if (n === null || n === undefined) return "";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; n = Number(n) || 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n >= 10 || i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
};
const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch {} return null; };
const tabFrom = (section) => (TABS.some(([k]) => k === section) ? section : "list");
// Lane colours skip red, which stays reserved for failures and destructive actions.
const LANE_TONES = ["blue", "purple", "green", "amber", "accent", "codex", "gemini"];
const laneColor = (lane) => (lane ? `var(--${LANE_TONES[(lane - 1) % LANE_TONES.length]})` : "var(--text-3)");
const toneBadge = (status) => { const st = statusOf({ status }); return `<span class="badge ${esc(st.tone || "")}">${esc(st.label)}</span>`; };

function skeleton(rows = 3) {
  return `<div class="card repo-list" aria-busy="true">${Array.from({ length: rows }, () => `<div class="repo-row skel-row"><div class="stack" style="gap:8px"><span class="skel" style="width:32%"></span><span class="skel" style="width:58%"></span><span class="skel" style="width:44%"></span></div><span class="skel" style="width:90px"></span></div>`).join("")}</div>`;
}
function errorState(title, message) {
  return `<div class="card"><div class="empty">${icon("alert", "lg")}<h3>${esc(title)}</h3><p>${esc(message)}</p><p style="margin-top:14px"><button class="btn" data-retry>${icon("refresh")}Try again</button></p></div></div>`;
}

export function mountRepos(main, section) {
  let tab = tabFrom(section);
  let alive = true;
  const data = { repos: null, root: "", reposError: null, worktrees: null, wtError: null, graph: null, graphError: null, graphFor: null };
  const ui = { filter: "all", repo: "", graphRepo: store("relay.graphRepo") || "" };

  main.innerHTML = `<div class="page repos-page">
    <div class="page-head"><div class="min0"><h1>Repositories</h1><p id="rpSub">Local repositories, the worktrees Relay created for tasks, and how their branches relate.</p></div>
      <div class="page-actions"><button class="btn" id="rpRefresh">${icon("refresh")}Refresh</button><button class="btn primary" id="rpClone">${icon("download")}Clone repository</button></div></div>
    <div class="repo-tabs" role="tablist" id="rpTabs"></div>
    <div id="rpBody"></div></div>`;
  const body = $("#rpBody", main);

  function drawTabs() {
    const counts = { list: data.repos?.length, worktrees: data.worktrees?.length };
    $("#rpTabs", main).innerHTML = TABS.map(([k, l, i]) => `<a role="tab" href="#/repos${k === "list" ? "" : `/${k}`}" class="${k === tab ? "active" : ""}" aria-selected="${k === tab}">${icon(i, "sm")}${l}${counts[k] !== undefined ? `<span class="n">${counts[k]}</span>` : ""}</a>`).join("");
    $(".repo-tabs a.active", main)?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  async function loadRepos() {
    try { const r = await api.repos(); data.repos = r.repos; data.root = r.root; data.reposError = null; }
    catch (e) { data.reposError = e.message; }
  }
  async function loadWorktrees() {
    try { const r = await api.worktrees(); data.worktrees = r.worktrees; data.wtRoot = r.root; data.wtError = null; }
    catch (e) { data.wtError = e.message; }
  }
  async function loadGraph() {
    const repo = ui.graphRepo;
    if (!repo) { data.graph = null; return; }
    try { data.graph = await api.repoGraph(repo); data.graphError = null; data.graphFor = repo; }
    catch (e) { data.graph = null; data.graphError = e.message; data.graphFor = repo; }
  }

  async function refresh(which = "all") {
    if (which === "all" || which === "repos") await loadRepos();
    if (which === "all" || which === "worktrees") await loadWorktrees();
    if (!alive) return;
    if (data.root) $("#rpSub", main).innerHTML = `Git repositories directly under <code class="mono">${esc(data.root)}</code>, the worktrees Relay created for tasks, and how their branches relate.`;
    drawTabs(); render();
  }

  function render() {
    if (!alive) return;
    if (tab !== "system") delete body.dataset.view;
    if (tab === "list") renderList();
    else if (tab === "worktrees") renderWorktrees();
    else if (tab === "system") {
      // The map loads its own data; repository refreshes (task events) must not redraw it under the person's hands.
      if (body.dataset.view !== "system") { body.dataset.view = "system"; renderSystemMap(body, { isAlive: () => alive && tab === "system" }); }
      return;
    }
    else renderGraphTab();
    $$("[data-retry]", body).forEach((b) => (b.onclick = () => { body.innerHTML = skeleton(); refresh(); }));
  }

  // -------------------------------------------------------------------------- repositories
  function renderList() {
    if (data.reposError) { body.innerHTML = errorState("Could not read repositories", data.reposError); return; }
    if (!data.repos) { body.innerHTML = skeleton(); return; }
    if (!data.repos.length) {
      body.innerHTML = `<div class="card"><div class="empty">${icon("folder", "lg")}<h3>No repositories yet</h3><p>Relay lists every git repository directly inside <code class="mono">${esc(data.root)}</code>. Clone one from GitHub, or pick any folder when you create a task.</p>
        <p style="margin-top:14px" class="row" ><span style="flex:1"></span><button class="btn primary" data-clone>${icon("download")}Clone repository</button><button class="btn" data-new>${icon("plus")}New task</button><span style="flex:1"></span></p></div></div>`;
      $("[data-clone]", body).onclick = openClone;
      $("[data-new]", body).onclick = () => openNewTask();
      return;
    }
    body.innerHTML = `<div class="card repo-list">${data.repos.map(repoRow).join("")}</div>`;
    $$("[data-act]", body).forEach((b) => (b.onclick = (e) => repoAction(b.dataset.act, data.repos.find((r) => r.path === b.closest("[data-repo]").dataset.repo), e)));
  }

  function repoRow(r) {
    const lc = r.last_commit;
    const sync = r.detached ? '<span class="badge amber">detached HEAD</span>'
      : !r.upstream ? '<span class="badge outline" title="This branch does not track a remote branch">no upstream</span>'
      : (!r.ahead && !r.behind) ? `<span class="badge green" title="Compared with ${esc(r.upstream)} as of the last fetch">in sync</span>`
      : `<span class="badge ${r.ahead && r.behind ? "red" : r.behind ? "blue" : "outline"}" title="Compared with ${esc(r.upstream)} as of the last fetch">${r.ahead ? `↑${r.ahead}` : ""}${r.ahead && r.behind ? " " : ""}${r.behind ? `↓${r.behind}` : ""}</span>`;
    const changes = r.changes ? `<span class="badge amber" title="${r.untracked ? `${r.untracked} untracked` : ""}">${r.changes} uncommitted</span>` : (lc ? '<span class="badge outline">clean</span>' : "");
    return `<div class="repo-row" data-repo="${esc(r.path)}">
      <div class="repo-main">
        <div class="repo-title">${icon("folder")}<strong>${esc(r.name)}</strong>
          ${r.github ? `<a class="badge outline" href="https://github.com/${esc(r.github)}" target="_blank" rel="noopener" title="Open on GitHub">${icon("github", "sm")}${esc(r.github)}</a>` : r.remote ? `<span class="badge outline remote-url" title="${esc(r.remote)}">${esc(r.remote)}</span>` : '<span class="badge outline">local only</span>'}</div>
        <div class="repo-path mono" title="${esc(r.path)}">${esc(r.path)}</div>
        <div class="repo-facts">
          <span class="badge outline" title="Current branch">${icon("branch", "sm")}<span class="bname">${esc(r.branch || "(no branch)")}</span></span>${sync}${changes}
          ${r.error ? `<span class="badge red" title="${esc(r.error)}">git error</span>` : ""}
          <span class="muted">${r.upstream ? (r.last_fetch ? `fetched ${timeAgo(r.last_fetch)}` : "never fetched") : ""}</span>
        </div>
        <div class="repo-commit">${lc ? `<code class="mono">${esc(lc.short)}</code><span class="truncate" title="${esc(lc.subject)}">${esc(lc.subject)}</span><span class="muted nowrap">${timeAgo(lc.time)} · ${esc(lc.author)}</span>` : '<span class="muted">No commits yet</span>'}</div>
      </div>
      <div class="repo-stats">
        <span class="stat-text" title="Relay tasks for this repository"><b>${r.tasks || 0}</b> task${r.tasks === 1 ? "" : "s"}${r.active_tasks ? ` <span class="badge blue">${r.active_tasks} active</span>` : ""}</span>
        <button class="stat-link" data-act="worktrees" title="Show its Relay worktrees"><b>${r.worktrees || 0}</b> worktree${r.worktrees === 1 ? "" : "s"}</button>
      </div>
      <div class="repo-actions">
        <button class="btn sm" data-act="fetch" ${r.remote ? "" : "disabled"} title="${r.remote ? "git fetch --prune" : "No remote configured"}">${icon("refresh")}Fetch</button>
        <button class="btn sm" data-act="pull" ${r.upstream ? "" : "disabled"} title="${r.upstream ? "Fast-forward from the upstream branch" : "No upstream branch"}">${icon("download")}Pull</button>
        <button class="btn sm" data-act="env" title="Variables, secret files, services and checks for tasks in this repository">${icon("shield")}Environment${r.env_vars || r.env_files ? ` <span class="n">${(r.env_vars || 0) + (r.env_files || 0)}</span>` : ""}</button>
        <button class="btn sm" data-act="new" title="New task in this repository">${icon("plus")}New task</button>
        <button class="btn sm icon" data-act="more" aria-label="More actions" title="More actions">${icon("more")}</button>
      </div>
    </div>`;
  }

  async function repoAction(act, r, e) {
    if (!r) return;
    const btn = e?.currentTarget;
    if (act === "new") { openNewTask({ repo: r.path }); return; }
    if (act === "env") { openRepoEnv(r, { onSaved: () => refresh() }); return; }
    if (act === "worktrees") { ui.repo = r.path; ui.filter = "all"; navigate("#/repos/worktrees"); return; }
    if (act === "more") {
      menu(btn, [
        { label: "Branch graph", icon: "branch", onClick: () => { ui.graphRepo = r.path; store("relay.graphRepo", r.path); navigate("#/repos/graph"); } },
        { label: "Open on GitHub", icon: "github", disabled: !r.github, onClick: () => window.open(`https://github.com/${r.github}`, "_blank", "noopener") },
        ...(S.config.ide_url ? [{ label: "Open in VS Code", icon: "code", onClick: () => window.open(`${S.config.ide_url.replace(/\/$/, "")}/?folder=${encodeURIComponent(r.path)}`, "_blank", "noopener") }] : []),
        { label: "Copy path", icon: "copy", onClick: () => copyText(r.path) },
      ]);
      return;
    }
    if (act === "fetch" || act === "pull") {
      const label = btn.innerHTML;
      btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}${act === "fetch" ? "Fetching…" : "Pulling…"}`;
      try {
        const res = act === "fetch" ? await api.repoFetch(r.path) : await api.repoPull(r.path);
        Object.assign(r, res);
        if (act === "fetch") toast("success", `Fetched ${r.name}`, r.upstream ? (r.behind ? `${r.behind} new commit(s) on ${r.upstream}` : `Up to date with ${r.upstream}`) : "");
        else toast("success", res.pulled ? `Pulled ${res.pulled} commit(s) into ${r.name}` : `${r.name} is already up to date`);
        if (alive && tab === "list") render();
      } catch (err) {
        toast("error", act === "fetch" ? `Could not fetch ${r.name}` : `Did not pull ${r.name}`, err.message);
        if (alive) { btn.disabled = false; btn.innerHTML = label; }
      }
    }
  }

  // -------------------------------------------------------------------------- clone (reuses the wizard's endpoints)
  function openClone() {
    const m = modal(`<h2>Clone repository</h2><p class="hint">Clones into <code class="mono" id="clRoot">${esc(data.root || "the repositories folder")}</code>. An existing clone is fetched and reused.</p>
      <div class="field"><label>Repository</label><input id="clRepo" list="clList" placeholder="owner/repository or git URL" autocomplete="off"><datalist id="clList"></datalist><div class="help" id="clHelp">Loading your GitHub repositories…</div></div>
      <div class="field"><label>Folder name (optional)</label><input id="clName" placeholder="defaults to the repository name"></div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="clGo">${icon("download")}Clone</button></div>`);
    const help = $("#clHelp", m.body);
    api.ghRepos().then((r) => {
      $("#clList", m.body).innerHTML = r.repos.map((x) => `<option value="${esc(x.repo)}">${esc([x.private ? "private" : "public", x.description].filter(Boolean).join(" · "))}</option>`).join("");
      help.textContent = `${r.repos.length} repositories available from GitHub.`;
    }).catch((e) => { help.textContent = `Could not list GitHub repositories (${e.message}). You can still type owner/repository or a git URL.`; });
    const go = async () => {
      const repo = $("#clRepo", m.body).value.trim(); const btn = $("#clGo", m.body);
      if (!repo) { toast("warning", "Pick or type a repository to clone"); return; }
      btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Cloning…`;
      try {
        const r = await api.ghClone(repo, $("#clName", m.body).value.trim());
        m.close();
        toast("success", r.cloned ? "Repository cloned" : "Using existing clone", r.path, { action: { label: "New task", onClick: () => openNewTask({ repo: r.path }) } });
        refresh("repos");
      } catch (e) { toast("error", "Clone failed", e.message); btn.disabled = false; btn.innerHTML = `${icon("download")}Clone`; }
    };
    $("#clGo", m.body).onclick = go;
    $("#clRepo", m.body).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(); } });
  }

  // -------------------------------------------------------------------------- worktrees
  const isOrphan = (w) => !w.task;
  const isMissing = (w) => !w.exists || w.prunable;
  // A task record whose worktree is already gone is history, not clutter: it only shows under Missing.
  const matches = (w, f) => (w.record_only ? f === "missing"
    : f === "all" || (f === "orphaned" && isOrphan(w)) || (f === "uncommitted" && !!w.changes) || (f === "merged" && w.merged) || (f === "missing" && isMissing(w)));

  function renderWorktrees() {
    if (data.wtError) { body.innerHTML = errorState("Could not list worktrees", data.wtError); return; }
    if (!data.worktrees) { body.innerHTML = skeleton(4); return; }
    const all = data.worktrees;
    const repoNames = [...new Map(all.filter((w) => w.repo).map((w) => [w.repo, w.repo_name])).entries()];
    if (ui.repo && !repoNames.some(([p]) => p === ui.repo)) ui.repo = "";
    const inRepo = all.filter((w) => !ui.repo || w.repo === ui.repo);
    const rows = inRepo.filter((w) => matches(w, ui.filter));
    const cleanable = all.filter((w) => w.task?.status === "done" && w.merged && w.exists && !w.busy && !w.changes && !w.unregistered).length;
    body.innerHTML = `${!all.length ? "" : `<div class="wt-toolbar">
        <div class="chips wt-chips">${FILTERS.map(([k, l]) => { const n = inRepo.filter((w) => matches(w, k)).length; return `<button class="chip ${ui.filter === k ? "active" : ""}" data-filter="${k}">${l} <span class="muted">${n}</span></button>`; }).join("")}</div>
        <select class="input wt-repo" id="wtRepo" aria-label="Repository"><option value="">All repositories</option>${repoNames.map(([p, n]) => `<option value="${esc(p)}" ${ui.repo === p ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>
        <span style="flex:1"></span>
        <button class="btn" id="wtClean" ${cleanable ? "" : "disabled"} title="Remove worktrees whose task is delivered and whose branch is merged">${icon("sparkles")}Clean up${cleanable ? ` (${cleanable})` : ""}</button>
      </div>`}
      ${!all.length ? `<div class="card"><div class="empty">${icon("layers", "lg")}<h3>No worktrees</h3><p>Relay creates one isolated worktree per task when it starts, under <code class="mono">${esc(data.wtRoot || "worktrees/")}</code>. Nothing is checked out right now.</p></div></div>`
        : !rows.length ? `<div class="card"><div class="empty small">${icon("filter")} No worktrees match this filter. <button class="btn xs" data-filter="all">Show all</button></div></div>`
        : `<div class="card repo-list">${rows.map(wtRow).join("")}</div>`}`;
    $$("[data-filter]", body).forEach((b) => (b.onclick = () => { ui.filter = b.dataset.filter; renderWorktrees(); }));
    if (all.length) {
      $("#wtRepo", body).onchange = (e) => { ui.repo = e.target.value; renderWorktrees(); };
      $("#wtClean", body).onclick = cleanUp;
    }
    $$("[data-wt]", body).forEach((b) => (b.onclick = () => wtAction(b.dataset.wt, data.worktrees.find((w) => w.path === b.closest("[data-path]").dataset.path), b)));
    loadSizes(rows);
  }

  function wtRow(w) {
    const t = w.task;
    const lc = w.last_commit;
    const badges = [
      w.changes ? `<span class="badge amber">${w.changes} uncommitted</span>` : "",
      w.unregistered ? '<span class="badge red" title="The folder exists but no repository lists it as a worktree">unregistered</span>'
        : isMissing(w) ? `<span class="badge red" title="${w.exists ? "git marks this worktree prunable" : "The folder no longer exists"}">${w.exists ? "prunable" : "missing"}</span>` : "",
      w.branch && !w.branch_exists && !w.unregistered ? '<span class="badge outline" title="The branch no longer exists locally">branch deleted</span>' : "",
      w.merged && w.branch_exists ? `<span class="badge green" title="Every commit is in ${esc(w.default_branch || "the default branch")}">merged</span>` : (w.branch && w.ahead ? `<span class="badge outline" title="Commits not in ${esc(w.default_branch || "the default branch")}">${w.ahead} ahead</span>` : ""),
      w.branch_exists ? (w.pushed ? '<span class="badge blue" title="A remote branch with this name exists (local refs, as of the last fetch)">pushed</span>' : '<span class="badge outline" title="No remote branch with this name">local only</span>') : "",
      w.locked ? '<span class="badge outline">locked</span>' : "",
    ].join("");
    const size = w.exists ? (sizes.has(w.path) ? fmtBytes(sizes.get(w.path)) : (w.size !== null && w.size !== undefined ? fmtBytes(w.size) : '<span class="skel" style="width:44px"></span>')) : "—";
    const canDelBranch = w.merged && w.branch_exists && !w.busy && !w.changes;
    return `<div class="repo-row wt-row" data-path="${esc(w.path)}">
      <div class="repo-main">
        <div class="repo-title">${icon("branch")}<strong class="bname mono-strong" title="${esc(w.branch || "detached")}">${esc(w.branch || (w.detached ? `detached ${w.head}` : "unknown branch"))}</strong></div>
        <div class="repo-path mono" title="${esc(w.path)}">${esc(w.repo_name || "unknown repository")} · ${esc(w.name)}</div>
        <div class="repo-facts">${t ? `<a href="#/task/${esc(t.id)}" class="wt-task truncate" title="${esc(t.name)}">${esc(t.name)}</a>${toneBadge(t.status)}` : '<span class="badge amber" title="No Relay task points at this worktree">orphaned</span>'}${badges}</div>
      </div>
      <div class="repo-stats wt-stats">
        <span title="Disk size"><span class="muted">size</span> <b data-size="${esc(w.path)}">${size}</b></span>
        <span title="${lc ? esc(`${lc.short} ${lc.subject}`) : ""}"><span class="muted">last commit</span> <b>${lc ? timeAgo(lc.time) : "—"}</b></span>
      </div>
      <div class="repo-actions">
        ${t ? `<a class="btn sm" href="#/task/${esc(t.id)}/result" title="Open the task's Try it tab">${icon("play")}Try it</a>` : ""}
        ${w.merged && w.branch_exists ? `<button class="btn sm" data-wt="branch" ${canDelBranch ? "" : "disabled"} title="${canDelBranch ? `Remove the worktree and delete ${esc(w.branch)}` : w.busy ? "The task is still running" : "Commit or discard the uncommitted changes first"}">${icon("trash")}Delete branch</button>` : ""}
        ${w.record_only ? "" : `<button class="btn sm danger" data-wt="remove" ${w.busy ? "disabled" : ""} title="${w.busy ? "Stop the task before removing its worktree" : "Remove this worktree"}">${icon("trash")}Remove</button>`}
      </div>
    </div>`;
  }

  let sizeQueue = [];
  let sizeRunning = 0;
  function loadSizes(rows) {
    sizeQueue = rows.filter((w) => w.exists && !sizes.has(w.path) && (w.size === null || w.size === undefined)).map((w) => w.path);
    const pump = async () => {
      // Two at a time: fast enough for a screenful, gentle on a disk full of node_modules.
      while (alive && sizeRunning < 2 && sizeQueue.length) {
        const p = sizeQueue.shift();
        sizeRunning++;
        api.worktreeSize(p).then((r) => { sizes.set(p, r.size); }).catch(() => { sizes.set(p, null); }).finally(() => {
          sizeRunning--;
          const cell = $$("[data-size]", body).find((c) => c.dataset.size === p);
          if (cell) cell.textContent = sizes.get(p) === null ? "—" : fmtBytes(sizes.get(p));
          pump();
        });
      }
    };
    pump();
  }

  function dirtyConfirm(w) {
    return new Promise((resolve) => {
      const m = modal(`<h2>Remove worktree with uncommitted changes?</h2>
        <p class="hint">${esc(w.name)} has ${w.changes} uncommitted change(s) on ${esc(w.branch || "a detached HEAD")}. Removing it deletes them for good; committed work stays on the branch.</p>
        <label class="check-row"><input type="checkbox" id="rmDiscard"> Discard ${w.changes} uncommitted change(s)</label>
        <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn danger" id="rmOk" disabled>Remove worktree</button></div>`, { onClose: () => resolve(false) });
      const ok = $("#rmOk", m.body);
      $("#rmDiscard", m.body).onchange = (e) => { ok.disabled = !e.target.checked; };
      ok.onclick = () => { resolve(true); m.close(); };
    });
  }

  async function wtAction(act, w, btn) {
    if (!w) return;
    if (act === "remove") {
      let discard = false;
      if (w.changes) { if (!(await dirtyConfirm(w))) return; discard = true; }
      else if (!(await confirm("Remove worktree?", `${w.name} (${w.branch || "detached"}) will be deleted from disk.${w.task ? ` The task "${w.task.name}" keeps its history but loses its working folder.` : ""} The branch itself is kept.`, { danger: true, okLabel: "Remove worktree" }))) return;
      btn.disabled = true;
      try {
        await api.removeWorktree(w.path, discard);
        sizes.delete(w.path);
        toast("success", "Worktree removed", w.merged && w.branch ? `${w.branch} is merged; you can delete the branch now.` : w.path);
      } catch (e) { toast("error", "Worktree not removed", e.message); }
      refresh("all");
    } else if (act === "branch") {
      const checkedOut = w.exists && !w.unregistered;
      if (!(await confirm("Delete merged branch?", `${w.branch} is fully merged into ${w.default_branch || "the default branch"}, so no work is lost.${checkedOut ? " Its worktree is removed first, because git cannot delete a checked-out branch." : ""} Only the local branch is deleted; a pushed copy stays on the remote.`, { danger: true, okLabel: checkedOut ? "Remove worktree and branch" : "Delete branch" }))) return;
      btn.disabled = true;
      try {
        if (checkedOut) { await api.removeWorktree(w.path, false); sizes.delete(w.path); }
        await api.deleteBranch(w.repo, w.branch);
        toast("success", "Branch deleted", w.branch);
      }
      catch (e) { toast("error", "Branch not deleted", e.message); }
      refresh("all");
    }
  }

  async function cleanUp() {
    let list;
    try { list = (await api.cleanupPreview()).candidates; } catch (e) { toast("error", "Could not prepare clean up", e.message); return; }
    if (!list.length) { toast("info", "Nothing to clean up", "Only worktrees whose task is delivered and whose branch is merged are removed."); refresh("worktrees"); return; }
    const m = modal(`<h2>Clean up ${list.length} worktree${list.length === 1 ? "" : "s"}?</h2>
      <p class="hint">These tasks are delivered and their branches are merged, with no uncommitted changes. The worktree folders are deleted; branches and task history are kept.</p>
      <div class="cleanup-list">${list.map((w) => `<div class="cleanup-item"><span class="bname mono-strong">${esc(w.branch)}</span><span class="muted truncate">${esc(w.repo_name)} · ${esc(w.task.name)}</span></div>`).join("")}</div>
      <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn danger" id="cuOk">${icon("trash")}Remove ${list.length}</button></div>`);
    $("#cuOk", m.body).onclick = async () => {
      const b = $("#cuOk", m.body); b.disabled = true; b.innerHTML = `${icon("spinner", "spin")}Removing…`;
      try {
        const r = await api.cleanup(list.map((w) => w.path));
        m.close();
        r.removed.forEach((p) => sizes.delete(p));
        toast(r.skipped.length ? "warning" : "success", `Removed ${r.removed.length} worktree${r.removed.length === 1 ? "" : "s"}`, r.skipped.length ? `${r.skipped.length} skipped: ${r.skipped.map((s) => s.reason).join(", ")}` : "");
      } catch (e) { m.close(); toast("error", "Clean up failed", e.message); }
      refresh("all");
    };
  }

  // -------------------------------------------------------------------------- branch graph
  async function renderGraphTab() {
    if (data.reposError) { body.innerHTML = errorState("Could not read repositories", data.reposError); return; }
    if (!data.repos) { body.innerHTML = skeleton(1); return; }
    if (!data.repos.length) { body.innerHTML = `<div class="card"><div class="empty">${icon("branch", "lg")}<h3>No repositories to graph</h3><p>Clone a repository first; its branches appear here.</p></div></div>`; return; }
    if (!data.repos.some((r) => r.path === ui.graphRepo)) ui.graphRepo = (data.repos.find((r) => r.worktrees) || data.repos[0]).path;
    body.innerHTML = `<div class="card">
      <div class="card-head graph-head"><div class="row wrap min0"><h3>Branches of</h3><select class="input graph-repo" id="grRepo" aria-label="Repository">${data.repos.map((r) => `<option value="${esc(r.path)}" ${r.path === ui.graphRepo ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></div>
        <div class="graph-legend"><span><i class="lg-main"></i>default branch</span><span><i class="lg-open"></i>open branch</span><span><i class="lg-merged"></i>merged commit</span><span><i class="lg-at"></i>tip already merged (fast-forward)</span></div></div>
      <div id="grBody">${data.graphFor === ui.graphRepo && (data.graph || data.graphError) ? "" : `<div class="card-body"><span class="skel" style="width:100%;height:120px;display:block"></span></div>`}</div></div>`;
    $("#grRepo", body).onchange = (e) => { ui.graphRepo = e.target.value; store("relay.graphRepo", ui.graphRepo); data.graph = null; data.graphFor = null; renderGraphTab(); };
    if (data.graphFor !== ui.graphRepo) await loadGraph();
    if (!alive || tab !== "graph") return;
    const host = $("#grBody", body);
    if (!host) return;
    if (data.graphError) { host.innerHTML = `<div class="empty">${icon("alert", "lg")}<h3>Could not build the graph</h3><p>${esc(data.graphError)}</p></div>`; return; }
    const g = data.graph;
    if (!g || g.empty) { host.innerHTML = `<div class="empty">${icon("branch", "lg")}<h3>No commits yet</h3><p>${esc(g?.default || "The default branch")} has no commits, so there is nothing to draw.</p></div>`; return; }
    host.innerHTML = `<div class="graph-scroll" tabindex="0" aria-label="Branch graph, scroll horizontally">${graphSvg(g)}</div>${branchTable(g)}`;
    const scroller = $(".graph-scroll", host);
    // Newest work is on the right; bring the latest commit and its label into view rather than the empty tail.
    const newest = Math.max(...g.nodes.map((n) => n.x));
    scroller.scrollLeft = Math.max(0, 22 + newest * 28 + 260 - scroller.clientWidth);
  }

  function graphSvg(g) {
    const COL = 28, LANE = 34, PADX = 22, PADY = 24, R = 8;
    const byName = new Map(g.branches.map((b) => [b.name, b]));
    const nodes = new Map(g.nodes.map((n) => [n.sha, n]));
    const X = (n) => PADX + n.x * COL, Y = (lane) => PADY + lane * LANE;
    const labelCols = Math.max(0, ...g.branches.map((b) => { const tip = g.nodes.filter((n) => n.branch === b.name).pop(); return tip ? tip.x + 1 + Math.ceil((b.name.length + 14) / 4) : 0; }));
    const lastCol = Math.max(g.columns - 1, labelCols);
    const endX = PADX + lastCol * COL;
    const width = endX + PADX + 8 * Math.max(4, g.default.length) + 16;
    const height = PADY * 2 + (g.lanes - 1) * LANE + 10;
    const parts = [];
    const mainTip = [...g.nodes].reverse().find((n) => n.lane === 0);
    // The default branch runs the full width so its name sits clear of every fork curve.
    if (mainTip) parts.push(`<path d="M${X(mainTip)} ${Y(0)}H${endX}" class="g-edge main tail"/>`);
    for (const e of g.edges) {
      const a = nodes.get(e.from), b = nodes.get(e.to);
      if (!a || !b) continue;
      const br = e.branch ? byName.get(e.branch) : null;
      const x1 = X(a), y1 = Y(a.lane), x2 = X(b), y2 = Y(b.lane);
      let d;
      // Forks drop straight down from their commit and joins rise straight up into the merge,
      // so several branches leaving one commit share a trunk instead of fanning into a knot.
      if (y1 === y2) d = `M${x1} ${y1}H${x2}`;
      else if (e.kind === "join") d = `M${x1} ${y1}H${x2 - R}Q${x2} ${y1} ${x2} ${y1 - R}V${y2}`;
      else d = `M${x1} ${y1}V${y2 - R}Q${x1} ${y2} ${x1 + R} ${y2}H${x2}`;
      parts.push(`<path d="${d}" class="g-edge ${esc(e.kind)} ${br?.merged ? "merged" : ""}" style="--c:${laneColor(br?.lane)}" ${e.dashed ? 'stroke-dasharray="3 4"' : ""}/>`);
    }
    for (const n of g.nodes) {
      const br = n.branch ? byName.get(n.branch) : null;
      const at = n.lane === 0 ? g.branches.filter((b) => b.at && n.sha.startsWith(b.at)) : [];
      const title = `${n.short} · ${n.subject}\n${n.author} · ${new Date(n.time * 1000).toLocaleString()}${n.branch ? `\n${n.branch}` : ""}${at.length ? `\nalso the tip of ${at.map((b) => b.name).join(", ")}` : ""}`;
      if (at.length) parts.push(`<circle cx="${X(n)}" cy="${Y(0)}" r="9.5" class="g-at"/>`);
      if (n.kind === "branch") parts.push(`<circle cx="${X(n)}" cy="${Y(n.lane)}" r="5" class="g-dot ${br?.merged ? "merged" : ""}" style="--c:${laneColor(br?.lane)}"><title>${esc(title)}</title></circle>`);
      else parts.push(`<circle cx="${X(n)}" cy="${Y(0)}" r="${n.kind === "merge" ? 5.5 : 4.5}" class="g-dot main ${n.kind}"><title>${esc(title)}</title></circle>`);
    }
    if (mainTip) parts.push(`<text x="${endX + 10}" y="${Y(0) + 4}" class="g-label main">${esc(g.default)}</text>`);
    for (const b of g.branches) {
      const own = g.nodes.filter((n) => n.branch === b.name);
      if (!own.length) continue;
      const tip = own[own.length - 1], first = own[0];
      const st = b.task ? statusOf({ status: b.task.status }) : null;
      const name = b.name.length > 40 ? `${b.name.slice(0, 39)}…` : b.name;
      // A merged branch's lane continues right into its join, so its name goes under the commits instead.
      const [lx, ly] = b.join ? [X(first) - 5, Y(tip.lane) + 17] : [X(tip) + 11, Y(tip.lane) + 4];
      parts.push(`<text x="${lx}" y="${ly}" class="g-label ${b.merged ? "merged" : ""}"><title>${esc(b.name)}${b.task ? ` · ${esc(b.task.name)} (${esc(st.label)})` : ""}</title>${esc(name)}${b.more ? `<tspan class="g-more"> +${b.more}</tspan>` : ""}${st ? `<tspan class="g-status" style="fill:${st.tone ? `var(--${st.tone})` : "var(--text-3)"}"> ${esc(st.label.toLowerCase())}</tspan>` : ""}</text>`);
    }
    return `<svg class="branch-graph" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="Branch graph of ${esc(g.default)}">${parts.join("")}</svg>`;
  }

  function branchTable(g) {
    if (!g.branches.length) return `<div class="empty small">Only ${esc(g.default)} exists. Task branches appear here once Relay starts a task.</div>`;
    return `<div class="graph-branches">${g.branches.map((b) => `<div class="gb-row">
        <span class="gb-name"><i class="gb-swatch ${b.merged ? "merged" : ""}" style="--c:${laneColor(b.lane)}"></i><span class="bname mono-strong">${esc(b.name)}</span></span>
        <span class="gb-task min0">${b.task ? `<a href="#/task/${esc(b.task.id)}" class="truncate" title="${esc(b.task.name)}">${esc(b.task.name)}</a>${toneBadge(b.task.status)}` : '<span class="muted">no task</span>'}</span>
        <span class="gb-state">${b.merged ? `<span class="badge green">${b.at ? `at ${esc(b.at.slice(0, 7))}` : "merged"}</span>` : `<span class="badge outline">${b.commits.length + (b.more || 0)} commit${b.commits.length + (b.more || 0) === 1 ? "" : "s"}</span>`}${b.fork_outside ? '<span class="badge outline" title="Forked before the commits shown">older fork</span>' : ""}</span>
      </div>`).join("")}${g.hidden_branches ? `<div class="muted gb-more">${g.hidden_branches} older branch(es) not shown.</div>` : ""}</div>`;
  }

  // -------------------------------------------------------------------------- wiring
  $("#rpRefresh", main).onclick = async (e) => {
    const b = e.currentTarget; b.disabled = true;
    data.graphFor = null;
    await refresh("all");
    if (alive) b.disabled = false;
  };
  $("#rpClone", main).onclick = openClone;
  drawTabs(); render();
  refresh("all");
  // Task events arrive in bursts while agents run; counts only need to catch up.
  const soon = debounce(() => { if (alive) refresh(tab === "graph" ? "repos" : "all"); }, 4000);
  return {
    update(reason) {
      if (reason === "route") {
        const next = tabFrom(S.route.section);
        if (next !== tab) { tab = next; drawTabs(); render(); }
      } else if (reason === "task") soon();
    },
    destroy() { alive = false; sizeQueue = []; },
  };
}
