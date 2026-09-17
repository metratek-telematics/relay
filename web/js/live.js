// What every running task is doing right now, shared by Mission Control, the Work board, the live theatre and the
// status page. Seeded from /api/mission and kept current from the event stream, so nothing polls per task.
import { S, LIVE, statusOf, roleAgent, agentLabel, ROLE_LABEL } from "./state.js";
import { describeEdit, shellEditTargets } from "./views/conversation.js";
import { esc, icon, fmtSec, basename } from "./ui.js";

export const activity = new Map(); // taskId -> { activity, said }

const ACT = new Set(["tool", "command", "connector"]);
const SAID = new Set(["text", "handoff", "plan", "decision", "review"]);

export function seedActivity(rows) {
  for (const r of rows || []) activity.set(r.task_id, { activity: r.activity || null, said: r.said || null });
}

export function noteMessage(m) {
  if (!m || !m.task_id) return false;
  const cur = activity.get(m.task_id) || { activity: null, said: null };
  let changed = false;
  if (ACT.has(m.kind)) {
    if (!cur.activity || cur.activity.id === m.id || (m.ts || 0) >= (cur.activity.ts || 0)) { cur.activity = { ...(cur.activity?.id === m.id ? cur.activity : {}), ...m }; changed = true; }
  } else if (SAID.has(m.kind) && (m.content || m.summary) && !m.streaming) {
    cur.said = { kind: m.kind, agent: m.agent, role: m.role, ts: m.ts, text: String(m.summary || m.content).replace(/\s+/g, " ").slice(0, 240) };
    changed = true;
  }
  activity.set(m.task_id, cur);
  return changed;
}

const TOOL_ICON = { shell: "terminal", read: "eye", edit: "edit", search: "search", web: "globe", agent: "bot", plan: "list", mcp: "zap", tool: "cpu" };

// A short, human sentence for a tool call: "Editing app.js +12 −3", "Running python -m pytest".
export function describeActivity(t, a) {
  if (!a) return null;
  const running = a.status === "running" || !a.status;
  if (a.kind === "connector") return { icon: "zap", verb: running ? "Calling" : "Called", detail: `${a.connector || "connector"} · ${a.operation || ""}`, running };
  if (a.kind === "command") return { icon: "terminal", verb: running ? "Relay is running" : "Relay ran", detail: a.content || a.title || a.summary || "", running };
  const ed = describeEdit(a, t);
  if (ed && ed.files.length) return { icon: "edit", verb: running ? "Editing" : "Edited", detail: ed.files.map((f) => basename(f)).join(", "), add: ed.add, del: ed.del, file: ed.files[0], running, edit: ed };
  if (a.category === "shell") {
    const targets = shellEditTargets(a.input || a.summary, t);
    if (targets.length) return { icon: "edit", verb: running ? "Editing" : "Edited", detail: targets.map(basename).join(", "), running };
    return { icon: "terminal", verb: running ? "Running" : "Ran", detail: String(a.summary || a.input || "").replace(/^\$\s*/, ""), running };
  }
  const verb = { read: running ? "Reading" : "Read", search: running ? "Searching" : "Searched", web: running ? "Browsing" : "Browsed", plan: "Planning", agent: running ? "Delegating" : "Delegated" }[a.category] || (running ? "Using" : "Used");
  return { icon: TOOL_ICON[a.category] || "cpu", verb, detail: `${a.category ? "" : `${a.tool || "a tool"} `}${a.summary || a.tool || ""}`.trim(), running };
}

export function activityHtml(t, { compact = false } = {}) {
  const live = activity.get(t.id) || {};
  const p = t.process || {};
  const d = describeActivity(t, live.activity);
  const who = p.agent ? agentLabel(p.agent) : p.role ? ROLE_LABEL[p.role] : "Relay";
  if (d && (d.running || !p.state || p.state !== "running" || (Date.now() / 1000 - (live.activity?.ts || 0)) < 90)) {
    const since = live.activity?.ts ? fmtSec(Date.now() / 1000 - live.activity.ts) : "";
    return `<span class="act ${d.running ? "is-running" : ""}">${icon(d.icon, "sm")}<span class="act-text"><b>${esc(d.verb)}</b> <span class="act-detail">${esc(d.detail)}</span></span>${d.add || d.del ? `<span class="diffstat"><span class="a">+${d.add}</span><span class="d">−${d.del}</span></span>` : ""}${!compact && since && d.running ? `<span class="act-since">${since}</span>` : ""}</span>`;
  }
  if (p.state === "running") {
    const quiet = Number(p.silent_for || 0) > 15;
    return `<span class="act">${icon("brain", "sm")}<span class="act-text"><b>${esc(who)}</b> <span class="act-detail plain">${quiet ? `is thinking · quiet for ${fmtSec(p.silent_for)}` : "is thinking"}</span></span></span>`;
  }
  return `<span class="act muted">${icon("clock", "sm")}<span class="act-text">${esc(t.detail || statusOf(t).label)}</span></span>`;
}

// ---------------------------------------------------------------------------- phases
// One outline of a task's life used everywhere: Plan → Design → Build → Verify → Review → Deliver.
const STEP_OF_STATUS = { running: "plan", preparing: "plan", planning: "plan", implementing: "build", verifying: "verify", reviewing: "review", delivering: "deliver" };
const STEP_OF_PHASE = { kickoff: "plan", design: "design", dialogue: "build", review: "review", deliver: "deliver", done: "done" };

export function phases(t) {
  const hasDesign = !!(t.design && (t.design.version || t.design.status)) || t.checkpoint?.phase === "design";
  const hasReview = !!roleAgent(t, "reviewer");
  const keys = ["plan", ...(hasDesign ? ["design"] : []), "build", "verify", ...(hasReview ? ["review"] : []), "deliver"];
  const LABEL = { plan: "Plan", design: "Design", build: "Build", verify: "Verify", review: "Review", deliver: "Deliver" };
  const failed = ["failed", "stopped", "interrupted"].includes(t.status);
  let cur;
  if (t.status === "done") cur = "done";
  else if (t.checkpoint?.phase === "design" && LIVE.has(t.status)) cur = "design";
  else if (STEP_OF_STATUS[t.status]) cur = STEP_OF_STATUS[t.status];
  else cur = STEP_OF_PHASE[t.checkpoint?.phase] || (t.started_at ? "plan" : null);
  if (cur && !keys.includes(cur) && cur !== "done") cur = cur === "review" ? "verify" : "build";
  const idx = cur === "done" ? keys.length : cur ? keys.indexOf(cur) : -1;
  const pk = packages(t);
  return {
    current: cur,
    steps: keys.map((k, i) => ({
      key: k, label: LABEL[k],
      state: i < idx ? "done" : i === idx ? (failed ? "fail" : t.status === "needs_input" || t.status === "paused" ? "wait" : "cur") : "todo",
      note: k === "build" && pk.total ? `${pk.done}/${pk.total}` : k === "review" && t.checkpoint?.review_round ? `round ${t.checkpoint.review_round}` : "",
    })),
    packages: pk,
  };
}

export function packages(t) {
  const list = t.plan?.work_packages || t.design?.work_packages || [];
  if (list.length) {
    const done = new Set(t.packages_done || []);
    return { total: list.length, done: t.status === "done" ? list.length : list.filter((p) => done.has(p.id)).length, list };
  }
  const turn = t.checkpoint?.turn || 0;
  return { total: turn ? Math.max(turn, 1) : 0, done: t.status === "done" ? turn : Math.max(0, turn - 1), list: [], turns: true, max: t.workflow?.max_turns };
}

export function stepperHtml(t, { size = "" } = {}) {
  const ph = phases(t);
  return `<ol class="stepper ${size}" aria-label="Progress">${ph.steps.map((s) => `<li class="st-${s.state}" title="${esc(s.label)}${s.note ? ` · ${esc(s.note)}` : ""}"><span class="st-dot" aria-hidden="true">${s.state === "done" ? icon("check") : s.state === "fail" ? icon("x") : ""}</span><span class="st-label">${esc(s.label)}${s.note ? ` <small>${esc(s.note)}</small>` : ""}</span><span class="sr-only">${esc(s.state === "cur" ? "in progress" : s.state)}</span></li>`).join("")}</ol>`;
}

export function teamHtml(t, { size = "sm" } = {}) {
  const p = t.process || {};
  const roles = ["supervisor", "worker", "reviewer"].filter((r) => roleAgent(t, r));
  return `<span class="team">${roles.map((r) => {
    const a = roleAgent(t, r);
    const on = p.state === "running" && p.role === r;
    return `<span class="team-member ${on ? "on" : ""}" title="${esc(ROLE_LABEL[r])} · ${esc(agentLabel(a))}${on ? " · working now" : ""}"><span class="av ${size} ${esc(a)}">${esc(agentLabel(a).slice(0, 2))}</span></span>`;
  }).join("")}</span>`;
}

export function reposOf(t) {
  const names = [basename(t.repo)];
  for (const r of t.repos || []) if (r.role !== "primary" && r.repo) names.push(basename(r.repo));
  return [...new Set(names.filter(Boolean))];
}

export function repoChips(t, max = 3) {
  const names = reposOf(t);
  return `<span class="repo-chips">${names.slice(0, max).map((n) => `<span class="repo-chip">${icon("folder", "sm")}${esc(n)}</span>`).join("")}${names.length > max ? `<span class="repo-chip more">+${names.length - max}</span>` : ""}</span>`;
}
