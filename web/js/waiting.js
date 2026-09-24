// What a task is waiting for, in the person's words, and what happens next.
//
// One answer for the whole interface: the task page banner, the Work board cards and the delivery
// story all ask this module instead of printing a status word. Every answer carries `next` — the
// sentence that says who or what moves it on — so no state is a dead end.
//
// Everything here is derived from what the server already sends: the task's `pending`, `status`,
// `waiting` record (orchestrator/autopilot.py writes kind/text/until/reason), its deploy record and
// the queue and autopilot snapshots in S. Nothing is invented; when a fact is not known the copy
// says so rather than showing a blank.
import { S, agentLabel, LIVE } from "./state.js";
import { phases } from "./live.js";
import { prCached } from "./prstatus.js";

const HHMM = (ts) => {
  if (!ts) return "";
  const d = new Date(Number(ts) * 1000);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};
const clause = (ts) => (HHMM(ts) ? ` It starts on its own at ${HHMM(ts)}.` : "");

// The queue sentence: how many slots are busy, when that is known.
function queueNext() {
  const ap = S.autopilot || {};
  if (ap.state === "halted" || ap.queue_running === false) return "The queue is halted: nothing starts until you press Run on Autopilot.";
  if (ap.paused) return "Autopilot is paused. Resume it and this task starts.";
  const q = S.queue || {};
  const max = q.max_parallel ?? ap.max_parallel;
  const busy = q.active ?? ap.running;
  if (max === undefined || busy === undefined) return "It starts when the queue reaches it. How many runs are in flight is not known yet.";
  return `${busy} of ${max} run${max === 1 ? "" : "s"} in flight; it starts when a slot frees up.`;
}

const PENDING = {
  question: (t) => ({
    icon: "question", tone: "amber", title: "Waiting for your answer",
    next: `${agentLabel(t.pending.agent)} asked${t.pending.question ? `: “${String(t.pending.question).replace(/\s+/g, " ").trim()}”` : " a question in the conversation"}. Nothing else runs on this task until you reply.`,
    go: "conversation",
  }),
  approval: () => ({
    icon: "shield", tone: "amber", title: "Waiting for your approval to deliver",
    next: "Approve and Relay commits, pushes and opens the pull request. Request changes and the team goes back to work.",
    go: "conversation",
  }),
  design_approval: () => ({
    icon: "shield", tone: "amber", title: "Waiting for your approval of the system design",
    next: "Approve and the team builds against it. Request changes and it revises the design first.",
    go: "design",
  }),
  design_pick: () => ({
    icon: "image", tone: "amber", title: "Waiting for you to pick a direction",
    next: "Open Mockups and choose one of the explored directions; the team then builds that one.",
    go: "mockups",
  }),
};

// The gate states autopilot can park a queued task in (orchestrator/autopilot.py gate()).
const GATE_NEXT = {
  cost_cap: "Today's spend has reached the daily cap. It starts after the cap resets, or raise it under Settings → Autopilot.",
  outside_window: "This is outside the hours you set for autopilot.",
  quiet_hours: "Quiet hours are on, so nothing starts now.",
  paused: "Autopilot is paused. Resume it and this task starts.",
  halted: "The queue is halted: nothing starts until you press Run on Autopilot.",
};

export function waitingFor(t) {
  if (!t) return null;

  // 1. Waiting for the owner.
  if (t.pending && PENDING[t.pending.kind]) return { kind: `pending_${t.pending.kind}`, ...PENDING[t.pending.kind](t) };

  // 2. Paused, by the owner or on request.
  if (t.pause_requested && LIVE.has(t.status)) return {
    kind: "pausing", icon: "pause", tone: "amber", title: "Pausing after the current agent turn",
    next: "The agent finishes what it is doing and then stops. Resume picks it up from there.",
  };
  if (t.status === "paused") return {
    kind: "paused", icon: "pause", tone: "amber", title: "Paused",
    next: "Nothing runs until you press Resume. The worktree and the agent sessions are kept as they are.",
  };

  // 3. A deployment of its own.
  const dp = t.deploy;
  if (dp?.status === "running") {
    const hosts = [...new Set((dp.targets || []).map((x) => x.host).filter(Boolean))];
    return { kind: "deploying", icon: "spinner", tone: "amber", title: hosts.length ? `Deploying to ${hosts.join(" and ")}` : "Deploying",
      next: "Relay is running this repository's deployment recipes. One deployment runs at a time across Relay, so anything else queued behind it waits.", go: "delivery" };
  }
  if (dp && (dp.status === "blocked" || dp.status === "manual")) {
    const stuck = (dp.targets || []).filter((x) => ["blocked", "refused", "manual"].includes(x.status));
    return { kind: "deploy_manual", icon: "alert", tone: "amber",
      title: dp.status === "manual" ? "This deployment is done by a person" : "A deployment step needs a person",
      next: (stuck[0]?.detail || "Relay ran what it could and stopped at a step it may not do by itself.") + " Open Delivery for the steps, then Deploy → Run now when it is done.",
      go: "delivery" };
  }
  if (dp?.status === "failed") {
    const bad = (dp.targets || []).find((x) => x.status === "failed");
    return { kind: "deploy_failed", icon: "alert", tone: "red", title: "A deployment failed",
      next: `${bad ? `${bad.service || bad.target} on ${bad.host} did not come up. ` : ""}The merge is in, so the repository is ahead of what is running. Read the output under Delivery, fix it and run the target again.`,
      go: "delivery" };
  }

  // 4. Queued: autopilot records why it is not starting.
  if (t.status === "queued") {
    const w = t.waiting || null;
    const kind = w?.kind || "";
    if (kind === "limits") return {
      kind: "limits", icon: "gauge", tone: "amber", until: w.until, title: "Waiting for an agent's usage limit to reset",
      next: `${w.reason || w.text || "An agent on this team is over its limit"}.${w.until ? clause(w.until)
        : " Nothing clears this on its own: change the team, give the role a fallback agent under Settings → Autopilot, or sign the agent in."}`,
    };
    if (kind === "dependency" || kind === "chain") return {
      kind, icon: "layers", tone: "", title: kind === "chain" ? "Waiting for the task ahead of it on this branch" : "Waiting for another task to finish",
      next: `${w.text || "Another task has to finish first"}. It starts on its own as soon as that one is done.`,
    };
    if (kind && GATE_NEXT[kind]) return {
      kind, icon: "clock", tone: "", until: w.until, title: w.text || "Queued", next: GATE_NEXT[kind] + clause(w.until),
    };
    if (w?.text) return { kind: "queued", icon: "clock", tone: "", until: w.until, title: w.text, next: queueNext() };
    return { kind: "queued", icon: "clock", tone: "", title: "Queued", next: queueNext() };
  }

  // 5. Delivered, but the change is not in yet.
  if (t.status === "done") {
    const pr = prCached(t.id);
    const state = pr?.ok ? pr.state : null;
    if (state === "merged" || (!t.pr_url && !t.pr_number)) return null;
    if (state === "closed") return {
      kind: "pr_closed", icon: "x", tone: "red", title: "The pull request was closed without merging",
      next: "Nothing from this task is live. Reopen it on GitHub, or start a follow-up task from this branch.", go: "delivery" };
    const checks = pr?.checks;
    if (checks?.failing) return {
      kind: "checks_failing", icon: "alert", tone: "red", title: `Waiting on ${checks.failing} failing check${checks.failing === 1 ? "" : "s"}`,
      next: "GitHub will not let the pull request merge while a required check fails. Fix it with a follow-up task, then merge.", go: "delivery" };
    if (pr && !pr.ok) return {
      kind: "pr_unknown", icon: "alert", tone: "amber", title: "The pull request state is not known",
      next: `Relay could not read it from GitHub: ${pr.error || "no reason given"}. Open it on GitHub to see where it stands.`, go: "delivery" };
    return {
      kind: "pr_open", icon: "github", tone: "", title: "Waiting for the pull request to be merged",
      next: "Merge it on GitHub and Relay runs the deployment recipes this repository has switched on. Until then nothing of this task is live.",
      go: "delivery" };
  }

  // 6. It stopped and will not move by itself.
  if (["failed", "stopped", "interrupted"].includes(t.status)) {
    const step = phases(t).steps.find((s) => s.state === "fail");
    const where = step ? ` at ${step.label.toLowerCase()}` : "";
    const label = { failed: "It failed", stopped: "You stopped it", interrupted: "It was interrupted" }[t.status];
    return { kind: t.status, icon: "alert", tone: t.status === "failed" ? "red" : "amber", title: `${label}${where}`,
      next: `${t.error ? `${t.error.split("\n")[0].replace(/[.!?]?$/, ".")} ` : ""}Nothing else runs on its own. ${t.checkpoint ? "Resume continues from the last checkpoint" : "Retry starts it again"}, or retry from scratch in a new worktree.` };
  }

  // 7. Running: not waiting for anything.
  return null;
}
