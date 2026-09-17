"""Task scorecards: how well a finished task went, in numbers a person can check.

A scorecard is computed when a task reaches done, failed or stopped, and again
whenever its pull request changes state afterwards. It is derived only from the
task record, its message log and GitHub, so an old task gets the same card as a
new one (see `Learning.backfill`).

Outcome
    delivered_pr   done, and a pull request was opened
    done_no_pr     done without a pull request (local repositories, PR creation off)
    failed         with a failure category read from the error (see FAILURES; relay_bug is Relay's own crash)
    stopped        stopped by the user

Success (the dashboard's success rate) means: outcome delivered_pr or done_no_pr,
and the pull request was not closed without merging.

Score, 0 to 100
    score = clamp(base + post-delivery + process adjustments, 0, 100)

    base                delivered_pr 70 · done_no_pr 65 · failed 15 · stopped 10
    post-delivery       PR merged +20 · PR closed unmerged -40 · PR still open 0
    (PR tasks only)     humans pushed commits to the branch after delivery -10
    verification        first verification run passed +5 · last run failed -10
    independent review  passed in round 1 +5 · every further round -4 (max -12)
    revisions           each supervisor revision request -3 (max -12)
    human steering      each guidance message or interrupt -3 (max -9)
    questions           each question a human had to answer -1 (max -4)
    approvals           each delivery approval the human rejected -5 (max -10)
    agent trouble       each failed turn, timeout or missing envelope -1 (max -6)
    retries             each fresh retry of the task -5 (max -10)

So a task that is delivered, merged, verified and reviewed cleanly in one pass
scores 100; one that is merged after two revisions and a guidance message scores
around 85; a delivered PR a human had to fix and then closed scores around 20.
Every card lists the parts that made up its score (`score_parts`).

"Humans pushed commits" counts commits on the PR made more than a minute after
delivery whose subject does not start with Relay's commit prefix and is not a
merge of the base branch ("Merge ..."). Relay commits everything it delivers
under that prefix, so what remains was written by someone else.
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import config as C
from .util import STATE_DIR, now, read_json, truncate, write_json

VERSION = 1
FINISHED = {"done", "failed", "stopped"}
SCORECARDS_FILE = STATE_DIR / "scorecards.json"

OUTCOME_LABEL = {"delivered_pr": "Delivered with a pull request", "done_no_pr": "Done without a pull request",
                 "failed": "Failed", "stopped": "Stopped by the user"}
BASE = {"delivered_pr": 70, "done_no_pr": 65, "failed": 15, "stopped": 10}

# (category, label, pattern on the error text). The first match wins.
FAILURES = [
    ("turn_budget", "Turn budget exhausted", r"turn budget exhausted"),
    ("review_rejected", "Reviewer kept blocking", r"reviewer still blocks"),
    ("verification", "Verification kept failing", r"verification still fails"),
    ("protocol", "Agent broke the protocol", r"valid protocol envelope|unexpected supervisor envelope|did not produce a plan"),
    ("agent_timeout", "Agent turn timed out", r"turn timeout|exceeded the .* minute"),
    ("agent_error", "Agent kept crashing", r"failed \d+ times in a row|agent turn failed|exited with code"),
    ("configuration", "Team or settings problem", r"no agent (is assigned|configured)|unknown agent"),
    ("delivery", "Commit, push or pull request failed", r"worktree is on|git push|pull request|gh pr|push .*failed|commit"),
    ("setup", "Worktree or environment setup failed", r"worktree|not a git repository|clone"),
    ("relay_bug", "Relay internal error", r"has no attribute|is not defined|unexpected keyword argument|not subscriptable|not callable"),
]
FAILURE_LABEL = {c: l for c, l, _ in FAILURES} | {"other": "Other error", "stopped": "Stopped by the user"}


def classify_failure(error: str) -> str:
    low = (error or "").lower()
    for cat, _, pat in FAILURES:
        if re.search(pat, low):
            return cat
    return "other"


# ----------------------------------------------------------------------------- inputs
def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return None


def read_messages(path: Path) -> list[dict]:
    """The message log with in-place updates applied, read without caching (for backfills)."""
    rows, by_id = [], {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("_patch"):
            tgt = by_id.get(r.get("id"))
            if tgt is not None:
                tgt.update({k: v for k, v in r.items() if k != "_patch"})
            continue
        by_id[r.get("id")] = r
        rows.append(r)
    return rows


def repo_key(task: dict) -> str:
    """GitHub name when the repository has one, else its local path. Lessons are filed under the same key."""
    gh = (task.get("github_repo") or "").strip().lower()
    if gh:
        return gh
    repo = (task.get("repo") or "").strip()
    return str(Path(repo).expanduser().resolve()) if repo else ""


def repo_label(key: str) -> str:
    if not key:
        return "(unknown)"
    return key if "/" in key and not key.startswith("/") and ":" not in key[:3] else Path(key).name


def team(task: dict) -> dict:
    roles = (task.get("workflow") or {}).get("roles") or {}
    sessions = task.get("sessions") or {}
    out = {}
    for r in C.ROLES:
        agent = (roles.get(r) or {}).get("agent") or ""
        if not agent:
            continue
        out[r] = {"agent": agent, "model": (roles.get(r) or {}).get("model") or (sessions.get(r) or {}).get("model") or "",
                  "effort": (roles.get(r) or {}).get("effort") or ""}
    return out


def pairing(tm: dict) -> tuple[str, str]:
    keys = [tm[r]["agent"] for r in C.ROLES if r in tm]
    return "→".join(keys), " → ".join(C.AGENTS.get(a, {}).get("label", a) for a in keys)


# ----------------------------------------------------------------------------- compute
def compute(task: dict, messages: list[dict], pr: dict | None = None) -> dict:
    """Build the scorecard of a finished task. Pure: no I/O."""
    status = task.get("status")
    run_start = _epoch(task.get("started_at"))
    # A task retried from scratch keeps its earlier messages; judge the run that produced this outcome.
    run_end = _epoch(task.get("finished_at"))
    msgs = [m for m in messages if not isinstance(m.get("ts"), (int, float))
            or ((not run_start or m["ts"] >= run_start - 1) and (not run_end or m["ts"] <= run_end + 2))]
    # Only what happened during the run: a note sent after a failure is a reaction to it, not steering.
    within = lambda iso: (not task.get("started_at") or (iso or "") >= task["started_at"][:19]) and (not task.get("finished_at") or (iso or "") <= task["finished_at"][:19])
    events = [e for e in task.get("events") or [] if within(e.get("time"))]

    if status == "done":
        outcome = "delivered_pr" if task.get("pr_url") else "done_no_pr"
    elif status == "failed":
        outcome = "failed"
    else:
        outcome = "stopped"
    category = classify_failure(task.get("error") or task.get("detail") or "") if outcome == "failed" else ("stopped" if outcome == "stopped" else None)

    verifs = [m for m in msgs if m.get("kind") == "verification"]
    reviews = [m for m in msgs if m.get("kind") == "review"]
    revisions = sum(1 for m in msgs if m.get("kind") == "decision" and m.get("decision") == "revise")
    questions = sum(1 for m in msgs if m.get("kind") == "question" and m.get("qid"))
    approvals = [m for m in msgs if m.get("kind") == "approval"]
    rejections = sum(1 for m in approvals if m.get("answered") and m.get("approved") is False)
    guidance = [g for g in task.get("guidance") or [] if within(g.get("time"))]
    titles = [e.get("title") or "" for e in events]
    interrupts = sum(1 for x in titles if x.startswith("Interrupting with guidance"))
    trouble = {"failures": sum(1 for x in titles if x.startswith("Agent turn failed")),
               "timeouts": sum(1 for m in msgs if m.get("kind") == "error" and "turn timeout" in str(m.get("content") or "")),
               "envelope_nudges": sum(1 for x in titles if x.startswith("Missing protocol envelope")),
               "fresh_sessions": sum(1 for x in titles if x.startswith("Starting a fresh session"))}
    retries = max(0, int(task.get("runs") or 1) - 1)

    metrics = task.get("metrics") or {}
    total = metrics.get("total") or {}
    roles_m = metrics.get("roles") or {}
    tm = team(task)
    pair_key, pair_label = pairing(tm)
    key = repo_key(task)
    started, finished = _epoch(task.get("started_at")), _epoch(task.get("finished_at"))
    blocked = task.get("blocked_checks") or []
    card = {
        "version": VERSION, "task_id": task.get("id"), "name": task.get("name"), "computed_at": now(),
        "started_at": task.get("started_at"), "finished_at": task.get("finished_at"),
        "outcome": outcome, "outcome_label": OUTCOME_LABEL[outcome],
        "failure_category": category, "failure_label": FAILURE_LABEL.get(category) if category else None,
        "failure_detail": truncate(task.get("error") or "", 300) if outcome == "failed" else "",
        "duration_seconds": round(finished - started, 1) if started and finished and finished >= started else None,
        "work_packages": int((task.get("checkpoint") or {}).get("turn") or 0),
        "turns": {r: int((roles_m.get(r) or {}).get("turns") or 0) for r in C.ROLES if r in roles_m},
        "total_turns": int(total.get("turns") or 0),
        "review_rounds": len(reviews),
        "review_first_pass": (str(reviews[0].get("verdict")).upper() == "PASS") if reviews else None,
        "verification": {"runs": len(verifs), "first_ok": bool(verifs[0].get("ok")) if verifs else None,
                         "final_ok": bool(verifs[-1].get("ok")) if verifs else None},
        "revisions": revisions,
        "blocked_checks": len(blocked), "blocked_action_required": sum(1 for b in blocked if b.get("action_required")),
        "cost_usd": round(float(total.get("cost_usd") or 0), 4), "cost_estimated": bool(total.get("estimated")),
        "tokens": {"input": int(total.get("input") or 0), "output": int(total.get("output") or 0),
                   "cached": sum(int(v.get("cached") or 0) for v in roles_m.values())},
        "team": tm, "pairing": pair_key, "pairing_label": pair_label,
        "repo": key, "repo_label": repo_label(key), "repo_path": task.get("repo") or "", "github_repo": task.get("github_repo") or "",
        "template": task.get("template") or "feature", "source": "github" if task.get("github_issue_key") else "manual",
        "tags": list(task.get("tags") or [])[:10],
        "human": {"questions": questions, "guidance": len(guidance), "interrupts": interrupts, "approvals": len(approvals),
                  "rejections": rejections, "file_edits": sum(1 for m in msgs if m.get("kind") == "file_edit"), "retries": retries},
        "agent_trouble": trouble,
        "lessons_used": len(task.get("lessons_used") or []),
        "pr": None,
    }
    if outcome == "delivered_pr":
        card["pr"] = {"url": task.get("pr_url"), "number": task.get("pr_number"), "state": "open", "merged_at": None,
                      "closed_at": None, "human_commits": None, "checked_at": None, "error": None, **(pr or {})}
    card["score"], card["score_parts"] = score(card)
    card["success"] = outcome in ("delivered_pr", "done_no_pr") and (card["pr"] or {}).get("state") != "closed"
    return card


def score(card: dict) -> tuple[int, list[dict]]:
    """Apply the formula in the module docstring; returns the score and its itemised parts."""
    parts = [{"label": card["outcome_label"], "points": BASE[card["outcome"]]}]

    def add(label, points, cap=None):
        if cap is not None:  # caps limit how far one kind of trouble can pull a score down
            points = max(points, cap)
        if points:
            parts.append({"label": label, "points": points})

    pr = card.get("pr") or {}
    if pr:
        if pr.get("state") == "merged":
            add("Pull request merged", 20)
        elif pr.get("state") == "closed":
            add("Pull request closed without merging", -40)
        if pr.get("human_commits"):
            add(f"{pr['human_commits']} human commit(s) after delivery", -10)
    v = card["verification"]
    if v["first_ok"]:
        add("Verification passed first time", 5)
    if v["final_ok"] is False:
        add("Last verification failed", -10)
    if card["review_first_pass"]:
        add("Review passed in round 1", 5)
    add("Extra review rounds", -4 * max(0, card["review_rounds"] - 1), -12)
    add(f"{card['revisions']} revision request(s)", -3 * card["revisions"], -12)
    h = card["human"]
    add("Human guidance or interrupts", -3 * (h["guidance"] + h["interrupts"]), -9)
    add("Questions a human answered", -1 * h["questions"], -4)
    add("Delivery rejected by a human", -5 * h["rejections"], -10)
    t = card["agent_trouble"]
    add("Failed turns, timeouts, missing envelopes", -1 * (t["failures"] + t["timeouts"] + t["envelope_nudges"]), -6)
    add("Fresh retries", -5 * h["retries"], -10)
    return max(0, min(100, sum(p["points"] for p in parts))), parts


# ----------------------------------------------------------------------------- post-delivery signal
PR_FIELDS = "state,mergedAt,closedAt,url,commits"


def human_commits(commits: list[dict], delivered_at: str, prefix: str) -> int:
    t0 = _epoch(delivered_at)
    if not t0:
        return 0
    prefix = (prefix or "").strip().lower()
    n = 0
    for c in commits or []:
        when = (c.get("committedDate") or c.get("authoredDate") or "").replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(when).timestamp()
        except ValueError:
            continue
        head = (c.get("messageHeadline") or "").strip()
        if ts <= t0 + 60 or head.startswith("Merge ") or (prefix and head.lower().startswith(prefix)):
            continue
        n += 1
    return n


def fetch_pr(card: dict, cfg: dict, gh_json) -> dict:
    """Ask GitHub for the PR's state and later commits. `gh_json` is github.gh_json (injected for tests)."""
    pr = dict(card.get("pr") or {})
    url = pr.get("url") or ""
    m = re.search(r"github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
    repo, number = (m.group(1), m.group(2)) if m else (card.get("github_repo"), str(pr.get("number") or ""))
    if not repo or not number:
        pr["error"] = "No pull request number or repository to check."
        return pr
    try:
        data = gh_json(["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS], timeout=45) or {}
    except Exception as e:  # GitHub down, gh signed out: keep the last known state
        pr.update(error=truncate(str(e).strip().splitlines()[-1] if str(e).strip() else "gh pr view failed", 300), checked_at=now())
        return pr
    state = (data.get("state") or "").upper()
    pr.update(state="merged" if state == "MERGED" else "closed" if state == "CLOSED" else "open",
              merged_at=data.get("mergedAt"), closed_at=data.get("closedAt"), url=data.get("url") or url,
              human_commits=human_commits(data.get("commits") or [], card.get("finished_at"), cfg.get("commit_message_prefix") or "agent:"),
              checked_at=now(), error=None)
    return pr


# ----------------------------------------------------------------------------- store
class ScorecardStore:
    """Every scorecard keyed by task id, in one file under DATA_DIR/state. Survives the task record."""

    def __init__(self, path: Path = SCORECARDS_FILE):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.cards: dict = read_json(self.path, {}) or {}

    def get(self, tid):
        with self.lock:
            c = self.cards.get(tid)
            return json.loads(json.dumps(c)) if c else None

    def put(self, card: dict):
        with self.lock:
            self.cards[card["task_id"]] = card
            write_json(self.path, self.cards)

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(c) for c in self.cards.values()]


# ----------------------------------------------------------------------------- dashboard summary
def _week_start(d):
    return d - timedelta(days=d.weekday())


def summarize(cards: list[dict], weeks: int = 8, today=None) -> dict:
    """Weekly success, score, and breakdowns by repository, team pairing and failure category."""
    today = today or datetime.now().date()
    first = _week_start(today) - timedelta(weeks=weeks - 1)
    series = [{"week": (first + timedelta(weeks=i)).isoformat(), "finished": 0, "success": 0, "score_sum": 0} for i in range(weeks)]
    rows = []
    for c in cards:
        try:
            day = datetime.fromisoformat(c.get("finished_at") or "").date()
        except ValueError:
            continue
        i = (_week_start(day) - first).days // 7
        if 0 <= i < weeks:
            rows.append(c)
            b = series[i]
            b["finished"] += 1
            b["success"] += bool(c.get("success"))
            b["score_sum"] += int(c.get("score") or 0)
    for b in series:
        n, total = b["finished"], b.pop("score_sum")
        b.update(success_rate=(b["success"] / n) if n else None, avg_score=round(total / n, 1) if n else None)

    def group(key, label):
        out = {}
        for c in rows:
            k = key(c)
            if not k:
                continue
            g = out.setdefault(k, {"key": k, "label": label(c), "finished": 0, "success": 0, "score_sum": 0, "cost_usd": 0.0})
            g["finished"] += 1
            g["success"] += bool(c.get("success"))
            g["score_sum"] += int(c.get("score") or 0)
            g["cost_usd"] = round(g["cost_usd"] + float(c.get("cost_usd") or 0), 2)
        res = []
        for g in out.values():
            n = g["finished"]
            res.append({**{k: v for k, v in g.items() if k != "score_sum"}, "success_rate": g["success"] / n, "avg_score": round(g["score_sum"] / n, 1)})
        return sorted(res, key=lambda g: (-g["finished"], -g["avg_score"], g["label"]))

    failures = {}
    for c in rows:
        cat = c.get("failure_category")
        if cat:
            failures[cat] = failures.get(cat, 0) + 1
    n = len(rows)
    prs = [c["pr"] for c in rows if c.get("pr")]
    return {
        "weeks": series, "window_weeks": weeks,
        "finished": n, "success": sum(1 for c in rows if c.get("success")),
        "success_rate": (sum(1 for c in rows if c.get("success")) / n) if n else None,
        "avg_score": round(sum(int(c.get("score") or 0) for c in rows) / n, 1) if n else None,
        "by_repo": group(lambda c: c.get("repo"), lambda c: c.get("repo_label") or c.get("repo"))[:8],
        "by_pairing": group(lambda c: c.get("pairing"), lambda c: c.get("pairing_label") or c.get("pairing"))[:8],
        "failures": sorted(({"category": k, "label": FAILURE_LABEL.get(k, k), "count": v} for k, v in failures.items()),
                           key=lambda x: (-x["count"], x["label"]))[:6],
        "prs": {"total": len(prs), "merged": sum(1 for p in prs if p.get("state") == "merged"),
                "closed": sum(1 for p in prs if p.get("state") == "closed"),
                "open": sum(1 for p in prs if p.get("state") == "open"),
                "human_fixed": sum(1 for p in prs if p.get("human_commits"))},
    }
