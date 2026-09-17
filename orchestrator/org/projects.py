"""Projects: workspaces that group repositories, connectors, stacks, lessons and tasks ("Navitrak", "Optimax").

A task belongs to the project it was created in (task["project_id"]); older tasks, and tasks created
without one, belong to the project that lists their repository, else to the default project. Connectors
and stacks listed by a project belong to it, as do stacks whose services build a project repository.
Existing installations migrate into one default project holding everything they already have.
"""
from __future__ import annotations

import re
from pathlib import Path

from .common import JsonStore, now_iso
from ..util import new_id

DEFAULT_ID = "default"
COLORS = ["#d97757", "#4f6fd0", "#3a8f62", "#8a5fc7", "#b9842d", "#c5473f", "#2f9d87", "#5d78d6"]

_store = JsonStore("projects.json", {"version": 1, "projects": []})

PROJECT_DEFAULTS = {
    "preset": "",                 # team preset for new tasks ("" = the global default team)
    "cost_cap_usd": 0,            # per-task autopilot cost cap for new tasks (0 = none)
    "approval_before_delivery": None,  # None = follow settings; True/False = force for this project's tasks
    "reviewers": [],              # usernames asked to approve; approvals notify them first
    "max_turns": 0,
}


def _key(path) -> str:
    try:
        return str(Path(str(path)).expanduser().resolve())
    except Exception:
        return str(path or "")


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "project"


def _normalize(p: dict) -> dict:
    p = dict(p)
    p.setdefault("description", "")
    p.setdefault("color", COLORS[sum(map(ord, p.get("id", ""))) % len(COLORS)])
    p["repos"] = list(dict.fromkeys(_key(r) for r in p.get("repos") or [] if str(r).strip()))
    p["connectors"] = list(dict.fromkeys(str(c) for c in p.get("connectors") or [] if str(c).strip()))
    p["stacks"] = list(dict.fromkeys(str(c) for c in p.get("stacks") or [] if str(c).strip()))
    p["defaults"] = {**PROJECT_DEFAULTS, **(p.get("defaults") or {})}
    b = p.get("budget") or {}
    p["budget"] = {"monthly_usd": max(0.0, float(b.get("monthly_usd") or 0)), "hard_cap": bool(b.get("hard_cap"))}
    p.setdefault("archived", False)
    return p


def migrate(known_repos: list[str], connector_names: list[str], stack_ids: list[str], tasks: list[dict], set_task_project) -> bool:
    """First run: one default project holding every repository, connector, stack and task Relay already knows."""
    if _store.exists() and _store.read().get("projects"):
        return False
    proj = _normalize({"id": DEFAULT_ID, "name": "Default", "slug": "default", "description": "Everything Relay had before projects existed.",
                       "color": COLORS[0], "repos": known_repos, "connectors": connector_names, "stacks": stack_ids,
                       "created_at": now_iso(), "created_by": "migration"})
    _store.write({"version": 1, "projects": [proj], "migrated_at": now_iso()})
    for t in tasks:
        if not t.get("project_id"):
            set_task_project(t["id"], DEFAULT_ID)
    return True


def all_projects(include_archived=True) -> list[dict]:
    rows = [_normalize(p) for p in _store.read().get("projects") or []]
    if not any(p["id"] == DEFAULT_ID for p in rows):
        rows.insert(0, _normalize({"id": DEFAULT_ID, "name": "Default", "slug": "default", "color": COLORS[0]}))
    return [p for p in rows if include_archived or not p.get("archived")]


def get(pid: str) -> dict | None:
    pid = str(pid or "")
    return next((p for p in all_projects() if p["id"] == pid or p.get("slug") == pid), None)


def save(incoming: dict, actor: str = "") -> dict:
    name = str(incoming.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Give the project a name.")

    pid = incoming.get("id")
    new_pid = pid or new_id("prj")

    def fn(d):
        rows = [_normalize(p) for p in d.get("projects") or []]
        old = next((p for p in rows if p["id"] == pid), None) if pid else None
        if pid and not old and pid != DEFAULT_ID:
            raise KeyError("No such project")
        if any(p["name"].lower() == name.lower() and p["id"] != pid for p in rows):
            raise ValueError(f"A project called {name} already exists.")
        base = old or {"id": new_pid, "created_at": now_iso(), "created_by": actor}
        merged = {**base, **{k: v for k, v in incoming.items() if k in ("name", "description", "color", "repos", "connectors", "stacks", "defaults", "budget", "archived")}}
        merged["name"] = name
        merged["slug"] = slugify(name) if merged["id"] != DEFAULT_ID else "default"
        if merged.get("color") and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(merged["color"])):
            raise ValueError("Colour must be a hex value like #4f6fd0.")
        if merged["id"] == DEFAULT_ID and merged.get("archived"):
            raise ValueError("The default project cannot be archived.")
        dflt = {**PROJECT_DEFAULTS, **(merged.get("defaults") or {})}
        dflt["cost_cap_usd"] = max(0.0, float(dflt.get("cost_cap_usd") or 0))
        dflt["max_turns"] = max(0, int(dflt.get("max_turns") or 0))
        dflt["reviewers"] = [str(x) for x in dflt.get("reviewers") or []][:20]
        merged["defaults"] = dflt
        merged["updated_at"] = now_iso()
        merged = _normalize(merged)
        # A repository, connector or stack sits in one project: claiming it here releases it elsewhere.
        for p in rows:
            if p["id"] == merged["id"]:
                continue
            p["repos"] = [r for r in p["repos"] if r not in merged["repos"]]
            p["connectors"] = [c for c in p["connectors"] if c not in merged["connectors"]]
            p["stacks"] = [s for s in p["stacks"] if s not in merged["stacks"]]
        rows = [p for p in rows if p["id"] != merged["id"]] + [merged]
        d["projects"] = rows
        return d
    _store.update(fn)
    return get(new_pid)


def delete(pid: str, move_tasks) -> None:
    if pid == DEFAULT_ID:
        raise ValueError("The default project cannot be deleted.")
    p = get(pid)
    if not p:
        raise KeyError("No such project")

    def fn(d):
        rows = [_normalize(x) for x in d.get("projects") or []]
        dflt = next((x for x in rows if x["id"] == DEFAULT_ID), None)
        if dflt:
            dflt["repos"] = list(dict.fromkeys(dflt["repos"] + p["repos"]))
            dflt["connectors"] = list(dict.fromkeys(dflt["connectors"] + p["connectors"]))
            dflt["stacks"] = list(dict.fromkeys(dflt["stacks"] + p["stacks"]))
        d["projects"] = [x for x in rows if x["id"] != pid]
    _store.update(fn)
    move_tasks(pid, DEFAULT_ID)


def add_repo(pid: str, repo: str):
    key = _key(repo)
    if any(key in p["repos"] for p in all_projects()):
        return
    def fn(d):
        rows = [_normalize(x) for x in d.get("projects") or []]
        for x in rows:
            if x["id"] == pid:
                x["repos"].append(key)
        d["projects"] = rows
    _store.update(fn)


# ---------------------------------------------------------------------------- membership
def project_for_repo(repo) -> str:
    key = _key(repo)
    for p in all_projects():
        if key in p["repos"]:
            return p["id"]
    return DEFAULT_ID


def project_of_task(t: dict, ids: set | None = None) -> str:
    pid = t.get("project_id")
    ids = ids if ids is not None else {p["id"] for p in all_projects()}
    if pid and pid in ids:
        return pid
    return project_for_repo(t.get("repo") or "")


class Scope:
    """Everything that belongs to one project, computed once per request."""

    def __init__(self, pid: str, tasks: list[dict]):
        self.project = get(pid)
        self.pid = self.project["id"] if self.project else pid
        ids = {p["id"] for p in all_projects()}
        self.task_ids = {t["id"] for t in tasks if project_of_task(t, ids) == self.pid}
        self.repos = set(self.project["repos"]) if self.project else set()
        for t in tasks:
            if t["id"] in self.task_ids and t.get("repo"):
                self.repos.add(_key(t["repo"]))
        self.connectors = set(self.project["connectors"]) if self.project else set()
        self.stacks = set(self.project["stacks"]) if self.project else set()
        if self.pid == DEFAULT_ID:
            # Things no project claims show in the default project.
            claimed_r = {r for p in all_projects() if p["id"] != DEFAULT_ID for r in p["repos"]}
            claimed_c = {c for p in all_projects() if p["id"] != DEFAULT_ID for c in p["connectors"]}
            claimed_s = {s for p in all_projects() if p["id"] != DEFAULT_ID for s in p["stacks"]}
            self.unclaimed = (claimed_r, claimed_c, claimed_s)
        else:
            self.unclaimed = None

    def has_repo(self, path) -> bool:
        k = _key(path)
        if k in self.repos:
            return True
        return bool(self.unclaimed) and k not in self.unclaimed[0]

    def has_connector(self, c: dict) -> bool:
        name = c.get("name")
        if name in self.connectors:
            return True
        if self.unclaimed is not None:
            return name not in self.unclaimed[1]
        return any(self.has_repo(r) for r in c.get("repos") or [])

    def has_stack(self, s: dict) -> bool:
        if s.get("id") in self.stacks:
            return True
        if self.unclaimed is not None and s.get("id") in self.unclaimed[2]:
            return False
        repos = [x.get("repo") for x in s.get("services") or [] if x.get("repo")]
        if repos:
            return any(_key(r) in self.repos for r in repos) or (self.unclaimed is not None and all(_key(r) not in self.unclaimed[0] for r in repos))
        return self.unclaimed is not None

    def has_task(self, tid) -> bool:
        return tid in self.task_ids


def filter_items(obj, scope: Scope, depth=0):
    """Drop list entries that name a task outside the project (inbox items, digest rows, dashboard lists)."""
    if depth > 6:
        return obj
    if isinstance(obj, list):
        out = []
        for x in obj:
            if isinstance(x, dict) and x.get("task_id") and not scope.has_task(x["task_id"]):
                continue
            out.append(filter_items(x, scope, depth + 1))
        return out
    if isinstance(obj, dict):
        return {k: filter_items(v, scope, depth + 1) for k, v in obj.items()}
    return obj


def raw_store() -> JsonStore:
    return _store
