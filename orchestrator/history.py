"""What a task produced, read back after the fact: its commits, where its time went,
and the live state of its pull request.

Everything here is derived from git, the message log and `gh`. Nothing is stored,
so a task delivered by an older build gets the same views as a new one.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path

from . import github, gitops
from .util import quiet, truncate

SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")
WORKING = "working"  # the pseudo-commit for uncommitted changes in the worktree
MAX_COMMITS = 300


def _git(args, cwd, timeout=30):
    try:
        return quiet(["git", *args], cwd=cwd, timeout=timeout)
    except Exception:
        return None


def _ok(p) -> str:
    return (p.stdout or "") if p is not None and p.returncode == 0 else ""


# ----------------------------------------------------------------------------- commits
def _source(task):
    """Where to read the branch from: the worktree while it exists, the branch ref after it is removed."""
    wt = task.get("worktree")
    if wt and Path(wt).exists():
        return Path(wt), "HEAD", True
    repo, branch = task.get("repo"), task.get("branch") or task.get("branch_name")
    if repo and branch and Path(repo).exists() and gitops.branch_exists(repo, branch):
        return Path(repo), f"refs/heads/{branch}", False
    return None, None, False


def _base(task, cwd, ref) -> str:
    base = task.get("base_commit") or ""
    if not base and task.get("worktree") and Path(task["worktree"]).exists():
        base = gitops.task_base(task)
    if not base and task.get("repo"):
        # Worktree gone and no recorded base: the merge base with the default branch is the next best answer.
        base = _ok(_git(["merge-base", ref, gitops.default_branch(task["repo"])], cwd)).strip()
    return base


def _numstat(text):
    files = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        binary = parts[0] == "-"
        files.append({"path": parts[2], "additions": 0 if binary else int(parts[0] or 0),
                      "deletions": 0 if binary else int(parts[1] or 0), "binary": binary})
    return files


def _uncommitted(wt: Path):
    if not _ok(_git(["status", "--porcelain"], wt)).strip():
        return None
    files = _numstat(_ok(_git(["diff", "--numstat", "HEAD"], wt)))
    for rel in _ok(_git(["ls-files", "--others", "--exclude-standard"], wt)).splitlines():
        rel = rel.strip()
        if not rel:
            continue
        p = wt / rel
        try:
            lines = len(p.read_text(encoding="utf-8").splitlines()) if p.stat().st_size < 2_000_000 else 0
            files.append({"path": rel, "additions": lines, "deletions": 0, "binary": False, "untracked": True})
        except (OSError, UnicodeDecodeError):
            files.append({"path": rel, "additions": 0, "deletions": 0, "binary": True, "untracked": True})
    return {"sha": WORKING, "short": "", "subject": "Uncommitted changes", "body": "", "author": "", "email": "",
            "time": None, "files": files, "additions": sum(f["additions"] for f in files),
            "deletions": sum(f["deletions"] for f in files), "uncommitted": True}


def commits(task) -> dict:
    cwd, ref, live = _source(task)
    if cwd is None:
        reason = ("The team has not created its branch yet." if not (task.get("branch") or task.get("worktree"))
                  else "The worktree and the branch are both gone, so there is no history to read.")
        return {"ok": False, "reason": reason, "commits": []}
    base = _base(task, cwd, ref)
    if not base:
        return {"ok": False, "reason": "Relay could not tell where this task's branch started.", "commits": []}
    # Record and field separators that cannot appear in commit text, so bodies may hold anything.
    fmt = "%x1e%H%x1f%h%x1f%an%x1f%ae%x1f%aI%x1f%s%x1f%b%x1f"
    p = _git(["log", f"-n{MAX_COMMITS}", "--no-color", "--no-renames", f"--format={fmt}", "--numstat", f"{base}..{ref}"], cwd, timeout=60)
    if p is None or p.returncode != 0:
        return {"ok": False, "reason": truncate((p.stderr if p is not None else "") or "git log failed", 300), "commits": []}
    rows = []
    for chunk in p.stdout.split("\x1e"):
        parts = chunk.split("\x1f")
        if len(parts) < 8:
            continue
        sha, short, author, email, when, subject, body, rest = parts[:8]
        files = _numstat(rest)
        rows.append({"sha": sha, "short": short, "subject": subject, "body": body.strip(), "author": author, "email": email,
                     "time": when, "files": files, "additions": sum(f["additions"] for f in files),
                     "deletions": sum(f["deletions"] for f in files)})
    touched = {f["path"] for c in rows for f in c["files"]}
    summary = {"commits": len(rows), "files": len(touched), "additions": sum(c["additions"] for c in rows),
               "deletions": sum(c["deletions"] for c in rows),
               "first": rows[-1]["time"] if rows else None, "last": rows[0]["time"] if rows else None,
               "truncated": len(rows) >= MAX_COMMITS}
    pending = _uncommitted(cwd) if live else None
    return {"ok": True, "base": base, "base_short": base[:7], "source": "worktree" if live else "branch",
            "branch": task.get("branch") or task.get("branch_name"), "summary": summary,
            "uncommitted": pending, "commits": rows}


def commit_diff(task, sha: str, path: str = "") -> dict:
    cwd, ref, live = _source(task)
    if cwd is None:
        raise FileNotFoundError("The task's worktree and branch are gone.")
    if sha == WORKING:
        if not live:
            raise FileNotFoundError("The worktree was removed, so there are no uncommitted changes to show.")
        if path:
            return {"text": gitops.diff_file(str(cwd), path, "HEAD")}
        return {"text": gitops.full_diff(str(cwd), 400000, "HEAD")}
    if not SHA_RE.match(sha or ""):
        raise ValueError("A commit id is 4 to 40 hexadecimal characters.")
    full = _ok(_git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], cwd)).strip()
    if not full:
        raise KeyError("Commit not found")
    # Only commits on this task's branch: the endpoint must not become a way to read any object in the repository.
    check = _git(["merge-base", "--is-ancestor", full, ref], cwd)
    base = _base(task, cwd, ref)
    before = _git(["merge-base", "--is-ancestor", full, base], cwd) if base else None
    if check is None or check.returncode != 0 or (before is not None and before.returncode == 0):
        raise PermissionError("That commit is not on this task's branch.")
    args = ["show", "--no-color", "--no-renames", "--format=", "--patch", full]
    if path:
        args += ["--", path]
    p = _git(args, cwd, timeout=60)
    return {"sha": full, "text": truncate(_ok(p), 400000)}


# ----------------------------------------------------------------------------- work chart
def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def work_segments(task, messages) -> dict:
    """Split the conversation into planning, work packages, review rounds and delivery.

    The message log is the only per-phase record Relay keeps: every message carries
    a timestamp and, for agent turns, the work package it belongs to. Review rounds
    reuse the work package number, so they are recognised by the review request
    handoff, and delivery by the orchestrator's git commands after the gate passes.
    """
    segs = []
    cur = None
    review_round = 0
    in_review_turn = None
    gate_passed = False
    # A task retried from scratch keeps its old messages for reference; chart only the current run.
    run_start = _epoch(task.get("started_at")) if task.get("started_at") else None
    for m in messages:
        ts = m.get("ts")
        if not isinstance(ts, (int, float)) or (run_start and ts < run_start - 1):
            continue
        kind, role, turn = m.get("kind"), m.get("role"), m.get("turn")
        key = cur["key"] if cur else "plan"
        if kind in ("complete", "approval"):
            key = "deliver"
        elif kind == "handoff" and m.get("subtype") == "review_request":
            review_round += 1
            in_review_turn = turn
            key = f"review:{review_round}"
        elif cur and cur["key"] == "deliver":
            key = "deliver"
        elif role in ("git", "github") and gate_passed:
            key = "deliver"
        elif cur and cur["key"].startswith("review:") and (turn is None or turn == in_review_turn):
            key = cur["key"]
        elif isinstance(turn, int):
            key = "plan" if turn == 0 else f"wp:{turn}"

        if kind == "decision":
            gate_passed = m.get("decision") == "done"
        elif kind == "review":
            gate_passed = m.get("verdict") == "PASS"
        elif isinstance(turn, int) and cur and cur["key"].startswith("wp:") and key != cur["key"]:
            gate_passed = False

        if not cur or key != cur["key"]:
            cur = {"key": key, "start": ts, "end": ts, "agents": {}, "messages": 0, "tool_calls": 0}
            segs.append(cur)
        cur["end"] = max(cur["end"], ts)
        cur["messages"] += 1
        if kind == "tool":
            cur["tool_calls"] += 1
        if m.get("agent") and kind in ("text", "tool", "thinking"):
            cur["agents"][m["agent"]] = cur["agents"].get(m["agent"], 0) + 1

    finished = _epoch(task.get("finished_at")) if task.get("finished_at") else None
    log = [e for e in (task.get("metrics") or {}).get("log") or [] if not run_start or (e.get("start") or 0) >= run_start - 1]
    out = []
    for i, s in enumerate(segs):
        # A phase lasts until the next one begins; waits for a person inside it are part of its wall time.
        end = segs[i + 1]["start"] if i + 1 < len(segs) else max(s["end"], finished or s["end"])
        kind, _, n = s["key"].partition(":")
        label = {"plan": "Planning", "deliver": "Delivery"}.get(kind) or (f"Work package {n}" if kind == "wp" else f"Review round {n}")
        agent = max(s["agents"], key=s["agents"].get) if s["agents"] else None
        out.append({"key": s["key"], "kind": kind, "label": label, "start": s["start"], "end": end,
                    "seconds": round(max(0.0, end - s["start"]), 1), "agent": agent, "agents": sorted(s["agents"]),
                    "messages": s["messages"], "tool_calls": s["tool_calls"], "usage": None})
    # Token and cost figures exist per agent turn only for runs recorded since the turn log was added.
    for row in log:
        start = row.get("start")
        if not isinstance(start, (int, float)) or not out:
            continue
        seg = next((s for s in reversed(out) if s["start"] <= start + 1), out[0])
        u = seg["usage"] or {"turns": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0, "estimated": False}
        u["turns"] += 1
        for k in ("input", "output", "cached"):
            u[k] += int(row.get(k) or 0)
        u["cost_usd"] = round(u["cost_usd"] + float(row.get("cost_usd") or 0), 5)
        u["estimated"] = u["estimated"] or bool(row.get("estimated"))
        seg["usage"] = u
    return {"segments": out, "total_seconds": round(sum(s["seconds"] for s in out), 1), "usage_recorded": bool(log)}


# ----------------------------------------------------------------------------- pull request
PR_FIELDS = "state,isDraft,mergedAt,closedAt,reviewDecision,statusCheckRollup,url,title,additions,deletions,changedFiles,mergeable"
PR_TTL = 60
_pr_cache: dict = {}
_pr_lock = threading.Lock()


def _pr_target(task):
    number = task.get("pr_number")
    repo = task.get("github_repo")
    url = task.get("pr_url") or ""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    if m:
        repo = repo or m.group(1)
        number = number or m.group(2)
    return (github.normalize_repo_full_name(repo or ""), str(number or "").strip())


def _checks(rollup) -> dict:
    out = {"total": 0, "passing": 0, "failing": 0, "pending": 0, "skipped": 0, "failed_names": []}
    for c in rollup or []:
        out["total"] += 1
        # CheckRun reports status + conclusion; the older StatusContext reports a single state.
        status = (c.get("status") or "").upper()
        result = (c.get("conclusion") or c.get("state") or "").upper()
        if c.get("__typename") == "CheckRun" and status and status != "COMPLETED":
            out["pending"] += 1
        elif result in ("SUCCESS",):
            out["passing"] += 1
        elif result in ("NEUTRAL", "SKIPPED"):
            out["skipped"] += 1
        elif result in ("PENDING", "EXPECTED", "") and status != "COMPLETED":
            out["pending"] += 1
        else:
            out["failing"] += 1
            out["failed_names"].append(c.get("name") or c.get("context") or "check")
    out["failed_names"] = out["failed_names"][:8]
    out["state"] = ("failing" if out["failing"] else "pending" if out["pending"]
                    else "passing" if out["passing"] else "none")
    return out


def pr_status(task, force=False) -> dict:
    repo, number = _pr_target(task)
    if not number:
        return {"ok": False, "none": True, "error": "This task has no pull request."}
    if not repo or "/" not in repo:
        return {"ok": False, "error": "Relay does not know which GitHub repository the pull request belongs to."}
    key = (task["id"], repo, number)
    with _pr_lock:
        hit = _pr_cache.get(key)
    if hit and not force and time.time() - hit["fetched"] < PR_TTL:
        return {**hit, "cached": True}
    try:
        data = github.gh_json(["pr", "view", number, "--repo", repo, "--json", PR_FIELDS], timeout=30) or {}
    except Exception as e:
        msg = str(e).strip().splitlines()[-1] if str(e).strip() else "gh pr view failed"
        # A stale answer beats none when GitHub is briefly unreachable; the error still shows.
        if hit:
            return {**hit, "cached": True, "stale": True, "error": truncate(msg, 300)}
        return {"ok": False, "error": truncate(msg, 300), "repo": repo, "number": int(number) if number.isdigit() else number}
    state = (data.get("state") or "").upper()
    shown = "merged" if state == "MERGED" else "closed" if state == "CLOSED" else "draft" if data.get("isDraft") else "open"
    out = {"ok": True, "repo": repo, "number": int(number) if number.isdigit() else number, "state": shown,
           "title": data.get("title"), "url": data.get("url"), "merged_at": data.get("mergedAt"), "closed_at": data.get("closedAt"),
           "review_decision": data.get("reviewDecision") or "", "mergeable": data.get("mergeable") or "",
           "additions": data.get("additions"), "deletions": data.get("deletions"), "changed_files": data.get("changedFiles"),
           "checks": _checks(data.get("statusCheckRollup")), "fetched": time.time()}
    with _pr_lock:
        _pr_cache[key] = out
    return {**out, "cached": False}
