"""Issues board: open issues across every repository Relay knows, and turning them into tasks.

Repositories come from local clones with a GitHub remote (under RELAY_REPOS) and the
watched sources of the GitHub inbox. Lists are cached briefly so the board stays quick
and GitHub's rate limit is left alone.
"""
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import github, repos
from .util import quiet, truncate

CACHE_SECONDS = 120
LIST_FIELDS = "number,title,labels,assignees,updatedAt,url,body,comments,milestone,state,author"
SEARCH_FIELDS = "number,title,labels,assignees,updatedAt,url,body,repository,state,commentsCount,author"
# A task in one of these states is finished, so the issue may be picked up again.
REUSABLE = {"done", "failed", "stopped"}
MAX_COMMENTS = 12

_lock = threading.RLock()
_cache: dict[tuple, dict] = {}
_pool = ThreadPoolExecutor(max_workers=6)


def _key(repo: str, number) -> str:
    return f"{(repo or '').lower()}#{number}"


def known_repositories(tasks=()) -> dict:
    """owner/name → local checkout path ("" when not cloned) for every repository the board lists."""
    found: dict[str, str] = {}
    for path in repos.candidate_repos():
        full = github.remote_repo_name(path)
        if full:
            found.setdefault(full, str(path))
    for s in github.load_sources():
        full = github.normalize_repo_full_name(s.get("repo", ""))
        if not full:
            continue
        local = s.get("local_path") or ""
        if local and Path(local).exists() and not found.get(full):
            found[full] = local
        else:
            found.setdefault(full, "")
    return found


def local_path_for(full: str, tasks=()) -> str:
    """A local checkout of owner/name, if Relay already has one."""
    low = full.lower()
    for name, path in known_repositories().items():
        if name.lower() == low and path and Path(path).exists():
            return path
    for t in tasks:
        if (t.get("github_repo") or "").lower() == low and t.get("repo") and Path(t["repo"]).exists():
            return t["repo"]
    return ""


def _slim(issue: dict, repo: str) -> dict:
    comments = issue.get("comments")
    return {
        "repo": repo, "number": issue.get("number"), "title": issue.get("title") or "",
        "url": issue.get("url") or "", "state": (issue.get("state") or "OPEN").lower(),
        "body": truncate(issue.get("body") or "", 20000),
        "labels": [{"name": l.get("name"), "color": l.get("color") or ""} for l in issue.get("labels") or []],
        "assignees": [a.get("login") for a in issue.get("assignees") or [] if a.get("login")],
        "author": (issue.get("author") or {}).get("login") or "",
        "milestone": (issue.get("milestone") or {}).get("title") if issue.get("milestone") else "",
        "updated": issue.get("updatedAt") or "",
        "comments": len(comments) if isinstance(comments, list) else int(issue.get("commentsCount") or 0),
    }


def _cached(key: tuple, loader, force: bool):
    with _lock:
        hit = _cache.get(key)
        if hit and not force and time.time() - hit["at"] < CACHE_SECONDS:
            return hit
    try:
        row = {"at": time.time(), "rows": loader(), "error": None}
    except Exception as e:  # one unreachable repository must not blank the board
        row = {"at": time.time(), "rows": (hit or {}).get("rows", []), "error": truncate(str(e), 300)}
    with _lock:
        _cache[key] = row
    return row


def _repo_issues(full: str, state: str, force: bool):
    def load():
        rows = github.gh_json(["issue", "list", "--repo", full, "--state", state, "--limit", "100", "--json", LIST_FIELDS], timeout=90) or []
        return [_slim(x, full) for x in rows]
    return _cached(("repo", full.lower(), state), load, force)


def _assigned_to_me(state: str, force: bool):
    def load():
        args = ["search", "issues", "--assignee", "@me", "--limit", "100", "--json", SEARCH_FIELDS]
        if state != "all":
            args += ["--state", state]
        rows = github.gh_json(args, timeout=90) or []
        return [_slim(x, (x.get("repository") or {}).get("nameWithOwner") or "") for x in rows if not x.get("isPullRequest")]
    return _cached(("mine", state), load, force)


def task_index(tasks) -> dict:
    """issue key → the most relevant Relay task for it (an unfinished one wins over a finished one)."""
    index: dict[str, dict] = {}
    for t in tasks:
        key = t.get("github_issue_key")
        if not key and t.get("issue") and t.get("github_repo"):
            key = f"{t['github_repo']}#{t['issue']}"
        if not key:
            continue
        key = key.lower()
        prev = index.get(key)
        rank = (not t.get("archived"), t.get("status") not in REUSABLE, t.get("created_at") or "")
        if not prev or rank > prev["_rank"]:
            index[key] = {"id": t["id"], "name": t.get("name"), "status": t.get("status"), "archived": bool(t.get("archived")), "_rank": rank}
    return index


def list_issues(tasks, state="open", mine=False, force=False, repo="", label="", assignee="", q="") -> dict:
    state = state if state in ("open", "closed", "all") else "open"
    known = known_repositories()
    jobs = {full: _pool.submit(_repo_issues, full, state, force) for full in known}
    if mine:
        jobs["@me"] = _pool.submit(_assigned_to_me, state, force)
    rows, errors, seen, oldest = [], [], set(), time.time()
    for name, fut in jobs.items():
        res = fut.result()
        oldest = min(oldest, res["at"])
        if res["error"]:
            errors.append({"repo": name, "error": res["error"]})
        for x in res["rows"]:
            k = _key(x["repo"], x["number"])
            if k not in seen:
                seen.add(k)
                rows.append(x)
    index = task_index(tasks)
    lower_known = {k.lower(): v for k, v in known.items()}
    for x in rows:
        hit = index.get(_key(x["repo"], x["number"]))
        x["task"] = {k: v for k, v in hit.items() if k != "_rank"} if hit else None
        x["local_path"] = lower_known.get(x["repo"].lower(), "")
    facets = {
        "repos": sorted({x["repo"] for x in rows} | set(known), key=str.lower),
        "labels": sorted({l["name"] for x in rows for l in x["labels"] if l["name"]}, key=str.lower),
        "assignees": sorted({a for x in rows for a in x["assignees"]}, key=str.lower),
    }
    q = (q or "").strip().lower()

    def keep(x):
        if repo and x["repo"].lower() != repo.lower():
            return False
        if label and label not in [l["name"] for l in x["labels"]]:
            return False
        if assignee == "none" and x["assignees"]:
            return False
        if assignee and assignee != "none" and assignee not in x["assignees"]:
            return False
        if q and q.lstrip("#") != str(x["number"]) and q not in f"{x['title']} {x['repo']} {x['body']}".lower():
            return False
        return True

    rows = sorted(filter(keep, rows), key=lambda x: x["updated"], reverse=True)
    return {"issues": rows, "errors": errors, "facets": facets, "fetched_at": oldest, "cache_seconds": CACHE_SECONDS,
            "clone_root": str(github.clone_root())}


def invalidate():
    with _lock:
        _cache.clear()


# ----------------------------------------------------------------------------- turning issues into tasks
def issue_detail(full: str, number) -> dict:
    return github.gh_json(["issue", "view", str(number), "--repo", full, "--json", "number,title,body,url,labels,comments,state"], timeout=60) or {}


def relevant_comments(comments) -> list:
    """The discussion worth handing to agents: people, not bots, most recent last."""
    keep = []
    for c in comments or []:
        login = (c.get("author") or {}).get("login") or ""
        body = (c.get("body") or "").strip()
        if not body or login.endswith("[bot]") or body.startswith("Relay picked this up"):
            continue
        keep.append({"author": login, "at": c.get("createdAt") or "", "body": truncate(body, 1500)})
    return keep[-MAX_COMMENTS:]


def requirements_for(full: str, issue: dict) -> str:
    parts = [f"GitHub issue {full}#{issue.get('number')}: {issue.get('title')}", issue.get("url") or "", "",
             (issue.get("body") or "").strip() or "(The issue has no description.)"]
    comments = relevant_comments(issue.get("comments"))
    if comments:
        parts += ["", "## Discussion on the issue"]
        for c in comments:
            parts += ["", f"**@{c['author']}** ({c['at'][:10]}):", c["body"]]
    parts += ["", "Implement this issue completely, preserving repository conventions. "
                  "The deliverable is a reviewable branch and pull request that closes the issue."]
    return "\n".join(parts)


def announce(full: str, number, task: dict, comment: bool, label: str) -> list:
    """Optionally tell the issue that Relay picked it up. Both are off unless asked for."""
    notes = []
    if comment:
        body = f"Relay picked this up: {task.get('name')} (branch `{task.get('branch_name') or ''}`)."
        p = quiet(["gh", "issue", "comment", str(number), "--repo", full, "--body", body], timeout=60)
        notes.append("commented" if p.returncode == 0 else f"comment failed: {truncate((p.stderr or p.stdout or '').strip(), 200)}")
    label = (label or "").strip()
    if label:
        p = quiet(["gh", "issue", "edit", str(number), "--repo", full, "--add-label", label], timeout=60)
        notes.append(f"labelled {label}" if p.returncode == 0 else f"label failed: {truncate((p.stderr or p.stdout or '').strip(), 200)}")
    return notes


def related_repos(local: str) -> list[dict]:
    """Repositories the system map says this one depends on or is used by (approved dependencies only), cloned if needed."""
    from . import systemmap
    out = []
    for sug in systemmap.related(local, include_proposed=False)["suggestions"]:
        try:
            _, path = systemmap.ensure_local(sug["component"])
            out.append({"repo": path, "reason": sug["reason"], "component": sug["component"]})
        except Exception:
            continue  # a repository Relay cannot clone must not stop the issue's own task
    return out


def create_tasks(manager, payload: dict) -> dict:
    """Create one task per issue, in the order given.

    mode "sequential" queues them as a chain: the scheduler starts each only after the
    previous one has ended, whatever the parallel limit. "parallel" queues them normally,
    and "draft" creates them unqueued for you to start by hand.
    """
    items = [x for x in (payload.get("issues") or []) if isinstance(x, dict)]
    if not items:
        raise ValueError("Select at least one issue.")
    if len(items) > 50:
        raise ValueError("Create at most 50 tasks at once.")
    mode = payload.get("mode") if payload.get("mode") in ("sequential", "parallel", "draft") else "sequential"
    queue = mode != "draft"
    chain = f"chain_{uuid.uuid4().hex[:10]}" if mode == "sequential" and len(items) > 1 else ""
    stop_on_failure = bool(payload.get("stop_on_failure"))
    priority = payload.get("priority") if payload.get("priority") in ("urgent", "high", "normal", "low") else "normal"
    tasks = manager.store.list()
    index = task_index(tasks)
    created, skipped, errors = [], [], []
    base_pos = time.time()
    for i, item in enumerate(items):
        full = github.normalize_repo_full_name(item.get("repo", ""))
        number = str(item.get("number") or "").lstrip("#")
        label = f"{full}#{number}"
        if not full or "/" not in full or not number.isdigit():
            errors.append({"issue": label, "error": "Not a GitHub issue reference"})
            continue
        existing = index.get(_key(full, number))
        if existing and existing["status"] not in REUSABLE and not existing["archived"] and not payload.get("allow_duplicates"):
            skipped.append({"issue": label, "task": existing["id"], "reason": f"already has a task ({existing['status']})"})
            continue
        try:
            local = local_path_for(full, tasks)
            cloned = False
            if not local:
                res = github.clone_repo(full)
                local, cloned = res["path"], res.get("cloned", False)
            issue = issue_detail(full, number)
            title = issue.get("title") or f"Issue #{number}"
            # The branch comes from the title alone; the issue number is added once, not twice through the "#12" in the name.
            branch = manager.branch_for({"template": payload.get("template") or "feature"}, local, title, "", number)
            related = related_repos(local) if payload.get("related_repos", True) else []
            t = manager.create_task({
                "repo": local, "name": f"#{number} {title}"[:120], "issue": number, "branch": branch, "repos": related,
                "requirements": requirements_for(full, issue),
                "template": payload.get("template") or "feature", "priority": priority,
                # Created unqueued and queued below, once its chain is recorded, so the scheduler never sees it half-made.
                "tags": ["github", "issues-board"], "workflow": payload.get("workflow") or {}, "queue": False,
            })
            meta = {"github_repo": full, "github_issue_number": int(number), "github_issue_title": title,
                    "github_issue_url": issue.get("url"), "github_issue_key": f"{full}#{number}", "github_source": "issues",
                    "queue_pos": base_pos + i}
            if chain:
                meta.update(chain_id=chain, chain_index=len(created), chain_stop_on_failure=stop_on_failure)
            if queue:
                meta.update(status="queued", detail="Queued · runs after the previous issue task" if chain and created else "Waiting in queue")
            manager.store.update(t["id"], immediate=True, **meta)
            manager.timeline(t["id"], "github", "Created from issue", f"{full}#{number}" + (" · cloned the repository" if cloned else ""))
            notes = announce(full, number, manager.store.get(t["id"]) or t, bool(payload.get("comment")), payload.get("label") or "")
            if notes:
                manager.timeline(t["id"], "github", "Issue updated", " · ".join(notes))
            manager.emit_task(t["id"])
            created.append({"issue": label, "task": t["id"], "name": t["name"], "cloned": cloned, "notes": notes})
        except Exception as e:
            errors.append({"issue": label, "error": truncate(str(e), 400)})
    if chain and len(created) < 2:
        for c in created:  # a chain of one is just a queued task
            manager.store.update(c["task"], immediate=True, chain_id=None, chain_index=None)
    if created and queue and not manager.scheduler:
        manager.start()
    invalidate()
    return {"created": created, "skipped": skipped, "errors": errors, "mode": mode, "chain": chain if len(created) > 1 else ""}
