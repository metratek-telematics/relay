// Header pieces: the project switcher and the user menu (with setup progress). Mounted by app.js into #orgProject and #orgUser.
import { $, $$, esc, icon, toast } from "../../ui.js";
import { S, bus, navigate } from "../../state.js";
import { ORG, loadMe, currentProject, setProject, projectById, avatar, roleBadge, xicon, can, orgApi, projectOfTask } from "./org.js";

let popover = null;
function closePop() { if (popover) { popover.remove(); popover = null; document.removeEventListener("mousedown", onDoc, true); document.removeEventListener("keydown", onKey); } }
function onDoc(e) { if (popover && !popover.contains(e.target) && !e.target.closest("[data-org-trigger]")) closePop(); }
function onKey(e) { if (e.key === "Escape") closePop(); }
function openPop(anchor, html, align = "left") {
  closePop();
  const pop = document.createElement("div");
  pop.className = "org-pop";
  pop.setAttribute("role", "dialog");
  pop.innerHTML = html;
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  pop.style.top = `${r.bottom + 6}px`;
  pop.style.left = `${Math.max(8, Math.min(align === "right" ? r.right - w : r.left, innerWidth - w - 8))}px`;
  popover = pop;
  setTimeout(() => { document.addEventListener("mousedown", onDoc, true); document.addEventListener("keydown", onKey); }, 0);
  return pop;
}

function counts() {
  const by = {};
  for (const t of S.tasks.values()) {
    if (t.archived) continue;
    const pid = projectOfTask(t);
    const c = (by[pid] ||= { tasks: 0, attention: 0 });
    c.tasks++;
    if (t.pending || t.status === "needs_input") c.attention++;
  }
  return by;
}

function renderProject(el) {
  const cur = currentProject();
  const p = projectById(cur);
  const all = cur === "all" || !p;
  el.innerHTML = `<button class="org-proj-btn" data-org-trigger aria-haspopup="dialog" title="Switch project">
    <span class="org-proj-dot" style="background:${all ? "var(--text-3)" : esc(p.color)}"></span>
    <span class="org-proj-name">${esc(all ? "All projects" : p.name)}</span>${icon("chevronDown", "sm")}</button>`;
  $("button", el).onclick = (e) => openSwitcher(e.currentTarget);
}

function openSwitcher(anchor) {
  if (popover) return closePop();
  const cur = currentProject();
  const c = counts();
  const total = [...S.tasks.values()].filter((t) => !t.archived).length;
  const row = (id, name, color, n, attn) => `<button class="org-proj-row ${id === cur ? "active" : ""}" data-proj="${esc(id)}" role="option" aria-selected="${id === cur}">
      <span class="org-proj-dot" style="background:${esc(color)}"></span><span class="truncate">${esc(name)}</span>
      ${attn ? `<span class="badge amber" title="Waiting for people">${attn}</span>` : ""}<span class="muted org-count">${n}</span>${id === cur ? icon("check", "sm") : ""}</button>`;
  const pop = openPop(anchor, `<div class="org-pop-head"><strong>Projects</strong><span class="muted">${ORG.projects.length}</span></div>
    ${ORG.projects.length > 6 ? `<div class="org-pop-search">${icon("search", "sm")}<input id="projQ" placeholder="Find a project" autocomplete="off"></div>` : ""}
    <div class="org-proj-list" role="listbox">${row("all", "All projects", "var(--text-3)", total, 0)}
      ${ORG.projects.map((p) => row(p.id, p.name, p.color, c[p.id]?.tasks || 0, c[p.id]?.attention || 0)).join("")}</div>
    <div class="org-pop-foot">${can("admin") ? `<button class="btn xs" id="projNew">${icon("plus")}New project</button>` : ""}<a class="btn xs ghost" href="#/org/projects">${xicon("grid")}Manage</a></div>`);
  const pick = (id) => {
    closePop();
    setProject(id);
    const p = projectById(id);
    if (p && !(counts()[id]?.tasks)) navigate(`#/org/project/${id}`);
    else if (p) toast("info", `Project: ${p.name}`, "Lists and pages now show this project.");
  };
  $$("[data-proj]", pop).forEach((b) => (b.onclick = () => pick(b.dataset.proj)));
  const q = $("#projQ", pop);
  if (q) { q.focus(); q.oninput = () => $$("[data-proj]", pop).forEach((b) => (b.hidden = !b.textContent.toLowerCase().includes(q.value.toLowerCase()))); }
  $("#projNew", pop) && ($("#projNew", pop).onclick = async () => { closePop(); const m = await import("./projects.js"); m.editProject(null); });
}

function renderUser(el) {
  const me = ORG.me;
  if (!me) {
    el.innerHTML = document.documentElement.dataset.role === "anonymous"
      ? `<a class="btn xs" href="/" title="Your session ended">${xicon("lock")}Sign in</a>` : "";
    return;
  }
  const u = me.user;
  const ob = me.onboarding || {};
  const setup = !ob.complete && !ob.dismissed && can("member");
  el.innerHTML = `${setup ? `<a class="org-setup-pill" href="#/org/welcome" title="Getting started">${xicon("rocket", "sm")}<span class="lbl">Setup</span><b>${ob.done}/${ob.total}</b></a>` : ""}
    ${me.role === "viewer" ? `<span class="badge org-ro" title="You can look around; changes need the member role">${xicon("lock", "sm")}<span class="lbl">read-only</span></span>` : ""}
    <button class="org-user-btn" data-org-trigger aria-haspopup="dialog" title="${esc(u.name)} · ${esc(me.role)}">${avatar(u, 26)}</button>`;
  $(".org-user-btn", el).onclick = (e) => openUserMenu(e.currentTarget);
}

function openUserMenu(anchor) {
  if (popover) return closePop();
  const me = ORG.me, u = me.user, ob = me.onboarding || {};
  const link = (href, ic, label, extra = "") => `<a class="org-menu-item" href="${href}">${xicon(ic)}<span>${esc(label)}</span>${extra}</a>`;
  const via = me.via === "proxy" ? "Signed in through your identity provider" : me.via === "local" ? "Local mode: no sign-in configured" : me.via || "";
  const pop = openPop(anchor, `<div class="org-user-card">${avatar(u, 42)}<div class="min0"><strong class="truncate">${esc(u.name)}</strong><span class="muted truncate">${esc(u.email || u.username)}</span>
      <span class="row" style="gap:6px;margin-top:4px">${roleBadge(me.role)}<span class="muted org-via" title="${esc(via)}">${esc(me.via === "proxy" ? "SSO" : me.via === "local" ? "local" : me.via || "")}</span></span></div></div>
    ${!ob.complete ? `<a class="org-menu-progress" href="#/org/welcome"><span class="row between"><span>${xicon("rocket", "sm")} Getting started</span><b>${ob.done}/${ob.total}</b></span><span class="org-bar"><i style="width:${Math.round((ob.done / (ob.total || 1)) * 100)}%"></i></span></a>` : ""}
    <div class="org-menu-sep"></div>
    ${link("#/org/profile", "user", "Profile")}${link("#/org/notifications", "bell", "Notifications")}${link("#/org/tokens", "key", "API tokens")}
    <div class="org-menu-sep"></div>
    ${link("#/org/projects", "grid", "Projects")}${link("#/org/people", "users", "People & roles")}
    ${can("admin") ? link("#/org/integrations", "plug", "Integrations") : ""}${link("#/org/usage", "chart", "Usage & budgets")}
    ${can("admin") ? link("#/org/audit", "scroll", "Audit log") : ""}${link("#/org/api", "api", "API docs")}
    ${me.via === "proxy" ? `<div class="org-menu-sep"></div><a class="org-menu-item" href="/outpost.goauthentik.io/sign_out">${xicon("logout")}<span>Sign out</span></a>` : ""}`, "right");
  $$("a", pop).forEach((a) => a.addEventListener("click", () => closePop()));
}

export function mountOrgHeader() {
  const projEl = document.getElementById("orgProject"), userEl = document.getElementById("orgUser");
  if (!projEl || !userEl) return;
  const draw = () => { renderProject(projEl); renderUser(userEl); };
  bus.on("org", draw);
  bus.on("project", () => renderProject(projEl));
  loadMe().then(draw);
  // Setup progress and project counts drift as work happens; refresh quietly.
  setInterval(() => { if (document.visibilityState === "visible") loadMe().then(draw).catch(() => {}); }, 60000);
}
