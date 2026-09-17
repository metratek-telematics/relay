"""Relay's public REST API, version 1: a small, stable surface for scripts, CI and other tools.

Authenticate with a personal access token (Profile → API tokens): Authorization: Bearer rly_…
Signed-in browser sessions work too, which is what the API docs page uses for "Try it".
Responses are JSON; errors are {"error": "..."} with a 4xx status. Fields may be added, never removed or
renamed, within v1.
"""
from __future__ import annotations

import functools
from pathlib import Path

from flask import Blueprint, jsonify, request

from .. import config as C, github, issues
from . import projects, rbac, tokens
from .common import now_iso

bp = Blueprint("api_v1", __name__)
STATE: dict = {}
VERSION = "1.0.0"


def _web():
    from . import web
    return web


def scope(needed_role: str, scope_name: str):
    def deco(fn):
        @functools.wraps(fn)
        def inner(*a, **k):
            w = _web()
            role = w.role_now()
            if rbac.level(role) < rbac.level(needed_role):
                token = w.g.get("org_token")
                if token and scope_name not in token.get("scopes", []) and rbac.level(w.current_user().get("role")) >= rbac.level(needed_role):
                    return jsonify({"error": f"This token lacks the {scope_name} scope."}), 403
                return jsonify({"error": f"This needs the {needed_role} role (you are {role})."}), 403
            return fn(*a, **k)
        inner._scope = scope_name
        return inner
    return deco


def _m():
    return STATE["manager"]


def task_out(t: dict) -> dict:
    pid = projects.project_of_task(t)
    p = projects.get(pid) or {}
    pend = t.get("pending") or None
    base = _web().notify.base_url()
    tot = (t.get("metrics") or {}).get("total") or {}
    return {
        "id": t["id"], "number": t.get("number"), "name": t.get("name"), "status": t.get("status"), "detail": t.get("detail"),
        "project": {"id": pid, "name": p.get("name")}, "repo": t.get("repo"), "github_repo": t.get("github_repo"),
        "branch": t.get("branch_name"), "pr_url": t.get("pr_url"), "issue": t.get("issue") or None, "priority": t.get("priority"),
        "tags": t.get("tags") or [], "archived": bool(t.get("archived")),
        "pending": {"id": pend.get("id"), "kind": pend.get("kind"), "question": pend.get("question"), "options": pend.get("options") or []} if pend else None,
        "created_by": t.get("created_by"), "answered_by": t.get("answered_by"), "approved_by": t.get("approved_by"), "merged_by": t.get("merged_by"),
        "created_at": t.get("created_at"), "started_at": t.get("started_at"), "finished_at": t.get("finished_at"), "updated_at": t.get("updated_at"),
        "usage": {"cost_usd": tot.get("cost_usd") or 0, "input_tokens": tot.get("input") or 0, "output_tokens": tot.get("output") or 0,
                  "seconds": tot.get("seconds") or 0, "turns": tot.get("turns") or 0, "estimated": bool(tot.get("estimated"))},
        "workflow": {r: {k: v for k, v in (((t.get("workflow") or {}).get("roles") or {}).get(r) or {}).items() if k in ("agent", "model", "effort")}
                     for r in ("supervisor", "worker", "reviewer")},
        "url": f"{base}/#/task/{t['id']}" if base else None,
    }


def _err(msg, code=400):
    return jsonify({"error": msg}), code


@bp.get("/api/v1/me")
@scope("viewer", "read")
def v1_me():
    w = _web()
    u = w.current_user()
    tok = w.g.get("org_token")
    return jsonify({"username": u["username"], "name": u.get("name"), "role": u.get("role"), "effective_role": w.role_now(),
                    "via": w.g.get("org_via"), "token": tokens.public(tok) if tok else None})


@bp.get("/api/v1/projects")
@scope("viewer", "read")
def v1_projects():
    return jsonify({"projects": [{k: p[k] for k in ("id", "name", "slug", "description", "color", "repos", "defaults", "budget")}
                                 for p in projects.all_projects(include_archived=False)]})


@bp.get("/api/v1/tasks")
@scope("viewer", "read")
def v1_tasks():
    a = request.args
    rows = _m().store.list()
    if a.get("project"):
        p = projects.get(a["project"])
        if not p:
            return _err("No such project", 404)
        ids = {x["id"] for x in projects.all_projects()}
        rows = [t for t in rows if projects.project_of_task(t, ids) == p["id"]]
    if a.get("status"):
        wanted = set(a["status"].split(","))
        rows = [t for t in rows if t.get("status") in wanted]
    if a.get("created_by"):
        rows = [t for t in rows if t.get("created_by") == a["created_by"]]
    if a.get("issue"):
        rows = [t for t in rows if str(t.get("issue") or "") == a["issue"].lstrip("#")]
    if a.get("archived") != "true":
        rows = [t for t in rows if not t.get("archived")]
    rows.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    limit = max(1, min(500, int(a.get("limit") or 50)))
    return jsonify({"tasks": [task_out(t) for t in rows[:limit]], "total": len(rows)})


def _resolve_repo(raw: str, project: dict | None) -> str:
    raw = str(raw or "").strip()
    if not raw:
        raise ValueError("repo is required: an absolute path Relay knows, or a GitHub owner/name.")
    if raw.startswith("/") or (len(raw) > 2 and raw[1] == ":"):
        return raw
    full = github.normalize_repo_full_name(raw)
    local = issues.local_path_for(full, _m().store.list())
    if local:
        return local
    for p in ([project] if project else []) + projects.all_projects():
        for r in p["repos"]:
            if Path(r).name.lower() == full.split("/")[-1].lower() and github.remote_repo_name(r).lower() == full.lower():
                return r
    raise ValueError(f"Relay has no checkout of {full}. Clone it from the Repositories page (or pass an absolute path).")


@bp.post("/api/v1/tasks")
@scope("member", "tasks:write")
def v1_task_create():
    b = request.get_json(silent=True) or {}
    project = projects.get(b.get("project")) if b.get("project") else None
    if b.get("project") and not project:
        return _err("No such project", 404)
    try:
        repo = _resolve_repo(b.get("repo"), project)
    except ValueError as e:
        return _err(str(e))
    wf = {}
    if b.get("preset"):
        if not C.preset(b["preset"]):
            return _err(f"Unknown preset {b['preset']}. Presets: " + ", ".join(p["id"] for p in C.PRESETS))
        wf["preset"] = b["preset"]
    if isinstance(b.get("roles"), dict):
        wf["roles"] = {r: {k: str(v.get(k) or "") for k in ("agent", "model", "effort") if k in v} for r, v in b["roles"].items() if isinstance(v, dict)}
    for k in ("max_turns", "approval_before_delivery", "verification_commands"):
        if k in b:
            wf[k] = b[k]
    payload = {"repo": repo, "requirements": b.get("requirements") or "", "name": b.get("name") or "", "issue": b.get("issue") or "",
               "priority": b.get("priority") or "normal", "tags": b.get("tags") or [], "queue": b.get("queue", True), "workflow": wf,
               "depends_on": b.get("depends_on"), "template": b.get("template") or "feature"}
    if project:
        payload["project_id"] = project["id"]
    if b.get("cost_cap_usd"):
        payload["cost_cap_usd"] = b["cost_cap_usd"]
    try:
        t = _m().create_task(payload)
    except ValueError as e:
        return _err(str(e))
    if b.get("start"):
        _m().start()
    return jsonify({"task": task_out(_m().store.get(t["id"]))}), 201


def _task(tid):
    t = _m().store.get(tid)
    if not t:
        return None
    return t


@bp.get("/api/v1/tasks/<tid>")
@scope("viewer", "read")
def v1_task_get(tid):
    t = _task(tid)
    return jsonify({"task": task_out(t)}) if t else _err("Task not found", 404)


@bp.get("/api/v1/tasks/<tid>/events")
@scope("viewer", "read")
def v1_task_events(tid):
    t = _task(tid)
    return jsonify({"events": (t.get("events") or [])[-200:]}) if t else _err("Task not found", 404)


@bp.post("/api/v1/tasks/<tid>/answer")
@scope("member", "tasks:write")
def v1_task_answer(tid):
    t = _task(tid)
    if not t:
        return _err("Task not found", 404)
    b = request.get_json(silent=True) or {}
    pend = t.get("pending") or {}
    if not pend:
        return _err("Nothing is waiting for an answer on this task.", 409)
    if b.get("question_id") and b["question_id"] != pend.get("id"):
        return _err("That question is no longer pending.", 409)
    try:
        if pend.get("kind") == "approval":
            if "approve" not in b:
                return _err("This is an approval: send {\"approve\": true} or {\"approve\": false, \"text\": \"what to change\"}.")
            if not b["approve"] and not str(b.get("text") or "").strip():
                return _err("Say what should change when requesting changes (text).")
            _m().approve(tid, bool(b["approve"]), str(b.get("text") or ""))
        else:
            text = str(b.get("text") or "").strip()
            if not text:
                return _err("text is required.")
            _m().answer(tid, pend.get("id"), text, {})
    except ValueError as e:
        return _err(str(e), 409)
    return jsonify({"task": task_out(_m().store.get(tid)), "answered_at": now_iso()})


@bp.post("/api/v1/tasks/<tid>/stop")
@scope("member", "tasks:write")
def v1_task_stop(tid):
    if not _task(tid):
        return _err("Task not found", 404)
    _m().stop(tid)
    return jsonify({"task": task_out(_m().store.get(tid))})


@bp.post("/api/v1/tasks/<tid>/start")
@scope("member", "tasks:write")
def v1_task_start(tid):
    if not _task(tid):
        return _err("Task not found", 404)
    try:
        _m().start_task(tid)
    except ValueError as e:
        return _err(str(e), 409)
    return jsonify({"task": task_out(_m().store.get(tid))})


@bp.get("/api/v1/queue")
@scope("viewer", "read")
def v1_queue():
    m = _m()
    order = m.queue_order() if hasattr(m, "queue_order") else []
    return jsonify({**m.queue_state(), "order": [{"id": t["id"], "number": t.get("number"), "name": t.get("name")} for t in order][:100]})


@bp.post("/api/v1/queue/start")
@scope("member", "tasks:write")
def v1_queue_start():
    _m().start()
    return jsonify(_m().queue_state())


@bp.post("/api/v1/issues/tasks")
@scope("member", "tasks:write")
def v1_issues_to_tasks():
    b = request.get_json(silent=True) or {}
    # Accept the compact form {"repo": "owner/name", "numbers": [12, 14]} as well as the board's {"issues": [{repo, number}]}.
    if b.get("numbers") and b.get("repo"):
        b["issues"] = [{"repo": b["repo"], "number": n} for n in b["numbers"]]
    try:
        res = issues.create_tasks(_m(), b)
    except ValueError as e:
        return _err(str(e))
    out = dict(res)
    out["created"] = [task_out(_m().store.get(t["id"])) if isinstance(t, dict) and t.get("id") and _m().store.get(t["id"]) else t for t in res.get("created") or []]
    return jsonify(out), 201 if out["created"] else 200


@bp.get("/api/v1/digest")
@scope("viewer", "read")
def v1_digest():
    try:
        hours = max(1.0, min(24 * 31, float(request.args.get("hours") or 24)))
    except ValueError:
        hours = 24
    return jsonify(_m().autopilot.digest(hours=hours))


@bp.get("/api/v1/openapi.json")
def v1_openapi():
    return jsonify(openapi())


# ============================================================================ OpenAPI
def _ref(name):
    return {"$ref": f"#/components/schemas/{name}"}


def openapi() -> dict:
    base = _web().notify.base_url() or ""
    err = {"description": "Error", "content": {"application/json": {"schema": _ref("Error")}}}
    std = {"401": {**err, "description": "Missing, invalid, expired or revoked token"}, "403": {**err, "description": "Role or scope too low"}}

    def op(summary, desc, scope_name, tag, ok_schema, body=None, params=None, code="200", example=None):
        o = {"summary": summary, "description": desc, "tags": [tag], "x-relay-scope": scope_name,
             "security": [{"bearer": []}], "responses": {code: {"description": "OK", "content": {"application/json": {"schema": ok_schema}}}, **std}}
        if body:
            o["requestBody"] = {"required": True, "content": {"application/json": {"schema": body, **({"example": example} if example else {})}}}
        if params:
            o["parameters"] = params
        return o

    q = lambda name, desc, typ="string": {"name": name, "in": "query", "required": False, "description": desc, "schema": {"type": typ}}
    pid = {"name": "id", "in": "path", "required": True, "description": "Task id", "schema": {"type": "string"}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Relay API", "version": VERSION,
                 "description": "Queue work for Relay's agent teams, follow it, and answer what they ask. Authenticate with a personal access token "
                                "created under Profile → API tokens and send it as `Authorization: Bearer rly_…`. Scopes: `read`, `tasks:write`, `admin`; "
                                "a token never does more than the person who created it."},
        "servers": [{"url": base or "/"}],
        "tags": [{"name": "Tasks"}, {"name": "Queue"}, {"name": "Projects"}, {"name": "Issues"}, {"name": "Digest"}, {"name": "Account"}],
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer", "bearerFormat": "rly_<id>_<secret>"}},
            "schemas": {
                "Error": {"type": "object", "properties": {"error": {"type": "string"}}},
                "Person": {"type": "object", "nullable": True, "properties": {"username": {"type": "string"}, "name": {"type": "string"}, "time": {"type": "string", "format": "date-time"}, "via": {"type": "string"}}},
                "Pending": {"type": "object", "nullable": True, "properties": {"id": {"type": "string"}, "kind": {"type": "string", "enum": ["question", "approval"]}, "question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}},
                "Task": {"type": "object", "properties": {
                    "id": {"type": "string"}, "number": {"type": "integer"}, "name": {"type": "string"},
                    "status": {"type": "string", "enum": ["draft", "queued", "running", "preparing", "planning", "implementing", "verifying", "reviewing", "delivering", "needs_input", "paused", "done", "failed", "stopped", "interrupted"]},
                    "detail": {"type": "string"}, "project": {"type": "object", "properties": {"id": {"type": "string"}, "name": {"type": "string"}}},
                    "repo": {"type": "string"}, "github_repo": {"type": "string"}, "branch": {"type": "string"}, "pr_url": {"type": "string", "nullable": True},
                    "pending": _ref("Pending"), "created_by": {"type": "string"}, "answered_by": _ref("Person"), "approved_by": _ref("Person"), "merged_by": _ref("Person"),
                    "usage": {"type": "object", "properties": {"cost_usd": {"type": "number"}, "input_tokens": {"type": "integer"}, "output_tokens": {"type": "integer"}, "seconds": {"type": "number"}, "turns": {"type": "integer"}, "estimated": {"type": "boolean"}}},
                    "created_at": {"type": "string", "format": "date-time"}, "finished_at": {"type": "string", "nullable": True}, "url": {"type": "string", "nullable": True}}},
                "TaskCreate": {"type": "object", "required": ["repo"], "properties": {
                    "project": {"type": "string", "description": "Project id or slug; default: the project that owns the repository"},
                    "repo": {"type": "string", "description": "GitHub owner/name Relay has cloned, or an absolute path"},
                    "requirements": {"type": "string", "description": "What to build or fix (required unless issue is given)"},
                    "issue": {"type": "string", "description": "GitHub issue number in that repository"},
                    "name": {"type": "string"}, "priority": {"type": "string", "enum": ["urgent", "high", "normal", "low"]},
                    "preset": {"type": "string", "description": "Team preset id"},
                    "roles": {"type": "object", "description": "Per-role overrides: {supervisor|worker|reviewer: {agent, model, effort}}"},
                    "max_turns": {"type": "integer"}, "approval_before_delivery": {"type": "boolean"}, "tags": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}}, "cost_cap_usd": {"type": "number"},
                    "queue": {"type": "boolean", "default": True}, "start": {"type": "boolean", "description": "Also start the queue"}}},
                "Answer": {"type": "object", "properties": {"text": {"type": "string"}, "approve": {"type": "boolean", "description": "For approvals"}, "question_id": {"type": "string"}}},
            }},
        "paths": {
            "/api/v1/me": {"get": op("Who am I", "The person and token behind this request.", "read", "Account", {"type": "object"})},
            "/api/v1/projects": {"get": op("List projects", "Projects with their repositories, defaults and budget.", "read", "Projects",
                                           {"type": "object", "properties": {"projects": {"type": "array", "items": {"type": "object"}}}})},
            "/api/v1/tasks": {
                "get": op("List tasks", "Newest first.", "read", "Tasks", {"type": "object", "properties": {"tasks": {"type": "array", "items": _ref("Task")}, "total": {"type": "integer"}}},
                          params=[q("project", "Project id or slug"), q("status", "Comma-separated statuses"), q("created_by", "Username"), q("issue", "Issue number"), q("limit", "1-500, default 50", "integer")]),
                "post": op("Create a task", "Queues a task for the project's default team unless roles or a preset are given.", "tasks:write", "Tasks",
                           {"type": "object", "properties": {"task": _ref("Task")}}, body=_ref("TaskCreate"), code="201",
                           example={"project": "navitrak", "repo": "acme/web", "requirements": "Fix the 500 on /login when the session cookie is expired. Add a regression test.",
                                    "priority": "high", "roles": {"worker": {"agent": "claude", "model": "haiku", "effort": "low"}}, "start": True}),
            },
            "/api/v1/tasks/{id}": {"get": op("Get a task", "Status, pending question, attribution, usage and links.", "read", "Tasks", {"type": "object", "properties": {"task": _ref("Task")}}, params=[pid])},
            "/api/v1/tasks/{id}/events": {"get": op("Task timeline", "The last 200 timeline events.", "read", "Tasks", {"type": "object"}, params=[pid])},
            "/api/v1/tasks/{id}/answer": {"post": op("Answer or approve", "Answer the pending question, or approve / request changes on a delivery approval.", "tasks:write", "Tasks",
                                                     {"type": "object", "properties": {"task": _ref("Task")}}, body=_ref("Answer"), params=[pid], example={"approve": True})},
            "/api/v1/tasks/{id}/stop": {"post": op("Stop a task", "Stops a running or waiting task.", "tasks:write", "Tasks", {"type": "object", "properties": {"task": _ref("Task")}}, params=[pid])},
            "/api/v1/tasks/{id}/start": {"post": op("Start a task now", "Starts a queued, draft or failed task alongside others.", "tasks:write", "Tasks", {"type": "object", "properties": {"task": _ref("Task")}}, params=[pid])},
            "/api/v1/queue": {"get": op("Queue state", "Whether the queue runs, how many tasks are active and waiting, in run order.", "read", "Queue", {"type": "object"})},
            "/api/v1/queue/start": {"post": op("Start the queue", "Queued tasks start as parallel slots free up.", "tasks:write", "Queue", {"type": "object"})},
            "/api/v1/issues/tasks": {"post": op("Turn issues into tasks", "One task per issue. mode: sequential (a chain), parallel or draft.", "tasks:write", "Issues", {"type": "object"},
                                                body={"type": "object", "properties": {"repo": {"type": "string"}, "numbers": {"type": "array", "items": {"type": "integer"}},
                                                                                       "issues": {"type": "array", "items": {"type": "object"}}, "mode": {"type": "string", "enum": ["sequential", "parallel", "draft"]}}},
                                                code="201", example={"repo": "acme/web", "numbers": [42], "mode": "parallel"})},
            "/api/v1/digest": {"get": op("Digest", "What was delivered, what waits for people, what failed and what is next.", "read", "Digest", {"type": "object"}, params=[q("hours", "Window, default 24", "number")])},
        },
    }
