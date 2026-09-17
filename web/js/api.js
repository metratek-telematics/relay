// HTTP + Server-Sent Events client for the local backend.
export const conn = { online: false, stream: false, signedOut: false, lastError: null, listeners: new Set() };

function notifyConn() { for (const fn of conn.listeners) { try { fn(conn); } catch {} } }

export async function request(url, options = {}) {
  let r;
  try {
    r = await fetch(url, { cache: "no-store", ...options });
    if (!conn.online) { conn.online = true; notifyConn(); }
  } catch (err) {
    conn.online = false; conn.lastError = err.message; notifyConn();
    throw new Error(`Local backend unreachable at ${location.origin}. Keep run.bat open. (${err.message || err})`);
  }
  const text = await r.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { error: text || `HTTP ${r.status}` }; }
  if (!r.ok) {
    const e = new Error(data.error || `Request failed (${r.status})`);
    e.status = r.status; e.data = data;
    throw e;
  }
  return data;
}
export const get = (url) => request(url);
export const post = (url, body = {}) => request(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const put = (url, body = {}) => request(url, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const patch = (url, body = {}) => request(url, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
export const del = (url) => request(url, { method: "DELETE" });

export const api = {
  state: () => get("/api/state"),
  dashboard: () => get("/api/dashboard"),
  health: (force) => get(`/api/health${force ? "?force=1" : ""}`),
  agents: (force) => get(`/api/agents${force ? "?force=1" : ""}`),
  testAgent: (name, model) => post(`/api/agents/${name}/test`, { model }),
  installAgent: (name, action) => post(`/api/agents/${name}/install`, { action }),
  agentJobs: () => get("/api/agents/jobs"),
  agentModels: (name, refresh) => get(`/api/agents/${name}/models${refresh ? "?refresh=1" : ""}`),
  agentAccount: (name, refresh) => get(`/api/agents/${name}/account${refresh ? "?refresh=1" : ""}`),
  settings: () => get("/api/settings"),
  saveSettings: (partial) => post("/api/settings", partial),
  presets: () => get("/api/presets"),
  rules: () => get("/api/rules"),
  rule: (name) => get(`/api/rules/${encodeURIComponent(name)}`),
  saveRule: (name, text) => put(`/api/rules/${encodeURIComponent(name)}`, { text }),
  browse: (path) => get(`/api/fs/browse?path=${encodeURIComponent(path || "")}`),
  repoInfo: (path) => get(`/api/fs/repo-info?path=${encodeURIComponent(path)}`),
  tasks: () => get("/api/tasks"),
  task: (id) => get(`/api/tasks/${encodeURIComponent(id)}`),
  createTask: (payload) => post("/api/tasks", payload),
  updateTask: (id, body) => patch(`/api/tasks/${encodeURIComponent(id)}`, body),
  editAcceptance: (id, ops) => patch(`/api/tasks/${encodeURIComponent(id)}/acceptance`, ops),
  deleteTask: (id, worktree) => del(`/api/tasks/${encodeURIComponent(id)}${worktree ? "?worktree=1" : ""}`),
  action: (id, action, body = {}) => post(`/api/tasks/${encodeURIComponent(id)}/${action}`, body),
  messages: (id, after = 0, limit = 0) => get(`/api/tasks/${encodeURIComponent(id)}/messages?after=${after}&limit=${limit}`),
  handoff: (id) => get(`/api/tasks/${encodeURIComponent(id)}/handoff`),
  commits: (id) => get(`/api/tasks/${encodeURIComponent(id)}/commits`),
  commitDiff: (id, sha, path) => get(`/api/tasks/${encodeURIComponent(id)}/commits/${encodeURIComponent(sha)}/diff${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  work: (id) => get(`/api/tasks/${encodeURIComponent(id)}/work`),
  changeset: (id) => get(`/api/tasks/${encodeURIComponent(id)}/changeset`),
  pr: (id, force) => get(`/api/tasks/${encodeURIComponent(id)}/pr${force ? "?force=1" : ""}`),
  branchName: (p) => get(`/api/branch-name?${new URLSearchParams(p)}`),
  files: (id) => get(`/api/tasks/${encodeURIComponent(id)}/files`),
  diff: (id, path) => get(`/api/tasks/${encodeURIComponent(id)}/diff${path ? `?path=${encodeURIComponent(path)}` : ""}`),
  artifact: (id, kind, tail) => get(`/api/tasks/${encodeURIComponent(id)}/artifact/${kind}${tail ? `?tail=${tail}` : ""}`),
  repoTree: (id) => get(`/api/tasks/${encodeURIComponent(id)}/repo-tree?depth=7`),
  repoFile: (id, path) => get(`/api/tasks/${encodeURIComponent(id)}/repo-file?path=${encodeURIComponent(path)}`),
  saveRepoFile: (id, path, text, create) => put(`/api/tasks/${encodeURIComponent(id)}/repo-file`, { path, text, create }),
  open: (id, what) => post(`/api/tasks/${encodeURIComponent(id)}/open/${what}`),
  queueStart: (parallel) => post("/api/queue/start", { parallel }),
  queueStop: () => post("/api/queue/stop"),
  queue: () => get("/api/queue"),
  ghStatus: () => get("/api/github/status"),
  ghRepos: (force) => get(`/api/github/repos${force ? "?force=1" : ""}`),
  ghClone: (repo, name) => post("/api/github/clone", { repo, name }),
  ghSources: () => get("/api/github/sources"),
  ghAddSource: (body) => post("/api/github/sources", body),
  ghDeleteSource: (id) => del(`/api/github/sources/${encodeURIComponent(id)}`),
  ghPoll: () => post("/api/github/poll"),
  issues: (p = {}) => get(`/api/issues?${new URLSearchParams(Object.entries(p).filter(([, v]) => v !== "" && v != null && v !== false).map(([k, v]) => [k, v === true ? "1" : String(v)]))}`),
  createIssueTasks: (body) => post("/api/issues/tasks", body),
  moveInQueue: (id, direction) => post(`/api/tasks/${encodeURIComponent(id)}/move`, { direction }),
  notificationsRead: () => post("/api/notifications/read"),
  autopilot: () => get("/api/autopilot"),
  autopilotAction: (action) => post(`/api/autopilot/${action}`),
  inbox: () => get("/api/autopilot/inbox"),
  digest: (hours) => get(`/api/digest${hours ? `?hours=${encodeURIComponent(hours)}` : ""}`),
  repos: () => get("/api/repos"),
  // System map and multi-repository tasks (orchestrator/systemmap.py, orchestrator/multirepo.py)
  system: () => get("/api/system"),
  systemScan: (paths) => post("/api/system/scan", { paths: paths || [] }),
  systemAddComponent: (body) => post("/api/system/components", body),
  systemPatchComponent: (id, body) => patch(`/api/system/components/${encodeURIComponent(id)}`, body),
  systemDeleteComponent: (id) => del(`/api/system/components/${encodeURIComponent(id)}`),
  systemAddEdge: (body) => post("/api/system/edges", body),
  systemPatchEdge: (id, body) => patch(`/api/system/edges/${encodeURIComponent(id)}`, body),
  systemDeleteEdge: (id) => del(`/api/system/edges/${encodeURIComponent(id)}`),
  systemDiscover: (force) => get(`/api/system/discover${force ? "?force=1" : ""}`),
  systemClone: (repos) => post("/api/system/discover/clone", { repos }),
  systemDescribe: () => post("/api/system/describe", {}),
  systemRelated: (repo) => get(`/api/system/related?repo=${encodeURIComponent(repo)}`),
  taskAddRepo: (id, repo, reason) => post(`/api/tasks/${encodeURIComponent(id)}/repos`, { repo, reason }),
  repoFetch: (path) => post("/api/repos/fetch", { path }),
  repoPull: (path) => post("/api/repos/pull", { path }),
  repoGraph: (path) => get(`/api/repos/graph?path=${encodeURIComponent(path)}`),
  worktrees: () => get("/api/worktrees"),
  repoEnv: (path) => get(`/api/repos/env?path=${encodeURIComponent(path)}`),
  saveRepoEnv: (path, env) => put("/api/repos/env", { path, env }),
  repoEnvParse: (text) => post("/api/repos/env/parse", { text }),
  worktreeSize: (path) => get(`/api/worktrees/size?path=${encodeURIComponent(path)}`),
  removeWorktree: (path, discard) => post("/api/worktrees/remove", { path, discard }),
  deleteBranch: (repo, branch) => post("/api/worktrees/delete-branch", { repo, branch }),
  cleanupPreview: () => get("/api/worktrees/cleanup"),
  cleanup: (paths) => post("/api/worktrees/cleanup", { paths }),
  scorecard: (id) => get(`/api/tasks/${encodeURIComponent(id)}/scorecard`),
  refreshScorecard: (id) => post(`/api/tasks/${encodeURIComponent(id)}/scorecard/refresh`),
  runRetro: (id) => post(`/api/tasks/${encodeURIComponent(id)}/retro`),
  connectors: () => get("/api/connectors"),
  saveConnector: (body) => post("/api/connectors", body),
  deleteConnector: (name) => del(`/api/connectors/${encodeURIComponent(name)}`),
  testConnector: (name) => post(`/api/connectors/${encodeURIComponent(name)}/test`),
  connectorCalls: (name) => get(`/api/connectors/calls${name ? `?name=${encodeURIComponent(name)}` : ""}`),
  connectorsForRepo: (path) => get(`/api/connectors/for-repo?path=${encodeURIComponent(path || "")}`),
  saveConnectorDefaults: (path, names) => put("/api/connectors/repo-defaults", { path, names }),
  lessons: () => get("/api/lessons"),
  addLesson: (body) => post("/api/lessons", body),
  approveLesson: (id, body = {}) => post(`/api/lessons/${encodeURIComponent(id)}/approve`, body),
  rejectLesson: (id) => post(`/api/lessons/${encodeURIComponent(id)}/reject`),
  updateLesson: (id, body) => patch(`/api/lessons/${encodeURIComponent(id)}`, body),
  deleteLesson: (id) => del(`/api/lessons/${encodeURIComponent(id)}`),
};

// ---------------------------------------------------------------------------- SSE
let es = null;
let backoff = 1000;
export function connectEvents(onEvent) {
  if (es) { try { es.close(); } catch {} }
  try { es = new EventSource("/api/events"); } catch { conn.stream = false; notifyConn(); return; }
  es.onopen = () => { conn.stream = true; conn.online = true; backoff = 1000; notifyConn(); };
  es.onerror = () => {
    conn.stream = false; notifyConn();
    try { es.close(); } catch {}
    es = null;
    setTimeout(() => connectEvents(onEvent), backoff);
    backoff = Math.min(backoff * 1.6, 15000);
  };
  es.onmessage = (e) => {
    if (!conn.stream) { conn.stream = true; notifyConn(); }
    try { onEvent(JSON.parse(e.data)); } catch (err) { console.warn("bad event", err); }
  };
}

export function onConnection(fn) { conn.listeners.add(fn); return () => conn.listeners.delete(fn); }

// Periodic reachability probe (cheap). It also notices a server restart: after a deploy the page still runs
// the old JavaScript until reloaded, which is how fixed buttons kept showing old errors.
let bootSeen = null, reloadOffered = false;
setInterval(async () => {
  try {
    // Behind a login proxy an expired session answers with a redirect to the sign-in page, which a normal
    // fetch follows cross-origin and reports as a network failure. Ask without following it instead.
    const raw = await fetch("/api/ping", { cache: "no-store", redirect: "manual" });
    const signedOut = raw.type === "opaqueredirect" || raw.status === 401;
    if (signedOut !== conn.signedOut) { conn.signedOut = signedOut; notifyConn(); }
    if (signedOut) return;
    const r = await get("/api/ping");
    if (!r.boot) return;
    if (bootSeen === null) { bootSeen = r.boot; return; }
    if (r.boot !== bootSeen && !reloadOffered) {
      reloadOffered = true;
      const { toast } = await import("./ui.js");
      toast("info", "Relay was updated", "Reload to use the new version.", { sticky: true, action: { label: "Reload", onClick: () => location.reload() } });
    }
  } catch {}
}, 8000);
