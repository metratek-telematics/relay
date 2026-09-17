// Issues board: open issues across every repository Relay knows, turned into tasks in the order you choose.
import { $, $$, esc, icon, md, modal, toast, timeAgo } from "../ui.js";
import { S, statusOf, navigate } from "../state.js";
import { api } from "../api.js";
import { workflowEditor, defaultWorkflow } from "./newtask.js";

const keyOf = (x) => `${x.repo}#${x.number}`.toLowerCase();
const REUSABLE = new Set(["done", "failed", "stopped"]);
const TONE_BADGE = { accent: "accent", blue: "blue", amber: "amber", purple: "purple", green: "green", red: "red" };

// The task for each issue comes from the live task list, so its status badge follows the run without refetching.
function taskIndex() {
  const index = new Map();
  for (const t of S.tasks.values()) {
    const key = (t.github_issue_key || (t.issue && t.github_repo ? `${t.github_repo}#${t.issue}` : "")).toLowerCase();
    if (!key) continue;
    const rank = [!t.archived, !REUSABLE.has(t.status), t.created_at || ""];
    const prev = index.get(key);
    if (!prev || rank.join("|") > prev.rank.join("|")) index.set(key, { t, rank });
  }
  return index;
}

const labelStyle = (c) => (/^[0-9a-f]{6}$/i.test(c || "") ? ` style="--lc:#${c}"` : "");

export function mountIssues(main) {
  const ui = { q: "", repo: "", label: "", assignee: "", state: "open", mine: false, hideTasked: false };
  try { Object.assign(ui, JSON.parse(localStorage.getItem("relay.issues.filters") || "{}")); } catch {}
  let data = null, loading = false, alive = true;
  const selected = new Map(); // key → issue, in the order you picked them
  const open = new Set();

  main.innerHTML = `<div class="page issues-page" id="issPage">
    <div class="page-head"><div><h1>Issues</h1><p>Open issues from your cloned repositories and watched GitHub repositories. Pick some and turn them into tasks that run one after the other.</p></div>
      <div class="page-actions"><span class="muted iss-fetched" id="issFetched"></span><button class="btn" id="issRefresh">${icon("refresh")}Refresh</button></div></div>
    <div class="card iss-toolbar">
      <div class="iss-search">${icon("search")}<input id="issQ" placeholder="Search title, body or #number" autocomplete="off" value="${esc(ui.q)}" aria-label="Search issues"></div>
      <select id="issRepo" aria-label="Repository"></select>
      <select id="issLabel" aria-label="Label"></select>
      <select id="issAssignee" aria-label="Assignee"></select>
      <select id="issState" aria-label="State">${["open", "closed", "all"].map((s) => `<option value="${s}" ${ui.state === s ? "selected" : ""}>${s[0].toUpperCase() + s.slice(1)}</option>`).join("")}</select>
      <label class="iss-check"><input type="checkbox" id="issMine" ${ui.mine ? "checked" : ""}> Assigned to me anywhere</label>
      <label class="iss-check"><input type="checkbox" id="issHide" ${ui.hideTasked ? "checked" : ""}> Hide issues with a task</label>
    </div>
    <div class="iss-selbar" id="issSelbar"></div>
    <div id="issErrors"></div>
    <div class="card iss-list" id="issList"><div class="empty small">Loading issues…</div></div>
  </div>`;
  const page = $("#issPage", main);
  const save = () => { try { localStorage.setItem("relay.issues.filters", JSON.stringify(ui)); } catch {} };

  async function load(force = false) {
    if (loading) return;
    loading = true;
    $("#issRefresh", page).innerHTML = `${icon("spinner", "spin")}Refreshing`;
    try {
      data = await api.issues({ state: ui.state, mine: ui.mine, force });
    } catch (e) {
      data = data || { issues: [], errors: [], facets: { repos: [], labels: [], assignees: [] } };
      toast("error", "Could not load issues", e.message);
    } finally {
      loading = false;
      if (alive) { $("#issRefresh", page).innerHTML = `${icon("refresh")}Refresh`; draw(); }
    }
  }

  function visible() {
    const q = ui.q.trim().toLowerCase(), index = taskIndex();
    return (data?.issues || []).map((x) => ({ ...x, live: index.get(keyOf(x))?.t || null })).filter((x) => {
      if (ui.repo && x.repo.toLowerCase() !== ui.repo.toLowerCase()) return false;
      if (ui.label && !x.labels.some((l) => l.name === ui.label)) return false;
      if (ui.assignee === "none" ? x.assignees.length : ui.assignee && !x.assignees.includes(ui.assignee)) return false;
      if (ui.hideTasked && x.live && !x.live.archived && !REUSABLE.has(x.live.status)) return false;
      if (q && q.replace(/^#/, "") !== String(x.number) && !`${x.title} ${x.repo} ${x.body}`.toLowerCase().includes(q)) return false;
      return true;
    });
  }

  function drawFilters() {
    const f = data?.facets || { repos: [], labels: [], assignees: [] };
    const opts = (el, all, values, cur, fmt = (v) => v) => {
      const list = cur && !values.includes(cur) && cur !== "none" ? [...values, cur] : values;
      el.innerHTML = `<option value="">${all}</option>${el.id === "issAssignee" ? `<option value="none" ${cur === "none" ? "selected" : ""}>Unassigned</option>` : ""}${list.map((v) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(fmt(v))}</option>`).join("")}`;
    };
    opts($("#issRepo", page), "All repositories", f.repos, ui.repo);
    opts($("#issLabel", page), "Any label", f.labels, ui.label);
    opts($("#issAssignee", page), "Anyone", f.assignees, ui.assignee, (v) => `@${v}`);
  }

  function statusBadge(t) {
    if (!t) return '<span class="muted iss-notask">No task</span>';
    const st = statusOf(t);
    return `<a class="badge ${TONE_BADGE[st.tone] || ""}" href="#/task/${encodeURIComponent(t.id)}" title="${esc(t.name)}">${esc(st.label)}</a>`;
  }

  function row(x) {
    const k = keyOf(x), isOpen = open.has(k);
    return `<div class="iss-row ${selected.has(k) ? "selected" : ""}" data-k="${esc(k)}">
      <input type="checkbox" class="iss-pick" data-pick="${esc(k)}" ${selected.has(k) ? "checked" : ""} aria-label="Select ${esc(x.repo)} #${x.number}">
      <button type="button" class="iss-main" data-open="${esc(k)}" aria-expanded="${isOpen}">
        <span class="iss-title"><span class="iss-repo">${esc(x.repo)}</span><span class="iss-num">#${x.number}</span><strong>${esc(x.title)}</strong>${x.state === "closed" ? '<span class="badge">closed</span>' : ""}</span>
        <span class="iss-meta">${x.labels.map((l) => `<span class="iss-label"${labelStyle(l.color)}>${esc(l.name)}</span>`).join("")}
          ${x.assignees.length ? `<span>${icon("user", "sm")}${x.assignees.map((a) => `@${esc(a)}`).join(", ")}</span>` : ""}
          ${x.milestone ? `<span>${icon("flag", "sm")}${esc(x.milestone)}</span>` : ""}
          ${x.comments ? `<span>${icon("message", "sm")}${x.comments}</span>` : ""}
          <span>updated ${timeAgo(x.updated)}</span>${x.local_path ? "" : '<span class="iss-clone" title="Created tasks clone it first">not cloned</span>'}</span>
      </button>
      <div class="iss-task">${statusBadge(x.live)}</div>
      ${isOpen ? `<div class="iss-preview">
        <div class="md">${x.body ? md(x.body) : '<p class="muted">No description.</p>'}</div>
        <div class="row wrap iss-preview-actions"><a class="btn sm" href="${esc(x.url)}" target="_blank" rel="noopener">${icon("external")}Open on GitHub</a>
          ${x.live ? `<a class="btn sm" href="#/task/${encodeURIComponent(x.live.id)}">${icon("tasks")}Open task</a>` : ""}
          <button type="button" class="btn sm primary" data-one="${esc(k)}">${icon("sparkles")}Create task</button></div>
      </div>` : ""}
    </div>`;
  }

  function drawSelbar(rows) {
    const bar = $("#issSelbar", page);
    const allVisible = rows.length && rows.every((x) => selected.has(keyOf(x)));
    bar.innerHTML = `<label class="iss-check"><input type="checkbox" id="issAll" ${allVisible ? "checked" : ""} ${rows.length ? "" : "disabled"}> Select all ${rows.length}</label>
      <span class="muted">${selected.size ? `${selected.size} selected` : "Select issues to create tasks"}</span>
      ${selected.size ? '<button type="button" class="btn xs ghost" id="issClear">Clear</button>' : ""}
      <span style="flex:1"></span>
      <button type="button" class="btn primary sm" id="issCreate" ${selected.size ? "" : "disabled"}>${icon("sparkles")}Create tasks${selected.size ? ` (${selected.size})` : ""}</button>`;
    $("#issAll", bar).onchange = (e) => { for (const x of rows) e.target.checked ? selected.set(keyOf(x), x) : selected.delete(keyOf(x)); draw(); };
    $("#issClear", bar) && ($("#issClear", bar).onclick = () => { selected.clear(); draw(); });
    $("#issCreate", bar).onclick = () => openCreate([...selected.values()]);
  }

  function draw() {
    if (!alive || !data) return;
    drawFilters();
    const rows = visible();
    const fetched = data.fetched_at ? new Date(data.fetched_at * 1000).toISOString() : null;
    $("#issFetched", page).textContent = fetched ? `fetched ${timeAgo(fetched)}` : "";
    $("#issErrors", page).innerHTML = (data.errors || []).length ? `<div class="modal-error iss-errors">${icon("alert", "sm")} Some repositories could not be listed: ${data.errors.map((e) => `<strong>${esc(e.repo)}</strong> ${esc(e.error)}`).join(" · ")}</div>` : "";
    drawSelbar(rows);
    const list = $("#issList", page);
    if (!rows.length) {
      const none = !(data.facets?.repos || []).length;
      list.innerHTML = `<div class="empty">${icon("issue", "lg")}<h3>${none ? "No GitHub repositories yet" : "No issues match"}</h3><p>${none ? 'Clone a repository on the <a href="#/repos">Repositories</a> page or watch one in the <a href="#/github">GitHub inbox</a>, and its issues show up here.' : "Try another filter, or include issues assigned to you anywhere."}</p></div>`;
      return;
    }
    list.innerHTML = rows.map(row).join("");
    const byKey = new Map(rows.map((x) => [keyOf(x), x]));
    $$("[data-pick]", list).forEach((c) => (c.onchange = () => { const x = byKey.get(c.dataset.pick); c.checked ? selected.set(c.dataset.pick, x) : selected.delete(c.dataset.pick); c.closest(".iss-row").classList.toggle("selected", c.checked); drawSelbar(rows); }));
    $$("[data-open]", list).forEach((b) => (b.onclick = () => { const k = b.dataset.open; open.has(k) ? open.delete(k) : open.add(k); draw(); }));
    $$("[data-one]", list).forEach((b) => (b.onclick = () => openCreate([byKey.get(b.dataset.one)])));
    $$(".iss-preview a[href^='#']", list).forEach((a) => a.addEventListener("click", (e) => e.stopPropagation()));
  }

  function openCreate(picked) {
    if (!picked.length) return;
    const cfg = S.config || {};
    const st = { order: picked.slice(), mode: "sequential", stop: !!cfg.queue_stop_chain_on_failure, template: "feature", priority: "normal",
      workflow: defaultWorkflow(), comment: !!cfg.issues_comment_on_pickup, label: cfg.issues_pickup_label || "" };
    const m = modal(`<div class="iss-dialog"><h2 id="icTitle"></h2><p class="hint">Each issue becomes a task named after it. The requirements are the issue body, its link and the discussion; the pull request closes the issue.</p>
      <div class="field"><div class="field-label">Order <span class="muted">(drag, or use the arrows)</span></div><ol class="iss-order" id="icOrder"></ol></div>
      <div class="field"><div class="field-label">How they run</div><div class="iss-modes" id="icModes" role="radiogroup"></div>
        <label class="iss-check" id="icStopRow"><input type="checkbox" id="icStop" ${st.stop ? "checked" : ""}> Stop the chain if a task fails <span class="muted">(otherwise the next one starts anyway)</span></label></div>
      <div class="grid2 iss-grid">
        <div class="field"><label>Type</label><div class="templ" id="icTpl"></div></div>
        <div class="field"><label for="icPrio">Priority</label><select id="icPrio">${["urgent", "high", "normal", "low"].map((p) => `<option ${p === st.priority ? "selected" : ""}>${p}</option>`).join("")}</select></div>
      </div>
      <div class="field"><div class="field-label">Team</div><div id="icTeam"></div></div>
      <details class="iss-gh"><summary>On GitHub</summary>
        <label class="iss-check"><input type="checkbox" id="icComment" ${st.comment ? "checked" : ""}> Comment “Relay picked this up: &lt;task&gt;” on each issue</label>
        <div class="field"><label for="icLabel">Add a label to each issue (blank = none)</label><input id="icLabel" value="${esc(st.label)}" placeholder="for example in-progress"></div>
      </details>
      <div id="icResult"></div>
      <div class="modal-actions"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" id="icGo"></button></div></div>`, { wide: true });
    const b = m.body;
    const parallel = S.queue?.max_parallel || cfg.max_parallel || 1;
    const MODES = [
      ["sequential", "One after the other", "Each task starts when the previous one ends, in the order above."],
      ["parallel", "Queue in parallel", `Queued together; up to ${parallel} run at once (the queue's parallel limit).`],
      ["draft", "Create only", "Drafts you start yourself with Start now, or queue later."],
    ];
    const drawOrder = () => {
      $("#icTitle", b).textContent = `Create ${st.order.length} task${st.order.length === 1 ? "" : "s"}`;
      $("#icOrder", b).innerHTML = st.order.map((x, i) => `<li draggable="true" data-i="${i}">
        <span class="iss-grip" aria-hidden="true">⋮⋮</span><span class="iss-ord">${i + 1}</span>
        <span class="iss-ord-main"><strong class="truncate">#${x.number} ${esc(x.title)}</strong><span class="muted truncate">${esc(x.repo)}${x.local_path ? "" : " · will be cloned first"}${x.live && !REUSABLE.has(x.live.status) && !x.live.archived ? ` · already has a task (${esc(statusOf(x.live).label)}), skipped` : ""}</span></span>
        <span class="iss-ord-btns"><button type="button" class="btn xs icon" data-up="${i}" ${i === 0 ? "disabled" : ""} aria-label="Move up">${icon("chevronUp")}</button><button type="button" class="btn xs icon" data-down="${i}" ${i === st.order.length - 1 ? "disabled" : ""} aria-label="Move down">${icon("chevronDown")}</button><button type="button" class="btn xs icon" data-rm="${i}" aria-label="Remove">${icon("x")}</button></span></li>`).join("");
      const move = (i, j) => { if (j < 0 || j >= st.order.length) return; const [x] = st.order.splice(i, 1); st.order.splice(j, 0, x); drawOrder(); };
      $$("[data-up]", b).forEach((x) => (x.onclick = () => move(+x.dataset.up, +x.dataset.up - 1)));
      $$("[data-down]", b).forEach((x) => (x.onclick = () => move(+x.dataset.down, +x.dataset.down + 1)));
      $$("[data-rm]", b).forEach((x) => (x.onclick = () => { st.order.splice(+x.dataset.rm, 1); if (!st.order.length) m.close(); else drawOrder(); }));
      let from = null;
      $$("#icOrder li", b).forEach((li) => {
        li.addEventListener("dragstart", (e) => { from = +li.dataset.i; li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", li.dataset.i); } catch {} });
        li.addEventListener("dragend", () => li.classList.remove("dragging"));
        li.addEventListener("dragover", (e) => { e.preventDefault(); li.classList.add("over"); });
        li.addEventListener("dragleave", () => li.classList.remove("over"));
        li.addEventListener("drop", (e) => { e.preventDefault(); li.classList.remove("over"); if (from !== null) move(from, +li.dataset.i); from = null; });
      });
      drawMode();
    };
    const drawMode = () => {
      $("#icModes", b).innerHTML = MODES.map(([id, name, help]) => `<button type="button" role="radio" aria-checked="${st.mode === id}" class="iss-mode ${st.mode === id ? "active" : ""}" data-mode="${id}"><strong>${esc(name)}</strong><span>${esc(help)}</span></button>`).join("");
      $$("[data-mode]", b).forEach((x) => (x.onclick = () => { st.mode = x.dataset.mode; drawMode(); }));
      $("#icStopRow", b).hidden = st.mode !== "sequential" || st.order.length < 2;
      const n = st.order.length;
      $("#icGo", b).innerHTML = `${icon(st.mode === "draft" ? "plus" : "play")}${st.mode === "draft" ? `Create ${n} draft${n === 1 ? "" : "s"}` : st.mode === "sequential" && n > 1 ? `Queue ${n} one after the other` : `Create & queue ${n}`}`;
    };
    const drawTpl = () => {
      $("#icTpl", b).innerHTML = (S.templates || []).map((x) => `<button type="button" class="chip ${st.template === x.id ? "active" : ""}" data-tpl="${esc(x.id)}">${esc(x.name)}</button>`).join("");
      $$("[data-tpl]", b).forEach((x) => (x.onclick = () => { st.template = x.dataset.tpl; drawTpl(); }));
    };
    drawOrder(); drawTpl();
    workflowEditor($("#icTeam", b), st.workflow, { agents: S.agentMeta, presets: S.presets || [], showAdvanced: false });
    $("#icGo", b).onclick = async () => {
      const r = st.workflow.roles;
      if (!r.supervisor?.agent || !r.worker?.agent) { toast("warning", "Pick a supervisor and a worker"); return; }
      const btn = $("#icGo", b);
      btn.disabled = true; btn.innerHTML = `${icon("spinner", "spin")}Creating…`;
      try {
        const res = await api.createIssueTasks({
          issues: st.order.map((x) => ({ repo: x.repo, number: x.number })), mode: st.mode, stop_on_failure: $("#icStop", b).checked,
          template: st.template, priority: $("#icPrio", b).value, workflow: st.workflow,
          comment: $("#icComment", b).checked, label: $("#icLabel", b).value.trim(),
        });
        const made = res.created.length;
        if (made) {
          toast("success", `${made} task${made === 1 ? "" : "s"} ${st.mode === "draft" ? "created as drafts" : st.mode === "sequential" && made > 1 ? "queued one after the other" : "queued"}`,
            res.created.map((c) => c.name).slice(0, 3).join(" · "), { action: { label: "Open first", onClick: () => navigate(`#/task/${res.created[0].task}`) } });
          for (const c of res.created) selected.delete(`${c.issue}`.toLowerCase());
        }
        const problems = [...res.skipped.map((s) => `${s.issue}: ${s.reason}`), ...res.errors.map((e) => `${e.issue}: ${e.error}`)];
        if (problems.length) {
          $("#icResult", b).innerHTML = `<div class="modal-error">${made ? `Created ${made}. ` : ""}Not created:<br>${problems.map(esc).join("<br>")}</div>`;
          btn.disabled = false; btn.innerHTML = "Close"; btn.onclick = m.close;
        } else m.close();
        load(true);
      } catch (e) {
        $("#icResult", b).innerHTML = `<div class="modal-error">${esc(e.message)}</div>`;
        btn.disabled = false; drawMode();
      }
    };
  }

  // ---- filters
  const refetch = () => { save(); load(); };
  $("#issQ", page).addEventListener("input", (e) => { ui.q = e.target.value; save(); draw(); });
  $("#issRepo", page).onchange = (e) => { ui.repo = e.target.value; save(); draw(); };
  $("#issLabel", page).onchange = (e) => { ui.label = e.target.value; save(); draw(); };
  $("#issAssignee", page).onchange = (e) => { ui.assignee = e.target.value; save(); draw(); };
  $("#issState", page).onchange = (e) => { ui.state = e.target.value; refetch(); };
  $("#issMine", page).onchange = (e) => { ui.mine = e.target.checked; refetch(); };
  $("#issHide", page).onchange = (e) => { ui.hideTasked = e.target.checked; save(); draw(); };
  $("#issRefresh", page).onclick = () => load(true);
  load();
  const timer = setInterval(() => { if (document.visibilityState === "visible") load(); }, 120000);

  let pending = null;
  return {
    update(reason) {
      if (reason !== "task" || !data) return;
      // Task events arrive in bursts while agents work; the badges only need an occasional redraw.
      if (!pending) pending = setTimeout(() => { pending = null; if (!$(".iss-row input:focus", page)) draw(); }, 600);
    },
    destroy() { alive = false; clearInterval(timer); clearTimeout(pending); },
  };
}
