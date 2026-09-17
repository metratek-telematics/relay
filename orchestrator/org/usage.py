"""Usage and budgets: what each project and person spends, from the per-turn metrics every task records.

Each task keeps a compact log of its agent turns (metrics.log: start, end, agent, role, tokens, cost). Those
turns are bucketed by day, project (the task's project) and person (who created the task). Monthly budgets
exist for the organisation, each project and each person; crossing an alert threshold (50/80/100 % by
default) raises one notification per threshold per month. A project with a hard cap refuses new tasks once
its month is spent.
"""
from __future__ import annotations

import calendar
import threading
import time
from datetime import datetime

from . import identity, projects, settings as OS
from .common import JsonStore, now_iso, parse_iso

_alerts = JsonStore("budget_alerts.json", {"sent": {}})


def month_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m")


def month_bounds(month: str) -> tuple[float, float, int]:
    y, m = [int(x) for x in month.split("-")]
    days = calendar.monthrange(y, m)[1]
    start = datetime(y, m, 1).timestamp()
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1).timestamp()
    return start, end, days


def turns(tasks: list[dict]):
    """Every recorded agent turn as (task, turn) with a timestamp; older tasks without a turn log count once at their end."""
    ids = {p["id"] for p in projects.all_projects()}
    for t in tasks:
        pid = projects.project_of_task(t, ids)
        who = t.get("created_by") or "unattributed"
        m = t.get("metrics") or {}
        log = m.get("log") or []
        if log:
            for x in log:
                yield {"ts": float(x.get("end") or 0), "project": pid, "user": who, "task_id": t["id"], "agent": x.get("agent") or "?",
                       "role": x.get("role") or "?", "cost": float(x.get("cost_usd") or 0), "input": int(x.get("input") or 0),
                       "output": int(x.get("output") or 0), "cached": int(x.get("cached") or 0),
                       "seconds": max(0.0, float(x.get("end") or 0) - float(x.get("start") or 0)), "estimated": bool(x.get("estimated"))}
        elif (m.get("total") or {}).get("turns"):
            tot = m["total"]
            ts = parse_iso(t.get("finished_at") or t.get("updated_at") or t.get("created_at"))
            yield {"ts": ts, "project": pid, "user": who, "task_id": t["id"], "agent": next(iter(m.get("agents") or {"?": 0})), "role": "?",
                   "cost": float(tot.get("cost_usd") or 0), "input": int(tot.get("input") or 0), "output": int(tot.get("output") or 0),
                   "cached": 0, "seconds": float(tot.get("seconds") or 0), "estimated": bool(tot.get("estimated"))}


def _blank():
    return {"cost_usd": 0.0, "input": 0, "output": 0, "cached": 0, "seconds": 0.0, "turns": 0, "tasks": set(), "estimated": False}


def _add(b, x):
    b["cost_usd"] += x["cost"]
    b["input"] += x["input"]
    b["output"] += x["output"]
    b["cached"] += x["cached"]
    b["seconds"] += x["seconds"]
    b["turns"] += 1
    b["tasks"].add(x["task_id"])
    b["estimated"] = b["estimated"] or x["estimated"]


def _out(b):
    return {**{k: (round(v, 4) if isinstance(v, float) else v) for k, v in b.items() if k != "tasks"}, "tasks": len(b["tasks"])}


def spend(tasks, month: str, project: str | None = None, user: str | None = None) -> float:
    lo, hi, _ = month_bounds(month)
    return round(sum(x["cost"] for x in turns(tasks) if lo <= x["ts"] < hi and (not project or x["project"] == project) and (not user or x["user"] == user)), 4)


def summary(tasks: list[dict], month: str | None = None, project: str | None = None, user: str | None = None) -> dict:
    month = month or month_key()
    lo, hi, days = month_bounds(month)
    total, by_project, by_user, by_agent, by_role = _blank(), {}, {}, {}, {}
    daily = [{"day": d + 1, "cost_usd": 0.0, "tokens": 0, "by_project": {}} for d in range(days)]
    tasks_cost = {}
    months = {}
    for x in turns(tasks):
        mk = month_key(x["ts"]) if x["ts"] else None
        if mk and (not project or x["project"] == project) and (not user or x["user"] == user):
            months[mk] = round(months.get(mk, 0.0) + x["cost"], 4)
        if not (lo <= x["ts"] < hi):
            continue
        if project and x["project"] != project:
            continue
        if user and x["user"] != user:
            continue
        _add(total, x)
        _add(by_project.setdefault(x["project"], _blank()), x)
        _add(by_user.setdefault(x["user"], _blank()), x)
        _add(by_agent.setdefault(x["agent"], _blank()), x)
        _add(by_role.setdefault(x["role"], _blank()), x)
        d = daily[datetime.fromtimestamp(x["ts"]).day - 1]
        d["cost_usd"] = round(d["cost_usd"] + x["cost"], 4)
        d["tokens"] += x["input"] + x["output"]
        d["by_project"][x["project"]] = round(d["by_project"].get(x["project"], 0) + x["cost"], 4)
        tasks_cost[x["task_id"]] = tasks_cost.get(x["task_id"], 0.0) + x["cost"]
    by_id = {t["id"]: t for t in tasks}
    top = sorted(tasks_cost.items(), key=lambda kv: -kv[1])[:10]
    now = time.time()
    elapsed_days = days if now >= hi else max(1.0, (now - lo) / 86400) if now > lo else 0
    forecast = round(total["cost_usd"] / elapsed_days * days, 2) if elapsed_days else 0.0
    plist = {p["id"]: p for p in projects.all_projects()}
    people = {u["username"]: u for u in identity.users()}
    org_budget = float(OS.load()["budgets"].get("org_monthly_usd") or 0)
    budgets = []
    if org_budget and not project and not user:
        budgets.append(_meter("organisation", "org", "Organisation", org_budget, total["cost_usd"], forecast))
    for pid, p in plist.items():
        if project and pid != project:
            continue
        if p["budget"]["monthly_usd"]:
            spent = _out(by_project.get(pid, _blank()))["cost_usd"]
            f = round(spent / elapsed_days * days, 2) if elapsed_days else 0
            budgets.append({**_meter("project", pid, p["name"], p["budget"]["monthly_usd"], spent, f), "hard_cap": p["budget"]["hard_cap"], "color": p["color"]})
    for un, u in people.items():
        if user and un != user:
            continue
        if float(u.get("budget_monthly_usd") or 0):
            spent = _out(by_user.get(un, _blank()))["cost_usd"]
            f = round(spent / elapsed_days * days, 2) if elapsed_days else 0
            budgets.append(_meter("user", un, u.get("name") or un, float(u["budget_monthly_usd"]), spent, f))
    return {
        "month": month, "days": days, "total": _out(total), "forecast_usd": forecast,
        "daily": daily,
        "by_project": [{"id": k, "name": (plist.get(k) or {}).get("name", k), "color": (plist.get(k) or {}).get("color"), **_out(v)}
                       for k, v in sorted(by_project.items(), key=lambda kv: -kv[1]["cost_usd"])],
        "by_user": [{"username": k, "name": (people.get(k) or {}).get("name") or k, "initials": identity.initials((people.get(k) or {}).get("name") or "", k),
                     "color": identity.avatar_color(k), **_out(v)} for k, v in sorted(by_user.items(), key=lambda kv: -kv[1]["cost_usd"])],
        "by_agent": [{"agent": k, **_out(v)} for k, v in sorted(by_agent.items(), key=lambda kv: -kv[1]["cost_usd"])],
        "by_role": [{"role": k, **_out(v)} for k, v in sorted(by_role.items(), key=lambda kv: -kv[1]["cost_usd"])],
        "top_tasks": [{"task_id": k, "name": (by_id.get(k) or {}).get("name"), "number": (by_id.get(k) or {}).get("number"), "cost_usd": round(c, 4),
                       "project": projects.project_of_task(by_id[k]) if k in by_id else None} for k, c in top],
        "months": [{"month": k, "cost_usd": v} for k, v in sorted(months.items())[-12:]],
        "budgets": budgets,
    }


def _meter(kind, key, label, budget, spent, forecast):
    pct = round(spent / budget * 100, 1) if budget else 0
    return {"kind": kind, "key": key, "label": label, "budget_usd": budget, "spent_usd": round(spent, 2), "pct": pct,
            "forecast_usd": forecast, "forecast_pct": round(forecast / budget * 100, 1) if budget else 0,
            "state": "over" if pct >= 100 else "warn" if pct >= 80 else "ok"}


def over_hard_cap(tasks, pid: str) -> tuple[bool, str]:
    p = projects.get(pid)
    if not p or not p["budget"]["hard_cap"] or not p["budget"]["monthly_usd"]:
        return False, ""
    s = spend(tasks, month_key(), project=p["id"])
    if s >= p["budget"]["monthly_usd"]:
        return True, f"{p['name']} has spent ~${s:.2f} of its ${p['budget']['monthly_usd']:.2f} monthly budget. An owner can raise the budget or lift the hard cap."
    return False, ""


def check_alerts(tasks, notify) -> list[str]:
    """Raise each crossed threshold once per month. notify(level, title, body)."""
    month = month_key()
    thresholds = OS.load()["budgets"].get("alert_thresholds") or [80, 100]
    data = summary(tasks, month)
    sent = _alerts.read().get("sent") or {}
    fired = []
    for b in data["budgets"]:
        crossed = [th for th in thresholds if b["pct"] >= th]
        if not crossed:
            continue
        th = max(crossed)
        key = f"{month}:{b['kind']}:{b['key']}:{th}"
        if key in sent:
            continue
        sent[key] = now_iso()
        fired.append(key)
        level = "error" if th >= 100 else "warning"
        what = {"organisation": "The organisation", "project": f"Project {b['label']}", "user": b["label"]}[b["kind"]]
        notify(level, f"Budget {th}% reached", f"{what} has spent ~${b['spent_usd']:.2f} of ${b['budget_usd']:.2f} this month"
               f" (forecast ~${b['forecast_usd']:.2f}).")
    if fired:
        _alerts.write({"sent": {k: v for k, v in sent.items() if k.startswith(month)}})
    return fired


class Watch:
    """Checks budgets every few minutes in the background."""

    def __init__(self, manager, every=300):
        self.m, self.every = manager, every
        threading.Thread(target=self._loop, name="relay-budgets", daemon=True).start()

    def _loop(self):
        time.sleep(20)
        while True:
            try:
                check_alerts(self.m.store.list(), lambda lv, title, body: self.m.notify(lv, title, body, None, kind="budget"))
            except Exception:
                pass
            time.sleep(self.every)
