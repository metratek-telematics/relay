// Command palette content: jump anywhere, act on the task in front of you, and create work from plain text.
//   "create task: add retries to the api client"   → repositories named in the text are detected
//   "#12"                                           → jump to task 12
import { S, bus, navigate, statusOf, LIVE, agentLabel } from "./state.js";
import { api } from "./api.js";
import { toast, basename, copyText } from "./ui.js";
import { openNewTask, defaultWorkflow } from "./views/newtask.js";
import { keysFor, openShortcuts } from "./shortcuts.js";
import { reposOf } from "./live.js";

const CREATE_RE = /^\s*(?:(?:create|new|add)(?:\s+(?:a\s+)?task)?|task|\+)\s*[:>-]?\s+(.+)$/i;

// Every repository Relay has seen: task repositories, related repositories and the recent list, most recent first.
export function knownRepos() {
  const seen = new Map();
  const add = (path, at) => { if (!path) return; const cur = seen.get(path); if (!cur || at > cur) seen.set(path, at || ""); };
  for (const p of S.config?.recent_repos || []) add(p, "9");
  for (const t of S.tasks.values()) {
    add(t.repo, t.updated_at);
    for (const r of t.repos || []) add(r.repo, t.updated_at);
  }
  return [...seen.entries()].sort((a, b) => String(b[1]).localeCompare(String(a[1]))).map(([p]) => p);
}

const norm = (s) => String(s || "").toLowerCase().replace(/[_\s.]+/g, "-");
export function detectRepos(text) {
  const words = norm(text);
  const repos = knownRepos();
  const hits = repos.filter((p) => {
    const b = norm(basename(p));
    if (!b || b.length < 3) return false;
    if (words.includes(b)) return true;
    // "the api" matches acme-api when that is the only repository ending in -api.
    const tail = b.split("-").pop();
    return tail.length >= 3 && new RegExp(`(^|[^a-z0-9])${tail}([^a-z0-9]|$)`).test(words) && repos.filter((x) => norm(basename(x)).split("-").pop() === tail).length === 1;
  });
  return { hits, repos };
}

async function quickCreate(repo, text) {
  try {
    const t = await api.createTask({ repo, name: "", requirements: text, template: "feature", priority: "normal", tags: [], workflow: defaultWorkflow(), queue: true, depends_on: [] });
    toast("success", "Task queued", t.name, { action: { label: "Open", onClick: () => navigate(`#/task/${t.id}`) } });
  } catch (e) { toast("error", "Could not create the task", e.message); }
}

export function commandItems(q, { view } = {}) {
  const items = [];
  const create = q.match(CREATE_RE);
  if (create) {
    const text = create[1].trim();
    const { hits, repos } = detectRepos(text);
    const targets = hits.length ? hits : repos.slice(0, 3);
    targets.forEach((repo, i) => {
      items.push({ group: "Create", always: true, icon: "sparkles", label: `Create task in ${basename(repo)}${hits.length > 1 && i === 0 ? ` (+${hits.length - 1} related)` : ""}`, sub: `${hits.includes(repo) ? "named in your text" : "recent repository"} · opens the task dialog with the request filled in`,
        onClick: () => openNewTask({ repo, requirements: text, step: 2, related: hits.filter((h) => h !== repo) }) });
    });
    if (targets[0]) items.push({ group: "Create", always: true, icon: "play", label: `Queue it now in ${basename(targets[0])}`, sub: "default team and workflow, no dialog", onClick: () => quickCreate(targets[0], text) });
    if (!targets.length) items.push({ group: "Create", always: true, icon: "plus", label: "Create task…", sub: "choose a repository first", onClick: () => openNewTask({ requirements: text }) });
  }
  const num = q.trim().match(/^#?(\d{1,6})$/);
  if (num) {
    const t = [...S.tasks.values()].find((x) => String(x.number) === num[1]);
    if (t) items.push({ group: "Jump", always: true, icon: "arrowRight", label: `#${t.number} ${t.name}`, sub: statusOf(t).label, onClick: () => navigate(`#/task/${t.id}`) });
  }

  const cur = S.route.id ? S.tasks.get(S.route.id) : null;
  if (cur) {
    const g = "This task";
    const active = LIVE.has(cur.status) || cur.status === "needs_input" || cur.status === "paused";
    items.push({ group: g, icon: "activity", label: "Watch it live", keywords: "theatre stream agents", onClick: () => navigate(`#/task/${cur.id}/live`) });
    items.push({ group: g, icon: "branch", label: "Changes across repositories", keywords: "diff files commits try", onClick: () => navigate(`#/task/${cur.id}/changes`) });
    if (cur.status === "done") items.push({ group: g, icon: "checkCircle", label: "Open the review cockpit", keywords: "review merge approve", onClick: () => navigate(`#/review/${cur.id}`) });
    items.push({ group: g, icon: "share", label: "Copy shareable status link", keywords: "stakeholder share read-only", onClick: () => copyText(`${location.origin}${location.pathname}#/status/${cur.id}`) });
    if (active) items.push({ group: g, icon: "stop", label: "Stop this task", onClick: () => view?.command?.("stop") });
    if (active) items.push({ group: g, icon: "pause", label: cur.status === "paused" ? "Resume this task" : "Pause this task", onClick: () => view?.command?.(cur.status === "paused" ? "resume" : "pause") });
    if (["failed", "stopped", "interrupted"].includes(cur.status)) items.push({ group: g, icon: "retry", label: "Retry this task", onClick: () => view?.command?.("retry") });
    if (cur.pr_url) items.push({ group: g, icon: "external", label: "Open pull request", onClick: () => window.open(cur.pr_url, "_blank", "noopener") });
    items.push({ group: g, icon: "message", label: "Focus the guidance box", hint: keysFor("guidance"), onClick: () => view?.command?.("guidance") });
  }

  const needs = [...S.tasks.values()].filter((t) => !t.archived && t.pending);
  for (const t of needs.slice(0, 6)) items.push({ group: "Needs you", icon: "question", label: t.pending.question ? `Answer: ${String(t.pending.question).slice(0, 80)}` : `Approve: ${t.name}`, sub: t.name, onClick: () => navigate("#/home/needs") });

  const ap = S.autopilot || {};
  items.push(
    { group: "Actions", icon: "plus", label: "New task", hint: keysFor("new"), onClick: () => openNewTask() },
    { group: "Actions", icon: ap.paused ? "play" : "pause", label: ap.paused ? "Resume autopilot" : "Pause autopilot", keywords: "stop everything", onClick: () => bus.emit("autopilot:toggle") },
    { group: "Actions", icon: S.queue?.running ? "stop" : "play", label: S.queue?.running ? "Halt the queue" : "Run the queue", onClick: () => bus.emit(S.queue?.running ? "queue:halt" : "queue:run") },
    { group: "Actions", icon: "sun", label: "Toggle theme", keywords: "dark light", onClick: () => document.getElementById("themeBtn").click() },
    { group: "Actions", icon: "keyboard", label: "Keyboard shortcuts", hint: keysFor("help"), onClick: () => openShortcuts() },
  );
  const nav = [
    ["Mission Control", "#/", "radar", "home live dashboard overview"], ["Needs you", "#/home/needs", "inbox", "inbox questions approvals answer"],
    ["Last 24 hours", "#/home/24h", "sunrise", "digest morning report overnight"], ["Last 7 days", "#/home/7d", "sunrise", "digest week"],
    ["Work board", "#/work", "kanban", "tasks queue board kanban"], ["GitHub issues on the board", "#/work/issues", "issue", "issues tickets"],
    ["System map", "#/knowledge/system", "globe", "dependencies components"], ["Repositories", "#/knowledge/repositories", "folder", "clone branches git env environment"],
    ["Worktrees", "#/knowledge/worktrees", "layers", "clean up disk"], ["Branch graph", "#/knowledge/graph", "branch", ""],
    ["Connectors", "#/knowledge/connectors", "zap", "environments databases apis"], ["Lessons", "#/knowledge/lessons", "brain", "learning retrospective"],
    ["Watched GitHub repositories", "#/knowledge/github", "github", "github inbox sources label"],
    ["Agents", "#/agents", "bot", "codex claude gemini login models"], ["Settings", "#/settings", "settings", "preferences"],
    ["Autopilot settings", "#/settings/autopilot", "clock", "schedule window quiet hours limits fallback cost cap"],
    ["Notification settings", "#/settings/notifications", "bell", "desktop sound"], ["Usage and budget", "#/settings/budget", "gauge", "cost pricing"],
  ];
  for (const [label, hash, ic, kw] of nav) items.push({ group: "Go to", icon: ic, label, keywords: kw, onClick: () => navigate(hash) });

  const tasks = [...S.tasks.values()].filter((x) => !x.archived).sort((a, b) => (LIVE.has(b.status) - LIVE.has(a.status)) || (b.updated_at || "").localeCompare(a.updated_at || ""));
  for (const x of tasks.slice(0, 200)) {
    items.push({ group: "Tasks", icon: LIVE.has(x.status) ? "activity" : x.status === "done" ? "check" : "tasks", label: x.name, sub: `${x.number ? `#${x.number} · ` : ""}${reposOf(x).join(", ")} · ${statusOf(x).label}`, keywords: `${x.number ? `#${x.number}` : ""} ${(x.tags || []).join(" ")} ${agentLabel(x.workflow?.roles?.worker?.agent || "")}`, onClick: () => navigate(`#/task/${x.id}`) });
  }
  return items;
}
