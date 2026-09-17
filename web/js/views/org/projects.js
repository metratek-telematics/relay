// Projects: cards with activity and budget, the create/edit dialog, and each project's home with its empty state.
import { $, $$, esc, icon, toast, confirm, modal, timeAgo, basename } from "../../ui.js";
import { S, bus, navigate, statusOf } from "../../state.js";
import { request } from "../../api.js";
import { ORG, loadMe, orgApi, can, xicon, money, setProject, currentProject, projectOfTask, readOnlyNote, avatar } from "./org.js";
import { openNewTask } from "../newtask.js";

const COLORS = ["#d97757", "#4f6fd0", "#3a8f62", "#8a5fc7", "#b9842d", "#c5473f", "#2f9d87", "#5d78d6", "#6b7280"];
const ALL = { headers: { "X-Relay-Project": "all" } };

function meter(spent, budget) {
  if (!budget) return `<div class="muted" style="font-size:12px">No monthly budget</div>`;
  const pct = Math.round((spent / budget) * 100);
  return `<div class="org-meter ${pct >= 100 ? "over" : pct >= 80 ? "warn" : ""}"><div class="row between"><span>${money(spent, 2)} of ${money(budget)} this month</span><b>${pct}%</b></div><span class="org-bar"><i style="width:${Math.min(100, pct)}%"></i></span></div>`;
}

export function mountProjects(body) {
  let alive = true, rows = [], spend = {};
  async function load() {
    try {
      const [p, u] = await Promise.all([orgApi.projects(), orgApi.usage({ project: "" }).catch(() => null)]);
      rows = p.projects || [];
      spend = Object.fromEntries(((u && u.by_project) || []).map((x) => [x.id, x.cost_usd]));
    } catch (e) { body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; return; }
    if (alive) draw();
  }
  function draw() {
    const cur = currentProject();
    body.innerHTML = `
      <div class="page-head"><div><h1>Projects</h1><p>Group repositories, connectors, integration stacks, lessons and tasks. The switcher at the top left scopes Relay to one project.</p></div>
        <div class="page-actions">${can("admin") ? `<button class="btn primary" id="pNew">${icon("plus")}New project</button>` : ""}</div></div>
      ${can("admin") ? "" : readOnlyNote("admin", "Creating and editing projects")}
      <div class="org-proj-grid">${rows.map((p) => `
        <div class="card org-proj-card" style="--pc:${esc(p.color)}">
          <h3><span class="org-proj-dot" style="background:${esc(p.color)}"></span><span class="truncate">${esc(p.name)}</span>${p.id === cur ? '<span class="badge outline">current</span>' : ""}${p.archived ? '<span class="badge">archived</span>' : ""}</h3>
          <p>${esc(p.description || "")}</p>
          <div class="org-proj-stats"><div><b>${p.task_count}</b><span>tasks</span></div><div><b>${p.active_count}</b><span>active</span></div><div><b style="color:${p.attention_count ? "var(--amber)" : "inherit"}">${p.attention_count}</b><span>need people</span></div></div>
          <div class="org-chips">${p.repos.slice(0, 4).map((r) => `<span class="org-chip" title="${esc(r)}">${icon("folder", "sm")}${esc(basename(r))}</span>`).join("")}${p.repos.length > 4 ? `<span class="org-chip">+${p.repos.length - 4}</span>` : ""}${!p.repos.length ? '<span class="muted" style="font-size:12px">No repositories yet</span>' : ""}</div>
          ${meter(spend[p.id] || 0, p.budget.monthly_usd)}
          <div class="row wrap" style="gap:6px"><button class="btn sm primary" data-open="${esc(p.id)}">${icon("arrowRight")}Open</button>
            ${can("admin") ? `<button class="btn sm" data-edit="${esc(p.id)}">${icon("edit")}Edit</button>` : ""}
            ${can("admin") && p.id !== "default" ? `<button class="btn sm ghost" data-del="${esc(p.id)}" title="Delete project">${icon("trash")}</button>` : ""}</div>
        </div>`).join("")}</div>`;
    $("#pNew", body) && ($("#pNew", body).onclick = () => editProject(null, load));
    $$("[data-open]", body).forEach((b) => (b.onclick = () => { setProject(b.dataset.open, { silent: true }); navigate(`#/org/project/${b.dataset.open}`); bus.emit("project", b.dataset.open); }));
    $$("[data-edit]", body).forEach((b) => (b.onclick = () => editProject(rows.find((p) => p.id === b.dataset.edit), load)));
    $$("[data-del]", body).forEach((b) => (b.onclick = async () => {
      const p = rows.find((x) => x.id === b.dataset.del);
      if (!(await confirm(`Delete ${p.name}?`, "Its tasks, repositories, connectors and stacks move to the default project. Nothing else is deleted.", { danger: true, okLabel: "Delete project" }))) return;
      try { await orgApi.deleteProject(p.id); if (currentProject() === p.id) setProject("all", { silent: true }); await loadMe(); toast("success", "Project deleted"); load(); } catch (e) { toast("error", "Could not delete", e.message); }
    }));
  }
  load();
  return { destroy() { alive = false; }, update(reason) { if (reason === "task") { /* counts refresh on next visit */ } } };
}

export async function editProject(p, onSaved) {
  const [repos, conns, stacks, users] = await Promise.all([
    request("/api/repos", ALL).catch(() => ({ repos: [] })), request("/api/connectors", ALL).catch(() => ({ connectors: [] })),
    request("/api/stacks", ALL).catch(() => ({ stacks: [] })), orgApi.users().catch(() => ({ users: [] }))]);
  const others = new Map();
  for (const x of ORG.projects) if (!p || x.id !== p.id) { for (const r of x.repos || []) others.set(r, x.name); for (const c of x.connectors || []) others.set("c:" + c, x.name); for (const s of x.stacks || []) others.set("s:" + s, x.name); }
  const repoPaths = [...new Set([...(repos.repos || []).map((r) => r.path), ...(p?.repos || [])])];
  const d = p ? JSON.parse(JSON.stringify(p)) : { name: "", description: "", color: COLORS[(ORG.projects.length) % COLORS.length], repos: [], connectors: [], stacks: [], defaults: { preset: "", cost_cap_usd: 0, approval_before_delivery: null, reviewers: [], max_turns: 0 }, budget: { monthly_usd: 0, hard_cap: false } };
  const check = (name, value, label, on, owner) => `<label><input type="checkbox" data-${name}="${esc(value)}" ${on ? "checked" : ""}><span title="${esc(value)}">${esc(label)}</span>${owner ? `<span class="muted">in ${esc(owner)}</span>` : ""}</label>`;
  const m = modal(`<h2>${p ? `Edit ${esc(p.name)}` : "New project"}</h2><p class="hint">A repository, connector or stack belongs to one project; ticking one that sits elsewhere moves it here.</p>
    <div class="grid2"><div class="field"><label>Name</label><input id="prName" value="${esc(d.name)}" placeholder="Navitrak" ${p?.id === "default" ? "" : ""}></div>
      <div class="field"><label>Colour</label><div class="org-swatches">${COLORS.map((c) => `<span class="org-swatch ${c === d.color ? "active" : ""}" data-color="${c}" style="background:${c}" role="button" aria-label="Colour ${c}"></span>`).join("")}</div></div></div>
    <div class="field"><label>Description</label><input id="prDesc" value="${esc(d.description)}" placeholder="What this project is for"></div>
    <div class="grid2">
      <div class="field"><label>Repositories</label><div class="org-checklist">${repoPaths.length ? repoPaths.map((r) => check("repo", r, basename(r), d.repos.includes(r), others.get(r))).join("") : '<span class="muted" style="padding:6px">No repositories cloned yet</span>'}</div></div>
      <div class="field"><label>Connectors</label><div class="org-checklist">${(conns.connectors || []).length ? conns.connectors.map((c) => check("conn", c.name, `${c.name} · ${c.environment}`, d.connectors.includes(c.name), others.get("c:" + c.name))).join("") : '<span class="muted" style="padding:6px">No connectors</span>'}</div>
        <label style="margin-top:10px">Integration stacks</label><div class="org-checklist">${(stacks.stacks || []).length ? stacks.stacks.map((s) => check("stack", s.id, s.name, d.stacks.includes(s.id), others.get("s:" + s.id))).join("") : '<span class="muted" style="padding:6px">No stacks</span>'}</div></div>
    </div>
    <h3 style="margin:14px 0 4px;font-size:13.5px">Defaults for new tasks</h3>
    <div class="grid3">
      <div class="field"><label>Team</label><select id="prPreset"><option value="">Relay's default team</option>${(S.presets || []).map((x) => `<option value="${esc(x.id)}" ${d.defaults.preset === x.id ? "selected" : ""}>${esc(x.label || x.name || x.id)}</option>`).join("")}</select></div>
      <div class="field"><label>Cost cap per task (USD)</label><input id="prCap" type="number" min="0" step="0.5" value="${d.defaults.cost_cap_usd || ""}" placeholder="none"></div>
      <div class="field"><label>Approval before delivery</label><select id="prApproval"><option value="" ${d.defaults.approval_before_delivery == null ? "selected" : ""}>Follow settings</option><option value="true" ${d.defaults.approval_before_delivery === true ? "selected" : ""}>Always</option><option value="false" ${d.defaults.approval_before_delivery === false ? "selected" : ""}>Never</option></select></div>
    </div>
    <div class="field"><label>Reviewers (asked first for approvals)</label><div class="org-chips">${(users.users || []).filter((u) => u.role !== "viewer").map((u) => `<label class="org-chip"><input type="checkbox" data-rev="${esc(u.username)}" ${d.defaults.reviewers.includes(u.username) ? "checked" : ""}>${esc(u.name || u.username)}</label>`).join("") || '<span class="muted">Nobody with the member role yet</span>'}</div></div>
    <h3 style="margin:14px 0 4px;font-size:13.5px">Budget</h3>
    <div class="grid2"><div class="field"><label>Monthly budget (USD)</label><input id="prBudget" type="number" min="0" step="10" value="${d.budget.monthly_usd || ""}" placeholder="none" ${can("owner") ? "" : "disabled"}></div>
      <div class="field inline" style="align-self:end"><label>Hard cap: refuse new tasks once spent</label><span class="switch ${d.budget.hard_cap ? "on" : ""}" id="prHard" ${can("owner") ? "" : 'style="opacity:.5;pointer-events:none"'}></span></div></div>
    ${can("owner") ? "" : '<p class="hint">Budgets are set by owners.</p>'}
    <div class="modal-actions"><button class="btn" data-close>Cancel</button><button class="btn primary" id="prSave">${icon("save")}${p ? "Save" : "Create project"}</button></div>`, { wide: true });
  const b = m.body;
  $$("[data-color]", b).forEach((s) => (s.onclick = () => { d.color = s.dataset.color; $$("[data-color]", b).forEach((x) => x.classList.toggle("active", x === s)); }));
  $("#prHard", b).onclick = (e) => e.currentTarget.classList.toggle("on");
  $("#prSave", b).onclick = async () => {
    const approval = $("#prApproval", b).value;
    const out = {
      name: $("#prName", b).value.trim(), description: $("#prDesc", b).value.trim(), color: d.color,
      repos: $$("[data-repo]", b).filter((x) => x.checked).map((x) => x.dataset.repo),
      connectors: $$("[data-conn]", b).filter((x) => x.checked).map((x) => x.dataset.conn),
      stacks: $$("[data-stack]", b).filter((x) => x.checked).map((x) => x.dataset.stack),
      defaults: { preset: $("#prPreset", b).value, cost_cap_usd: Number($("#prCap", b).value) || 0, approval_before_delivery: approval === "" ? null : approval === "true",
        reviewers: $$("[data-rev]", b).filter((x) => x.checked).map((x) => x.dataset.rev), max_turns: d.defaults.max_turns || 0 },
    };
    if (can("owner")) out.budget = { monthly_usd: Number($("#prBudget", b).value) || 0, hard_cap: $("#prHard", b).classList.contains("on") };
    try {
      const saved = p ? await orgApi.saveProject(p.id, out) : await orgApi.createProject(out);
      m.close();
      await loadMe();
      toast("success", p ? "Project saved" : `Project ${saved.name} created`, p ? "" : "Switch to it from the top left.");
      if (onSaved) onSaved(saved); else if (!p) { setProject(saved.id, { silent: true }); navigate(`#/org/project/${saved.id}`); bus.emit("project", saved.id); }
    } catch (e) { toast("error", "Could not save the project", e.message); }
  };
}

// ---------------------------------------------------------------------------- project home
export function mountProjectHome(body, pid) {
  let alive = true;
  async function draw() {
    if (!ORG.me) await loadMe();
    const p = ORG.projects.find((x) => x.id === pid);
    if (!p) { body.innerHTML = `<div class="empty">${icon("alert", "lg")}<h3>Project not found</h3><p><a href="#/org/projects">All projects</a></p></div>`; return; }
    const tasks = [...S.tasks.values()].filter((t) => !t.archived && projectOfTask(t) === p.id).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    let usage = null;
    try { usage = await orgApi.usage({ project: p.id }); } catch {}
    if (!alive) return;
    const waiting = tasks.filter((t) => t.pending);
    const people = new Map();
    for (const t of tasks) if (t.created_by) people.set(t.created_by, (people.get(t.created_by) || 0) + 1);
    const hero = `<div class="org-hero" style="--pc:${esc(p.color)}"><div class="min0"><h1><span class="org-proj-dot" style="background:${esc(p.color)};width:14px;height:14px"></span>${esc(p.name)}</h1><p>${esc(p.description || "A project groups repositories, connectors, stacks and the tasks that change them.")}</p></div>
      <div class="row wrap" style="gap:6px">${can("member") ? `<button class="btn primary" id="phNew">${icon("plus")}New task</button>` : ""}${can("admin") ? `<button class="btn" id="phEdit">${icon("edit")}Edit</button>` : ""}</div></div>`;
    if (!tasks.length) {
      body.innerHTML = `${hero}
        <div class="org-empty" style="--pc:${esc(p.color)}"><span class="org-empty-art">${icon("sparkles")}</span><h2>Nothing in ${esc(p.name)} yet</h2>
          <p>Give the project a repository, then describe a change. A supervisor plans it, a worker builds it and a reviewer checks it before a draft pull request appears.</p>
          <div class="org-empty-steps">
            <a href="#/repos"><b>${icon("folder", "sm")}1 · Add a repository</b><span>${p.repos.length ? `${p.repos.length} linked: ${esc(p.repos.map((r) => basename(r)).slice(0, 2).join(", "))}` : "Clone from GitHub, then tick it in Edit project."}</span></a>
            <a href="#/issues"><b>${icon("issue", "sm")}2 · Pick an issue</b><span>Turn labelled GitHub issues into tasks in one go.</span></a>
            <a href="#" id="phNew2"><b>${icon("plus", "sm")}3 · Describe a change</b><span>Or start from a saved prompt in the New task dialog.</span></a>
          </div>
          <div class="row wrap" style="gap:8px;justify-content:center;margin-top:8px">${can("member") ? `<button class="btn primary" id="phNew3">${icon("plus")}New task in ${esc(p.name)}</button>` : ""}<a class="btn" href="#/org/welcome">${xicon("rocket")}Setup checklist</a><a class="btn ghost" href="#/org/api">${xicon("api")}Queue from CI</a></div>
        </div>`;
    } else {
      const u = usage?.total || {};
      body.innerHTML = `${hero}
        <div class="org-kpis">
          <div class="org-kpi"><span>Tasks</span><b>${tasks.length}</b><small>${tasks.filter((t) => t.status === "done").length} delivered</small></div>
          <div class="org-kpi"><span>Waiting for people</span><b style="color:${waiting.length ? "var(--amber)" : "inherit"}">${waiting.length}</b><small><a href="#/inbox">Needs you</a></small></div>
          <div class="org-kpi"><span>Spend this month</span><b>${money(u.cost_usd, 2)}</b><small>${p.budget.monthly_usd ? `of ${money(p.budget.monthly_usd)}` : "no budget"}</small></div>
          <div class="org-kpi"><span>Repositories</span><b>${p.repos.length}</b><small>${p.connectors.length} connectors · ${p.stacks.length} stacks</small></div>
        </div>
        <div class="org-grid-2">
          <div class="card"><div class="card-head"><h3>Recent tasks</h3><a class="btn xs ghost" href="#/tasks">All</a></div><div class="card-body" style="padding:4px 8px">
            <table class="org-table"><tbody>${tasks.slice(0, 12).map((t) => { const st = statusOf(t); return `<tr class="clickable" data-task="${esc(t.id)}"><td><strong class="truncate" style="display:block;max-width:420px">${t.number ? `#${esc(t.number)} ` : ""}${esc(t.name)}</strong><span class="muted" style="font-size:11px">${esc(t.github_repo || basename(t.repo))}${t.created_by ? ` · by ${esc(t.created_by)}` : ""}</span></td><td class="num"><span class="badge ${esc(st.tone || "")}">${esc(st.label)}</span></td><td class="num muted org-hide-sm">${esc(timeAgo(t.updated_at))}</td></tr>`; }).join("")}</tbody></table></div></div>
          <div class="stack" style="gap:18px">
            <div class="card"><div class="card-head"><h3>Budget</h3></div><div class="card-body">${meter(u.cost_usd || 0, p.budget.monthly_usd)}${p.budget.hard_cap ? '<p class="hint" style="margin:8px 0 0">Hard cap: new tasks are refused once the month is spent.</p>' : ""}</div></div>
            <div class="card"><div class="card-head"><h3>Defaults</h3></div><div class="card-body"><dl class="kv" style="margin:0">
              <dt>Team</dt><dd>${esc((S.presets || []).find((x) => x.id === p.defaults.preset)?.label || p.defaults.preset || "Relay's default")}</dd>
              <dt>Cost cap</dt><dd>${p.defaults.cost_cap_usd ? money(p.defaults.cost_cap_usd, 2) + " per task" : "none"}</dd>
              <dt>Approval</dt><dd>${p.defaults.approval_before_delivery == null ? "follows settings" : p.defaults.approval_before_delivery ? "always" : "never"}</dd>
              <dt>Reviewers</dt><dd>${p.defaults.reviewers.length ? esc(p.defaults.reviewers.join(", ")) : "anyone with the member role"}</dd></dl></div></div>
            ${people.size ? `<div class="card"><div class="card-head"><h3>People</h3></div><div class="card-body stack" style="gap:8px">${[...people.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([n, c]) => { const x = (usage?.by_user || []).find((y) => y.username === n) || { initials: n.slice(0, 2).toUpperCase(), color: "var(--text-3)", name: n }; return `<div class="row between"><span class="org-person">${avatar(x, 24)}<span>${esc(x.name || n)}</span></span><span class="muted">${c} task${c === 1 ? "" : "s"}</span></div>`; }).join("")}</div></div>` : ""}
          </div>
        </div>`;
    }
    const newTask = (e) => { e && e.preventDefault(); setProject(p.id, { silent: true }); openNewTask(p.repos.length ? { repo: p.repos[0] } : undefined); };
    ["#phNew", "#phNew2", "#phNew3"].forEach((id) => $(id, body) && ($(id, body).onclick = newTask));
    $("#phEdit", body) && ($("#phEdit", body).onclick = () => editProject(p, async () => { await loadMe(); draw(); }));
    $$("[data-task]", body).forEach((r) => (r.onclick = () => navigate(`#/task/${r.dataset.task}`)));
  }
  draw();
  return { destroy() { alive = false; }, update(reason) { if (reason === "task" && alive) { /* keep the page stable while reading */ } } };
}
