// Shared application state + tiny event bus.
export const S = {
  ready: false,
  build: "",
  route: { view: "dashboard", id: null, tab: null, section: null },
  tasks: new Map(),
  msgs: new Map(),            // taskId -> { list: [], byId: Map, loaded: bool, count: n }
  config: {},
  agents: {},
  agentMeta: {},
  presets: [],
  templates: [],
  github: {},
  queue: {},
  notifications: [],
  ui: {
    filter: "all", search: "", showTools: true, showThinking: false, showGit: false, inspector: true, inspectorTab: "overview",
    theme: "system", density: "comfortable",
  },
};

const handlers = new Map();
export const bus = {
  on(ev, fn) { if (!handlers.has(ev)) handlers.set(ev, new Set()); handlers.get(ev).add(fn); return () => handlers.get(ev).delete(fn); },
  emit(ev, ...args) { for (const fn of handlers.get(ev) || []) { try { fn(...args); } catch (e) { console.error(e); } } },
};

export function navigate(hash) { if (location.hash !== hash) location.hash = hash; else bus.emit("route"); }
export const task = (id) => S.tasks.get(id);
export const current = () => (S.route.id ? S.tasks.get(S.route.id) : null);

export const AGENT_META_FALLBACK = {
  codex: { label: "Codex", vendor: "OpenAI", color: "#5d78d6" },
  claude: { label: "Claude", vendor: "Anthropic", color: "#d97757" },
  gemini: { label: "Gemini", vendor: "Google", color: "#2f9d87" },
};
export function agentLabel(a) { return (S.agentMeta[a] || AGENT_META_FALLBACK[a] || {}).label || ({ user: "You", orchestrator: "Orchestrator", system: "Orchestrator", verify: "Verification", git: "Git", github: "GitHub" }[a]) || a || "—"; }
export function agentInitial(a) { return ({ codex: "Cx", claude: "Cl", gemini: "Ge", user: "You", orchestrator: "O", system: "O", verify: "V", git: "G", github: "GH" }[a]) || (a || "?").slice(0, 1).toUpperCase(); }
export const ROLE_LABEL = { supervisor: "Supervisor", worker: "Worker", reviewer: "Reviewer", user: "You", orchestrator: "Orchestrator", system: "Orchestrator", verify: "Verification", git: "Git", github: "GitHub" };

export const STATUS = {
  draft: { label: "Draft", tone: "", live: false },
  queued: { label: "Queued", tone: "", live: false },
  running: { label: "Starting", tone: "blue", live: true },
  planning: { label: "Planning", tone: "blue", live: true },
  implementing: { label: "Implementing", tone: "accent", live: true },
  verifying: { label: "Verifying", tone: "amber", live: true },
  reviewing: { label: "Reviewing", tone: "purple", live: true },
  delivering: { label: "Delivering", tone: "green", live: true },
  needs_input: { label: "Needs input", tone: "amber", live: false, attention: true },
  paused: { label: "Paused", tone: "amber", live: false, attention: true },
  done: { label: "Delivered", tone: "green", live: false },
  failed: { label: "Failed", tone: "red", live: false },
  stopped: { label: "Stopped", tone: "", live: false },
  interrupted: { label: "Interrupted", tone: "amber", live: false, attention: true },
};
export const LIVE = new Set(["running", "planning", "implementing", "verifying", "reviewing", "delivering"]);
export const statusOf = (t) => STATUS[t?.status] || STATUS.queued;
export const toneVar = (tone) => ({ blue: "var(--blue)", accent: "var(--accent)", amber: "var(--amber)", purple: "var(--purple)", green: "var(--green)", red: "var(--red)" }[tone] || "var(--text-3)");

export function roleAgent(t, role) { return t?.workflow?.roles?.[role]?.agent || ""; }
export function roleModel(t, role) { return t?.workflow?.roles?.[role]?.model || S.config?.roles?.[role]?.model || ""; }
export function roleEffort(t, role) { return t?.workflow?.roles?.[role]?.effort || S.config?.roles?.[role]?.effort || ""; }
export function defaultModelLabel(agent) { const h = S.agents?.[agent]; return h?.default_model ? `CLI default (${h.default_model}${h.default_effort ? ` · ${h.default_effort}` : ""})` : "CLI default"; }

export function msgStore(tid) {
  if (!S.msgs.has(tid)) S.msgs.set(tid, { list: [], byId: new Map(), loaded: false, count: 0 });
  return S.msgs.get(tid);
}
export function taskElapsed(t) {
  if (!t?.started_at) return 0;
  const a = Date.parse(t.started_at);
  const b = t.finished_at ? Date.parse(t.finished_at) : (LIVE.has(t.status) || t.status === "needs_input" || t.status === "paused" ? Date.now() : Date.parse(t.updated_at));
  return Math.max(0, (b - a) / 1000);
}
