// Delivery: the whole story of one change, in order — request → plan → work per repository →
// pull requests → merge → deployment per host → live — with what failed and what that blocks.
//
// It lives on the task page because that is where a person already looks. Everything is rendered
// from what the task already carries (plan, repo_worktrees, pull request status, the deploy record
// from orchestrator/deploy.py) plus the read-only deployment map at /api/deploy, which says what a
// merge *will* run for this repository before anything has run. Facts Relay does not have yet are
// written as "not known yet", never as a blank or a zero.
//
// The richer per-task structure that issue #60 adds drops in through `storyOf()`: it is the single
// place that turns a task into steps, so the views below do not change when the payload does.
import { $, $$, esc, icon, fmtDur, timeAgo, basename } from "../ui.js";
import { LIVE, statusOf } from "../state.js";
import { api } from "../api.js";
import { phases, packages } from "../live.js";
import { prCached, prStatus, PR_STATE, checksLabel } from "../prstatus.js";
import { waitingFor } from "../waiting.js";

const METHOD_WORDS = {
  actions_watch: "follows the repository's own GitHub Actions run",
  actions_dispatch: "triggers a GitHub Actions workflow and follows it",
  image_pull: "pulls the image on the host and recreates the service",
  compose_build: "builds the image on the host and recreates the service",
  manual: "is done by a person, by hand",
};
const T_TONE = { succeeded: "green", failed: "red", running: "amber", blocked: "amber", refused: "amber", manual: "outline", skipped: "outline" };
const T_WORD = {
  succeeded: "live", failed: "failed", running: "running", blocked: "needs a person",
  refused: "refused", manual: "by hand", skipped: "not run",
};

// A repository row of this task: the primary one, then every related worktree (orchestrator/multirepo.py).
export function repoRows(t) {
  const rt = t.repo_worktrees || {};
  const rows = [{
    name: basename(t.repo), primary: true, branch: t.branch, worktree: t.worktree, github_repo: t.github_repo,
    pr_url: t.pr_url, pr_number: t.pr_number, diffstat: t.diffstat, changed_count: t.changed_count, committed: t.committed,
  }];
  for (const [name, w] of Object.entries(rt)) {
    if (!w || !w.repo) continue;
    rows.push({ name: name || basename(w.repo), branch: w.branch, worktree: w.worktree, github_repo: w.github_repo,
      pr_url: w.pr_url, pr_number: w.pr_number, diffstat: w.diffstat, changed_count: w.changed_count, committed: w.committed, reason: w.reason });
  }
  return rows;
}

const unknown = (text) => `<span class="jn-unknown">${esc(text)}</span>`;
const diffText = (d, count) => {
  if (d && (d.files || d.insertions || d.deletions)) return `${d.files} file${d.files === 1 ? "" : "s"} · +${d.insertions} −${d.deletions}`;
  if (count) return `${count} file${count === 1 ? "" : "s"} changed`;
  return "";
};

// ---------------------------------------------------------------------------- the story
// One structure per task, so every view below reads the same shape. When issue #60 lands its
// per-task story endpoint, only this function changes.
export function storyOf(t, { map = null, mapError = "" } = {}) {
  const ph = phases(t);
  const pk = packages(t);
  const rows = repoRows(t);
  const pr = prCached(t.id);
  const prState = pr?.ok ? pr.state : null;
  const dp = t.deploy || null;
  const done = t.status === "done";
  const idx = ph.steps.findIndex((s) => s.state === "cur" || s.state === "wait" || s.state === "fail");
  const beyond = (key) => done || (idx >= 0 && ph.steps.findIndex((s) => s.key === key) < idx) || (idx < 0 && !!t.started_at);

  // --- request
  const steps = [{
    key: "request", label: "Request", icon: "message", state: "done",
    when: t.created_at,
    note: (t.requirements || "").trim().split("\n").filter(Boolean)[0] || (t.github_issue_url ? `Picked up from issue #${t.github_issue_number}.` : ""),
    facts: [
      t.github_issue_url ? { label: "Issue", html: `<a href="${esc(t.github_issue_url)}" target="_blank" rel="noopener">#${esc(t.github_issue_number || "")} ${icon("external", "sm")}</a>` } : null,
      { label: "Repositories", html: rows.map((r) => esc(r.name)).join(", ") },
    ].filter(Boolean),
  }];

  // --- plan
  const planStep = ph.steps.find((s) => s.key === "plan");
  steps.push({
    key: "plan", label: "Plan", icon: "list",
    state: t.plan?.plan || pk.list.length ? "done" : planStep ? planStep.state : "todo",
    note: t.plan?.summary || (t.plan?.plan ? "" : t.started_at ? "" : "Not planned yet: the supervisor writes the plan on its first turn."),
    facts: pk.list.length
      ? [{ label: "Work packages", html: `${pk.done} of ${pk.total} done` }]
      : pk.total
        ? [{ label: "Turns so far", html: `${pk.done} of ${pk.total}${pk.max ? ` (up to ${pk.max})` : ""}` }]
        : [{ label: "Work packages", html: unknown("not known yet") }],
    rows: pk.list.map((p) => ({
      title: `${p.id} · ${p.summary || ""}`, chip: p.repo || "", tone: (t.packages_done || []).includes(p.id) || done ? "green" : "outline",
      word: (t.packages_done || []).includes(p.id) || done ? "done" : "to do",
    })),
  });

  // --- work per repository
  steps.push({
    key: "work", label: "Work, repository by repository", icon: "edit",
    state: rows.some((r) => diffText(r.diffstat, r.changed_count)) ? (beyond("build") ? "done" : "cur") : t.started_at ? "cur" : "todo",
    note: rows.length > 1 ? "Each repository gets its own worktree, its own checks and its own pull request." : "",
    rows: rows.map((r) => ({
      title: r.name, chip: r.primary ? "primary" : r.reason || "related", tone: diffText(r.diffstat, r.changed_count) ? "green" : "outline",
      word: diffText(r.diffstat, r.changed_count) || (t.started_at ? "nothing changed yet" : "not started"),
      sub: r.branch ? `branch ${r.branch}` : t.started_at ? "" : "branch created when the task starts",
    })),
  });

  // --- pull requests
  const withPr = rows.filter((r) => r.pr_url || r.pr_number);
  steps.push({
    key: "pr", label: "Pull requests", icon: "github",
    state: withPr.length ? (prState === "merged" ? "done" : pr?.checks?.failing ? "fail" : "wait") : done ? "fail" : "todo",
    note: withPr.length ? "" : done ? "No pull request was opened for this task." : "Relay opens one per repository once the work is approved and pushed.",
    rows: withPr.map((r) => {
      const live = r.primary ? pr : null;
      const st = r.primary && live?.ok ? PR_STATE[live.state] || PR_STATE.open : null;
      return {
        title: `${r.name}${r.pr_number ? ` · #${r.pr_number}` : ""}`,
        href: r.pr_url, tone: st ? (st.tone === "muted" ? "outline" : st.tone) : "outline",
        word: st ? st.label : r.primary && live && !live.ok ? "state not known" : "state not read",
        sub: r.primary
          ? live?.ok ? `${checksLabel(live.checks)}${live.merged_at ? ` · merged ${timeAgo(live.merged_at)}` : ""}`
            : live ? `Relay could not read it from GitHub: ${live.error || "no reason given"}` : "asking GitHub…"
          : "Relay follows the primary pull request live; open this one on GitHub for its state.",
      };
    }),
  });

  // --- merge
  steps.push({
    key: "merge", label: "Merge", icon: "merge",
    state: prState === "merged" ? "done" : prState === "closed" ? "fail" : withPr.length ? "wait" : "todo",
    note: prState === "merged" ? `Merged ${timeAgo(pr.merged_at)}. Relay then runs the deployments this repository has switched on.`
      : prState === "closed" ? "The pull request was closed without merging, so nothing from this task reaches production."
      : withPr.length ? "Nothing is deployed until the pull request is merged. Merging it on GitHub starts the deployment on its own."
      : "",
  });

  // --- deployments per host
  const targets = dp?.targets || [];
  const planned = map ? (map.targets || []).filter((x) => sameRepo(x.repo, t.github_repo)) : [];
  steps.push({
    key: "deploy", label: "Deployment, host by host", icon: "package",
    state: dp ? ({ succeeded: "done", failed: "fail", running: "cur", blocked: "wait", manual: "wait", skipped: "todo" }[dp.status] || "todo")
      : prState === "merged" ? "wait" : "todo",
    when: dp?.finished_at || dp?.started_at,
    note: dp ? `Ran ${dp.trigger === "pr_merged" ? "after the merge" : dp.trigger === "manual" ? "because someone pressed Run now" : `after ${dp.trigger}`}.`
      : mapError ? `The deployment map could not be read: ${mapError}`
      : !map ? "Reading what a merge deploys for this repository…"
      : planned.length ? `${planned.filter((x) => x.enabled && x.auto_on_merge && !x.critical).length} of ${planned.length} target${planned.length === 1 ? "" : "s"} for this repository run by themselves on merge; the rest wait for Deploy → Run now.`
      : "No deployment target is configured for this repository, so merging the pull request is the last step.",
    rows: (targets.length ? targets : planned).map((x) => {
      const ran = targets.length;
      const word = ran ? T_WORD[x.status] || x.status : x.critical ? "only from Run now" : x.enabled && x.auto_on_merge ? "runs on merge" : x.enabled ? "only from Run now" : "switched off";
      return {
        title: `${x.service || x.site || x.workflow || x.target || x.id}`, chip: x.host || "",
        tone: ran ? T_TONE[x.status] || "outline" : x.enabled && x.auto_on_merge && !x.critical ? "green" : "outline",
        word,
        sub: ran ? `${METHOD_WORDS[x.method] || x.method}${x.duration ? ` · ${fmtDur(x.duration)}` : ""}${x.detail ? ` — ${x.detail}` : ""}`
          : `${METHOD_WORDS[x.method] || x.method}${x.critical ? " — critical: never automatic, a person presses Run now" : ""}${x.never_pull ? " — the image is not on the registry, so a pull is refused" : ""}`,
      };
    }),
  });

  // --- live
  const liveTargets = targets.filter((x) => x.status === "succeeded");
  const stuck = targets.filter((x) => ["failed", "blocked", "refused", "manual"].includes(x.status));
  steps.push({
    key: "live", label: "Live", icon: "globe",
    state: liveTargets.length && !stuck.length ? "done" : stuck.length ? "fail" : "todo",
    note: liveTargets.length
      ? `Running on ${[...new Set(liveTargets.map((x) => x.host))].join(" and ")}${dp?.finished_at ? ` since ${timeAgo(dp.finished_at)}` : ""}.`
        + (stuck.length ? ` ${stuck.length} target${stuck.length === 1 ? " is" : "s are"} not, so that part of the change is not live.` : "")
      : stuck.length ? "The merge is in but nothing came up, so production still runs the previous version."
      : prState === "merged" ? "Merged, but nothing has been deployed from this task yet."
      : "Nothing of this task is live yet.",
  });

  return { steps, blocked: stuck, waiting: waitingFor(t) };
}

const sameRepo = (a, b) => {
  const norm = (x) => String(x || "").toLowerCase().replace(/\.git$/, "").split("/").filter(Boolean).slice(-1)[0] || "";
  return !!norm(a) && norm(a) === norm(b);
};

// ---------------------------------------------------------------------------- rendering
export function bannerHtml(w, { compact = false } = {}) {
  if (!w) return "";
  return `<div class="jn-banner" data-tone="${esc(w.tone || "none")}" role="status">
    <span class="jn-banner-ic">${icon(w.icon || "clock", w.icon === "spinner" ? "spin" : "")}</span>
    <span class="jn-banner-copy"><strong>${esc(w.title)}</strong><span>${esc(w.next)}</span></span>
    ${!compact && w.go ? `<button type="button" class="btn sm jn-banner-go" data-go="${esc(w.go)}">${icon("arrowRight", "sm")}Take me there</button>` : ""}
  </div>`;
}

function stepHtml(s) {
  const rows = (s.rows || []).map((r) => `<div class="jn-row">
      <span class="jn-row-main">${r.href ? `<a href="${esc(r.href)}" target="_blank" rel="noopener">${esc(r.title)} ${icon("external", "sm")}</a>` : `<strong>${esc(r.title)}</strong>`}${r.chip ? `<span class="badge outline">${esc(r.chip)}</span>` : ""}</span>
      <span class="badge ${esc(r.tone || "outline")} jn-row-word">${esc(r.word)}</span>
      ${r.sub ? `<span class="jn-row-sub">${esc(r.sub)}</span>` : ""}
    </div>`).join("");
  const facts = (s.facts || []).map((f) => `<span class="jn-fact"><span>${esc(f.label)}</span><b>${f.html}</b></span>`).join("");
  return `<li class="jn-step st-${esc(s.state)}">
    <span class="jn-dot" aria-hidden="true">${icon(s.state === "done" ? "check" : s.state === "fail" ? "x" : s.icon, "sm")}</span>
    <div class="jn-body">
      <div class="jn-head"><h3>${esc(s.label)}</h3>${s.when ? `<span class="jn-when">${esc(timeAgo(s.when))}</span>` : ""}<span class="sr-only">${esc(s.state === "cur" ? "in progress" : s.state === "wait" ? "waiting" : s.state)}</span></div>
      ${s.note ? `<p class="jn-note">${esc(s.note)}</p>` : ""}
      ${facts ? `<div class="jn-facts">${facts}</div>` : ""}
      ${rows ? `<div class="jn-rows">${rows}</div>` : ""}
    </div>
  </li>`;
}

// `banner: false` when the page already carries the waiting line above this panel, so it is not said twice.
export function mountJourney(host, getTask, { onGo, banner = true } = {}) {
  const state = { map: null, mapError: "", loading: false };

  function render() {
    const t = getTask();
    if (!t) return;
    const story = storyOf(t, { map: state.map, mapError: state.mapError });
    host.innerHTML = `<div class="insp-body jn">
      ${banner ? bannerHtml(story.waiting) : ""}
      ${!story.waiting && LIVE.has(t.status) ? `<div class="jn-banner" data-tone="none" role="status"><span class="jn-banner-ic">${icon("spinner", "spin")}</span><span class="jn-banner-copy"><strong>${esc(statusOf(t).label)} — nothing is waiting on you</strong><span>The team is working. This page fills in as each step finishes.</span></span></div>` : ""}
      <ol class="jn-steps">${story.steps.map(stepHtml).join("")}</ol>
      <p class="hint jn-foot">Each step says what it is waiting for and who moves it on. What a merge deploys per repository is set under Settings → Deploy; nothing runs there unless it is switched on.</p>
    </div>`;
    $$("[data-go]", host).forEach((b) => (b.onclick = () => onGo?.(b.dataset.go)));
  }

  function loadMap() {
    if (state.loading || state.map) return;
    state.loading = true;
    api.deployTargets()
      .then((d) => { state.map = d || { targets: [] }; })
      .catch((e) => { state.mapError = e.message; })
      .finally(() => { state.loading = false; render(); });
  }

  render();
  loadMap();
  const t = getTask();
  if (t && (t.pr_url || t.pr_number)) prStatus(t.id).then(render);
  return {
    update() { render(); },
    destroy() {},
  };
}
