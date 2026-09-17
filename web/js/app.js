// Relay 14 · application bootstrap, routing, live events, sidebar.
import { api, connectEvents, onConnection, conn } from "./api.js";
import { $, $$, el, esc, icon, toast, palette, timeAgo, fmtSec, basename, throttle } from "./ui.js";
import { S, bus, navigate, statusOf, LIVE, msgStore, agentLabel, agentInitial, roleAgent, ROLE_LABEL } from "./state.js";
import { mountDashboard } from "./views/dashboard.js";
import { mountTask } from "./views/task.js";
import { mountSettings } from "./views/settings.js";
import { mountAgents } from "./views/agents.js";
import { mountGithub } from "./views/github.js";
import { mountRepos } from "./views/repos.js";
import { openNewTask } from "./views/newtask.js";
import { TABS } from "./views/inspector.js";
import { deliver, markSeen, renderAttention, permission, requestPermission } from "./notify.js";
import { GOTO, keysFor, openShortcuts } from "./shortcuts.js";

const main = $("#main");
let view = null;

// ---------------------------------------------------------------------------- icons in static html
$$("[data-icon]").forEach((i) => { i.outerHTML = icon(i.dataset.icon); });

// ---------------------------------------------------------------------------- theme
function applyTheme(theme, density) {
  S.ui.theme = theme || S.ui.theme || "system";
  S.ui.density = density || S.ui.density || "comfortable";
  const resolved = S.ui.theme === "system" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : S.ui.theme;
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.density = S.ui.density;
  try { localStorage.setItem("relay.theme", S.ui.theme); localStorage.setItem("relay.density", S.ui.density); } catch {}
  $("#themeBtn").innerHTML = icon(resolved === "dark" ? "sun" : "moon");
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme());
bus.on("theme", ({ theme, density }) => applyTheme(theme, density));
$("#themeBtn").onclick = () => { const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark"; applyTheme(next, S.ui.density); api.saveSettings({ ui_theme: next }).catch(() => {}); };

// ---------------------------------------------------------------------------- sidebar: resize + collapse
const SIDEBAR_MIN = 220, SIDEBAR_MAX = 600, SIDEBAR_DEFAULT = 292;
const appEl = document.getElementById("app");

function applySidebarWidth(px) {
  const w = Math.max(SIDEBAR_MIN, Math.min(SIDEBAR_MAX, Math.round(px)));
  document.documentElement.style.setProperty("--sidebar", w + "px");
  try { localStorage.setItem("relay.sidebarW", String(w)); } catch {}
  return w;
}
function setSidebarCollapsed(collapsed) {
  const narrow = matchMedia("(max-width: 980px)").matches;
  if (narrow) {
    // On small screens the list is an overlay drawer rather than a column.
    appEl.classList.toggle("show-sidebar", !collapsed);
    appEl.classList.remove("sidebar-collapsed");
  } else {
    appEl.classList.toggle("sidebar-collapsed", !!collapsed);
    appEl.classList.remove("show-sidebar");
  }
  try { localStorage.setItem("relay.sidebarCollapsed", collapsed ? "1" : "0"); } catch {}
  $("#sidebarToggle")?.classList.toggle("active", !collapsed);
}
// Close the overlay drawer when you pick a task or click away.
addEventListener("click", (e) => {
  if (!appEl.classList.contains("show-sidebar")) return;
  const inSidebar = e.target.closest("#sidebar");
  const onToggle = e.target.closest("#sidebarToggle");
  if (onToggle) return;
  if (!inSidebar || e.target.closest("[data-task]")) appEl.classList.remove("show-sidebar");
});
(function initSidebar() {
  try {
    const w = Number(localStorage.getItem("relay.sidebarW"));
    if (w) applySidebarWidth(w);
    // On a phone the list is a drawer over the page; opening it on load would hide
    // the page you asked for, so it starts closed there.
    setSidebarCollapsed(matchMedia("(max-width: 980px)").matches || localStorage.getItem("relay.sidebarCollapsed") === "1");
  } catch {}
  const handle = $("#sidebarResize");
  if (!handle) return;
  let startX = 0, startW = 0;
  const onMove = (e) => applySidebarWidth(startW + (e.clientX - startX));
  const onUp = () => {
    handle.classList.remove("dragging");
    document.body.classList.remove("resizing");
    removeEventListener("mousemove", onMove);
    removeEventListener("mouseup", onUp);
  };
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    startX = e.clientX;
    startW = $("#sidebar").getBoundingClientRect().width;
    handle.classList.add("dragging");
    document.body.classList.add("resizing");
    addEventListener("mousemove", onMove);
    addEventListener("mouseup", onUp);
  });
  handle.addEventListener("dblclick", () => applySidebarWidth(SIDEBAR_DEFAULT));
})();
$("#sidebarToggle").onclick = () => {
  const narrow = matchMedia("(max-width: 980px)").matches;
  const shown = narrow ? appEl.classList.contains("show-sidebar") : !appEl.classList.contains("sidebar-collapsed");
  setSidebarCollapsed(shown);
};

// Inspector tab strip: fade when more tabs are off-screen, and keep the active tab visible.
function wireTabScroll() {
  const tabs = document.querySelector(".tabs");
  if (!tabs || tabs.dataset.wired) return;
  tabs.dataset.wired = "1";
  let wrap = tabs.parentElement;
  if (!wrap.classList.contains("tabs-wrap")) {
    wrap = document.createElement("div");
    wrap.className = "tabs-wrap";
    tabs.parentElement.insertBefore(wrap, tabs);
    wrap.appendChild(tabs);
  }
  const update = () => wrap.classList.toggle("can-scroll-right", tabs.scrollWidth - tabs.clientWidth - tabs.scrollLeft > 4);
  tabs.addEventListener("scroll", update);
  addEventListener("resize", update);
  new MutationObserver(() => {
    update();
    tabs.querySelector("button.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }).observe(tabs, { attributes: true, subtree: true, attributeFilter: ["class"] });
  update();
}

// ---------------------------------------------------------------------------- routing
function parseRoute() {
  const h = location.hash.replace(/^#\/?/, "");
  const parts = h.split("/").filter(Boolean);
  if (!parts.length) return { view: "dashboard", id: null, tab: null, section: null };
  if (parts[0] === "task" && parts[1]) return { view: "task", id: decodeURIComponent(parts[1]), tab: parts[2] || null, section: null };
  if (parts[0] === "settings") return { view: "settings", id: null, tab: null, section: parts[1] || "workflow" };
  if (parts[0] === "repos") return { view: "repos", id: null, tab: null, section: parts[1] || "list" };
  if (["agents", "github", "tasks", "dashboard"].includes(parts[0])) return { view: parts[0] === "dashboard" ? "dashboard" : parts[0], id: null, tab: null, section: null };
  return { view: "dashboard", id: null, tab: null, section: null };
}
function route() {
  const r = parseRoute();
  const same = view && S.route.view === r.view && S.route.id === r.id;
  S.route = r;
  if (r.id) { markSeen(r.id); renderAttention(); }
  $$(".rail-btn[data-nav]").forEach((b) => b.classList.toggle("active", b.dataset.nav === (r.view === "task" ? "tasks" : r.view)));
  document.getElementById("app").classList.toggle("no-sidebar", false);
  if (same) { view.update && view.update("route"); bus.emit("route"); renderSidebar(); return; }
  view && view.destroy && view.destroy();
  view = null;
  if (r.view === "task") view = mountTask(main, r.id);
  else if (r.view === "settings") view = mountSettings(main, r.section);
  else if (r.view === "agents") view = mountAgents(main);
  else if (r.view === "github") view = mountGithub(main);
  else if (r.view === "repos") view = mountRepos(main, r.section);
  else if (r.view === "tasks") view = mountTasksHome();
  else view = mountDashboard(main);
  renderSidebar();
  bus.emit("route");
  requestAnimationFrame(wireTabScroll);
}
window.addEventListener("hashchange", route);

function mountTasksHome() {
  const tasks = [...S.tasks.values()].filter((t) => !t.archived).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  if (tasks.length) { navigate(`#/task/${tasks[0].id}`); return { update() {}, destroy() {} }; }
  main.innerHTML = `<div class="page"><div class="empty">${icon("sparkles", "lg")}<h3>No tasks yet</h3><p>Create your first task. Choose a repository, describe the change once, and let one agent supervise the other until a reviewable branch and draft PR exist.</p><p style="margin-top:14px"><button class="btn primary" id="emptyNew">${icon("plus")}New task</button></p></div></div>`;
  $("#emptyNew", main).onclick = () => openNewTask();
  return { update() {}, destroy() {} };
}

// ---------------------------------------------------------------------------- sidebar
const groupOf = (t) => {
  const st = statusOf(t);
  if (st.attention) return 0;
  if (LIVE.has(t.status)) return 1;
  if (t.status === "queued" || t.status === "draft") return 2;
  return 3;
};
const GROUPS = ["Needs attention", "Active", "Queued", "Recent"];
function matchesFilter(t) {
  const f = S.ui.filter;
  if (f === "archived") return !!t.archived;
  if (t.archived) return false;
  if (f === "attention") return !!statusOf(t).attention;
  if (f === "active") return LIVE.has(t.status) || statusOf(t).attention;
  if (f === "done") return t.status === "done";
  if (f === "failed") return ["failed", "stopped", "interrupted"].includes(t.status);
  return true;
}
function renderSidebar() {
  const q = S.ui.search.trim().toLowerCase();
  const all = [...S.tasks.values()];
  const rows = all.filter(matchesFilter).filter((t) => !q || `${t.name} ${t.repo} ${(t.tags || []).join(" ")} ${t.github_repo || ""}`.toLowerCase().includes(q))
    .sort((a, b) => groupOf(a) - groupOf(b) || (b.updated_at || "").localeCompare(a.updated_at || ""));
  $("#taskCount").textContent = all.filter((t) => !t.archived).length;
  const list = $("#taskList");
  if (!rows.length) { list.innerHTML = `<div class="empty small">${q ? "No tasks match." : S.ui.filter === "all" ? "No tasks yet." : "Nothing here."}</div>`; return; }
  let lastGroup = -1;
  list.innerHTML = rows.map((t) => {
    const g = groupOf(t);
    const head = g !== lastGroup ? `<div class="tl-group"><span>${GROUPS[g]}</span></div>` : "";
    lastGroup = g;
    const st = statusOf(t);
    const live = LIVE.has(t.status) || t.process?.state === "running";
    const color = st.tone ? `var(--${st.tone === "accent" ? "accent" : st.tone})` : "var(--text-3)";
    const roles = ["supervisor", "worker", "reviewer"].map((r) => roleAgent(t, r)).filter(Boolean);
    const badge = st.attention ? `<span class="badge amber">${t.pending ? (t.pending.kind === "approval" ? "approve" : "question") : st.label}</span>` : (t.pr_number ? `<span class="badge green">PR #${esc(t.pr_number)}</span>` : (t.status === "failed" ? '<span class="badge red">failed</span>' : ""));
    const canStart = ["queued", "draft", "failed", "stopped", "interrupted"].includes(t.status);
    const canStop = live || t.status === "needs_input" || t.status === "paused";
    const quick = canStop
      ? `<button class="task-quick stop" data-stop="${esc(t.id)}" title="Stop this task">${icon("stop")}</button>`
      : (canStart ? `<button class="task-quick" data-start="${esc(t.id)}" title="Start now (runs alongside other tasks)">${icon("play")}</button>` : "");
    return head + `<div class="task-row ${S.route.id === t.id ? "active" : ""}">
      <button class="task-item" data-task="${esc(t.id)}" role="listitem" title="${esc(t.name)}">
      <span class="st ${live ? "live" : ""}" style="background:${color};color:${color}"></span>
      <span class="name">${esc(t.name)}</span>
      ${badge || "<span></span>"}
      <span class="sub"><span class="wf-mini" title="${esc(roles.map(agentLabel).join(" → "))}">${roles.map((a) => `<i class="av ${esc(a)}">${esc(agentInitial(a)[0])}</i>`).join("")}</span><span class="truncate">${esc(t.github_repo || basename(t.repo))}</span><span style="margin-left:auto">${live ? esc(st.label.toLowerCase()) : timeAgo(t.updated_at)}</span></span>
    </button>${quick}</div>`;
  }).join("");
  $$("[data-task]", list).forEach((b) => (b.onclick = () => navigate(`#/task/${b.dataset.task}`)));
  $$("[data-start]", list).forEach((b) => (b.onclick = async (e) => {
    e.stopPropagation();
    b.disabled = true;
    try { await api.action(b.dataset.start, "start"); toast("success", "Started"); }
    catch (err) { toast("error", "Could not start", err.message); b.disabled = false; }
  }));
  $$("[data-stop]", list).forEach((b) => (b.onclick = async (e) => {
    e.stopPropagation();
    b.disabled = true;
    try { await api.action(b.dataset.stop, "stop"); toast("info", "Stopping"); }
    catch (err) { toast("error", "Could not stop", err.message); b.disabled = false; }
  }));
}
$("#taskSearch").addEventListener("input", (e) => { S.ui.search = e.target.value; renderSidebar(); });
$$("#taskFilters .chip").forEach((c) => (c.onclick = () => { S.ui.filter = c.dataset.f; $$("#taskFilters .chip").forEach((x) => x.classList.toggle("active", x === c)); renderSidebar(); }));
$("#newTaskBtn").onclick = () => openNewTask();

// ---------------------------------------------------------------------------- queue + connection
function renderQueue() {
  const q = S.queue || {};
  $("#queueDot").className = "dot " + (q.running ? "on" : "");
  $("#queueText").textContent = q.running ? `Queue running · ${q.active || 0} active · ${q.queued || 0} waiting` : `Queue idle · ${q.queued || 0} waiting`;
  $("#runBtn").hidden = !!q.running;
  $("#stopQueueBtn").hidden = !q.running;
  if (document.activeElement !== $("#parallelInput")) $("#parallelInput").value = q.max_parallel || S.config.max_parallel || 1;
}
$("#runBtn").onclick = async () => { try { S.queue = await api.queueStart(Number($("#parallelInput").value) || 1); renderQueue(); toast("success", "Queue running", "Queued tasks start now."); } catch (e) { toast("error", "Queue", e.message); } };
$("#stopQueueBtn").onclick = async () => { try { S.queue = await api.queueStop(); renderQueue(); toast("info", "Queue halted", "Running tasks continue; nothing new starts."); } catch (e) { toast("error", "Queue", e.message); } };
$("#parallelInput").addEventListener("change", async (e) => { const n = Math.max(1, Number(e.target.value) || 1); await api.saveSettings({ max_parallel: n }); if (S.queue.running) S.queue = await api.queueStart(n); renderQueue(); });
function renderConn() {
  const dot = $("#connDot"), txt = $("#connText");
  if (conn.signedOut) { dot.className = "dot warn"; txt.innerHTML = 'Signed out · <a href="" onclick="location.reload();return false">reload to sign in</a>'; return; }
  if (!conn.online) { dot.className = "dot off"; txt.textContent = S.config?.ide_url || location.hostname !== "127.0.0.1" ? "Relay server unreachable · retrying" : "Backend offline · keep run.bat open"; return; }
  if (conn.stream) { dot.className = "dot on"; txt.textContent = `Live · v${S.build || ""}`; }
  else { dot.className = "dot warn"; txt.textContent = "Reconnecting live stream…"; }
}
onConnection(renderConn);

// ---------------------------------------------------------------------------- notifications
function renderNotifBadge() { const unread = (S.notifications || []).filter((n) => !n.read).length; const b = $("#notifBadge"); b.hidden = !unread; }
$("#notifBtn").onclick = () => {
  const p = $("#notifPanel");
  if (!p.hidden) { p.hidden = true; return; }
  const rows = S.notifications || [];
  const askDesktop = S.config.ui_notifications && permission() === "default";
  p.innerHTML = `<div class="row between" style="padding:8px 10px 4px"><strong>Notifications</strong><button class="btn xs" id="notifClear">Mark all read</button></div>` +
    (askDesktop ? `<div class="notif-ask"><span>Get a desktop alert when a task needs you, even with Relay in the background.</span><button class="btn xs primary" id="notifAllow">${icon("bell")}Allow</button></div>` : "") + (rows.length ? rows.slice(0, 40).map((n) => `<div class="notif-row" data-n="${esc(n.task_id || "")}"><span class="toast-ic ${esc(n.level)}" style="background:var(--${n.level === "success" ? "green" : n.level === "error" ? "red" : n.level === "warning" ? "amber" : "blue"}-soft);color:var(--${n.level === "success" ? "green" : n.level === "error" ? "red" : n.level === "warning" ? "amber" : "blue"})">${icon(n.level === "success" ? "check" : n.level === "info" ? "info" : "alert")}</span><div><strong>${esc(n.title)}</strong><span>${esc(n.body || "")}</span><time>${timeAgo(n.time)}</time></div></div>`).join("") : '<div class="empty small">No notifications yet.</div>');
  p.hidden = false;
  $("#notifAllow", p) && ($("#notifAllow", p).onclick = async () => {
    const res = await requestPermission();
    toast(res === "granted" ? "success" : "info", res === "granted" ? "Desktop notifications on" : "Desktop notifications not allowed", res === "granted" ? "Choose which events alert you in Settings → Notifications." : "You can change this in the browser's site settings.");
    p.hidden = true;
  });
  $("#notifClear", p).onclick = async () => { await api.notificationsRead(); S.notifications.forEach((n) => (n.read = true)); renderNotifBadge(); p.hidden = true; };
  $$("[data-n]", p).forEach((r) => (r.onclick = () => { p.hidden = true; if (r.dataset.n) navigate(`#/task/${r.dataset.n}`); }));
  const close = (e) => { if (!p.contains(e.target) && e.target !== $("#notifBtn") && !$("#notifBtn").contains(e.target)) { p.hidden = true; document.removeEventListener("mousedown", close); } };
  setTimeout(() => document.addEventListener("mousedown", close), 0);
};

// ---------------------------------------------------------------------------- live events
const sidebarSoon = throttle(renderSidebar, 400);
function upsertTask(t) {
  const prev = S.tasks.get(t.id);
  S.tasks.set(t.id, { ...t, process: t.process || prev?.process || { state: "idle" } });
  return { statusChanged: !prev || prev.status !== t.status, pendingChanged: (prev?.pending?.id || null) !== (t.pending?.id || null) };
}
function onEvent(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "task": {
      const info = upsertTask(p);
      sidebarSoon();
      if (S.route.id === p.id) markSeen(p.id);
      if (info.statusChanged || info.pendingChanged) renderAttention();
      if (view && (S.route.view !== "task" || S.route.id === p.id)) view.update && view.update("task", info);
      if (info.statusChanged && S.route.id !== p.id && statusOf(p).attention && p.pending) {
        toast("warning", `${p.name}: ${p.pending.kind === "approval" ? "approval needed" : "question for you"}`, p.pending.question || "", { action: { label: "Open", onClick: () => navigate(`#/task/${p.id}`) } });
      }
      break;
    }
    case "task_deleted":
      S.tasks.delete(p.id);
      S.msgs.delete(p.id);
      S.notifications = (S.notifications || []).filter((n) => n.task_id !== p.id);
      renderNotifBadge();
      renderSidebar();
      renderAttention();
      if (S.route.id === p.id) navigate("#/");
      else if (view && view.update) view.update("task", {});
      break;
    case "message": {
      const store = msgStore(p.task_id);
      if (store.loaded) { store.list.push(p); store.byId.set(p.id, p); store.count = p.seq || store.count + 1; if (store.list.length > 6000) store.list.splice(0, store.list.length - 6000); }
      if (S.route.view === "task" && S.route.id === p.task_id && store.loaded && view) view.update("message", p);
      break;
    }
    case "message_update": {
      const store = S.msgs.get(p.task_id);
      if (store?.loaded) { const m = store.byId.get(p.id); if (m) { Object.assign(m, p); if (S.route.id === p.task_id && view) view.update("message_update", m); } }
      break;
    }
    case "process": {
      const t = S.tasks.get(p.task_id);
      if (t) { t.process = p; if (S.route.id === p.task_id && view) view.update("process", p); }
      break;
    }
    case "event": {
      const t = S.tasks.get(p.task_id);
      if (t) { t.events = [...(t.events || []), p].slice(-400); if (S.route.id === p.task_id && view) view.update("event", p); }
      break;
    }
    case "artifact": if (S.route.id === p.task_id && view) view.update("artifact", p); break;
    case "notify": S.notifications.unshift(p); S.notifications = S.notifications.slice(0, 100); renderNotifBadge(); toast(p.level, p.title, p.body, p.task_id ? { action: { label: "Open", onClick: () => navigate(`#/task/${p.task_id}`) } } : {}); deliver(p); break;
    case "github": S.github = { ...S.github, ...p }; view && view.update && view.update("github", p); break;
    case "config": S.config = p; applyTheme(p.ui_theme, p.ui_density); renderQueue(); break;
    case "queue": S.queue = { ...S.queue, ...p }; renderQueue(); break;
    case "agents": { const j = p.job; if (j) toast(j.state === "done" ? "success" : "error", `${agentLabel(p.agent)} ${j.action === "remove" ? "removal" : j.action} ${j.state === "done" ? "finished" : "failed"}`, j.error || ""); view && view.update && view.update("agents"); break; }
  }
}

// ---------------------------------------------------------------------------- shortcuts + palette
function paletteItems() {
  const t = S.route.id ? S.tasks.get(S.route.id) : null;
  const items = [
    { group: "Actions", label: "New task", icon: "plus", hint: keysFor("new"), onClick: () => openNewTask() },
    { group: "Actions", label: S.queue.running ? "Halt queue" : "Run queue", icon: S.queue.running ? "stop" : "play", onClick: () => (S.queue.running ? $("#stopQueueBtn") : $("#runBtn")).click() },
    { group: "Actions", label: "Toggle theme", icon: "sun", onClick: () => $("#themeBtn").click() },
    { group: "Actions", label: "Show or hide the task list", icon: "tasks", hint: keysFor("sidebar"), onClick: () => $("#sidebarToggle").click() },
    { group: "Navigate", label: "Dashboard", icon: "home", hint: keysFor("goDashboard"), onClick: () => navigate("#/") },
    { group: "Navigate", label: "Agents", icon: "bot", hint: keysFor("goAgents"), onClick: () => navigate("#/agents") },
    { group: "Navigate", label: "GitHub inbox", icon: "github", hint: keysFor("goGithub"), onClick: () => navigate("#/github") },
    { group: "Navigate", label: "Repositories", icon: "folder", hint: keysFor("goRepos"), keywords: "branches clone git", onClick: () => navigate("#/repos") },
    { group: "Navigate", label: "Worktrees", icon: "layers", keywords: "clean up repositories", onClick: () => navigate("#/repos/worktrees") },
    { group: "Navigate", label: "Settings", icon: "settings", hint: keysFor("goSettings"), onClick: () => navigate("#/settings") },
    { group: "Navigate", label: "Notification settings", icon: "bell", onClick: () => navigate("#/settings/notifications") },
    { group: "Help", label: "Keyboard shortcuts", icon: "keyboard", hint: keysFor("help"), keywords: "keys hotkeys help", onClick: () => openShortcuts() },
  ];
  if (t) {
    const active = LIVE.has(t.status) || t.status === "needs_input" || t.status === "paused";
    if (active) items.push({ group: "Current task", label: "Stop task", icon: "stop", onClick: () => $("[data-act='stop']")?.click() }, { group: "Current task", label: t.status === "paused" ? "Resume task" : "Pause task", icon: "pause", onClick: () => ($("[data-act='pause']") || $("[data-act='resume']"))?.click() });
    if (["failed", "stopped", "interrupted"].includes(t.status)) items.push({ group: "Current task", label: "Retry / resume task", icon: "retry", onClick: () => $("[data-act='retry']")?.click() });
    if (t.pr_url) items.push({ group: "Current task", label: "Open pull request", icon: "external", onClick: () => window.open(t.pr_url, "_blank") });
    for (const [k, l] of TABS) items.push({ group: "Inspector", label: `Show ${l}`, icon: "panel", hint: k === "result" ? keysFor("tryIt") : "", onClick: () => openTab(k) });
    items.push({ group: "Current task", label: "Focus guidance box", icon: "message", hint: keysFor("guidance"), onClick: () => $("#guidance")?.focus() });
  }
  for (const x of [...S.tasks.values()].filter((x) => !x.archived).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")).slice(0, 30)) {
    items.push({ group: "Tasks", label: x.name, keywords: x.repo, icon: "tasks", hint: statusOf(x).label, onClick: () => navigate(`#/task/${x.id}`) });
  }
  return items;
}
$("#paletteBtn").onclick = () => palette(paletteItems);
$("#shortcutsBtn").onclick = () => openShortcuts();

function openTab(tab) {
  if (S.route.view !== "task") return;
  // The inspector may be hidden, or replaced by the conversation on a narrow screen.
  bus.emit("show-inspector");
  navigate(`#/task/${S.route.id}/${tab}`);
}
function stepTask(delta) {
  const ids = $$("#taskList [data-task]").map((b) => b.dataset.task);
  if (!ids.length) return;
  const cur = ids.indexOf(S.route.id);
  const next = ids[cur < 0 ? (delta > 0 ? 0 : ids.length - 1) : Math.max(0, Math.min(ids.length - 1, cur + delta))];
  if (next === S.route.id) return;
  navigate(`#/task/${next}`);
  requestAnimationFrame(() => $("#taskList .task-row.active")?.scrollIntoView({ block: "nearest" }));
}

// "g" starts a two-key sequence; a small pill shows it is waiting for the second key.
let chordUntil = 0;
const chordPill = el('<div class="chord-pill" hidden><kbd>G</kbd> then <kbd>D</kbd> dashboard · <kbd>T</kbd> tasks · <kbd>A</kbd> agents · <kbd>H</kbd> GitHub · <kbd>S</kbd> settings · <kbd>R</kbd> repositories</div>');
document.body.appendChild(chordPill);
const endChord = () => { chordUntil = 0; chordPill.hidden = true; };

document.addEventListener("keydown", (e) => {
  const a = document.activeElement;
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(a?.tagName) || a?.isContentEditable;
  // A dialog or the palette owns the keyboard; they handle Escape themselves.
  const overlay = !!$(".modal-backdrop, .palette-back");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { if (!overlay) { e.preventDefault(); palette(paletteItems); } return; }
  if (e.key === "Escape") {
    endChord();
    if (overlay) return;
    $("#notifPanel").hidden = true;
    if (appEl.classList.contains("show-sidebar")) setSidebarCollapsed(true);
    return;
  }
  if (typing || overlay || e.ctrlKey || e.metaKey || e.altKey) return;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (chordUntil) {
    const target = Date.now() < chordUntil && GOTO[key];
    endChord();
    if (target) { e.preventDefault(); navigate(target); }
    return;
  }
  switch (e.key) {
    case "?": e.preventDefault(); openShortcuts(); return;
    case "/": e.preventDefault(); setSidebarCollapsed(false); $("#taskSearch").focus(); return;
    case ".": if (S.route.view === "task" && $("#guidance")) { e.preventDefault(); $("#guidance").focus(); } return;
    case "[": case "]": {
      if (S.route.view !== "task") return;
      const keys = TABS.map(([k]) => k); const cur = keys.indexOf(S.ui.inspectorTab || "overview");
      openTab(keys[(cur + (e.key === "]" ? 1 : keys.length - 1)) % keys.length]);
      return;
    }
  }
  if (e.shiftKey) return;
  switch (key) {
    case "n": e.preventDefault(); openNewTask(); break;
    case "b": e.preventDefault(); $("#sidebarToggle").click(); break;
    case "g": e.preventDefault(); chordUntil = Date.now() + 1500; chordPill.hidden = false; setTimeout(() => { if (chordUntil && Date.now() >= chordUntil) endChord(); }, 1600); break;
    case "j": e.preventDefault(); stepTask(1); break;
    case "k": e.preventDefault(); stepTask(-1); break;
    case "t": if (S.route.view === "task") { e.preventDefault(); openTab("result"); } break;
  }
});

// Avatars take each agent's colour from the registry, so agents added later need no stylesheet change.
function paintAgentColors() {
  let tag = document.getElementById("agentColors");
  if (!tag) { tag = document.createElement("style"); tag.id = "agentColors"; document.head.appendChild(tag); }
  // Every agent avatar shows the product's own logo; the brand colour is only the fallback.
  tag.textContent = Object.entries(S.agentMeta || {}).map(([id, m]) => {
    const sel = `.av.${CSS.escape(id)}`;
    const color = m.color && /^#[0-9a-f]{3,8}$/i.test(m.color) ? `background-color:${m.color};` : "";
    return m.logo ? `${sel}{${color}background-image:url(${encodeURI(m.logo)});color:transparent;background-position:center;background-size:cover;background-repeat:no-repeat;box-shadow:inset 0 0 0 1px rgba(128,128,128,.22)}` : (color ? `${sel}{${color}}` : "");
  }).join("\n");
}

// ---------------------------------------------------------------------------- bootstrap
async function bootstrap() {
  let st;
  try { st = await api.state(); } catch (e) { main.innerHTML = `<div class="page"><div class="empty">${icon("alert", "lg")}<h3>Backend unreachable</h3><p>${esc(e.message)}</p></div></div>`; renderConn(); setTimeout(bootstrap, 3000); return; }
  S.build = st.build; S.config = st.config || {}; S.agents = st.agents || {}; S.agentMeta = st.agent_meta || {};
  paintAgentColors(); S.presets = st.presets || []; S.templates = st.templates || [];
  S.github = st.github || {}; S.queue = st.queue || {}; S.notifications = st.notifications || [];
  S.tasks = new Map((st.tasks || []).map((t) => [t.id, t]));
  applyTheme(S.config.ui_theme, S.config.ui_density);
  renderQueue(); renderNotifBadge(); renderConn(); renderAttention();
  if (!S.ready) { S.ready = true; connectEvents(onEvent); route(); }
  else { renderSidebar(); view && view.update && view.update("task", {}); }
}
bootstrap();
// Coming back to the tab counts as looking at the task that is open.
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && S.route.id) { markSeen(S.route.id); renderAttention(); } });
// Periodic reconciliation in case an SSE event was missed.
setInterval(async () => { if (!conn.online) return; try { const rows = await api.tasks(); const seen = new Set(); for (const t of rows) { seen.add(t.id); const prev = S.tasks.get(t.id); if (!prev || prev.updated_at !== t.updated_at || prev.status !== t.status) { const info = upsertTask(t); if (S.route.id === t.id && view) view.update("task", info); } } for (const id of [...S.tasks.keys()]) if (!seen.has(id)) S.tasks.delete(id); renderSidebar(); renderAttention(); } catch {} }, 20000);
