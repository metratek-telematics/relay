// Organisation layer, browser side: who you are, the current project, and the pages under #/org/<section>.
// Self-contained so it can move into any navigation: app.js calls mountOrg(main, section) and mountOrgHeader(el);
// settings.js lists one "Workspace & access" entry that links here. Server side: orchestrator/org.
import { $, $$, esc, icon, toast } from "../../ui.js";
import { S, bus, navigate } from "../../state.js";
import { request } from "../../api.js";

// ---------------------------------------------------------------------------- API
const json = (method) => (url, body) => request(url, { method, headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
const get = (url) => request(url);
const post = json("POST"), put = json("PUT"), patch = json("PATCH"), del = json("DELETE");
const qs = (o) => { const p = new URLSearchParams(); for (const [k, v] of Object.entries(o || {})) if (v !== undefined && v !== null && v !== "") p.set(k, v); const s = p.toString(); return s ? `?${s}` : ""; };

export const orgApi = {
  me: () => get("/api/org/me"),
  saveMe: (b) => patch("/api/org/me", b),
  testChannel: (ch) => post(`/api/org/me/channels/${ch}/test`, {}),
  myDeliveries: () => get("/api/org/me/deliveries"),
  tokens: () => get("/api/org/me/tokens"),
  createToken: (b) => post("/api/org/me/tokens", b),
  revokeToken: (id) => del(`/api/org/me/tokens/${encodeURIComponent(id)}`),
  pushSubscribe: (subscription) => post("/api/org/me/push", { subscription }),
  pushUnsubscribe: (endpoint) => del("/api/org/me/push", { endpoint }),
  users: () => get("/api/org/users"),
  invite: (b) => post("/api/org/users", b),
  saveUser: (u, b) => patch(`/api/org/users/${encodeURIComponent(u)}`, b),
  removeUser: (u) => del(`/api/org/users/${encodeURIComponent(u)}`),
  matrix: () => get("/api/org/access-matrix"),
  projects: () => get("/api/org/projects"),
  createProject: (b) => post("/api/org/projects", b),
  saveProject: (id, b) => patch(`/api/org/projects/${encodeURIComponent(id)}`, b),
  deleteProject: (id) => del(`/api/org/projects/${encodeURIComponent(id)}`),
  moveTasks: (id, task_ids) => post(`/api/org/projects/${encodeURIComponent(id)}/tasks`, { task_ids }),
  settings: () => get("/api/org/settings"),
  saveAuth: (b) => put("/api/org/settings/auth", b),
  saveIntegrations: (b) => put("/api/org/settings/integrations", b),
  saveBudgets: (b) => put("/api/org/budgets", b),
  vapidKeys: () => post("/api/org/integrations/webpush/keys", {}),
  testIntegration: (b) => post("/api/org/integrations/test", b),
  deliveries: () => get("/api/org/deliveries"),
  audit: (f) => get(`/api/org/audit${qs(f)}`),
  auditCsvUrl: (f) => `/api/org/audit/export.csv${qs(f)}`,
  auditVerify: () => get("/api/org/audit/verify"),
  usage: (f) => get(`/api/org/usage${qs(f)}`),
  onboarding: () => get("/api/org/onboarding"),
  dismissOnboarding: (dismissed = true) => post("/api/org/onboarding/dismiss", { dismissed }),
  chooseTeam: (preset) => post("/api/org/onboarding/team", { preset }),
  sampleTask: () => post("/api/org/onboarding/sample-task", {}),
  openapi: () => get("/api/v1/openapi.json"),
};

// ---------------------------------------------------------------------------- who am I
export const ORG = { me: null, projects: [], people: [], loaded: false };
const LEVEL = { anonymous: 0, viewer: 10, member: 20, admin: 30, owner: 40 };
export const can = (role) => (LEVEL[ORG.me?.role] || 0) >= (LEVEL[role] || 0);

export async function loadMe() {
  try {
    ORG.me = await orgApi.me();
    ORG.projects = ORG.me.projects || [];
    ORG.loaded = true;
    orgApi.users().then((r) => { ORG.people = r.users || []; }).catch(() => {});
    document.documentElement.dataset.role = ORG.me.role;
    const p = currentProject();
    if (p && !ORG.projects.some((x) => x.id === p)) setProject("all", { silent: true });
    bus.emit("org");
  } catch (e) {
    ORG.loaded = true;
    if (e.status === 401) document.documentElement.dataset.role = "anonymous";
  }
  return ORG.me;
}

// ---------------------------------------------------------------------------- current project
const PKEY = "relay.project";
export function currentProject() { try { return localStorage.getItem(PKEY) || "all"; } catch { return "all"; } }
export function projectById(id) { return ORG.projects.find((p) => p.id === id) || null; }
export function setProject(id, { silent = false } = {}) {
  try { localStorage.setItem(PKEY, id || "all"); } catch {}
  if (!silent) bus.emit("project", id);
}
// A task belongs to its recorded project, else to the project that lists its repository, else to the default project.
const norm = (p) => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "");
export function projectOfTask(t) {
  if (t?.project_id && ORG.projects.some((p) => p.id === t.project_id)) return t.project_id;
  const repo = norm(t?.repo);
  const hit = ORG.projects.find((p) => (p.repos || []).some((r) => norm(r) === repo));
  return hit ? hit.id : "default";
}
export function inProject(t) {
  const cur = currentProject();
  return cur === "all" || !ORG.projects.length || projectOfTask(t) === cur;
}

// ---------------------------------------------------------------------------- shared rendering
const XICONS = {
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  key: '<circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.7 12.3 9.8-9.8M17 6l3 3M14.5 8.5l2 2"/>',
  scroll: '<path d="M8 21h12a2 2 0 0 0 2-2v-2H10v2a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v3h4"/><path d="M19 17V5a2 2 0 0 0-2-2H4"/><path d="M11 8h6M11 12h5"/>',
  plug: '<path d="M12 22v-5M9 8V2M15 8V2M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8z"/>',
  chart: '<path d="M3 3v18h18"/><rect x="7" y="12" width="3" height="6" rx="1"/><rect x="12" y="8" width="3" height="10" rx="1"/><rect x="17" y="5" width="3" height="13" rx="1"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  api: '<path d="m8 9-3 3 3 3M16 9l3 3-3 3M13.5 6l-3 12"/>',
  rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
  mail: '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
  webhook: '<path d="M18 16.98h-5.99c-1.1 0-1.95.94-2.48 1.9A4 4 0 0 1 2 17c.01-.7.2-1.4.57-2"/><path d="m6 17 3.13-5.78c.53-.97.1-2.18-.5-3.1a4 4 0 1 1 6.89-4.06"/><path d="m12 6 3.13 5.73C15.66 12.7 16.9 13 18 13a4 4 0 0 1 0 8"/>',
  slack: '<rect x="3" y="10" width="8" height="3" rx="1.5"/><rect x="13" y="11" width="8" height="3" rx="1.5"/><rect x="10" y="3" width="3" height="8" rx="1.5"/><rect x="11" y="13" width="3" height="8" rx="1.5"/>',
  telegram: '<path d="m22 3-9.5 18-2.5-7.5L2.5 11z"/><path d="m22 3-12 10.5"/>',
  discord: '<path d="M8 12h.01M16 12h.01"/><path d="M7 5.5c1.6-.7 3.3-1 5-1s3.4.3 5 1c2 2.8 3 6 3 9.5-1.6 1.4-3.5 2.4-5.5 3l-1-2.1M7 5.5C5 8.3 4 11.5 4 15c1.6 1.4 3.5 2.4 5.5 3l1-2.1"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  lock: '<rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
  checkCircle: '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
};
export function xicon(name, cls = "") {
  if (!XICONS[name]) return icon(name, cls);
  return `<svg class="ic ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${XICONS[name]}</svg>`;
}

export function avatar(u, size = 28) {
  if (!u) return "";
  const style = `width:${size}px;height:${size}px;font-size:${Math.round(size * 0.4)}px;background:${esc(u.color || "var(--text-3)")}`;
  const img = u.avatar_url ? `<img src="${esc(u.avatar_url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : "";
  return `<span class="org-av" style="${style}" title="${esc(u.name || u.username)}" aria-hidden="true"><span>${esc(u.initials || "?")}</span>${img}</span>`;
}

export const ROLE_TONE = { owner: "purple", admin: "blue", member: "green", viewer: "", anonymous: "red" };
export const roleBadge = (role) => `<span class="badge ${ROLE_TONE[role] || ""}">${esc(role || "—")}</span>`;
export const money = (n, digits) => { n = Number(n) || 0; return `$${n.toFixed(digits ?? (n && n < 10 ? 2 : 0))}`; };

export function copyButton(text, label = "Copy") {
  return `<button class="btn xs" data-copy="${esc(text)}">${icon("copy")}${esc(label)}</button>`;
}
export function bindCopy(root) {
  $$("[data-copy]", root).forEach((b) => (b.onclick = async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("success", "Copied"); } catch { toast("error", "Copy failed", "Select the text and copy it by hand."); }
  }));
}

export function readOnlyNote(role, what) {
  return `<div class="org-note">${xicon("lock", "sm")}<span>${esc(what)} needs the <b>${esc(role)}</b> role. You can look, not change.</span></div>`;
}

// ---------------------------------------------------------------------------- pages
export const SECTIONS = [
  { group: "You", items: [["profile", "Profile", "user"], ["notifications", "Notifications", "bell"], ["tokens", "API tokens", "key"]] },
  { group: "Workspace", items: [["welcome", "Getting started", "rocket"], ["projects", "Projects", "grid"], ["people", "People & roles", "users"], ["integrations", "Integrations", "plug", "admin"], ["usage", "Usage & budgets", "chart"], ["audit", "Audit log", "scroll", "admin"], ["api", "API docs", "api"]] },
];

const LOADERS = {
  profile: () => import("./profile.js").then((m) => m.mountProfile),
  notifications: () => import("./profile.js").then((m) => m.mountNotifications),
  tokens: () => import("./tokens.js").then((m) => m.mountTokens),
  welcome: () => import("./onboarding.js").then((m) => m.mountWelcome),
  projects: () => import("./projects.js").then((m) => m.mountProjects),
  project: () => import("./projects.js").then((m) => m.mountProjectHome),
  people: () => import("./people.js").then((m) => m.mountPeople),
  integrations: () => import("./integrations.js").then((m) => m.mountIntegrations),
  usage: () => import("./usage.js").then((m) => m.mountUsage),
  audit: () => import("./audit.js").then((m) => m.mountAudit),
  api: () => import("./apidocs.js").then((m) => m.mountApiDocs),
};

export function mountOrg(main, section, arg) {
  const sec = LOADERS[section] ? section : "profile";
  main.innerHTML = `<div class="page org-page"><div class="org-layout"><nav class="settings-nav org-nav" id="orgNav" aria-label="Workspace"></nav><div class="org-main" id="orgMain"><div class="empty small">Loading…</div></div></div></div>`;
  const nav = $("#orgNav", main), body = $("#orgMain", main);
  let child = null, alive = true;
  const drawNav = () => {
    nav.innerHTML = SECTIONS.map((g) => `<div class="org-nav-group">${esc(g.group)}</div>` + g.items.filter(([, , , need]) => !need || can(need))
      .map(([k, l, i]) => `<a href="#/org/${k}" class="${k === sec || (sec === "project" && k === "projects") ? "active" : ""}">${xicon(i)}${esc(l)}</a>`).join("")).join("");
  };
  drawNav();
  const off = bus.on("org", drawNav);
  (async () => {
    if (!ORG.me) await loadMe();
    const mount = await LOADERS[sec]();
    if (!alive) return;
    body.innerHTML = "";
    child = mount(body, arg);
  })().catch((e) => { body.innerHTML = `<div class="empty">${icon("alert", "lg")}<p>${esc(e.message)}</p></div>`; });
  return {
    update(reason, info) { child?.update?.(reason, info); },
    destroy() { alive = false; off(); child?.destroy?.(); },
  };
}

// Route helper for app.js: #/org/<section>/<arg>
export function parseOrgRoute(parts) {
  if (parts[0] !== "org") return null;
  return { view: "org", id: null, tab: parts[2] ? decodeURIComponent(parts[2]) : null, section: parts[1] || "profile" };
}

export { mountOrgHeader } from "./header.js";
export { navigate };
