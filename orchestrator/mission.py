"""Read models for the Mission Control screens (web/js/views/mission.js, work.js, changes.js, review.js).

Nothing here decides anything: it gathers what the manager, the autopilot and git already know into the shapes
the home screen, the work board, the cross-repository Changes view and the review cockpit draw, plus one action,
merging a delivered change set's pull requests in order, which a person starts from the review cockpit.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import github, gitops, history
from .store import ACTIVE
from .util import truncate

_ACTIVITY_KINDS = ("tool", "command", "connector")
_SAID_KINDS = ("text", "handoff", "plan", "decision", "review")


def last_activity(store, tid: str, window: int = 60) -> dict:
    """The latest tool call and the latest thing said in a task: what its agents are doing right now."""
    n = store.message_count(tid)
    rows = store.messages(tid, after=max(0, n - window))
    act = said = None
    for m in reversed(rows):
        k = m.get("kind")
        if act is None and k in _ACTIVITY_KINDS:
            act = {key: m.get(key) for key in ("id", "kind", "tool", "category", "summary", "status", "ts", "agent", "role",
                                                "duration", "connector", "operation", "title")}
            # Edit tools carry the file and the change in their input; keep enough of it to name the file.
            act["input"] = truncate(str(m.get("input") or m.get("content") or ""), 1500)
        if said is None and k in _SAID_KINDS and (m.get("content") or m.get("summary")):
            said = {"kind": k, "agent": m.get("agent"), "role": m.get("role"), "ts": m.get("ts"),
                    "text": truncate(re.sub(r"\s+", " ", str(m.get("summary") or m.get("content"))).strip(), 240)}
        if act and said:
            break
    return {"activity": act, "said": said}


def mission(manager, hours: float = 24) -> dict:
    """Home screen: the digest for the chosen period, every running task with its live activity, and the 14-day trend."""
    d = manager.autopilot.digest(hours=hours)
    rows = [t for t in manager.store.list() if not t.get("archived")]
    live = []
    for t in rows:
        if t.get("status") not in ACTIVE:
            continue
        view = manager.task_view(t)
        live.append({"task_id": t["id"], **last_activity(manager.store, t["id"]), "process": view.get("process")})
    try:
        trend = manager.insights(rows).get("days") or []
    except Exception:
        trend = []
    d["live"] = live
    d["trend"] = trend
    d["hours"] = hours
    return d


# ----------------------------------------------------------------------------- changes across repositories
def _repo_rows(t: dict) -> list[dict]:
    """Every repository of a task, primary first, with what the pipeline recorded about it."""
    rows = [{"name": Path(t.get("repo") or "").name or "primary", "primary": True, "repo": t.get("repo"), "worktree": t.get("worktree"),
             "branch": t.get("branch") or t.get("branch_name"), "base": gitops.task_base(t) if t.get("worktree") else "",
             "pr_url": t.get("pr_url"), "pr_number": t.get("pr_number"), "github_repo": t.get("github_repo")}]
    for name, w in (t.get("repo_worktrees") or {}).items():
        rows.append({"name": name, "primary": False, "repo": w.get("repo"), "worktree": w.get("worktree"), "branch": w.get("branch"),
                     "base": w.get("base_commit") or (gitops.base_commit(w.get("worktree"), w.get("repo")) if w.get("worktree") else ""),
                     "pr_url": w.get("pr_url"), "pr_number": w.get("pr_number"), "github_repo": w.get("github_repo")})
    return rows


def merge_order(t: dict, rows: list[dict] | None = None) -> list[str]:
    """Names in the order their pull requests should merge.

    The design step records an explicit order when it exists. Otherwise related repositories go first: they are what
    the primary repository calls (the system map's dependencies), so the provider lands before its consumer.
    """
    rows = rows if rows is not None else _repo_rows(t)
    names = [r["name"] for r in rows]
    explicit = [n for n in ((t.get("design") or {}).get("merge_order") or []) if n in names]
    rest = [r["name"] for r in rows if not r["primary"] and r["name"] not in explicit] + [r["name"] for r in rows if r["primary"] and r["name"] not in explicit]
    return explicit + rest


def changes(t: dict) -> dict:
    rows = _repo_rows(t)
    order = merge_order(t, rows)
    out = []
    for r in sorted(rows, key=lambda r: order.index(r["name"])):
        wt = r.get("worktree")
        exists = bool(wt) and Path(wt).exists()
        files, stat = ([], {}) if not exists else (gitops.changed_files(wt, r["base"] or None), gitops.diff_stat(wt, r["base"] or None))
        out.append({**{k: v for k, v in r.items() if k != "base"}, "base_short": (r["base"] or "")[:8], "exists": exists, "files": files, "stat": stat})
    return {"repos": out, "merge_order": order}


def diff(t: dict, repo_name: str, path: str) -> str:
    rows = _repo_rows(t)
    r = next((x for x in rows if x["name"] == repo_name), None) if repo_name else rows[0]
    if not r or not r.get("worktree"):
        raise ValueError("That repository has no worktree for this task.")
    if path:
        return gitops.diff_file(r["worktree"], path, r["base"] or None)
    return gitops.full_diff(r["worktree"], 400000, r["base"] or None)


# ----------------------------------------------------------------------------- merge a change set in order
def _pr_ref(url: str, number, repo: str):
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url or "")
    if m:
        return m.group(1), m.group(2)
    return github.normalize_repo_full_name(repo or ""), str(number or "")


def merge_change_set(t: dict, method: str = "squash", dry_run: bool = False) -> dict:
    """Merge every pull request of a delivered task, in merge order, stopping at the first one that fails.

    Draft pull requests are marked ready first. Already merged ones are skipped. Nothing is merged when a pull
    request is missing, so a half-delivered change set is never partly merged.
    """
    if t.get("status") != "done":
        raise ValueError("Only a delivered task can be merged.")
    method = method if method in ("squash", "merge", "rebase") else "squash"
    rows = _repo_rows(t)
    order = merge_order(t, rows)
    by_name = {r["name"]: r for r in rows}
    plan = []
    for name in order:
        r = by_name[name]
        repo, number = _pr_ref(r.get("pr_url"), r.get("pr_number"), r.get("github_repo"))
        if not (r.get("pr_url") or r.get("pr_number")):
            continue
        plan.append({"name": name, "repo": repo, "number": number, "url": r.get("pr_url")})
    if not plan:
        raise ValueError("This task has no pull requests to merge.")
    if any(not p["repo"] or "/" not in p["repo"] or not p["number"] for p in plan):
        raise ValueError("Relay cannot tell which GitHub repository one of the pull requests belongs to.")
    if dry_run:
        return {"ok": True, "plan": plan, "results": []}
    results = []
    for p in plan:
        step = {**p, "ok": False}
        try:
            view = github.gh_json(["pr", "view", p["number"], "--repo", p["repo"], "--json", "state,isDraft"], timeout=30) or {}
            if (view.get("state") or "").upper() == "MERGED":
                step.update(ok=True, skipped="already merged")
                results.append(step)
                continue
            if (view.get("state") or "").upper() == "CLOSED":
                raise RuntimeError("the pull request is closed")
            if view.get("isDraft"):
                github.gh_text(["pr", "ready", p["number"], "--repo", p["repo"]], timeout=60)
            github.gh_text(["pr", "merge", p["number"], "--repo", p["repo"], f"--{method}"], timeout=120)
            step["ok"] = True
        except Exception as e:  # stop here: later pull requests depend on this one
            step["error"] = truncate(str(e).strip().splitlines()[-1] if str(e).strip() else e.__class__.__name__, 400)
            results.append(step)
            break
        results.append(step)
    # Scorecards and the header read pull request state from this cache.
    try:
        history.pr_status(t, force=True)
    except Exception:
        pass
    return {"ok": all(r["ok"] for r in results) and len(results) == len(plan), "plan": plan, "results": results, "time": time.time()}
