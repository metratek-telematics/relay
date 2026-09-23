// Relay 15 · application shell: navigation, routing (with redirects from older URLs), live events, command palette.
import { api, connectEvents, onConnection, conn } from "./api.js";
import { $, $$, el, esc, icon, toast, palette, timeAgo, throttle, basename, menu } from "./ui.js";
import { S, bus, navigate, statusOf, LIVE, agentLabel } from "./state.js";
import { mountMission } from "./views/mission.js";
import { mountWork } from "./views/work.js";
import { mountTask } from "./views/task.js";
import { mountReview } from "./views/review.js";
import { mountStatus } from "./views/status.js";
import { mountKnowledge } from "./views/knowledge.js";
import { mountSettings } from "./views/settings.js";
import { mountAgents } from "./views/agents.js";
import { openNewTask } from "./views/newtask.js";
import { deliver, markSeen, renderAttention, permission, requestPermission } from "./notify.js";
import { GOTO, keysFor, openShortcuts } from "./shortcuts.js";
import { noteMessage } from "./live.js";
import { commandItems } from "./commands.js";
import { mountOrg, mountOrgHeader, parseOrgRoute, inProject } from "./views/org/org.js";
import { mountNav, toggleNav, openDrawer, closeDrawer, drawerOpen } from "./nav.js";

const main = $("#main");
const root = document.documentElement;
let view = null;

// ---------------------------------------------------------------------------- icons in static html
$$("[data-icon]").forEach((i) => { i.outerHTML = icon(i.dataset.icon); });
if (/Mac|iPhone|iPad/.test(navigator.platform || "")) $("#paletteKbd").textContent = "⌘K";

// ---------------------------------------------------------------------------- theme
function applyTheme(theme, density) {
  S.ui.theme = theme || S.ui.theme || "system";
  S.ui.density = density || S.ui.density || "comfortable";
  const resolved = S.ui.theme === "system" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : S.ui.theme;
  root.dataset.theme = resolved;
  root.dataset.density = S.ui.density;
  try { localStorage.setItem("relay.theme", S.ui.theme); localStorage.setItem("relay.density", S.ui.density); } catch {}
  $("#themeBtn").innerHTML = icon(resolved === "dark" ? "sun" : "moon");
  $("#themeBtn").title = resolved === "dark" ? "Switch to the light theme" : "Switch to the dark theme";
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme());
bus.on("theme", ({ theme, density }) => applyTheme(theme, density));
function toggleTheme() { const next = root.dataset.theme === "dark" ? "light" : "dark"; applyTheme(next, S.ui.density); api.saveSettings({ ui_theme: next }).catch(() => {}); }
$("#themeBtn").onclick = toggleTheme;

// ---------------------------------------------------------------------------- navigation chrome
mountNav();
$("#newTaskBtn").onclick = () => openNewTask();
$("#tabNew").onclick = () => openNewTask();
// On a phone "More" opens the whole navigation as a drawer rather than a short menu of leftovers.
$("#tabMore").onclick = (e) => innerWidth <= 760 ? openDrawer() : menu(e.currentTarget, [
  { label: "Agents", icon: "bot", onClick: () => navigate("#/agents") },
  { label: "Settings", icon: "settings", onClick: () => navigate("#/settings") },
  { label: "Projects", icon: "layers", onClick: () => navigate("#/org/projects") },
  { label: "Profile", icon: "user", onClick: () => navigate("#/org/profile") },
  "-",
  { label: "Search or run a command", icon: "search", onClick: () => openPalette() },
  { label: "Notifications", icon: "bell", onClick: () => $("#notifBtn").click() },
  { label: root.dataset.theme === "dark" ? "Light theme" : "Dark theme", icon: root.dataset.theme === "dark" ? "sun" : "moon", onClick: toggleTheme },
]);

// ---------------------------------------------------------------------------- routing
// Old addresses keep working: each is rewritten to where that content lives now.
const TASK_VIEW_OF_TAB = { overview: "", result: "changes/try", changes: "changes", history: "changes/commits", repository: "changes/edit",
  checks: "checks", review: "checks/review", timeline: "checks/timeline", logs: "logs", sessions: "logs/sessions", design: "design" };
function redirectOf(parts) {
  const [a, b, c] = parts;
  if (a === "dashboard" || a === "home" && !b) return "#/";
  if (a === "inbox") return "#/home/needs";
  if (a === "digest") { let h = 24; try { h = Number(localStorage.getItem("relay.digestHours")) || 24; } catch {} return `#/home/${h >= 168 ? "7d" : h >= 72 ? "3d" : "24h"}`; }
  if (a === "tasks") return "#/work";
  if (a === "issues") return "#/work/issues";
  if (a === "github") return "#/knowledge/github";
  if (a === "lessons") return "#/knowledge/lessons";
  if (a === "learning") return "#/knowledge/learning";
  if (a === "repos") return `#/knowledge/${{ list: "repositories", worktrees: "worktrees", graph: "graph", system: "system" }[b] || "repositories"}`;
  if (a === "settings" && b === "connectors") return "#/knowledge/connectors";
  if (a === "task" && b && c && c in TASK_VIEW_OF_TAB) return `#/task/${b}${TASK_VIEW_OF_TAB[c] ? `/${TASK_VIEW_OF_TAB[c]}` : ""}`;
  return null;
}
function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const to = redirectOf(parts);
  if (to && to !== location.hash) { history.replaceState(null, "", to); return parseRoute(); }
  const [a, b, c, d] = parts;
  if (!a || a === "home") return { view: "home", id: null, tab: b || null, section: null };
  if (a === "work") return { view: "work", id: null, tab: b || null, section: null };
  if (a === "task" && b) return { view: "task", id: b, tab: c || null, section: d || null };
  if (a === "review" && b) return { view: "review", id: b, tab: null, section: null };
  if (a === "status" && b) return { view: "status", id: b, tab: null, section: null };
  if (a === "knowledge") return { view: "knowledge", id: null, tab: b || "system", section: null };
  if (a === "settings") return { view: "settings", id: null, tab: null, section: b || "workflow" };
  if (a === "agents") return { view: "agents", id: null, tab: null, section: null };
  if (a === "org") return parseOrgRoute(parts);
  return { view: "home", id: null, tab: null, section: null };
}
const NAV_OF_VIEW = { home: "home", work: "work", task: "work", review: "work", knowledge: "knowledge", agents: "agents", settings: "settings", org: "settings" };
function route() {
  const r = parseRoute();
  const same = view && S.route.view === r.view && S.route.id === r.id && (r.view !== "org" || (S.route.section === r.section && S.route.tab === r.tab));
  S.route = r;
  root.classList.toggle("bare", r.view === "status");
  root.dataset.view = r.view;
  if (r.id) { markSeen(r.id); renderAttention(); }
  $$("[data-nav]").forEach((b) => { const on = b.dataset.nav === NAV_OF_VIEW[r.view]; b.classList.toggle("active", on); on ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"); });
  if (same) { view.update && view.update("route"); bus.emit("route"); return; }
  rememberScroll();
  view && view.destroy && view.destroy();
  view = null;
  main.scrollTop = 0;
  routeKey = keyOf(r);
  if (r.view === "task") view = mountTask(main, r.id);
  else if (r.view === "review") view = mountReview(main, r.id);
  else if (r.view === "status") view = mountStatus(main, r.id);
  else if (r.view === "work") view = mountWork(main, r.tab);
  else if (r.view === "knowledge") view = mountKnowledge(main, r.tab);
  else if (r.view === "settings") view = mountSettings(main, r.section);
  else if (r.view === "agents") view = mountAgents(main);
  else if (r.view === "org") view = mountOrg(main, r.section, r.tab);
  else view = mountMission(main, r.tab);
  enterView();
  restoreScroll();
  bus.emit("route");
}

// A view arrives with a short fade and a 4 px lift; nothing else on the page moves.
function enterView() {
  const first = main.firstElementChild;
  if (!first) return;
  first.classList.add("view-enter");
  first.addEventListener("animationend", () => first.classList.remove("view-enter"), { once: true });
}
// Where each route was scrolled to is remembered for as long as the tab lives, so going back lands where
// you left rather than at the top.
let routeKey = "";
const scrollMemory = new Map();
const scrollerOf = () => main.querySelector(".page, .work, .convo-wrap, .org-main") || main;
function rememberScroll() { const el = scrollerOf(); if (routeKey && el && el.scrollTop) scrollMemory.set(routeKey, el.scrollTop); }
function restoreScroll() {
  const y = scrollMemory.get(routeKey);
  if (!y) return;
  const key = routeKey;
  // The page is often still loading, so the attempt repeats briefly until it can actually scroll that far.
  const tryIt = (delay) => setTimeout(() => {
    if (routeKey !== key) return;
    const el = scrollerOf();
    if (el && el.scrollHeight - el.clientHeight >= y - 4) el.scrollTop = y;
  }, delay);
  [0, 120, 400].forEach(tryIt);
}
const keyOf = (r) => `${r.view}|${r.id || ""}|${r.tab || ""}|${r.section || ""}`;
addEventListener("beforeunload", rememberScroll);

window.addEventListener("hashchange", route);
// Switching project re-draws the page for that project; lists, counts and the board follow it.
bus.on("project", () => { view && view.destroy && view.destroy(); view = null; renderCounts(); route(); });
bus.on("org", () => { if (S.ready) renderCounts(); });

// ---------------------------------------------------------------------------- nav counts, autopilot, connection
function renderCounts() {
  const tasks = [...S.tasks.values()].filter((t) => !t.archived && inProject(t));
  const live = tasks.filter((t) => LIVE.has(t.status)).length;
  const needs = tasks.filter((t) => statusOf(t).attention || t.pending).length;
  const set = (id, n, title) => { const b = $(id); if (!b) return; b.hidden = !n; b.textContent = n > 99 ? "99+" : String(n); if (title) b.title = title; };
  set("#navLive", live, `${live} running`);
  set("#navNeeds", needs, `${needs} need you`);
  const toReview = (S.lessonsPending || 0) + (S.learningProposals || 0);
  set("#navLessons", toReview, `${S.lessonsPending || 0} lessons and ${S.learningProposals || 0} learning proposals to review`);
  $("#tabLive").hidden = !live; $("#tabNeeds").hidden = !needs;
}

const AP_TONE = { running: "ok", idle: "ok", paused: "warn", halted: "", outside_window: "", waiting_limits: "warn", cost_cap: "alarm" };
function renderAutopilot() {
  const a = S.autopilot, host = $("#navAutopilot");
  if (!host) return;
  renderCounts();
  if (!a) { host.innerHTML = ""; return; }
  const label = a.state === "halted" ? "Queue halted" : a.paused ? "Paused" : a.state === "running" ? `${a.running} running` : a.state === "idle" ? "On shift · idle" : (a.label || a.state).replace(/^Autopilot /, "");
  const tone = a.paused ? "warn" : AP_TONE[a.state] || "";
  host.innerHTML = `<div class="ap-card" data-state="${esc(a.state)}">
      <span class="ap-status"><span class="dot ${tone} ${a.state === "running" && a.running ? "live" : ""}" aria-hidden="true"></span><span class="lbl"><span class="ap-name">Autopilot</span><span class="ap-state">${esc(label)}</span></span></span>
      ${a.state === "halted" ? `<button class="btn xs" id="apRun" type="button" title="Start the queue">${icon("play")}<span class="lbl">Run</span></button>`
        : `<button class="btn xs ghost icon" id="apToggle" type="button" title="${a.paused ? "Resume: queued tasks start and paused runs continue" : "Pause everything: nothing new starts, running tasks stop after their current turn"}" aria-label="${a.paused ? "Resume autopilot" : "Pause autopilot"}">${icon(a.paused ? "play" : "pause")}</button>`}
    </div>`;
  $("#apToggle", host) && ($("#apToggle", host).onclick = toggleAutopilot);
  $("#apRun", host) && ($("#apRun", host).onclick = runQueue);
}
export async function toggleAutopilot() {
  const paused = !!S.autopilot?.paused;
  // The button answers at once and the server confirms after; a failure puts the old state back.
  const before = S.autopilot;
  S.autopilot = { ...(S.autopilot || {}), paused: !paused, state: !paused ? "paused" : (before?.running ? "running" : "idle") };
  renderAutopilot();
  try {
    S.autopilot = await api.autopilotAction(paused ? "resume" : "pause");
    renderAutopilot(); view?.update?.("autopilot", S.autopilot);
    toast(paused ? "success" : "info", paused ? "Autopilot resumed" : "Autopilot paused", paused ? "Queued tasks start again." : "Nothing new starts; running tasks stop after their current turn.");
  } catch (e) { S.autopilot = before; renderAutopilot(); toast("error", "Autopilot", e.message); }
}
export async function runQueue() {
  try { S.queue = await api.queueStart(S.queue?.max_parallel || S.config.max_parallel || 1); S.autopilot = await api.autopilot(); renderAutopilot(); view?.update?.("autopilot", S.autopilot); toast("success", "Queue running", "Queued tasks start now."); }
  catch (e) { toast("error", "Queue", e.message); }
}
export async function haltQueue() {
  try { S.queue = await api.queueStop(); S.autopilot = await api.autopilot(); renderAutopilot(); view?.update?.("autopilot", S.autopilot); toast("info", "Queue halted", "Running tasks continue; nothing new starts."); }
  catch (e) { toast("error", "Queue", e.message); }
}
bus.on("autopilot:toggle", toggleAutopilot);
bus.on("queue:run", runQueue);
bus.on("queue:halt", haltQueue);
setInterval(async () => { if (!conn.online || !S.ready) return; try { S.autopilot = await api.autopilot(); renderAutopilot(); } catch {} }, 30000);

function renderConn() {
  const dot = $("#connDot"), txt = $("#connText");
  if (conn.signedOut) { dot.className = "dot warn"; txt.innerHTML = 'Signed out · <a href="" onclick="location.reload();return false">sign in</a>'; return; }
  if (!conn.online) { dot.className = "dot alarm"; txt.textContent = "Server unreachable · retrying"; return; }
  if (conn.stream) { dot.className = "dot ok"; txt.textContent = `Live · v${S.build || ""}`; }
  else { dot.className = "dot warn"; txt.textContent = "Reconnecting…"; }
}
onConnection(renderConn);

// ---------------------------------------------------------------------------- notifications
// A design choice made without asking offers one click to build another direction instead (orchestrator/exploration.py).
function notifyAction(p) {
  const pick = (p.actions || []).find((a) => a.direction);
  if (p.kind === "design_choice" && pick && p.task_id) {
    return { duration: 20000, action: { label: pick.label || `Use ${pick.direction} instead`, onClick: async () => {
      try { const r = await api.chooseDirection(p.task_id, pick.direction); toast("success", `Switched to ${pick.direction}`, r.note || "The team gets the new direction at its next turn."); }
      catch (e) { toast("error", "Could not change the direction", e.message); }
    } } };
  }
  return p.task_id ? { action: { label: "Open", onClick: () => navigate(`#/task/${p.task_id}`) } } : {};
}

function renderNotifBadge() { const unread = (S.notifications || []).filter((n) => !n.read).length; $("#notifBadge").hidden = !unread; }
const LEVEL_TONE = { success: "ok", error: "alarm", warning: "warn", info: "info" };
$("#notifBtn").onclick = () => {
  const p = $("#notifPanel");
  if (!p.hidden) { p.hidden = true; return; }
  const rows = S.notifications || [];
  const askDesktop = S.config.ui_notifications && permission() === "default";
  p.innerHTML = `<div class="notif-head"><strong>Notifications</strong><button class="btn xs ghost" id="notifClear" type="button">Mark all read</button></div>` +
    (askDesktop ? `<div class="notif-ask"><span>Get a desktop alert when a task needs you, even with Relay in the background.</span><button class="btn xs primary" id="notifAllow" type="button">${icon("bell")}Allow</button></div>` : "") +
    (rows.length ? `<div class="notif-list">${rows.slice(0, 40).map((n) => `<button type="button" class="notif-row ${n.read ? "" : "unread"}" data-n="${esc(n.task_id || "")}" data-kind="${esc(n.kind || "")}"><span class="notif-ic tone-${LEVEL_TONE[n.level] || "info"}">${icon(n.level === "success" ? "check" : n.level === "info" ? "info" : "alert", "sm")}</span><span class="notif-copy"><strong>${esc(n.title)}</strong><span>${esc(n.body || "")}</span><time>${timeAgo(n.time)}</time></span></button>`).join("")}</div>` : '<div class="empty small">No notifications yet. Deliveries, questions and failures appear here.</div>');
  p.hidden = false;
  $("#notifAllow", p) && ($("#notifAllow", p).onclick = async () => {
    const res = await requestPermission();
    toast(res === "granted" ? "success" : "info", res === "granted" ? "Desktop notifications on" : "Desktop notifications not allowed", res === "granted" ? "Choose which events alert you in Settings → Notifications." : "You can change this in the browser's site settings.");
    p.hidden = true;
  });
  $("#notifClear", p).onclick = async () => { await api.notificationsRead(); S.notifications.forEach((n) => (n.read = true)); renderNotifBadge(); p.hidden = true; };
  $$("[data-n]", p).forEach((r) => (r.onclick = () => { p.hidden = true; if (r.dataset.n) navigate(`#/task/${r.dataset.n}`); else if (r.dataset.kind === "digest") navigate("#/home/24h"); }));
  const close = (e) => { if (!p.contains(e.target) && !$("#notifBtn").contains(e.target)) { p.hidden = true; document.removeEventListener("mousedown", close); } };
  setTimeout(() => document.addEventListener("mousedown", close), 0);
};

// ---------------------------------------------------------------------------- live events
const countsSoon = throttle(renderCounts, 500);
function upsertTask(t) {
  const prev = S.tasks.get(t.id);
  S.tasks.set(t.id, { ...t, process: t.process || prev?.process || { state: "idle" } });
  return { statusChanged: !prev || prev.status !== t.status, pendingChanged: (prev?.pending?.id || null) !== (t.pending?.id || null), created: !prev };
}
export { upsertTask };
function onEvent(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "task": {
      const info = upsertTask(p);
      countsSoon();
      if (S.route.id === p.id) markSeen(p.id);
      if (info.statusChanged || info.pendingChanged) renderAttention();
      if (view && (S.route.view !== "task" || S.route.id === p.id)) view.update && view.update("task", { ...info, id: p.id });
      if (info.statusChanged && S.route.id !== p.id && statusOf(p).attention && p.pending) {
        toast("warning", `${p.name}: ${p.pending.kind === "question" ? "question for you" : "approval needed"}`, p.pending.question || "", { action: { label: "Answer", onClick: () => navigate("#/home/needs") } });
      }
      break;
    }
    case "task_deleted":
      S.tasks.delete(p.id); S.msgs.delete(p.id);
      S.notifications = (S.notifications || []).filter((n) => n.task_id !== p.id);
      renderNotifBadge(); renderCounts(); renderAttention();
      if (S.route.id === p.id) navigate("#/work");
      else view?.update?.("task", { id: p.id, deleted: true });
      break;
    case "message": {
      const store = S.msgs.get(p.task_id);
      if (store?.loaded) { store.list.push(p); store.byId.set(p.id, p); store.count = p.seq || store.count + 1; if (store.list.length > 6000) store.list.splice(0, store.list.length - 6000); }
      const moved = noteMessage(p);
      if (S.route.id === p.task_id && store?.loaded && view) view.update("message", p);
      else if (moved && view && S.route.view !== "task") view.update?.("activity", p);
      break;
    }
    case "message_update": {
      const store = S.msgs.get(p.task_id);
      let m = p;
      if (store?.loaded) { const cur = store.byId.get(p.id); if (cur) { Object.assign(cur, p); m = cur; } }
      const moved = noteMessage({ ...m, task_id: p.task_id });
      if (S.route.id === p.task_id && store?.loaded && view) view.update("message_update", m);
      else if (moved && view && S.route.view !== "task") view.update?.("activity", m);
      break;
    }
    case "process": {
      const t = S.tasks.get(p.task_id);
      if (t) { t.process = p; if (view && (S.route.id === p.task_id || S.route.view !== "task")) view.update?.("process", p); }
      break;
    }
    case "event": {
      const t = S.tasks.get(p.task_id);
      if (t) { t.events = [...(t.events || []), p].slice(-400); if (S.route.id === p.task_id && view) view.update("event", p); }
      break;
    }
    case "artifact": if (S.route.id === p.task_id && view) view.update("artifact", p); break;
    case "notify": S.notifications.unshift(p); S.notifications = S.notifications.slice(0, 100); renderNotifBadge(); toast(p.level, p.title, p.body, notifyAction(p)); deliver(p); break;
    case "github": S.github = { ...S.github, ...p }; view?.update?.("github", p); break;
    case "config": S.config = p; applyTheme(p.ui_theme, p.ui_density); break;
    case "queue": S.queue = { ...S.queue, ...p }; view?.update?.("queue", p); break;
    case "autopilot": S.autopilot = p; renderAutopilot(); view?.update?.("autopilot", p); break;
    case "knowledge": if (p.error) toast("error", "Knowledge refresh failed", p.error); else if (p.refreshed) { const bad = p.refreshed.filter((r) => r.status === "error"); toast(bad.length ? "error" : "success", "Knowledge docs checked", p.refreshed.map((r) => `${r.repo.split("/").pop()}: ${r.status}${r.error ? ` (${r.error})` : ""}`).join(" · ")); } view?.update?.("knowledge"); break;
    case "lessons": S.lessonsPending = p.pending || 0; renderCounts(); view?.update?.("lessons"); break;
    case "learning": if (p.proposals !== undefined) { S.learningProposals = p.proposals || 0; renderCounts(); } if (p.error) toast("error", "Playbook refresh failed", p.error); view?.update?.("learning"); break;
    case "agents": { const j = p.job; if (j) toast(j.state === "done" ? "success" : "error", `${agentLabel(p.agent)} ${j.action === "remove" ? "removal" : j.action} ${j.state === "done" ? "finished" : "failed"}`, j.error || ""); view?.update?.("agents"); break; }
  }
}

// ---------------------------------------------------------------------------- command palette + keyboard
function openPalette(initial = "") { palette((q) => commandItems(q, { view }), { initial, placeholder: "Search tasks, jump anywhere, or type “create task: …”" }); }
bus.on("palette", openPalette);
$("#paletteBtn").onclick = () => openPalette();
$("#shortcutsBtn").onclick = () => openShortcuts();

let chordUntil = 0;
const chordPill = el('<div class="chord-pill" hidden><kbd>G</kbd> then <kbd>H</kbd> Mission Control · <kbd>W</kbd> Work · <kbd>K</kbd> Knowledge · <kbd>A</kbd> Agents · <kbd>S</kbd> Settings · <kbd>Y</kbd> Needs you</div>');
document.body.appendChild(chordPill);
const endChord = () => { chordUntil = 0; chordPill.hidden = true; };

function stepTask(delta) {
  const ids = S.workOrder?.length ? S.workOrder : [...S.tasks.values()].filter((t) => !t.archived).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")).map((t) => t.id);
  if (!ids.length) return;
  const cur = ids.indexOf(S.route.id);
  const next = ids[cur < 0 ? 0 : Math.max(0, Math.min(ids.length - 1, cur + delta))];
  if (next && next !== S.route.id) navigate(`#/task/${next}`);
}

// Tab strips move with the arrow keys, as ARIA tabs do.
document.addEventListener("keydown", (e) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const tab = e.target.closest?.('[role="tab"]');
  const list = tab?.closest('[role="tablist"]');
  if (!list) return;
  const tabs = [...list.querySelectorAll('[role="tab"]')].filter((x) => !x.disabled && x.offsetParent !== null);
  const i = tabs.indexOf(tab);
  const next = e.key === "Home" ? tabs[0] : e.key === "End" ? tabs[tabs.length - 1] : tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
  if (next && next !== tab) { e.preventDefault(); next.focus(); next.click(); }
}, true);

document.addEventListener("keydown", (e) => {
  const a = document.activeElement;
  const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(a?.tagName) || a?.isContentEditable;
  const overlay = !!$(".modal-backdrop, .palette-back");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { if (!overlay) { e.preventDefault(); openPalette(); } return; }
  if ((e.ctrlKey || e.metaKey) && e.key === "\\") { if (!overlay) { e.preventDefault(); toggleNav(); } return; }
  if (e.key === "Escape") {
    endChord();
    if (overlay) return;                       // modals and the palette close themselves
    if (drawerOpen()) { closeDrawer(); return; }
    if (!$("#notifPanel").hidden) { $("#notifPanel").hidden = true; $("#notifBtn").focus(); return; }
    return;
  }
  if (typing || overlay || e.ctrlKey || e.metaKey || e.altKey) return;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  if (chordUntil) { const target = Date.now() < chordUntil && GOTO[key]; endChord(); if (target) { e.preventDefault(); navigate(target); } return; }
  if (e.key === "?") { e.preventDefault(); openShortcuts(); return; }
  if (e.key === "/") { e.preventDefault(); if (S.route.view === "work" && $("#workSearch")) $("#workSearch").focus(); else openPalette(); return; }
  if (e.key === "." && S.route.view === "task" && $("#guidance")) { e.preventDefault(); $("#guidance").focus(); return; }
  if (e.shiftKey) return;
  switch (key) {
    case "n": e.preventDefault(); openNewTask(); break;
    case "[": e.preventDefault(); toggleNav(); break;
    case "g": e.preventDefault(); chordUntil = Date.now() + 1500; chordPill.hidden = false; setTimeout(() => { if (chordUntil && Date.now() >= chordUntil) endChord(); }, 1600); break;
    case "j": if (S.route.view === "task") { e.preventDefault(); stepTask(1); } break;
    case "k": if (S.route.view === "task") { e.preventDefault(); stepTask(-1); } break;
    default: view?.key?.(key, e);
  }
});

// Avatars take each agent's colour from the registry, so agents added later need no stylesheet change.
function paintAgentColors() {
  let tag = document.getElementById("agentColors");
  if (!tag) { tag = document.createElement("style"); tag.id = "agentColors"; document.head.appendChild(tag); }
  tag.textContent = Object.entries(S.agentMeta || {}).map(([id, m]) => {
    const sel = `.av.${CSS.escape(id)}`;
    const color = m.color && /^#[0-9a-f]{3,8}$/i.test(m.color) ? `background-color:${m.color};` : "";
    return m.logo ? `${sel}{${color}background-image:url(${encodeURI(m.logo)});color:transparent;background-position:center;background-size:cover;background-repeat:no-repeat}` : (color ? `${sel}{${color}}` : "");
  }).join("\n");
}

// ---------------------------------------------------------------------------- bootstrap
async function bootstrap() {
  let st;
  try { st = await api.state(); } catch (e) { main.innerHTML = `<div class="page"><div class="empty-state">${icon("alert", "lg")}<h3>Relay's server is unreachable</h3><p>${esc(e.message)}</p><p class="muted">Retrying every few seconds.</p></div></div>`; renderConn(); setTimeout(bootstrap, 3000); return; }
  S.build = st.build; S.config = st.config || {}; S.agents = st.agents || {}; S.agentMeta = st.agent_meta || {};
  paintAgentColors(); S.presets = st.presets || []; S.templates = st.templates || []; S.providers = st.providers || {};
  S.github = st.github || {}; S.queue = st.queue || {}; S.autopilot = st.autopilot || null; S.notifications = st.notifications || []; S.lessonsPending = st.lessons_pending || 0;
  S.tasks = new Map((st.tasks || []).map((t) => [t.id, t]));
  applyTheme(S.config.ui_theme, S.config.ui_density);
  renderNotifBadge(); renderConn(); renderAttention(); renderAutopilot();
  if (!S.ready) { S.ready = true; connectEvents(onEvent); mountOrgHeader(); route(); }
  else view?.update?.("task", {});
}
bootstrap();
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && S.route.id) { markSeen(S.route.id); renderAttention(); } });
// Periodic reconciliation in case an event was missed.
setInterval(async () => {
  if (!conn.online) return;
  try {
    const rows = await api.tasks(); const seen = new Set(); let changed = false;
    for (const t of rows) { seen.add(t.id); const prev = S.tasks.get(t.id); if (!prev || prev.updated_at !== t.updated_at || prev.status !== t.status) { const info = upsertTask(t); changed = true; if (view && (S.route.view !== "task" || S.route.id === t.id)) view.update?.("task", { ...info, id: t.id }); } }
    for (const id of [...S.tasks.keys()]) if (!seen.has(id)) { S.tasks.delete(id); changed = true; }
    if (changed) { renderCounts(); renderAttention(); }
  } catch {}
}, 20000);
