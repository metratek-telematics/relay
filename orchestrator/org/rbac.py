"""Roles and the permission matrix for Relay's HTTP API.

    viewer  reads everything a person may see; changes only their own profile, preferences and read-only tokens
    member  creates, answers, approves, steers and stops tasks; runs the queue; proposes lessons
    admin   manages repositories, connectors, stacks, agents, projects, integrations and settings
    owner   manages people, roles, sign-in, budgets and everything above

Every changing endpoint is matched here, first rule wins; anything not listed needs admin, so a new endpoint
is safe by default. Reads default to viewer. Token scopes cap what a token can do below its owner's role.
"""
from __future__ import annotations

import re

from .identity import LEVEL

SCOPES = {"read": "viewer", "tasks:write": "member", "admin": "admin"}
SCOPE_HELP = {
    "read": "Read projects, tasks, the queue and the digest.",
    "tasks:write": "Also create, answer, stop and queue tasks, and turn issues into tasks.",
    "admin": "Also everything an admin can do through the API.",
}

W = ("POST", "PUT", "PATCH", "DELETE")
T = r"[^/]+"

# (methods, path regex, role, label). A role may also be "self" (any signed-in person, acting on themselves)
# or "task_owner" (member who created the task, or admin).
RULES: list[tuple[tuple, re.Pattern, str, str]] = [(m, re.compile(rx), r, label) for m, rx, r, label in [
    # ---- reads that need more than viewer
    (("GET",), r"^/api/fs/browse$", "member", "browse the server's folders"),
    (("GET",), r"^/api/org/audit(/.*)?$", "admin", "read the audit log"),
    (("GET",), r"^/api/org/settings$", "admin", "read organisation settings"),
    (("GET",), r"^/api/org/providers(/.*)?$", "admin", "read model provider settings"),
    (("GET",), r"^/api/org/deliveries$", "admin", "read the delivery log"),
    (("GET",), r"^/api/org/integrations(/.*)?$", "admin", "read integration status"),
    (("GET",), r"^/api/org/users/[^/]+$", "admin", "read a person's details"),
    # ---- everyone signed in, for themselves
    # Personal settings: the defaults this person's own tasks start from. The keys that may be
    # personal, and their validation, are in orchestrator/personal.py; everything shared stays on
    # /api/settings below, which is admin only.
    (W, r"^/api/org/me/settings$", "self", "change your own default team and workflow"),
    (W, r"^/api/org/me(/.*)?$", "self", "change your own profile, preferences, tokens and channels"),
    (W, r"^/api/notifications/read$", "viewer", "mark notifications read"),
    (W, r"^/api/org/onboarding/dismiss$", "viewer", "hide the setup checklist"),
    # ---- members: tasks
    (("POST",), r"^/api/tasks$", "member", "create tasks"),
    (("PATCH",), rf"^/api/tasks/{T}$", "member", "edit tasks"),
    (("DELETE",), rf"^/api/tasks/{T}$", "task_owner", "delete tasks you created"),
    (("PATCH",), rf"^/api/tasks/{T}/acceptance$", "member", "edit acceptance criteria"),
    (("POST",), rf"^/api/tasks/{T}/(start|stop|pause|resume|retry|answer|approve|reject|guidance|archive|duplicate|queue|unqueue|move|retro)$", "member", "run and steer tasks"),
    (("POST",), rf"^/api/tasks/{T}/scorecard/refresh$", "member", "refresh a scorecard"),
    (("POST",), rf"^/api/tasks/{T}/mockups/choose$", "member", "pick a design direction"),
    (("POST",), rf"^/api/tasks/{T}/repos$", "member", "add a repository to a task"),
    (("POST",), rf"^/api/tasks/{T}/open/.+$", "member", "open a task's folder"),
    (("PUT",), rf"^/api/tasks/{T}/repo-file$", "member", "edit files in a task's worktree"),
    (W, rf"^/api/tasks/{T}/stack(/.*)?$", "member", "run a task's integration stack"),
    (("POST",), r"^/api/issues/tasks$", "member", "turn issues into tasks"),
    (("POST",), r"^/api/(queue/(start|stop)|run)$", "member", "run or halt the queue"),
    (("POST",), r"^/api/lessons$", "member", "propose a lesson"),
    (("POST",), r"^/api/org/onboarding/sample-task$", "member", "run the sample task"),
    (("POST",), r"^/api/learning/preflight$", "member", "check a task's risk before creating it"),
    # ---- owners
    (W, r"^/api/org/users(/.*)?$", "owner", "manage people and roles"),
    (W, r"^/api/org/settings/auth$", "owner", "change sign-in and role mapping"),
    (W, r"^/api/org/budgets$", "owner", "change budgets"),
    (W, r"^/api/org/audit/verify$", "admin", "verify the audit chain"),
    # ---- admins (stated for the Access page; the default would be admin too)
    # The organisation's shared settings: provider keys and OpenRouter, spend caps, connectors,
    # repositories, the redeploy command, sign-in and role mapping. Anyone below admin changes their
    # own copy instead, through /api/org/me/settings.
    (W, r"^/api/settings$", "admin", "change the organisation's shared settings"),
    (W, r"^/api/knowledge(/.*)?$", "admin", "edit knowledge docs and refresh them"),
    # ---- public API: each route checks its scope; the matrix only requires a person
    (W, r"^/api/v1/.*$", "viewer", "use the public API"),
]]

DEFAULT_WRITE = "admin"


def rule_for(method: str, path: str):
    method = method.upper()
    for methods, rx, role, label in RULES:
        if method in methods and rx.match(path):
            return role, label
    if method in ("GET", "HEAD", "OPTIONS"):
        return "viewer", "read"
    return DEFAULT_WRITE, "change Relay's configuration"


def level(role: str) -> int:
    return LEVEL.get(role or "anonymous", 0)


def effective_role(user_role: str, token_scopes: list[str] | None) -> str:
    """A token can never do more than its owner, nor more than its widest scope."""
    if token_scopes is None:
        return user_role
    cap = max((level(SCOPES[s]) for s in token_scopes if s in SCOPES), default=0)
    lv = min(level(user_role), cap)
    return next((r for r, v in LEVEL.items() if v == lv), "anonymous")


def allowed(user: dict | None, role_needed: str, method: str, path: str, task_lookup=None, token_scopes=None) -> tuple[bool, str]:
    """(ok, reason). task_lookup(tid) returns the task for task_owner checks."""
    if not user:
        return False, "Sign in to use Relay."
    if user.get("disabled"):
        return False, "Your Relay account is disabled. Ask an owner to enable it."
    role = effective_role(user.get("role") or "viewer", token_scopes)
    have = level(role)
    if role_needed == "self":
        return (have >= level("viewer")), "Viewer access is needed."
    if role_needed == "task_owner":
        if have >= level("admin"):
            return True, ""
        if have >= level("member") and task_lookup:
            tid = path.rstrip("/").split("/")[3] if path.count("/") >= 3 else ""
            t = task_lookup(tid) or {}
            if (t.get("created_by") or "") == user.get("username"):
                return True, ""
        return False, "Only the person who created this task, or an admin, can delete it."
    if have >= level(role_needed):
        return True, ""
    return False, f"This needs the {role_needed} role (you are {role})."


def matrix() -> list[dict]:
    """The rules as data, for the Access page."""
    rows = []
    for methods, rx, role, label in RULES:
        rows.append({"methods": list(methods), "path": rx.pattern, "role": role, "label": label})
    rows.append({"methods": ["GET"], "path": "everything else", "role": "viewer", "label": "read"})
    rows.append({"methods": list(W), "path": "everything else", "role": DEFAULT_WRITE, "label": "change Relay's configuration"})
    return rows
