"""Autopilot: what keeps an unattended queue moving, and what it tells you when you come back.

Queue a stack of tasks, walk away, come back to reviewed pull requests. This module owns the
operational layer around the scheduler in manager.py:

Dependencies and parking
    A task may depend on other tasks (`depends_on`, plus the older chain groups). It starts only
    when every dependency is done. A task that stops to wait for you (a question, an approval, a
    judge escalation) is PARKED: its runner keeps its place, but it no longer takes a parallel slot,
    so the queue carries on with the next task that does not depend on it. Waiting tasks carry a
    `waiting` record ("waiting for #12 ...") that the task list shows.

Retry policy
    Infrastructure failures (an agent that crashed, hung, broke the protocol or hit a rate limit)
    are retried from the checkpoint automatically, `retry_infra_failures` times per task. Judge
    outcomes (turn budget, review kept blocking, verification kept failing) and configuration
    problems are never retried: another run would fail the same way.

Capacity and limits
    Before a task starts, each role's agent is checked against what agent_info knows: signed in,
    5-hour / weekly utilisation above `limit_threshold_percent`, a limit already reached, a zero
    balance for a paid model. A blocked role switches to the first available agent of its
    fallback chain (recorded on the task), or the task waits until the limit resets. Cost caps per
    task (the task pauses after its current turn) and per day (nothing new starts) are estimates
    from the same token pricing the dashboard uses.

Schedule
    Run windows (always, or days + HH:MM ranges, overnight allowed), quiet hours in which judge
    escalations take their automatic choice instead of waiting, and pause / resume of everything.

Watchdog
    Runner threads that died, agent processes that vanished while their reader waits, and tasks
    that claim to be running with no runner are detected and resumed from their checkpoint.

Digest and inbox
    `digest()` summarises a period: delivered PRs with scorecards, what waits for you with the exact
    question and one-click answers, failures with their cause, spend, lessons to review and what is
    next with an ETA from historical durations. `inbox()` is the "Needs you" list alone.

The functions at the top are pure (no I/O) so the rules can be tested without agents.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .util import STATE_DIR, now, read_json, truncate, write_json

log = logging.getLogger("relay.autopilot")

DEFAULTS = {
    "paused": False,                      # pause everything: nothing starts, running tasks stop after their turn
    "schedule": "always",                 # always | windows
    "windows": [{"days": [0, 1, 2, 3, 4], "start": "20:00", "end": "07:00"}, {"days": [5, 6], "start": "00:00", "end": "23:59"}],
    "timezone": "",                       # IANA name, e.g. Europe/Athens; "" = the server's local time
    "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"},
    "park_waiting_tasks": True,           # a task waiting for you frees its parallel slot
    "retry_infra_failures": 1,            # automatic resumes after an agent crash, hang or rate limit
    "limit_check": True,
    "limit_threshold_percent": 90,        # a 5-hour or weekly window above this counts as exhausted
    "limit_action": "fallback",           # fallback: switch to the role's fallback chain first · wait: wait for the reset
    "fallbacks": {"supervisor": [], "worker": [], "reviewer": []},  # "agent" or "agent:model", tried in order
    "task_cost_cap_usd": 0,               # 0 = no cap; estimated
    "daily_cost_cap_usd": 0,              # 0 = no cap; estimated, local day
    "watchdog": True,
    "watchdog_silent_minutes": 20,        # an agent process that is gone while its turn still waits this long is stuck
    "digest_time": "08:00",               # a morning digest notification at this local time; "" = off
    "digest_hours": 24,                   # default period of the Digest page
}

INFRA_FAILURES = {"agent_timeout", "agent_error", "protocol", "rate_limit"}
RATE_LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|quota exceeded|too many requests|\b429\b|overloaded|insufficient (?:credits|balance)", re.I)
ETA_DEFAULT_SECONDS = 20 * 60


# ============================================================================ time
def tzinfo(name: str):
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def local_now(settings: dict, ts: float | None = None) -> datetime:
    tz = tzinfo(settings.get("timezone") or "")
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, tz) if tz else datetime.fromtimestamp(ts)


def _minutes(hhmm: str, default: int = 0) -> int:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(hhmm or ""))
    if not m:
        return default
    return max(0, min(24 * 60, int(m.group(1)) * 60 + int(m.group(2))))


def _in_range(minute: int, start: int, end: int) -> bool:
    """[start, end) in minutes of the day; end <= start wraps past midnight."""
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def in_windows(dt: datetime, windows: list) -> bool:
    """True when dt falls in any window. An overnight window belongs to the day it starts on."""
    minute = dt.hour * 60 + dt.minute
    wd = dt.weekday()
    for w in windows or []:
        days = w.get("days")
        days = list(range(7)) if days in (None, []) else [int(d) for d in days]
        s, e = _minutes(w.get("start"), 0), _minutes(w.get("end"), 24 * 60)
        if e == 23 * 60 + 59:
            e = 24 * 60  # "23:59" means to the end of the day
        if s < e:
            if wd in days and s <= minute < e:
                return True
        elif s == e:
            if wd in days:
                return True
        else:
            if wd in days and minute >= s:
                return True
            if (wd - 1) % 7 in days and minute < e:
                return True
    return False


def schedule_open(dt: datetime, settings: dict) -> bool:
    if (settings.get("schedule") or "always") != "windows":
        return True
    return in_windows(dt, settings.get("windows") or [])


def next_open(dt: datetime, settings: dict) -> datetime | None:
    """The next minute at which the schedule opens (dt itself when open); None when it never does."""
    if schedule_open(dt, settings):
        return dt
    probe = dt.replace(second=0, microsecond=0)
    # Candidates are window starts over the next 8 days; checking them is exact and cheap.
    starts = []
    for w in settings.get("windows") or []:
        s = _minutes(w.get("start"), 0)
        for d in range(8):
            day = probe + timedelta(days=d)
            c = day.replace(hour=0, minute=0) + timedelta(minutes=s)
            if c > dt:
                starts.append(c)
    for c in sorted(starts):
        if schedule_open(c, settings):
            return c
    return None


def window_close(dt: datetime, settings: dict) -> datetime | None:
    """When an open schedule next closes (None for always)."""
    if (settings.get("schedule") or "always") != "windows" or not schedule_open(dt, settings):
        return None
    probe = dt.replace(second=0, microsecond=0)
    for i in range(1, 8 * 24 * 60, 15):
        c = probe + timedelta(minutes=i)
        if not schedule_open(c, settings):
            # step back to the exact minute
            for j in range(15):
                b = c - timedelta(minutes=14 - j)
                if b > dt and not schedule_open(b, settings):
                    return b
            return c
    return None


def quiet_now(dt: datetime, settings: dict) -> bool:
    q = settings.get("quiet_hours") or {}
    if not q.get("enabled"):
        return False
    return _in_range(dt.hour * 60 + dt.minute, _minutes(q.get("start"), 22 * 60), _minutes(q.get("end"), 8 * 60))


# ============================================================================ cost
def spend_since(tasks: list, since_ts: float) -> dict:
    """Estimated spend of every turn that ended after since_ts, in total and per agent."""
    total, by_agent, turns, estimated = 0.0, {}, 0, False
    for t in tasks:
        for row in ((t.get("metrics") or {}).get("log") or []):
            if float(row.get("end") or 0) < since_ts:
                continue
            c = float(row.get("cost_usd") or 0)
            total += c
            turns += 1
            estimated = estimated or bool(row.get("estimated"))
            a = row.get("agent") or "?"
            by_agent[a] = round(by_agent.get(a, 0.0) + c, 4)
    return {"cost_usd": round(total, 4), "by_agent": by_agent, "turns": turns, "estimated": estimated}


def task_cost(t: dict) -> float:
    return float(((t.get("metrics") or {}).get("total") or {}).get("cost_usd") or 0)


def day_start_ts(settings: dict, ts: float | None = None) -> float:
    d = local_now(settings, ts)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ============================================================================ failures and retries
def failure_category(error: str) -> str:
    from .scorecard import classify_failure
    if RATE_LIMIT_RE.search(error or ""):
        return "rate_limit"
    return classify_failure(error or "")


def retry_allowance(t: dict, settings: dict) -> int:
    pol = t.get("retry_policy") or {}
    if "infra" in pol:
        try:
            return max(0, int(pol["infra"]))
        except (TypeError, ValueError):
            return 0
    return max(0, int(settings.get("retry_infra_failures") or 0))


def should_auto_retry(t: dict, settings: dict) -> tuple[bool, str]:
    """(retry?, category) for a task that just failed."""
    if t.get("status") != "failed":
        return False, ""
    cat = failure_category(t.get("error") or t.get("detail") or "")
    if cat not in INFRA_FAILURES:
        return False, cat
    return int(t.get("auto_retries") or 0) < retry_allowance(t, settings), cat


# ============================================================================ dependencies
def label(t: dict) -> str:
    n = t.get("number")
    return f"#{n} {t.get('name') or ''}".strip() if n else (t.get("name") or t.get("id") or "")


WAIT_WORD = {"needs_input": "waits for your answer", "paused": "is paused", "failed": "failed", "stopped": "was stopped",
             "interrupted": "was interrupted", "draft": "is a draft", "queued": "is queued"}


def dependency_blockers(t: dict, by_id: dict) -> list[dict]:
    """Dependencies of t that are not done yet. A deleted dependency no longer blocks."""
    out = []
    for dep in t.get("depends_on") or []:
        d = by_id.get(dep)
        if not d or d.get("status") == "done":
            continue
        out.append({"id": d["id"], "number": d.get("number"), "name": d.get("name"), "status": d.get("status"),
                    "label": label(d)})
    return out


def waiting_text(blockers: list[dict]) -> str:
    b = blockers[0]
    word = WAIT_WORD.get(b.get("status"), "is running")
    more = f" and {len(blockers) - 1} more" if len(blockers) > 1 else ""
    return f"Waiting for {b['label']}{more} ({word})"


def dependency_cycle(tid: str, deps: list, by_id: dict) -> bool:
    """Would tid depending on deps create a cycle?"""
    seen, stack = set(), list(deps or [])
    while stack:
        cur = stack.pop()
        if cur == tid:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend((by_id.get(cur) or {}).get("depends_on") or [])
    return False


# ============================================================================ limits
def _is_free_model(model: str) -> bool:
    return bool(re.search(r"(?::|/|-)free\b|\bfree$", model or "", re.I))


def _money(value) -> float | None:
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(value or ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except ValueError:
        return None


def limit_state(agent: str, model: str, account: dict | None, health: dict | None, threshold: float, ts: float,
                free: bool | None = None) -> dict:
    """Can `agent` (with `model`) take a turn now? {"ok", "reason", "resets_at"}. Pure."""
    from . import config as C
    lbl = C.AGENTS.get(agent, {}).get("label", agent)
    h = health or {}
    if health is not None and h.get("installed") is False:
        return {"ok": False, "reason": f"{lbl} is not installed", "resets_at": None, "kind": "unavailable"}
    if h.get("signed_in") is False:
        return {"ok": False, "reason": f"{lbl} is not signed in", "resets_at": None, "kind": "unavailable"}
    acc = account or {}
    rows = list(acc.get("plan") or []) + list(acc.get("usage") or [])
    for r in rows:
        lab, val = str(r.get("label") or ""), str(r.get("value") or "")
        if lab.lower() == "status" and re.search(r"not signed in|not logged in", val, re.I):
            return {"ok": False, "reason": f"{lbl} is not signed in", "resets_at": None, "kind": "unavailable"}
    blocked = None
    for w in acc.get("windows") or []:
        used = w.get("used")
        reset = w.get("resets_at")
        if reset and float(reset) <= ts:
            continue  # the window has reset since this reading
        if used is not None and float(used) >= threshold:
            cand = {"ok": False, "reason": f"{lbl} {str(w.get('label') or 'limit').lower()} at {round(float(used))}%",
                    "resets_at": float(reset) if reset else None, "kind": "limit"}
            # The longest wait decides: a weekly limit outlasts a 5-hour one.
            if not blocked or (cand["resets_at"] or 9e12) > (blocked["resets_at"] or 9e12):
                blocked = cand
    if blocked:
        return blocked
    for r in rows:
        lab, val = str(r.get("label") or ""), str(r.get("value") or "")
        if re.search(r"limit reached", lab, re.I) and val:
            return {"ok": False, "reason": f"{lbl}: limit reached ({val})", "resets_at": None, "kind": "limit"}
        if re.search(r"limit status", lab, re.I) and re.search(r"reject|block|exceed", val, re.I):
            return {"ok": False, "reason": f"{lbl}: {val}", "resets_at": None, "kind": "limit"}
        if re.search(r"balance|credits", lab, re.I) and not re.search(r"unlimited", val, re.I):
            amount = _money(val)
            if amount is not None and amount <= 0 and not (free if free is not None else _is_free_model(model)):
                return {"ok": False, "reason": f"{lbl} balance is {val.strip()} and {model or 'the default model'} is paid",
                        "resets_at": None, "kind": "balance"}
    return {"ok": True, "reason": "", "resets_at": None, "kind": ""}


def parse_fallback(entry) -> tuple[str, str]:
    """"agent" or "agent:model" (the model may itself contain colons, e.g. kilo/x/y:free)."""
    if isinstance(entry, dict):
        return (str(entry.get("agent") or "").strip(), str(entry.get("model") or "").strip())
    s = str(entry or "").strip()
    if ":" in s:
        a, m = s.split(":", 1)
        return a.strip(), m.strip()
    return s, ""


def plan_capacity(roles: dict, state_of, settings: dict) -> dict:
    """Decide how a task can start given each agent's state.

    roles:    the task's workflow roles {role: {agent, model, effort}}
    state_of: fn(agent, model) -> limit_state dict
    Returns {"ok": True, "roles": new_roles, "switches": [...]} or {"ok": False, "wait_until", "reason", "blocked": [...]}.
    """
    from . import config as C
    fallbacks = settings.get("fallbacks") or {}
    action = settings.get("limit_action") or "fallback"
    _state = state_of

    def state_of(agent, model, role):  # older callers take (agent, model) only
        try:
            return _state(agent, model, role)
        except TypeError:
            return _state(agent, model)
    new_roles = {r: dict(v or {}) for r, v in (roles or {}).items()}
    switches, blocked = [], []
    for role in C.ROLES:
        cur = new_roles.get(role) or {}
        agent = (cur.get("agent") or "").strip()
        if not agent:
            continue
        st = state_of(agent, (cur.get("model") or "").strip(), role)
        if st.get("ok"):
            continue
        chosen = None
        if action == "fallback":
            for entry in fallbacks.get(role) or []:
                fa, fm = parse_fallback(entry)
                if not fa or fa not in C.AGENTS or (fa == agent and fm == (cur.get("model") or "")):
                    continue
                if state_of(fa, fm, role).get("ok"):
                    chosen = (fa, fm)
                    break
        if chosen:
            new_roles[role] = {"agent": chosen[0], "model": chosen[1], "effort": cur.get("effort") if cur.get("effort") in (C.AGENTS[chosen[0]].get("efforts") or []) else ""}
            switches.append({"role": role, "from": {"agent": agent, "model": cur.get("model") or ""},
                             "to": {"agent": chosen[0], "model": chosen[1]}, "reason": st.get("reason")})
        else:
            blocked.append({"role": role, "agent": agent, **st})
    if blocked:
        resets = [b.get("resets_at") for b in blocked]
        until = None if any(r is None for r in resets) else max(resets)
        return {"ok": False, "wait_until": until, "reason": "; ".join(dict.fromkeys(b["reason"] for b in blocked)), "blocked": blocked}
    return {"ok": True, "roles": new_roles, "switches": switches}


# ============================================================================ estimates
def _dur(t: dict) -> float | None:
    try:
        a = datetime.fromisoformat(t["started_at"]).timestamp()
        b = datetime.fromisoformat(t["finished_at"]).timestamp()
        return b - a if b > a else None
    except (KeyError, TypeError, ValueError):
        return None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _team(t: dict) -> str:
    roles = (t.get("workflow") or {}).get("roles") or {}
    return "/".join((roles.get(r) or {}).get("agent") or "-" for r in ("supervisor", "worker", "reviewer"))


def estimate_seconds(t: dict, history: list) -> tuple[float, str]:
    """Median duration of delivered tasks like this one: same repository and team, then repository, then all."""
    done = [h for h in history if h.get("status") == "done" and h.get("id") != t.get("id")]
    for basis, pick in (("repository and team", lambda h: h.get("repo") == t.get("repo") and _team(h) == _team(t)),
                        ("repository", lambda h: h.get("repo") == t.get("repo")),
                        ("all delivered tasks", lambda h: True)):
        m = _median([_dur(h) for h in done if pick(h)])
        if m:
            return m, basis
    return ETA_DEFAULT_SECONDS, "default estimate"


def queue_eta(queued: list, running: list, history: list, parallel: int, start_ts: float) -> dict:
    """Start and finish estimates for queued tasks, filling `parallel` slots in queue order.

    Tasks that wait on a dependency start no earlier than that dependency's estimated finish.
    """
    slots = []
    finish = {}
    for r in running:
        est, _ = estimate_seconds(r, history)
        try:
            began = datetime.fromisoformat(r.get("started_at")).timestamp()
        except (TypeError, ValueError):
            began = start_ts
        end = max(start_ts + 60, began + est)
        slots.append(end)
        finish[r["id"]] = end
    parallel = max(1, int(parallel or 1))
    slots = sorted(slots)[:parallel] + [start_ts] * max(0, parallel - len(slots))
    out = {}
    for t in queued:
        est, basis = estimate_seconds(t, history)
        slots.sort()
        begin = slots[0]
        for dep in t.get("depends_on") or []:
            if dep in finish:
                begin = max(begin, finish[dep])
        end = begin + est
        slots[0] = end
        finish[t["id"]] = end
        out[t["id"]] = {"start": begin, "finish": end, "estimate_seconds": round(est), "basis": basis}
    return out


# ============================================================================ inbox
def _pending_tool_requests() -> int:
    """Agents' tool requests waiting for the owner (orchestrator/toolbox.py) also count as Needs you."""
    try:
        from . import toolbox
        return len(toolbox.requests("pending"))
    except Exception:
        return 0


def inbox_items(tasks: list, settings: dict | None = None) -> list[dict]:
    """Everything that waits for the owner, oldest first."""
    by_id = {t["id"]: t for t in tasks}
    items = []
    for t in tasks:
        if t.get("archived"):
            continue
        base = {"task_id": t["id"], "number": t.get("number"), "task": t.get("name"), "repo": t.get("github_repo") or Path(t.get("repo") or "").name,
                "status": t.get("status")}
        p = t.get("pending")
        if p and t.get("status") == "needs_input":
            kind = p.get("kind") or "question"
            if kind == "question" and p.get("from") == "orchestrator":
                kind = "escalation"
            dependents = [label(o) for o in tasks if t["id"] in (o.get("depends_on") or []) and o.get("status") == "queued"]
            items.append({**base, "id": p.get("id"), "kind": kind, "from": p.get("from"), "agent": p.get("agent"),
                          "question": p.get("question") or "", "options": list(p.get("options") or []),
                          "summary": p.get("summary") or "", "diffstat": p.get("diffstat") or "", "auto": p.get("auto") or "",
                          "design_md": p.get("design_md") or "", "design_version": p.get("design_version"),
                          "exploration": p.get("exploration") or None,
                          "time": p.get("time") or t.get("updated_at"), "blocks": dependents})
            continue
        ap = t.get("autopilot_parked") or {}
        if t.get("status") == "paused" and ap:
            items.append({**base, "id": f"park-{t['id']}", "kind": "parked", "question": ap.get("reason") or "Paused by autopilot",
                          "options": [], "time": ap.get("time") or t.get("updated_at"), "reason": ap.get("kind")})
            continue
        if t.get("status") == "queued" and t.get("depends_on"):
            failed = [b for b in dependency_blockers(t, by_id) if b["status"] in ("failed", "stopped")]
            if failed:
                items.append({**base, "id": f"dep-{t['id']}", "kind": "blocked", "time": t.get("updated_at"),
                              "question": f"Waits for {failed[0]['label']}, which {WAIT_WORD.get(failed[0]['status'])}.",
                              "blocker": failed[0], "options": []})
    return sorted(items, key=lambda x: x.get("time") or "")


# ============================================================================ digest
def one_line(text: str, n: int = 160) -> str:
    for line in str(text or "").splitlines():
        line = line.strip().lstrip("#*- ").strip()
        if line:
            return truncate(line, n)
    return ""


def _ts(iso) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


def build_digest(tasks: list, cards: dict, since_ts: float, now_ts: float, *, queue_order: list, running: list,
                 parallel: int, lessons_pending: list, settings: dict, status: dict | None = None) -> dict:
    """The digest for [since_ts, now_ts]. Pure: everything it needs is passed in."""
    live = [t for t in tasks if not t.get("archived")]
    delivered, failures = [], []
    for t in live:
        fin = _ts(t.get("finished_at"))
        if not fin or fin < since_ts:
            continue
        card = cards.get(t["id"]) or t.get("scorecard") or {}
        row = {"task_id": t["id"], "number": t.get("number"), "name": t.get("name"), "repo": t.get("github_repo") or Path(t.get("repo") or "").name,
               "finished_at": t.get("finished_at"), "duration_seconds": _dur(t), "cost_usd": round(task_cost(t), 4),
               "score": card.get("score"), "outcome": card.get("outcome"), "fallbacks": len(t.get("fallbacks") or []),
               "auto_retries": int(t.get("auto_retries") or 0)}
        if t.get("status") == "done":
            row.update(pr_url=t.get("pr_url"), pr_number=t.get("pr_number"), pr_state=(card.get("pr") or {}).get("state"),
                       summary=one_line(t.get("pr_summary") or t.get("summary") or t.get("requirements")),
                       follow_ups=len(t.get("follow_ups") or []))
            delivered.append(row)
        elif t.get("status") in ("failed", "stopped"):
            cat = "stopped" if t.get("status") == "stopped" else failure_category(t.get("error") or t.get("detail") or "")
            from .scorecard import FAILURE_LABEL
            row.update(category=cat, category_label=FAILURE_LABEL.get(cat) or ("Rate or usage limit" if cat == "rate_limit" else cat),
                       error=truncate(t.get("error") or t.get("detail") or "", 240),
                       infra=cat in INFRA_FAILURES, retried=int(t.get("auto_retries") or 0))
            failures.append(row)
    delivered.sort(key=lambda r: r["finished_at"] or "", reverse=True)
    failures.sort(key=lambda r: r["finished_at"] or "", reverse=True)
    spend = spend_since(live, since_ts)
    today = spend_since(live, day_start_ts(settings, now_ts))
    eta = queue_eta(queue_order, running, live, parallel, now_ts if not status or not status.get("until") else max(now_ts, float(status["until"])))
    next_up = []
    for t in queue_order[:12]:
        e = eta.get(t["id"]) or {}
        next_up.append({"task_id": t["id"], "number": t.get("number"), "name": t.get("name"),
                        "repo": t.get("github_repo") or Path(t.get("repo") or "").name, "priority": t.get("priority"),
                        "waiting": (t.get("waiting") or {}).get("text") or "", "eta_start": e.get("start"), "eta_finish": e.get("finish"),
                        "estimate_seconds": e.get("estimate_seconds"), "basis": e.get("basis")})
    inbox = inbox_items(live, settings)
    scores = [r["score"] for r in delivered if r.get("score") is not None]
    return {
        "since": datetime.fromtimestamp(since_ts).isoformat(timespec="seconds"),
        "until": datetime.fromtimestamp(now_ts).isoformat(timespec="seconds"),
        "hours": round((now_ts - since_ts) / 3600, 1),
        "headline": {"delivered": len(delivered), "needs_you": len(inbox), "failed": len([f for f in failures if f["category"] != "stopped"]),
                     "queued": len(queue_order), "running": len(running), "cost_usd": spend["cost_usd"],
                     "avg_score": round(sum(scores) / len(scores)) if scores else None},
        "delivered": delivered, "failures": failures, "inbox": inbox,
        "usage": {**spend, "today_usd": today["cost_usd"], "daily_cap_usd": float(settings.get("daily_cost_cap_usd") or 0)},
        "lessons": {"pending": len(lessons_pending), "items": [{"id": x.get("id"), "text": x.get("text"), "task_id": x.get("task_id"),
                                                                "task_name": x.get("task_name")} for x in lessons_pending[:5]]},
        "next": next_up,
        "running": [{"task_id": r["id"], "number": r.get("number"), "name": r.get("name"), "status": r.get("status"),
                     "detail": r.get("detail"), "eta_finish": None} for r in running],
    }


def summary_line(d: dict) -> str:
    h = d["headline"]
    parts = [f"{h['delivered']} delivered", f"{h['needs_you']} need you" if h["needs_you"] else "nothing waits for you",
             f"{h['failed']} failed" if h["failed"] else "", f"{h['queued']} queued" if h["queued"] else "",
             f"~${h['cost_usd']:.2f} spent" if h["cost_usd"] else ""]
    return " · ".join(p for p in parts if p)


# ============================================================================ the stateful part
STATE_FILE = STATE_DIR / "autopilot.json"


class Autopilot:
    """Hooks the manager calls, a background tick (watchdog, quiet hours, digest), and the views."""

    def __init__(self, manager):
        self.m = manager
        self._account_fn = None   # tests replace these two
        self._health_fn = None
        self._acc_cache: dict[str, tuple[float, dict]] = {}
        self._acc_lock = threading.Lock()
        self._refreshing: set = set()
        self._status_sig = None
        self._suspect: dict[str, float] = {}
        self.recovered: list[dict] = []
        self.last_tick = None
        self.limit_wait: dict | None = None
        self._thread = None
        self._state = read_json(STATE_FILE, {}) or {}

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        raw = self.m.cfg().get("autopilot") or {}
        out = json.loads(json.dumps(DEFAULTS))
        for k, v in raw.items():
            if isinstance(out.get(k), dict) and isinstance(v, dict):
                out[k].update(v)
            else:
                out[k] = v
        return out

    def now_local(self, ts=None) -> datetime:
        return local_now(self.settings(), ts)

    def quiet(self) -> bool:
        return quiet_now(self.now_local(), self.settings())

    # ------------------------------------------------------------ gate
    def gate(self, rows=None) -> dict:
        """Whether new tasks may start now, and if not why and until when."""
        s = self.settings()
        ts = time.time()
        if s.get("paused"):
            return {"ok": False, "state": "paused", "label": "Autopilot paused", "until": None}
        dt = local_now(s, ts)
        if not schedule_open(dt, s):
            nxt = next_open(dt, s)
            return {"ok": False, "state": "outside_window", "label": "Outside the run window",
                    "until": nxt.timestamp() if nxt else None}
        cap = float(s.get("daily_cost_cap_usd") or 0)
        if cap > 0:
            spent = spend_since(rows if rows is not None else self.m.store.list(), day_start_ts(s, ts))["cost_usd"]
            if spent >= cap:
                tomorrow = local_now(s, ts).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                return {"ok": False, "state": "cost_cap", "label": f"Daily cost cap reached (~${spent:.2f} of ${cap:.2f})",
                        "until": tomorrow.timestamp()}
        return {"ok": True, "state": "running", "label": "Running", "until": None}

    # ------------------------------------------------------------ limits
    def _account(self, agent: str) -> dict:
        if self._account_fn:
            return self._account_fn(agent)
        hit = self._acc_cache.get(agent)
        if hit:
            if time.time() - hit[0] > 120:
                # Reading an account runs CLIs; the scheduler keeps the last reading and refreshes in the background.
                self._refresh_async(agent)
            return hit[1]
        return self.refresh_account(agent)

    def refresh_account(self, agent: str, force: bool = False) -> dict:
        from . import agent_info
        try:
            acc = agent_info.account(agent, self.m.cfg(), None, refresh=force)
        except Exception as e:  # an unreadable account never blocks a task
            acc = {"error": str(e)}
        self._acc_cache[agent] = (time.time(), acc)
        return acc

    def _refresh_async(self, agent: str):
        with self._acc_lock:
            if agent in self._refreshing:
                return
            self._refreshing.add(agent)

        def run():
            try:
                self.refresh_account(agent)
            finally:
                with self._acc_lock:
                    self._refreshing.discard(agent)
        threading.Thread(target=run, daemon=True).start()

    def refresh_limits(self) -> list[dict]:
        """Read every used agent's account again, bypassing caches (the Digest page's "Check limits")."""
        from . import agents
        agents.invalidate_health()
        rows = self.m.store.list()
        for a in {r.get("agent") for t in rows for r in ((t.get("workflow") or {}).get("roles") or {}).values() if (r or {}).get("agent")}:
            self.refresh_account(a, force=True)
        return self.limits_snapshot(rows)

    def _health(self, agent: str) -> dict | None:
        if self._health_fn:
            return self._health_fn(agent)
        from . import agents
        try:
            return (agents.agent_health(self.m.cfg()) or {}).get(agent)
        except Exception:
            return None

    def resolve_model(self, role: str, agent: str, model: str) -> str:
        """The model the pipeline will really run (Pipeline.role_agent): the role's own, else the global
        role's when it uses the same agent, else the agent's default model from settings."""
        if (model or "").strip():
            return model.strip()
        cfg = self.m.cfg()
        glob = (cfg.get("roles") or {}).get(role) or {}
        if (glob.get("agent") or "").strip() == agent and (glob.get("model") or "").strip():
            return glob["model"].strip()
        return (((cfg.get("agent_defaults") or {}).get(agent) or {}).get("model") or "").strip()

    def model_is_free(self, agent: str, model: str) -> bool:
        """Free by name (…:free) or by the agent's own model list (Models & usage), which knows the prices."""
        if _is_free_model(model):
            return True
        if not model:
            return False
        try:
            from . import agent_info
            rows = (agent_info.models(agent, self.m.cfg()) or {}).get("models") or []
        except Exception:
            return False
        return any(r.get("id") == model and (r.get("free") or r.get("included")) for r in rows)

    def agent_state(self, agent: str, model: str = "", role: str = "") -> dict:
        s = self.settings()
        model = self.resolve_model(role, agent, model) if role else model
        free = self.model_is_free(agent, model)
        return limit_state(agent, model, self._account(agent), self._health(agent),
                           float(s.get("limit_threshold_percent") or 90), time.time(), free=free)

    def capacity(self, t: dict) -> dict:
        s = self.settings()
        if not s.get("limit_check", True):
            return {"ok": True, "roles": None, "switches": []}
        cache: dict = {}

        def state_of(agent, model, role=""):
            key = (agent, model, role)
            if key not in cache:
                cache[key] = self.agent_state(agent, model, role)
            return cache[key]
        return plan_capacity((t.get("workflow") or {}).get("roles") or {}, state_of, s)

    # ------------------------------------------------------------ scheduling hook
    def occupies_slot(self, t: dict) -> bool:
        """Does a task count against max_parallel? Parked tasks (waiting for you) do not."""
        busy = t["id"] in self.m.runners or t.get("status") in _active()
        if not busy:
            return False
        if self.settings().get("park_waiting_tasks", True) and t.get("status") == "needs_input":
            return False
        if t.get("status") == "paused" and t.get("autopilot_parked"):
            return False
        return True

    def set_waiting(self, t: dict, waiting: dict | None):
        """Record why a queued task is not starting; writes only when the reason changes."""
        old = t.get("waiting") or None
        sig = lambda w: (w or {}).get("text"),
        if (old or {}).get("text") == (waiting or {}).get("text") and (old or {}).get("until") == (waiting or {}).get("until"):
            return
        patch = {"waiting": waiting}
        if waiting:
            patch["detail"] = waiting["text"]
        elif old:
            patch["detail"] = "Waiting in queue"
        self.m.store.update(t["id"], touch=False, **patch)
        self.m.emit_task(t["id"])

    def schedule(self):
        """One pass of the queue: called by Manager._loop every half second."""
        m = self.m
        rows = m.store.list()
        by_id = {t["id"]: t for t in rows}
        gate = self.gate(rows)
        self._gate = gate
        active = sum(1 for t in rows if self.occupies_slot(t))
        queued = m.queue_order(rows)
        room = max(0, m.max_parallel - active)
        limit_waits = []
        for t in queued:
            blockers = dependency_blockers(t, by_id)
            if blockers:
                self.set_waiting(t, {"kind": "dependency", "text": waiting_text(blockers), "on": [b["id"] for b in blockers]})
                continue
            chain = m.chain_blocker(t, rows, queued)
            if chain:
                self.set_waiting(t, {"kind": "chain", "text": waiting_text([{**chain, "label": label(chain)}]), "on": [chain["id"]]})
                continue
            if not gate["ok"]:
                self.set_waiting(t, {"kind": gate["state"], "text": gate["label"], "until": gate.get("until")})
                continue
            if room <= 0:
                self.set_waiting(t, None)
                continue
            try:
                # Learning: pick the team from past outcomes when that is switched on (learning_engine.auto_pick).
                if m.learning.engine.auto_pick(t["id"]):
                    t = m.store.get(t["id"]) or t
            except Exception:
                log.exception("auto-picking the team of %s failed", t["id"])
            cap = self.capacity(t)
            if not cap["ok"]:
                until = cap.get("wait_until")
                txt = f"Waiting for limits: {cap['reason']}" + (f" · until {datetime.fromtimestamp(until).strftime('%H:%M')}" if until else "")
                self.set_waiting(t, {"kind": "limits", "text": txt, "until": until, "reason": cap["reason"]})
                limit_waits.append({"task_id": t["id"], "until": until, "reason": cap["reason"]})
                if not until and not (t.get("waiting") or {}).get("notified"):
                    # Nothing will clear this on its own (no balance, not signed in, not installed): say so once
                    # instead of leaving a queued task silently parked.
                    m.notify("warning", "Queued task cannot start", f"{t.get('name', '')}: {cap['reason']}. Change the team, "
                             "set a fallback agent (Settings → Autopilot) or fix the agent, and it starts on its own.", t["id"], kind="needs_input")
                    m.store.update(t["id"], touch=False, waiting={**(m.store.get(t["id"]) or {}).get("waiting", {}), "notified": True})
                continue
            if cap.get("switches"):
                self.apply_fallbacks(t["id"], cap)
            self.set_waiting(t, None)
            m.launch(t["id"])
            room -= 1
            rows = m.store.list()
            by_id = {x["id"]: x for x in rows}
        self.limit_wait = min(limit_waits, key=lambda w: w["until"] or 9e12) if limit_waits else None
        self.emit_status()

    def apply_fallbacks(self, tid: str, cap: dict):
        t = self.m.store.get(tid) or {}
        wf = dict(t.get("workflow") or {})
        wf["roles"] = cap["roles"]
        rows = list(t.get("fallbacks") or [])
        from . import config as C
        for sw in cap["switches"]:
            rows.append({"time": now(), **sw})
            fr = C.AGENTS.get(sw["from"]["agent"], {}).get("label", sw["from"]["agent"])
            to = C.AGENTS.get(sw["to"]["agent"], {}).get("label", sw["to"]["agent"]) + (f" ({sw['to']['model']})" if sw["to"]["model"] else "")
            self.m.timeline(tid, "system", f"Autopilot switched the {sw['role']}", f"{fr} → {to}: {sw['reason']}")
        self.m.store.update(tid, immediate=True, workflow=wf, fallbacks=rows[-20:])
        self.m.notify("info", "Switched to a fallback agent", f"{label(t)}: " + "; ".join(
            f"{s['role']} → {s['to']['agent']}" for s in cap["switches"]), tid, kind="info")

    # ------------------------------------------------------------ run hooks
    def after_run(self, tid: str):
        """Called when a run ends. Retries infrastructure failures; tells you about dependents left waiting."""
        t = self.m.store.get(tid)
        if not t or self.m.store.is_deleted(tid):
            return
        s = self.settings()
        if t.get("status") == "failed":
            retry, cat = should_auto_retry(t, s)
            if retry:
                n = int(t.get("auto_retries") or 0) + 1
                self.m.store.update(tid, immediate=True, auto_retries=n)
                self.m.timeline(tid, "system", "Autopilot retry", f"{cat.replace('_', ' ')}: resuming from the checkpoint ({n}/{retry_allowance(t, s)})")
                try:
                    self.m.retry(tid, fresh=False, start_queue=False)
                    self.m.store.update(tid, immediate=True, detail=f"Queued · automatic retry {n} after {cat.replace('_', ' ')}")
                    self.m.emit_task(tid)
                    return
                except Exception as e:
                    log.warning("auto retry of %s failed: %s", tid, e)
        if t.get("status") in ("failed", "stopped"):
            waiting = [o for o in self.m.store.list() if tid in (o.get("depends_on") or []) and o.get("status") == "queued"]
            if waiting:
                self.m.notify("warning", "Dependent tasks are waiting", f"{label(t)} {t.get('status')}; {len(waiting)} task(s) wait for it: "
                              + ", ".join(label(o) for o in waiting[:3]), tid, kind="failed")

    def on_cost(self, tid: str):
        """After every turn: pause a task that went over its cost cap (after the current turn)."""
        t = self.m.store.get(tid) or {}
        cap = float((t.get("cost_cap_usd") or 0) or self.settings().get("task_cost_cap_usd") or 0)
        if cap <= 0 or t.get("autopilot_parked") or t.get("cost_cap_ack", 0) >= cap:
            return
        spent = task_cost(t)
        if spent < cap:
            return
        r = self.m.runners.get(tid)
        if not r:
            return
        r.pause()
        reason = f"Estimated cost ~${spent:.2f} reached the task cap of ${cap:.2f}. Resume to continue, or stop it."
        self.m.store.update(tid, immediate=True, pause_requested=True,
                            autopilot_parked={"kind": "cost_cap", "reason": reason, "time": now(), "cap": cap})
        self.m.timeline(tid, "system", "Cost cap reached", reason)
        self.m.notify("warning", "Task paused at its cost cap", f"{label(t)} · ~${spent:.2f}", tid, kind="needs_input")
        self.m.emit_task(tid)

    def resume_parked(self, tid: str):
        t = self.m.store.get(tid) or {}
        ap = t.get("autopilot_parked") or {}
        patch = {"autopilot_parked": None}
        if ap.get("kind") == "cost_cap":
            patch["cost_cap_ack"] = max(float(ap.get("cap") or 0), task_cost(t))  # do not pause again at the same cap
        self.m.store.update(tid, immediate=True, **patch)

    # ------------------------------------------------------------ pause everything
    def set_paused(self, paused: bool):
        from . import config as C
        cur = dict(self.m.cfg().get("autopilot") or {})
        cur["paused"] = bool(paused)
        C.update({"autopilot": cur})
        self.m.config_changed()
        if paused:
            for tid, r in list(self.m.runners.items()):
                t = self.m.store.get(tid) or {}
                if t.get("status") in _active():
                    r.pause()
                    self.m.store.update(tid, immediate=True, pause_requested=True,
                                        autopilot_parked={"kind": "autopilot_paused", "reason": "Autopilot paused", "time": now()})
                    self.m.timeline(tid, "user", "Autopilot paused", "Stops after the current agent turn")
                    self.m.emit_task(tid)
        else:
            for tid, r in list(self.m.runners.items()):
                t = self.m.store.get(tid) or {}
                if (t.get("autopilot_parked") or {}).get("kind") == "autopilot_paused":
                    self.m.store.update(tid, immediate=True, autopilot_parked=None)
                    self.m.resume(tid)
            if not self.m.scheduler:
                self.m.start()
        self.emit_status(force=True)
        return self.status()

    # ------------------------------------------------------------ background tick
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="relay-autopilot", daemon=True)
        self._thread.start()

    def _loop(self):
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("autopilot tick failed")
            time.sleep(15)

    def ensure_queue_running(self) -> bool:
        """Queued work must never sit behind a halted queue: start the scheduler when something is queued,
        unless the owner paused the autopilot (the one explicit "don't start anything" switch)."""
        if self.settings().get("paused") or self.m.scheduler:
            return False
        if not any(t.get("status") == "queued" for t in self.m.store.list()):
            return False
        log.info("queued task found while the queue was halted: starting the queue")
        self.m.start()
        return True

    def tick(self):
        self.last_tick = time.time()
        s = self.settings()
        try:
            self.ensure_queue_running()
        except Exception:
            log.exception("could not start the queue")
        if s.get("watchdog", True):
            self.watchdog(s)
        if quiet_now(local_now(s), s):
            self.quiet_answers()
        self.maybe_digest(s)
        self.emit_status()

    def watchdog(self, s: dict):
        m = self.m
        ts = time.time()
        silent_limit = float(s.get("watchdog_silent_minutes") or 20) * 60
        for tid, r in list(m.runners.items()):
            th = getattr(r, "thread", None)
            if th is not None and not th.is_alive():
                # The runner thread ended without its cleanup: nothing will ever move this task again.
                if self._confirm(("dead", tid), ts):
                    m.runners.pop(tid, None)
                    m.process_state[tid] = {"state": "idle"}
                    self.recover(tid, "The run's worker thread ended unexpectedly")
                continue
            cur = getattr(r, "current", {}) or {}
            pid = cur.get("pid")
            if cur.get("state") == "running" and pid and not _pid_alive(pid):
                since = float((m.process_state.get(tid) or {}).get("silent_for") or 0)
                started = float(cur.get("started_at") or ts)
                if ts - started > 60 and (since > silent_limit or ts - started > silent_limit) and self._confirm(("gone", tid), ts):
                    # The CLI is gone but its reader still waits (a grandchild holds the pipe): interrupt so the turn restarts.
                    m.timeline(tid, "system", "Watchdog: agent process vanished", f"PID {pid} is gone; restarting the turn")
                    r.interrupt("The agent process stopped responding and was restarted by Relay's watchdog. Check the working tree and continue.")
                    self._record(tid, "process_gone")
        for t in m.store.list():
            if t.get("status") in _active() and t["id"] not in m.runners:
                if self._confirm(("orphan", t["id"]), ts):
                    self.recover(t["id"], "The task was marked running but no runner owned it")
            else:
                self._suspect.pop(("orphan", t["id"]), None)

    def _confirm(self, key, ts, seconds=20) -> bool:
        """A condition must hold on two ticks at least `seconds` apart before the watchdog acts."""
        first = self._suspect.setdefault(key, ts)
        if ts - first >= seconds:
            self._suspect.pop(key, None)
            return True
        return False

    def _record(self, tid, kind):
        self.recovered.insert(0, {"task_id": tid, "kind": kind, "time": now()})
        self.recovered = self.recovered[:20]

    def recover(self, tid: str, why: str):
        m = self.m
        t = m.store.get(tid) or {}
        has_cp = bool(t.get("checkpoint") and t.get("worktree") and Path(t.get("worktree") or "").exists())
        m.store.update(tid, immediate=True, status="interrupted", detail=f"Watchdog: {why}", pending=None)
        m.timeline(tid, "system", "Watchdog recovered the task", why + ("; resuming from the checkpoint" if has_cp else "; starting over"))
        self._record(tid, "orphan")
        try:
            m.retry(tid, fresh=not has_cp, start_queue=False)
        except Exception as e:
            log.warning("watchdog could not requeue %s: %s", tid, e)
        m.notify("warning", "Watchdog resumed a stuck task", f"{label(t)}: {why}", tid, kind="info")

    def quiet_answers(self):
        """During quiet hours a judge escalation that is already waiting takes its automatic choice."""
        for t in self.m.store.list():
            p = t.get("pending") or {}
            if t.get("status") != "needs_input" or p.get("from") != "orchestrator" or not p.get("auto"):
                continue
            r = self.m.runners.get(t["id"])
            if r:
                self.m.timeline(t["id"], "system", "Quiet hours", f"Taking the automatic choice: {p['auto']}")
                r.answer(p.get("id"), p["auto"], {"auto": True})

    def maybe_digest(self, s: dict):
        at = s.get("digest_time") or ""
        if not at:
            return
        dt = local_now(s)
        today = dt.date().isoformat()
        if self._state.get("digest_sent") == today or dt.hour * 60 + dt.minute < _minutes(at, 8 * 60):
            return
        self._state["digest_sent"] = today
        try:
            write_json(STATE_FILE, self._state)
        except Exception:
            pass
        d = self.digest(hours=float(s.get("digest_hours") or 24))
        self.m.notify("info", "Your digest is ready", summary_line(d), None, kind="digest")

    # ------------------------------------------------------------ views
    def status(self) -> dict:
        m = self.m
        rows = m.store.list()
        s = self.settings()
        gate = self.gate(rows)
        running = [t for t in rows if t["id"] in m.runners and t.get("status") in _active()]
        parked = [t for t in rows if t.get("status") == "needs_input" or (t.get("status") == "paused" and t.get("autopilot_parked"))]
        queued = m.queue_order(rows)
        waiting_dep = [t for t in queued if (t.get("waiting") or {}).get("kind") in ("dependency", "chain")]
        state, lbl, until = gate["state"], gate["label"], gate.get("until")
        if not m.scheduler:
            state, lbl, until = "halted", "Queue halted", None
        elif gate["ok"] and self.limit_wait and not running:
            state, until = "waiting_limits", self.limit_wait.get("until")
            lbl = "Waiting for limits" + (f" until {datetime.fromtimestamp(until).strftime('%H:%M')}" if until else "")
        elif gate["ok"] and not running and not queued:
            state, lbl = "idle", "Idle · nothing queued"
        ts = time.time()
        close = window_close(local_now(s, ts), s) if gate["ok"] else None
        return {
            "state": state, "label": lbl, "until": until, "until_text": datetime.fromtimestamp(until).strftime("%a %H:%M") if until else "",
            "paused": bool(s.get("paused")), "queue_running": bool(m.scheduler), "max_parallel": m.max_parallel,
            "running": len(running), "parked": len(parked), "queued": len(queued), "waiting_dependencies": len(waiting_dep),
            "needs_you": len(inbox_items(rows, s)) + _pending_tool_requests(), "quiet_hours": quiet_now(local_now(s, ts), s),
            "window_closes": close.timestamp() if close else None,
            "limit_wait": self.limit_wait,
            "today_cost_usd": spend_since(rows, day_start_ts(s, ts))["cost_usd"], "daily_cap_usd": float(s.get("daily_cost_cap_usd") or 0),
            "watchdog": {"enabled": bool(s.get("watchdog", True)), "last_tick": self.last_tick, "recovered": self.recovered[:5]},
        }

    def emit_status(self, force=False):
        if not force and time.time() - getattr(self, "_status_at", 0) < 3:
            return
        self._status_at = time.time()
        st = self.status()
        sig = json.dumps({k: v for k, v in st.items() if k not in ("watchdog", "today_cost_usd")}, sort_keys=True, default=str)
        if force or sig != self._status_sig:
            self._status_sig = sig
            self.m.emit("autopilot", st)

    def inbox(self) -> list[dict]:
        return inbox_items(self.m.store.list(), self.settings())

    def digest(self, hours: float | None = None, since: str | None = None) -> dict:
        from . import lessons
        s = self.settings()
        ts = time.time()
        since_ts = _ts(since) if since else ts - float(hours or s.get("digest_hours") or 24) * 3600
        rows = self.m.store.list()
        cards = {}
        try:
            cards = {c.get("task_id"): c for c in self.m.learning.cards.all()}
        except Exception:
            pass
        try:
            pending = lessons.listing().get("queue") or []
        except Exception:
            pending = []
        running = [t for t in rows if t.get("status") in _active()]  # parked tasks are not running
        st = self.status()
        d = build_digest(rows, cards, since_ts, ts, queue_order=self.m.queue_order(rows), running=running,
                         parallel=self.m.max_parallel, lessons_pending=pending, settings=s,
                         status={"until": st["until"]} if st["state"] in ("outside_window", "waiting_limits", "cost_cap") else None)
        d["status"] = st
        d["summary"] = summary_line(d)
        d["limits"] = self.limits_snapshot(rows)
        return d

    def limits_snapshot(self, rows) -> list[dict]:
        """Agents the queue uses, with their current limit state (from cached readings only)."""
        used = set()
        for t in rows:
            if t.get("status") in ("queued",) or t["id"] in self.m.runners:
                for r in ((t.get("workflow") or {}).get("roles") or {}).values():
                    if (r or {}).get("agent"):
                        used.add(r["agent"])
        out = []
        for a in sorted(used):
            hit = self._acc_cache.get(a)
            if not hit and not self._account_fn:
                out.append({"agent": a, "ok": None, "reason": "no reading yet"})
                continue
            st = self.agent_state(a, "")
            out.append({"agent": a, **st})
        return out


def _active():
    from .store import ACTIVE
    return ACTIVE


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, ValueError):
        return True
