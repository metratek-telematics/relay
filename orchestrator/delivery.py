"""The delivery pipeline (#60): from a merge, wherever it happens, to running in production.

Issue #53 gave every repository a deploy recipe and a merge hook on the task that opened the pull
request. Three things were still missing, and this module is all three plus the one structure that
tells the story.

1. **A merge is noticed wherever it happens.** `sweep_merges()` polls `gh pr list --state merged`
   for the repositories the deployment map covers and deploys the ones nobody was watching: a
   merge made from a phone, from another machine, or by a person who never heard of Relay.
   Merges older than the watcher's first sweep are ignored (Relay does not redeploy history), and
   every merge is recorded by `owner/repo#number` before anything runs, so a second sweep never
   acts on it twice. What triggered each deployment is written into the record.

2. **Deployments are ordered by the system map.** `plan()` turns a set of targets into an ordered
   plan: an approved edge "navitrak-vue depends on ais-decoder-rust" means the decoder deploys
   first, and the plan says so in a sentence a person can check. Where the map has no approved
   opinion, the deployment map's own file order is kept, and the plan says that too.

3. **A failure stops what depends on it.** `run_plan()` runs the plan in order; when a target
   fails, every target that depends on it (transitively) is recorded as "not attempted because X
   failed" and nothing is run for it. The result says exactly what is deployed, what is not, and
   which step to resume from.

`story()` is the single structure per task: the plan, the repositories, the pull requests, the
merges, every deployment with host, method, outcome, duration and log path, and what is blocked and
why. Nothing in it has to be re-derived in a browser.

Every rule from #53 still holds, because every deployment still goes through `deploy.run_target`:
only enabled targets run automatically, critical targets only from the button, `never_pull` is
refused, commands come from the deployment map alone, and one deployment runs at a time
instance-wide (`deploy.LOCK`).
"""
from __future__ import annotations

import json
import threading

from . import deploy, systemmap
from .util import DATA_DIR, now, read_json, truncate, write_json

MERGES_FILE = DATA_DIR / "merges.json"
MAX_SEEN = 500          # merges remembered per file; older keys are dropped oldest-first
MERGE_LIMIT = 20        # merged pull requests asked of `gh` per repository per sweep

# A target in one of these states did not put new code where it belongs, so anything that depends
# on it is not attempted. `skipped` (disabled / not automatic) and `manual` do not block: they are
# deliberate choices, not failures.
BLOCKING = ("failed", "refused", "blocked")
NOT_ATTEMPTED = "not_attempted"

_lock = threading.RLock()


# ============================================================================ merge ledger
def _blank_ledger() -> dict:
    return {"version": 1, "started_at": None, "last_poll": None, "error": None, "seen": {}}


def load_ledger() -> dict:
    d = read_json(MERGES_FILE, None)
    if not isinstance(d, dict) or not isinstance(d.get("seen"), dict):
        return _blank_ledger()
    base = _blank_ledger()
    base.update({k: v for k, v in d.items() if k in base})
    return base


def save_ledger(doc: dict) -> dict:
    seen = doc.get("seen") or {}
    if len(seen) > MAX_SEEN:
        keep = sorted(seen.items(), key=lambda kv: str((kv[1] or {}).get("noticed_at") or ""), reverse=True)[:MAX_SEEN]
        doc["seen"] = dict(keep)
    with _lock:
        write_json(MERGES_FILE, doc)
    return doc


def begin_watch(at=None) -> dict:
    """Mark where history stops. Merges older than this are never deployed by the watcher."""
    doc = load_ledger()
    if not doc.get("started_at"):
        doc["started_at"] = at or now()
        save_ledger(doc)
    return doc


def seen_key(repo: str, number) -> str:
    return f"{deploy.full_repo(repo)}#{number}"


def already_seen(repo: str, number) -> bool:
    return seen_key(repo, number) in (load_ledger().get("seen") or {})


def remember(merge: dict, **extra) -> dict:
    """Record a merge before anything runs, so a crash mid-deploy cannot cause a second attempt."""
    key = seen_key(merge.get("repo"), merge.get("number"))
    with _lock:
        doc = load_ledger()
        row = dict(doc["seen"].get(key) or {})
        row.update({k: v for k, v in merge.items() if k != "seen"})
        row.setdefault("noticed_at", now())
        row.update(extra)
        row["key"] = key
        doc["seen"][key] = row
        save_ledger(doc)
    return row


def merge_records(limit: int = 50) -> list[dict]:
    rows = list((load_ledger().get("seen") or {}).values())
    rows.sort(key=lambda r: str(r.get("noticed_at") or ""), reverse=True)
    return rows[:limit]


# ============================================================================ polling GitHub
def watched_repos(automatic_only: bool = True) -> list[str]:
    """The repositories worth polling: those the deployment map can actually deploy.

    A repository with no enabled, automatic target is not polled at all: nothing would happen and
    an installation that enabled nothing must make no GitHub calls.
    """
    out = []
    for t in deploy.list_targets():
        if automatic_only and not deploy.automatic_allowed(t):
            continue
        if not automatic_only and not t.get("enabled"):
            continue
        full = deploy.full_repo(t.get("repo"))
        if full and full not in out:
            out.append(full)
    return out


def merged_pulls(repo: str, limit: int = MERGE_LIMIT, runner=None) -> list[dict]:
    """Recently merged pull requests of one repository, newest first.

    Read-only: `gh pr list`. A GitHub failure returns nothing rather than raising, so one
    unreachable repository never stops the sweep.
    """
    args = ["pr", "list", "-R", deploy.full_repo(repo), "--state", "merged", "--limit", str(int(limit)),
            "--json", "number,title,url,mergedAt,mergeCommit,headRefName,author,baseRefName"]
    code, out = (runner or deploy._gh)(args, 120)
    if code != 0:
        return []
    try:
        rows = json.loads(out or "[]")
    except Exception:
        return []
    merges = []
    for r in rows if isinstance(rows, list) else []:
        merges.append({"repo": deploy.full_repo(repo), "number": r.get("number"), "title": r.get("title") or "",
                       "url": r.get("url") or "", "merged_at": r.get("mergedAt") or "",
                       "commit": ((r.get("mergeCommit") or {}) or {}).get("oid") or "",
                       "branch": r.get("headRefName") or "", "base": r.get("baseRefName") or "",
                       "author": ((r.get("author") or {}) or {}).get("login") or ""})
    merges.sort(key=lambda m: str(m.get("merged_at") or ""), reverse=True)
    return merges


def new_merges(runner=None) -> list[dict]:
    """Merges that happened after the watcher started and have never been acted on."""
    doc = begin_watch()
    start = str(doc.get("started_at") or "")
    seen = doc.get("seen") or {}
    fresh = []
    for repo in watched_repos():
        for m in merged_pulls(repo, runner=runner):
            if not m.get("number"):
                continue
            merged_at = str(m.get("merged_at") or "")
            if merged_at and start and merged_at < start:
                continue  # history: Relay was not running when this merged
            if seen_key(m["repo"], m["number"]) in seen:
                continue
            fresh.append(m)
    fresh.sort(key=lambda m: str(m.get("merged_at") or ""))
    with _lock:
        doc = load_ledger()
        doc["last_poll"] = now()
        save_ledger(doc)
    return fresh


# ============================================================================ ordering
def _component_for(data: dict, repo: str):
    return systemmap.find(data, deploy.full_repo(repo)) or systemmap.find(data, deploy.repo_name(repo))


def dependencies(targets: list[dict], data: dict | None = None) -> dict:
    """{target id: [(target id it must follow, reason sentence)]}, from approved system-map edges.

    An approved edge `from -> to` says `from` depends on `to`. In deployment terms the provider
    goes first, so every target of `to` must run before every target of `from`.
    """
    data = data or systemmap.load()
    comp_of, out = {}, {}
    for t in targets:
        c = _component_for(data, t.get("repo"))
        comp_of[t["id"]] = c["id"] if c else None
        out[t["id"]] = []
    names = {c["id"]: (c.get("name") or c["id"]) for c in data.get("components") or []}
    for e in data.get("edges") or []:
        if e.get("status") != "approved":
            continue
        caller, provider = e.get("from"), e.get("to")
        if caller == provider:
            continue
        for later in targets:
            if comp_of[later["id"]] != caller:
                continue
            for earlier in targets:
                if comp_of[earlier["id"]] != provider or earlier["id"] == later["id"]:
                    continue
                detail = str(e.get("details") or "").strip()
                reason = (f"{names.get(provider, provider)} before {names.get(caller, caller)}: "
                          f"{names.get(caller, caller)} depends on {names.get(provider, provider)} via {e.get('via')}"
                          + (f" ({truncate(detail, 120)})" if detail else ""))
                if all(p[0] != earlier["id"] for p in out[later["id"]]):
                    out[later["id"]].append((earlier["id"], reason))
    return out


def plan(targets: list[dict], data: dict | None = None) -> dict:
    """Order a set of targets so a service deploys before the thing that calls it.

    A stable topological sort: among targets whose prerequisites are all placed, the one that comes
    first in the deployment map wins, so two runs of the same set always produce the same order and
    the order can be explained without reading this code.
    """
    targets = [t for t in targets if isinstance(t, dict) and t.get("id")]
    index = {t["id"]: i for i, t in enumerate(targets)}
    deps = dependencies(targets, data)
    placed, order, cycles = set(), [], []
    remaining = list(targets)
    while remaining:
        ready = [t for t in remaining if all(d in placed for d, _ in deps[t["id"]])]
        if not ready:
            # A cycle in the approved edges: break it at the target the map puts first, and say so.
            ready = [min(remaining, key=lambda t: index[t["id"]])]
            cycles.append(ready[0]["id"])
        pick = min(ready, key=lambda t: index[t["id"]])
        order.append(pick)
        placed.add(pick["id"])
        remaining.remove(pick)
    rows, reasons = [], []
    for position, t in enumerate(order, start=1):
        after = [d for d, _ in deps[t["id"]]]
        because = [r for _, r in deps[t["id"]]]
        before = [o["id"] for o in order if t["id"] in [d for d, _ in deps[o["id"]]]]
        if t["id"] in cycles:
            why = ("The approved edges make a loop here, so the deployment map's own order decides. "
                   "Check the system map.")
        elif because:
            why = " · ".join(because)
        else:
            why = "No approved edge relates this to the others; kept in deployment-map order."
        rows.append({"target": t["id"], "repo": t.get("repo") or "", "host": t.get("host") or "",
                     "method": t.get("method") or "", "service": t.get("service") or t.get("site") or "",
                     "critical": bool(t.get("critical")), "position": position,
                     "after": after, "before": before, "reason": why})
        reasons += [r for r in because if r not in reasons]
    return {"created_at": now(), "source": "system map (approved edges) + deployment-map order",
            "order": [t["id"] for t in order], "steps": rows, "reasons": reasons,
            "cycles": cycles, "targets": order}


# ============================================================================ running a plan
def _not_attempted(target: dict, blocker: str, blocker_status: str) -> dict:
    rec = deploy._record(target, status=NOT_ATTEMPTED, exit_code=None,
                         detail=f"not attempted because {blocker} {blocker_status}")
    rec["finished_at"] = rec["started_at"]
    rec["blocked_by"] = blocker
    rec["duration"] = 0.0
    return rec


def run_plan(steps: dict, *, trigger: str = "pr_merged", task=None, automatic: bool = True,
             runner=None, only=None) -> dict:
    """Run an ordered plan under one lock, stopping whatever depends on a failure.

    `only` limits the run to a set of target ids (resuming from a failed step); targets left out
    keep the outcome they already had, and their dependents are judged on that outcome.
    """
    runner = runner or deploy.run_target
    targets = steps.get("targets") or []
    deps = {s["target"]: list(s.get("after") or []) for s in steps.get("steps") or []}
    records, status_of = [], {}
    prior = {r.get("target"): r for r in (steps.get("previous") or [])}
    with deploy.LOCK:  # one deployment at a time across the whole instance
        for t in targets:
            tid = t["id"]
            blocker = next((d for d in deps.get(tid, []) if status_of.get(d) in BLOCKING + (NOT_ATTEMPTED,)), None)
            if blocker:
                rec = _not_attempted(t, blocker, status_of[blocker] if status_of[blocker] in BLOCKING else "was not attempted")
            elif only is not None and tid not in only and tid in prior:
                rec = dict(prior[tid])
                rec["detail"] = (rec.get("detail") or "") + " (kept from the earlier run)"
            else:
                rec = runner(t, trigger=trigger, task=task, automatic=automatic)
            records.append(rec)
            status_of[tid] = rec.get("status")
    return summarize(steps, records, trigger)


def summarize(steps: dict, records: list[dict], trigger: str = "pr_merged") -> dict:
    """The deployment record stored on a task: the plan, every outcome, and what is half-deployed."""
    by_id = {s["target"]: s for s in steps.get("steps") or []}
    for r in records:
        s = by_id.get(r.get("target")) or {}
        r.setdefault("blocked_by", None)
        r["position"] = s.get("position")
        r["order_reason"] = s.get("reason", "")
    ran = [r for r in records if r.get("status") != "skipped"]
    blocked = [{"target": r["target"], "repo": r.get("repo"), "because": r.get("blocked_by"),
                "reason": r.get("detail") or ""} for r in records if r.get("status") == NOT_ATTEMPTED]
    failed = [r["target"] for r in records if r.get("status") in BLOCKING]
    status = ("failed" if any(r.get("status") == "failed" for r in ran)
              else "blocked" if any(r.get("status") in ("refused", "blocked", NOT_ATTEMPTED) for r in ran)
              else "manual" if ran and all(r.get("status") == "manual" for r in ran)
              else "succeeded" if ran else "skipped")
    deployed = [r["target"] for r in records if r.get("status") == "succeeded"]
    not_deployed = [r["target"] for r in records if r.get("status") in BLOCKING + (NOT_ATTEMPTED,)]
    resume_from = next((r["target"] for r in records if r.get("status") in BLOCKING), None)
    return {"status": status, "trigger": trigger,
            "started_at": records[0]["started_at"] if records else now(), "finished_at": now(),
            "plan": {k: v for k, v in steps.items() if k != "targets"},
            "targets": records, "blocked": blocked,
            "half_deployed": {"deployed": deployed, "not_deployed": not_deployed,
                              "failed": failed, "resume_from": resume_from},
            "can_resume": bool(not_deployed)}


# ============================================================================ the one story
def task_repositories(task: dict) -> list[dict]:
    """Every repository a task touched, primary first, with its GitHub name and system component."""
    data = systemmap.load()
    rows, seen = [], set()

    def add(path, github_repo, role, name="", reason="", component=""):
        full = deploy.full_repo(github_repo) if github_repo else ""
        key = (full or str(path or "")).lower()
        if not key or key in seen:
            return
        seen.add(key)
        comp = component or ""
        if not comp:
            c = _component_for(data, full or path or "")
            comp = c["id"] if c else ""
        rows.append({"name": name or deploy.repo_name(full or path or ""), "repo": full,
                     "path": str(path or ""), "role": role, "reason": reason, "component": comp})

    add(task.get("repo"), task.get("github_repo"), "primary")
    for r in task.get("repos") or []:
        if not isinstance(r, dict) or r.get("role") == "primary":
            continue
        add(r.get("repo"), r.get("github_repo"), "related", reason=r.get("reason") or "",
            component=r.get("component") or "")
    for name, w in (task.get("repo_worktrees") or {}).items():
        if isinstance(w, dict):
            add(w.get("repo"), w.get("github_repo"), "related", name=name)
    return rows


def task_pull_requests(task: dict) -> list[dict]:
    """Each repository's pull request and what is known about its state."""
    out = []
    if task.get("pr_url") or task.get("pr_number"):
        out.append({"repo": deploy.full_repo(task.get("github_repo")), "number": task.get("pr_number"),
                    "url": task.get("pr_url") or "", "title": task.get("name") or "",
                    "branch": task.get("branch") or task.get("branch_name") or "", "role": "primary"})
    for name, w in (task.get("repo_worktrees") or {}).items():
        if isinstance(w, dict) and (w.get("pr_url") or w.get("pr_number")):
            out.append({"repo": deploy.full_repo(w.get("github_repo")), "number": w.get("pr_number"),
                        "url": w.get("pr_url") or "", "title": name, "branch": w.get("branch") or "",
                        "role": "related"})
    ledger = load_ledger().get("seen") or {}
    # What the deploy hook saw when it decided to run: the state of every pull request it checked.
    checked = {seen_key(p.get("repo"), p.get("number")): p
               for p in (((task.get("deploy") or {}).get("pr_state") or {}).get("pull_requests") or [])}
    for pr in out:
        key = seen_key(pr["repo"], pr["number"])
        row = ledger.get(key) or {}
        seen_state = checked.get(key) or {}
        state = str(seen_state.get("state") or "").upper()
        pr["state"] = "MERGED" if (row.get("merged_at") or state == "MERGED") else (state or "OPEN")
        pr["merged_at"] = row.get("merged_at") or seen_state.get("merged_at") or ""
        pr["merge_commit"] = row.get("commit") or seen_state.get("merge_commit") or ""
    return out


def task_merges(task: dict) -> list[dict]:
    rows = []
    ledger = load_ledger().get("seen") or {}
    for pr in task_pull_requests(task):
        row = ledger.get(seen_key(pr["repo"], pr["number"]))
        if row:
            rows.append(dict(row))
        elif pr.get("merged_at"):
            rows.append({"repo": pr["repo"], "number": pr["number"], "url": pr["url"],
                         "merged_at": pr["merged_at"], "commit": pr.get("merge_commit") or "",
                         "noticed_at": "", "source": "task pull request"})
    rows.sort(key=lambda r: str(r.get("merged_at") or ""))
    return rows


STATES = ("planning", "in review", "merged", "deploying", "deployed", "partly deployed", "failed", "not deployed")


def _state(task: dict, prs: list[dict], record: dict) -> str:
    if record:
        st = record.get("status")
        if st == "running":
            return "deploying"
        if st == "failed":
            return "failed"
        if st == "blocked":
            return "partly deployed" if (record.get("half_deployed") or {}).get("deployed") else "not deployed"
        if st == "succeeded":
            return "deployed"
        if st in ("manual", "skipped"):
            return "not deployed"
    if prs and all(str(p.get("state") or "").upper() == "MERGED" for p in prs):
        return "merged"
    if prs:
        return "in review"
    return "planning"


def story(task: dict, targets=None) -> dict:
    """One structure per task: request → pull requests → merges → deployments → what is blocked.

    Everything a person or an interface needs is here, already joined: no second call, no
    re-derivation in the browser.
    """
    record = task.get("deploy") or {}
    repos = task_repositories(task)
    prs = task_pull_requests(task)
    merges = task_merges(task)
    known = targets if targets is not None else [t for r in repos for t in deploy.targets_for_repo(r["repo"] or r["path"])]
    steps = record.get("plan") or plan(known)
    deployments = list(record.get("targets") or [])
    if not deployments:
        # Nothing has run yet: show the plan as the deployments that are going to happen.
        deployments = [{"target": s["target"], "repo": s["repo"], "host": s["host"], "method": s["method"],
                        "service": s.get("service", ""), "status": "pending", "position": s["position"],
                        "order_reason": s["reason"], "critical": s.get("critical", False),
                        "duration": None, "log_path": None, "exit_code": None, "detail": "",
                        "started_at": None, "finished_at": None, "trigger": None, "blocked_by": None}
                       for s in steps.get("steps") or []]
    half = record.get("half_deployed") or {"deployed": [], "not_deployed": [], "failed": [], "resume_from": None}
    state = _state(task, prs, record)
    return {
        "task": {"id": task.get("id"), "name": task.get("name") or "", "status": task.get("status"),
                 "created_at": task.get("created_at"), "finished_at": task.get("finished_at"),
                 "issue": task.get("github_issue_number"), "issue_url": task.get("github_issue_url") or ""},
        "state": state,
        "plan": {k: v for k, v in steps.items() if k != "targets"},
        "repositories": repos,
        "pull_requests": prs,
        "merges": merges,
        "deployments": deployments,
        "blocked": record.get("blocked") or [],
        "half_deployed": half,
        "can_resume": bool(record.get("can_resume")),
        "trigger": record.get("trigger") or "",
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "summary": summary_line(task.get("name") or "", state, deployments, half),
    }


def merge_story(row: dict) -> dict:
    """The same structure for a merge nobody was watching: no task, one repository, one deployment set."""
    record = row.get("deploy") or {}
    steps = record.get("plan") or plan(deploy.targets_for_repo(row.get("repo") or ""))
    deployments = list(record.get("targets") or [])
    half = record.get("half_deployed") or {"deployed": [], "not_deployed": [], "failed": [], "resume_from": None}
    state = "deploying" if record.get("status") == "running" else {
        "succeeded": "deployed", "failed": "failed", "blocked": "partly deployed" if half.get("deployed") else "not deployed",
        "manual": "not deployed", "skipped": "not deployed"}.get(record.get("status"), "merged")
    pr = {"repo": row.get("repo"), "number": row.get("number"), "url": row.get("url") or "",
          "title": row.get("title") or "", "branch": row.get("branch") or "", "role": "primary",
          "state": "MERGED", "merged_at": row.get("merged_at") or "", "merge_commit": row.get("commit") or ""}
    return {
        "task": {"id": None, "name": row.get("title") or f"{row.get('repo')}#{row.get('number')}",
                 "status": "merged outside a task", "created_at": row.get("merged_at"),
                 "finished_at": record.get("finished_at"), "issue": None, "issue_url": ""},
        "state": state, "plan": {k: v for k, v in steps.items() if k != "targets"},
        "repositories": [{"name": deploy.repo_name(row.get("repo")), "repo": deploy.full_repo(row.get("repo")),
                          "path": "", "role": "primary", "reason": "", "component": ""}],
        "pull_requests": [pr], "merges": [{k: v for k, v in row.items() if k != "deploy"}],
        "deployments": deployments, "blocked": record.get("blocked") or [], "half_deployed": half,
        "can_resume": bool(record.get("can_resume")), "trigger": record.get("trigger") or "merge_watcher",
        "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
        "summary": summary_line(pr["title"], state, deployments, half),
    }


def summary_line(name: str, state: str, deployments: list[dict], half: dict) -> str:
    done = len([d for d in deployments if d.get("status") == "succeeded"])
    total = len(deployments)
    line = f"{truncate(name, 80)} — {state}"
    if total:
        line += f" ({done}/{total} target{'s' if total != 1 else ''} deployed)"
    if half.get("failed"):
        line += f"; {', '.join(half['failed'])} failed"
    if half.get("not_deployed"):
        rest = [t for t in half["not_deployed"] if t not in (half.get("failed") or [])]
        if rest:
            line += f"; {', '.join(rest)} not attempted"
    return line
