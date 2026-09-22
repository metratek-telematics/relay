"""Where a task's wall time went, by phase and by kind of work.

Two views over the same run, both derived from what Relay already records (so a task delivered by an
older build gets them too):

    phases   queue · setup · triage · planning · design · exploration · build · verification · review · delivery
    kinds    agent turns (per role) · checks Relay ran · setup · waiting for a person · retry back-off · Relay overhead

Phase boundaries come from the task's timeline events; agent turns from the per-turn metrics log; shell
commands (setup, verification, git) from the message log; waits for a person from questions and their answers.
Whatever a phase's wall time is not explained by those spans is "overhead" (orchestration, prompt building,
process start-up), so the numbers always add up to the wall time.
"""
from __future__ import annotations

from datetime import datetime

PHASES = [
    ("queue", "Waiting in the queue"),
    ("setup", "Worktree and environment"),
    ("triage", "Triage"),
    ("plan", "Planning"),
    ("design", "Design step"),
    ("explore", "Design exploration"),
    ("build", "Implementation"),
    ("verify", "Verification"),
    ("review", "Independent review"),
    ("deliver", "Delivery"),
]
LABEL = dict(PHASES)
KINDS = [
    ("worker", "Worker turns"),
    ("supervisor", "Supervisor turns"),
    ("reviewer", "Reviewer turns"),
    ("checks", "Checks Relay ran"),
    ("setup", "Dependency install"),
    ("git", "Git and GitHub"),
    ("human", "Waiting for you"),
    ("backoff", "Retry back-off"),
    ("overhead", "Relay overhead"),
]
KIND_LABEL = dict(KINDS)

# Timeline titles that open a phase (first match wins; order matters for prefixes).
_MARKS = [
    ("Preparing environment", "setup"),
    ("Worktree ready", "setup"),
    ("Triage", "triage"),
    ("Environment ready", "plan"),
    ("Environment setup failed", "plan"),
    ("Plan agreed", "build"),      # logged when planning ends; a design step, when any, opens right after
    ("Complexity:", "build"),
    ("Solo mode", "build"),
    ("Design first", "design"),
    ("Implementation starts from the design", "build"),
    ("Design exploration", "explore"),
    ("Exploration skipped", "build"),
    ("Supervisor declared the task complete", "verify"),
    ("Solo work reported", "verify"),
    ("Verification", "verify"),
    ("Revision requested", "build"),
    ("Next work package", "build"),
    ("Independent check", "review"),
    ("Committed", "deliver"),
    ("Nothing new to commit", "deliver"),
]


def _epoch(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return None


def _mark(title: str) -> str | None:
    t = title or ""
    if t.startswith("Design review round") or t.startswith("Design revision") or t.startswith("Design v") or t == "Design auto-approved":
        return "design"
    if t.startswith("Review round"):
        return "review"
    if " directions explored" in t:
        return "build"   # logged when exploration ends; the first work package follows
    if t.startswith("Focus group") or t == "Mockups rendered":
        return "explore"
    if t.startswith("Verification passed") or t.startswith("Verification failed"):
        return "verify_end"
    for prefix, phase in _MARKS:
        if t == prefix or t.startswith(prefix + " ") or t.startswith(prefix + " ·"):
            return phase
    return None


def phase_marks(task: dict) -> list[tuple[float, str]]:
    """(epoch, phase) boundaries of the current run, from explicit marks when recorded, else from timeline events."""
    start = _epoch(task.get("started_at"))
    explicit = [(float(ts), p) for p, ts in ((task.get("timing") or {}).get("marks") or []) if isinstance(ts, (int, float))]
    if explicit:
        rows = explicit
    else:
        rows = []
        for e in task.get("events") or []:
            ts = _epoch(e.get("time"))
            if ts is None or (start and ts < start - 1):
                continue
            p = _mark(e.get("title") or "")
            if p:
                rows.append((ts, p))
        # "Verification passed/failed" closes a check run: what follows is a review (logged when it ends),
        # more building, or delivery. The next boundary says which.
        fixed = []
        for i, (ts, p) in enumerate(rows):
            if p == "verify_end":
                nxt = next((q for _, q in rows[i + 1:] if q != "verify_end"), "deliver")
                p = "review" if nxt == "review" else ("build" if nxt in ("build", "verify") else "deliver")
            fixed.append((ts, p))
        rows = fixed
        # Planning starts once the environment is ready; a run without setup plans from the start.
        if start and not any(p == "setup" for _, p in rows):
            rows.insert(0, (start, "plan"))
    rows.sort(key=lambda r: r[0])
    # Collapse repeats so each row opens a new phase.
    out = []
    for ts, p in rows:
        if not out or out[-1][1] != p:
            out.append((ts, p))
    if start and out and out[0][0] > start + 1:
        out.insert(0, (start, "setup"))
    return out


def _spans(task: dict, messages: list[dict]) -> list[tuple[float, float, str]]:
    """(start, end, kind) intervals of known work."""
    start = _epoch(task.get("started_at")) or 0
    out = []
    for row in (task.get("metrics") or {}).get("log") or []:
        s, e = row.get("start"), row.get("end")
        if not isinstance(s, (int, float)) or not isinstance(e, (int, float)) or s < start - 1:
            continue
        role = row.get("role") or "worker"
        kind = role if role in ("worker", "supervisor", "reviewer") else "reviewer"
        out.append((s, e, kind))
    questions = {}
    for m in messages or []:
        ts = m.get("ts")
        if not isinstance(ts, (int, float)) or ts < start - 1:
            continue
        kind, role = m.get("kind"), m.get("role")
        if kind == "command" and isinstance(m.get("duration"), (int, float)):
            k = {"verify": "checks", "setup": "setup", "git": "git", "github": "git"}.get(role)
            if k:
                out.append((ts, ts + float(m["duration"]), k))
        if kind in ("question", "approval") and m.get("qid") and not str(m.get("answer") or "").startswith("(decided by Relay)"):
            questions[m["qid"]] = ts
        if kind == "user" and m.get("reply_to") in questions:
            out.append((questions.pop(m["reply_to"]), ts, "human"))
    for s, e, k in (task.get("timing") or {}).get("spans") or []:
        if isinstance(s, (int, float)) and isinstance(e, (int, float)):
            out.append((float(s), float(e), k))
    return sorted(out)


# Overlapping spans (a check inside an agent turn, parallel checks) are counted once, by priority.
_PRIORITY = ["human", "backoff", "worker", "supervisor", "reviewer", "checks", "setup", "git"]


def _attribute(lo: float, hi: float, spans) -> dict:
    """Seconds of [lo, hi) per kind; uncovered time is overhead."""
    if hi <= lo:
        return {}
    points = {lo, hi}
    live = [(max(s, lo), min(e, hi), k) for s, e, k in spans if e > lo and s < hi and e > s]
    for s, e, _ in live:
        points.update((s, e))
    edges = sorted(points)
    acc = {}
    for a, b in zip(edges, edges[1:]):
        if b <= a:
            continue
        kinds = {k for s, e, k in live if s <= a and e >= b}
        k = next((p for p in _PRIORITY if p in kinds), "overhead")
        acc[k] = acc.get(k, 0.0) + (b - a)
    return acc


def breakdown(task: dict, messages: list[dict] | None = None, now_ts: float | None = None) -> dict:
    """Phase durations with a per-kind split, totals per kind, and the recorded reasons each heavy phase ran."""
    created = _epoch(task.get("created_at"))
    started = _epoch(task.get("started_at"))
    finished = _epoch(task.get("finished_at")) or now_ts
    if finished is None:
        import time
        finished = time.time()
    phases = []
    if created and started and started > created:
        phases.append({"key": "queue", "label": LABEL["queue"], "start": created, "end": started,
                       "seconds": round(started - created, 1), "kinds": {"queue": round(started - created, 1)}})
    marks = phase_marks(task) if started else []
    if marks:
        finished = max(finished, marks[-1][0] + 0.5)   # finished_at has one-second resolution
    spans = _spans(task, messages or [])
    totals = {}
    merged = {}
    for i, (ts, p) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else finished
        if end <= ts:
            continue
        kinds = _attribute(ts, end, spans)
        for k, v in kinds.items():
            totals[k] = totals.get(k, 0.0) + v
        row = merged.get(p)
        if row is None:
            row = merged[p] = {"key": p, "label": LABEL.get(p, p), "start": ts, "end": end, "seconds": 0.0, "kinds": {}, "visits": 0}
            phases.append(row)
        row["visits"] += 1
        row["end"] = max(row["end"], end)
        row["seconds"] += end - ts
        for k, v in kinds.items():
            row["kinds"][k] = row["kinds"].get(k, 0.0) + v
    for row in phases:
        row["seconds"] = round(row["seconds"], 1)
        row["kinds"] = {k: round(v, 1) for k, v in sorted(row["kinds"].items(), key=lambda kv: -kv[1]) if v >= 0.5}
    active = round(finished - started, 1) if started else 0.0
    queue = round(started - created, 1) if created and started and started > created else 0.0
    ceremony = (task.get("checkpoint") or {}).get("ceremony") or task.get("ceremony") or {}
    return {
        "active_seconds": active,
        "queue_seconds": queue,
        "wall_seconds": round(active + queue, 1),
        "phases": phases,
        "kinds": [{"key": k, "label": KIND_LABEL.get(k, k), "seconds": round(v, 1)} for k, v in
                  sorted(totals.items(), key=lambda kv: -kv[1]) if v >= 0.5],
        "mode": (task.get("triage") or {}).get("mode") or ceremony.get("mode") or "",
        "triage": task.get("triage") or {},
        "ceremony": ceremony,
    }


def mark(manager, tid: str, phase: str, ts: float | None = None):
    """Record an explicit phase boundary (the timeline events stay the human-readable record)."""
    import time
    t = manager.store.get(tid) or {}
    timing = dict(t.get("timing") or {})
    marks = list(timing.get("marks") or [])
    if marks and marks[-1][0] == phase:
        return
    marks.append([phase, round(ts or time.time(), 2)])
    timing["marks"] = marks[-400:]
    manager.set_meta(tid, timing=timing)


def span(manager, tid: str, kind: str, start: float, end: float):
    """Record a span that no message or turn log captures (retry back-off)."""
    t = manager.store.get(tid) or {}
    timing = dict(t.get("timing") or {})
    rows = list(timing.get("spans") or [])
    rows.append([round(start, 2), round(end, 2), kind])
    timing["spans"] = rows[-400:]
    manager.set_meta(tid, timing=timing)
