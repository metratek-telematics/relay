"""Did it actually ship? The honest success record (#65).

The scorecard (orchestrator/scorecard.py) counts a task a success when a pull request was opened and
not closed. That is optimism: three of Relay's own "delivered" pull requests were written against a
38-commit-old base, could not be merged at all, and were rebuilt by hand. They still counted as wins.

This module answers a narrower question from facts nobody self-reports:

    shipped = the pull request merged
              AND the merge reached production (or the repository has nothing to deploy)
              AND no person had to touch the delivered work afterwards
              AND it was not reverted

Per finished task it records, each as true / false / unknown, never as a guess:

    base_current        the branch was cut from current code (the guard writes what it measured)
    merged              the pull request merged; `merge_clean` says whether it merged without conflict
    human_touched       a person changed the delivered files after delivery: commits pushed to the
                        branch after Relay finished, or commits on the base branch after the merge
                        that touch files the merge delivered and are not Relay's own. `human_touched_scope`
                        says which of the two was actually looked at, so a "no" is never broader than
                        the check behind it; when neither ran the fact is unknown
    review_rounds       how many rounds the reviewer needed
    deployed            the deployment recipe ran and succeeded (`none_configured` when the
                        repository has no deployment target: merging is where it ships)
    reverted            reverted after merging, and whether that happened within a day

What cannot be reconstructed is marked `unknown` and the record says which fact is missing and why.
An unknown never counts as a success and never counts as a failure: it is reported separately, so the
rate is computed over the runs where every required fact is actually known.

`facts`, `verdict`, `record` and `summarize` are pure, so the whole history can be rebuilt from the
task records and scorecards, and `backfill` can run against a copy of the data.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .util import STATE_DIR, now, read_json, truncate, write_json

VERSION = 1
SHIPPED_FILE = STATE_DIR / "shipped.json"

# Under this many known runs a rate is noise, and every view says so instead of printing a number.
MIN_SAMPLE = 10

SHIPPED, RESCUED, UNKNOWN = "shipped", "rescued", "unknown"
VERDICT_LABEL = {SHIPPED: "Shipped", RESCUED: "Not shipped", UNKNOWN: "Cannot be told"}

# Reason codes, so a failure in the view is traceable to the fact that produced it.
REASON_TEXT = {
    "not_delivered": "The run never delivered anything",
    "pr_open": "The pull request was never merged",
    "pr_closed": "The pull request was closed without merging",
    "merge_conflicted": "The branch no longer merged cleanly and had to be fixed by hand",
    "stale_base": "The branch was cut from a base that had moved on",
    "human_touched": "A person changed the delivered files afterwards",
    "deploy_failed": "The deployment did not succeed",
    "reverted": "It was reverted after merging",
    "reverted_fast": "It was reverted within a day of merging",
}


def _epoch(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _tri(value):
    """true / false / unknown, never a truthy guess."""
    return None if value is None else bool(value)


# ----------------------------------------------------------------------------- facts
def facts(task: dict, card: dict, *, deploy_targets: int | None = None, post_merge: dict | None = None) -> dict:
    """Everything known about one finished run, from records rather than from what an agent claimed.

    `deploy_targets` is how many deployment targets Relay would actually run for the repository
    (0: nothing to deploy, so merging is where it ships; None: nobody looked).
    `post_merge` is the result of `post_merge_edits` when it could be run: {"checked": bool,
    "commits": n, "files": [...], "authors": [...]}.
    """
    task, card = task or {}, card or {}
    pr = card.get("pr") or {}
    check = task.get("delivery_check") or {}
    dep = task.get("deploy") or {}
    post_merge = post_merge or {}

    outcome = card.get("outcome")
    delivered = outcome in ("delivered_pr", "done_no_pr")
    has_pr = outcome == "delivered_pr"

    state = pr.get("state") if has_pr else None
    merged = True if state == "merged" else False if state in ("closed", "open") else None
    if not delivered:
        merged = False
    if outcome == "done_no_pr":
        merged = None  # nothing to merge; `merge_state` records that

    # A branch that had to be rebased or that conflicted at the delivery gate did not merge cleanly.
    if check.get("conflicted") is not None:
        merge_clean = not bool(check["conflicted"])
    elif check.get("rebased"):
        merge_clean = False
    elif merged and check:
        merge_clean = True
    else:
        merge_clean = None

    behind = check.get("base_behind")
    base_current = None if behind is None else int(behind) == 0

    # Two ways a person's hand shows: commits on the branch after Relay finished (the scorecard
    # already counts these) and commits on the base branch after the merge that touch delivered files.
    branch_commits = pr.get("human_commits") if has_pr else None
    after_merge = int(post_merge.get("commits") or 0) if post_merge.get("checked") else None
    if (branch_commits or 0) > 0 or (after_merge or 0) > 0:
        human_touched, scope = True, "branch and base branch" if after_merge is not None else "branch"
    elif branch_commits is None and after_merge is None:
        human_touched, scope = None, "nothing was checked"
    elif after_merge is None:
        # The pull request's own commits were checked and there were none; nobody looked at the base branch.
        human_touched, scope = False, "branch only"
    else:
        human_touched, scope = False, "branch and base branch"

    dep_status = dep.get("status")
    if dep_status == "succeeded":
        deploy_state = "succeeded"
    elif dep_status in ("failed", "blocked", "refused"):
        deploy_state = "failed"
    elif dep_status in ("manual", "skipped"):
        deploy_state = "not_run"
    elif deploy_targets == 0:
        deploy_state = "none_configured"
    elif dep_status:
        deploy_state = "unknown"
    else:
        deploy_state = "none_configured" if deploy_targets == 0 else "unknown"

    # The revert watcher (orchestrator/learning.py) writes `reverted` when it finds one; a pull request
    # record that does not say so has not been seen reverted.
    reverted = bool(pr.get("reverted")) if has_pr else (False if merged is False else None)
    fast = None
    if pr.get("reverted"):
        merged_at, revert_at = _epoch(pr.get("merged_at")), _epoch(pr.get("revert_at"))
        fast = (revert_at - merged_at) <= 86400 if (merged_at and revert_at) else None

    return {
        "outcome": outcome,
        "delivered": delivered,
        "base_current": base_current, "base_behind": behind, "base_ref": check.get("ref") or check.get("base_ref") or "",
        "merged": merged, "merge_state": state or ("no pull request" if outcome == "done_no_pr" else ""),
        "merge_clean": merge_clean,
        "human_touched": human_touched, "human_touched_scope": scope,
        "human_branch_commits": branch_commits, "human_commits_after_merge": after_merge,
        "human_files": list(post_merge.get("files") or [])[:10],
        "review_rounds": card.get("review_rounds"),
        "deploy_state": deploy_state, "deploy_targets": deploy_targets,
        "reverted": reverted, "reverted_within_day": fast,
    }


# ----------------------------------------------------------------------------- the verdict
def verdict(f: dict) -> dict:
    """shipped / rescued / unknown, with the fact behind each. Pure.

    A run is `shipped` only when every required fact is known and good. Anything known to be bad is
    `rescued` (a person had to step in, or it never landed). A run whose required facts are not all
    known is `unknown` and is left out of the rate.
    """
    reasons, missing = [], []

    def bad(code, extra=""):
        reasons.append({"code": code, "text": REASON_TEXT.get(code, code) + (f" ({extra})" if extra else "")})

    if not f.get("delivered"):
        bad("not_delivered", str(f.get("outcome") or ""))
        return {"verdict": RESCUED, "reasons": reasons, "missing": []}

    if f.get("merged") is False:
        bad("pr_closed" if f.get("merge_state") == "closed" else "pr_open")
    elif f.get("merged") is None and f.get("merge_state") != "no pull request":
        missing.append("merged")
    if f.get("merge_clean") is False:
        bad("merge_conflicted")
    if f.get("base_current") is False:
        bad("stale_base", f"{f.get('base_behind')} commits behind {f.get('base_ref') or 'the base branch'}")

    if f.get("human_touched") is True:
        n = (f.get("human_branch_commits") or 0) + (f.get("human_commits_after_merge") or 0)
        bad("human_touched", f"{n} commit(s)")
    elif f.get("human_touched") is None:
        missing.append("human_touched")

    if f.get("deploy_state") == "failed":
        bad("deploy_failed")
    elif f.get("deploy_state") in ("unknown", "not_run"):
        missing.append("deployed")

    if f.get("reverted") is True:
        bad("reverted_fast" if f.get("reverted_within_day") else "reverted")
    elif f.get("reverted") is None and f.get("merged"):
        missing.append("reverted")

    if reasons:
        return {"verdict": RESCUED, "reasons": reasons, "missing": missing}
    if missing:
        return {"verdict": UNKNOWN, "reasons": [], "missing": missing}
    return {"verdict": SHIPPED, "reasons": [], "missing": []}


def record(task: dict, card: dict, *, deploy_targets: int | None = None, post_merge: dict | None = None) -> dict:
    """One row per finished run: who did it, where, and whether it shipped. Pure."""
    task, card = task or {}, card or {}
    f = facts(task, card, deploy_targets=deploy_targets, post_merge=post_merge)
    v = verdict(f)
    team = card.get("team") or {}
    agents = sorted({(r or {}).get("agent") for r in team.values() if (r or {}).get("agent")})
    return {
        "version": VERSION,
        "task_id": card.get("task_id") or task.get("id"),
        "number": task.get("number"),
        "name": card.get("name") or task.get("name") or "",
        "finished_at": card.get("finished_at") or task.get("finished_at"),
        "repo": card.get("repo") or "", "repo_label": card.get("repo_label") or card.get("repo") or "",
        "preset": card.get("pairing") or "", "preset_label": card.get("pairing_label") or card.get("pairing") or "",
        "template": card.get("template") or task.get("template") or "feature",
        "agents": agents,
        "pr_url": (card.get("pr") or {}).get("url") or task.get("pr_url") or "",
        "pr_number": (card.get("pr") or {}).get("number") or task.get("pr_number"),
        "score": card.get("score"),
        "facts": f,
        "verdict": v["verdict"], "verdict_label": VERDICT_LABEL[v["verdict"]],
        "reasons": v["reasons"], "missing": v["missing"],
        "computed_at": now(),
    }


# ----------------------------------------------------------------------------- post-merge edits
def _norm(path: str) -> str:
    return str(path or "").strip().lstrip("./").replace("\\", "/").lower()


def a_persons_hand(message: str, *, prefix: str, own_prs=(), number=None) -> bool:
    """Was this commit on the base branch a person fixing what the delivery left behind?

    It is not, when:
        it is a merge commit, or Relay's own commit (the commit prefix, kept in a squash body);
        it belongs to another pull request — separate work that happens to touch the same file is not a
        rescue of this one, and a delivery that had to be rebuilt in another pull request already shows up
        as a pull request that never merged.

    It is, when a person committed straight onto the base branch, or when the commit names this pull
    request or reverts it: that is somebody cleaning up after the delivery.
    """
    text = str(message or "")
    lines = [l.strip().lstrip("*-").strip() for l in text.splitlines()]
    head = lines[0] if lines else ""
    pref = (prefix or "").strip().lower()
    if head.startswith("Merge ") or (pref and any(l.lower().startswith(pref) for l in lines)):
        return False
    m = re.search(r"\(#(\d+)\)\s*$", head)
    if not m:
        return True                      # committed straight onto the base branch
    n = int(m.group(1))
    if number and n == int(number):
        return False                     # the delivery's own squash commit
    names_it = bool(number and re.search(rf"(?:#|pull/){int(number)}\b", text))
    return bool(names_it or re.match(r"^revert\b", head, re.I)) and n not in set(own_prs or ())


def post_merge_edits(repo: str, merge_sha: str, merged_at: str, files, gh_json, *, prefix: str = "agent:",
                     base: str = "", window_days: int = 30, own_prs=(), number=None) -> dict:
    """Commits on the base branch after the merge that touch files the merge delivered, by a person.

    `gh_json` is github.gh_json (injected so tests and a dry backfill can supply their own). The merge
    commit itself and Relay's own work (see `relay_commit`) are not a person's hand. Returns
    {"checked": bool, "commits": n, "files": [...], "authors": [...], "why": "..."} — `checked` false
    means the answer is unknown, never zero.
    """
    want = {_norm(p) for p in files or []}
    if not repo or not merged_at or not want:
        return {"checked": False, "commits": 0, "files": [], "authors": [], "why": "no merge commit or file list to check"}
    since = str(merged_at).replace("Z", "+00:00")
    try:
        until = (datetime.fromisoformat(since) + timedelta(days=window_days)).isoformat()
    except ValueError:
        return {"checked": False, "commits": 0, "files": [], "authors": [], "why": "the merge time could not be read"}
    path = f"repos/{repo}/commits?since={since}&until={until}&per_page=100" + (f"&sha={base}" if base else "")
    try:
        rows = gh_json(["api", path], timeout=60) or []
    except Exception as e:
        return {"checked": False, "commits": 0, "files": [], "authors": [],
                "why": truncate(str(e).strip().splitlines()[-1] if str(e).strip() else "gh failed", 200)}
    hits, touched, authors = 0, [], []
    for r in rows if isinstance(rows, list) else []:
        sha = r.get("sha") or ""
        if merge_sha and sha.lower().startswith(str(merge_sha).lower()[:7]):
            continue
        if not a_persons_hand((r.get("commit") or {}).get("message") or "", prefix=prefix, own_prs=own_prs, number=number):
            continue
        try:
            detail = gh_json(["api", f"repos/{repo}/commits/{sha}"], timeout=60) or {}
        except Exception:
            continue
        changed = [_norm((x or {}).get("filename")) for x in detail.get("files") or []]
        overlap = [p for p in changed if p in want]
        if not overlap:
            continue
        hits += 1
        authors.append(((r.get("author") or {}) or {}).get("login") or ((r.get("commit") or {}).get("author") or {}).get("name") or "")
        for p in overlap:
            if p not in touched:
                touched.append(p)
    return {"checked": True, "commits": hits, "files": touched[:20],
            "authors": sorted({a for a in authors if a})[:10], "why": ""}


# ----------------------------------------------------------------------------- aggregation
def _week_start(d):
    return d - timedelta(days=d.weekday())


def _rate(rows: list[dict], key, label, min_sample: int = MIN_SAMPLE) -> list[dict]:
    groups = {}
    for r in rows:
        for k in key(r):
            if not k:
                continue
            g = groups.setdefault(k, {"key": k, "label": label(r, k), "n": 0, "shipped": 0, "rescued": 0, "unknown": 0})
            g["n"] += 1
            g[r["verdict"]] += 1
    out = []
    for g in groups.values():
        known = g["shipped"] + g["rescued"]
        out.append({**g, "known": known, "rate": (g["shipped"] / known) if known else None,
                    "too_few": known < min_sample})
    return sorted(out, key=lambda g: (-g["n"], g["label"]))


def summarize(records: list[dict], *, weeks: int = 8, today=None, min_sample: int = MIN_SAMPLE) -> dict:
    """The rate over time, by repository, by team preset and by agent, each with its sample size. Pure."""
    today = today or datetime.now().date()
    first = _week_start(today) - timedelta(weeks=weeks - 1)
    series = [{"week": (first + timedelta(weeks=i)).isoformat(), "n": 0, "shipped": 0, "rescued": 0, "unknown": 0}
              for i in range(weeks)]
    rows = []
    for r in records:
        try:
            day = datetime.fromisoformat(str(r.get("finished_at") or "")).date()
        except ValueError:
            continue
        rows.append(r)
        i = (_week_start(day) - first).days // 7
        if 0 <= i < weeks:
            b = series[i]
            b["n"] += 1
            b[r["verdict"]] += 1
    for b in series:
        known = b["shipped"] + b["rescued"]
        b["known"] = known
        b["rate"] = (b["shipped"] / known) if known else None
        b["too_few"] = known < min_sample

    shipped = sum(1 for r in rows if r["verdict"] == SHIPPED)
    rescued = sum(1 for r in rows if r["verdict"] == RESCUED)
    unknown = sum(1 for r in rows if r["verdict"] == UNKNOWN)
    known = shipped + rescued
    causes = {}
    for r in rows:
        for x in r.get("reasons") or []:
            c = causes.setdefault(x["code"], {"code": x["code"], "label": REASON_TEXT.get(x["code"], x["code"]), "count": 0})
            c["count"] += 1
    gaps = {}
    for r in rows:
        for m in r.get("missing") or []:
            gaps[m] = gaps.get(m, 0) + 1
    return {
        "version": VERSION, "min_sample": min_sample, "window_weeks": weeks,
        "n": len(rows), "shipped": shipped, "rescued": rescued, "unknown": unknown, "known": known,
        "rate": (shipped / known) if known else None,
        "too_few": known < min_sample,
        "definition": "merged, deployed, and not touched by a person afterwards",
        "weeks": series,
        "by_repo": _rate(rows, lambda r: [r.get("repo")], lambda r, k: r.get("repo_label") or k, min_sample),
        "by_preset": _rate(rows, lambda r: [r.get("preset")], lambda r, k: r.get("preset_label") or k, min_sample),
        "by_agent": _rate(rows, lambda r: r.get("agents") or [], lambda r, k: k, min_sample),
        "causes": sorted(causes.values(), key=lambda c: (-c["count"], c["label"])),
        "unknown_facts": sorted(({"fact": k, "count": v} for k, v in gaps.items()), key=lambda x: (-x["count"], x["fact"])),
        "failures": sorted([r for r in rows if r["verdict"] == RESCUED], key=lambda r: r.get("finished_at") or "", reverse=True)[:40],
        "records": sorted(rows, key=lambda r: r.get("finished_at") or "", reverse=True),
    }


# ----------------------------------------------------------------------------- store
class ShippedStore:
    """Every honest record keyed by task id, in one file under DATA_DIR/state."""

    def __init__(self, path: Path = SHIPPED_FILE):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.rows: dict = read_json(self.path, {}) or {}

    def get(self, tid):
        with self.lock:
            r = self.rows.get(tid)
            return json.loads(json.dumps(r)) if r else None

    def put(self, rec: dict):
        with self.lock:
            self.rows[rec["task_id"]] = rec
            write_json(self.path, self.rows)

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.rows.values()]


# ----------------------------------------------------------------------------- backfill
def delivered_files(task: dict) -> list[str]:
    """The files the run delivered, as the task recorded them."""
    out = []
    for c in task.get("changed_files") or []:
        p = c if isinstance(c, str) else (c or {}).get("path") or ""
        if p and p not in out:
            out.append(p)
    return out


def pr_files(repo: str, number, gh_json) -> list[str]:
    """The files a pull request delivered, from GitHub, for tasks whose record does not list them."""
    if not repo or not number:
        return []
    try:
        rows = gh_json(["api", f"repos/{repo}/pulls/{int(number)}/files?per_page=100"], timeout=60) or []
    except Exception:
        return []
    return [(r or {}).get("filename") or "" for r in rows if isinstance(r, dict)][:300]


def _repo_of(card: dict, task: dict) -> str:
    pr = (card.get("pr") or {}).get("url") or ""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/\d+", pr)
    return m.group(1) if m else (task.get("github_repo") or card.get("github_repo") or "")


def backfill(tasks: list[dict], cards: dict, *, gh_json=None, targets_for_repo=None, prefix: str = "agent:",
             check_github: bool = True) -> list[dict]:
    """Rebuild the honest record of every scored run. Pure apart from the GitHub reads it is given.

    Without `gh_json` nothing is asked of GitHub and every post-merge fact stays unknown, which is the
    point: the record says so instead of assuming nobody touched the work.
    """
    by_id = {t.get("id"): t for t in tasks or []}
    # Every pull request Relay opened, per repository: a later merge of one of those is Relay's work, not a rescue.
    own: dict = {}
    for c in (cards or {}).values():
        pr = c.get("pr") or {}
        if pr.get("number"):
            own.setdefault(_repo_of(c, by_id.get(c.get("task_id")) or {}), set()).add(int(pr["number"]))
    out = []
    for tid, card in (cards or {}).items():
        task = by_id.get(tid) or {}
        repo = _repo_of(card, task)
        targets = None
        if targets_for_repo and repo:
            try:
                targets = len(targets_for_repo(repo) or [])
            except Exception:
                targets = None
        post = None
        pr = card.get("pr") or {}
        if check_github and gh_json and pr.get("state") == "merged":
            files = delivered_files(task) or pr_files(repo, pr.get("number"), gh_json)
            post = post_merge_edits(repo, pr.get("merge_sha") or "", pr.get("merged_at") or "",
                                    files, gh_json, prefix=prefix, base=pr.get("base") or "",
                                    own_prs=own.get(repo) or (), number=pr.get("number"))
        out.append(record(task, card, deploy_targets=targets, post_merge=post))
    out.sort(key=lambda r: r.get("finished_at") or "")
    return out
