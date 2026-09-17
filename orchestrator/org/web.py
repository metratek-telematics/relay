"""Wires the organisation layer into Relay's Flask app.

install(app, manager, broadcast) adds, without changing any existing route:

  * a request guard: resolves the person (trusted forward-auth headers, the local owner, or a bearer token
    on /api/v1), answers 401 for anonymous API calls in identity mode, and enforces the role matrix;
  * an audit hook that records every changing request with before/after summaries (secrets masked);
  * project scoping: pages that fetch their own lists (inbox, digest, dashboard, repositories, connectors,
    stacks, lessons) only show the project named by the X-Relay-Project header;
  * attribution on tasks (created_by, answered_by, approved_by, merged_by) and project defaults on creation;
  * the notification dispatcher, the budget watch, the digest scheduler;
  * /api/org/* for the UI and /api/v1/* for everyone else.
"""
from __future__ import annotations

import functools
import json
import re
import threading
import time
from datetime import datetime

from flask import Blueprint, Response, g, jsonify, request

from .. import config as C, connectors, github, lessons, repos, stacks
from . import audit, identity, notify, onboarding, projects, rbac, settings as OS, tokens, usage, webpush
from .common import MASK, mask, now_iso

PUBLIC_API = {"/api/ping", "/api/build", "/api/v1/openapi.json", "/api/org/integrations/telegram/webhook"}
UI_KEYS = {"ui_theme", "ui_density", "ui_notifications", "ui_notify_events", "ui_sound"}
STATE = {"manager": None, "dispatcher": None}

bp = Blueprint("org", __name__)


# ============================================================================ identity per request
def client_ip() -> str:
    return request.remote_addr or ""


def resolve():
    """Sets g.org_user, g.org_via ('local' | 'proxy' | 'token' | None) and g.org_token."""
    if hasattr(g, "org_resolved"):
        return g.get("org_user")
    g.org_resolved = True
    g.org_user, g.org_via, g.org_token = None, None, None
    data = OS.load()
    ip = client_ip()
    authz = request.headers.get("Authorization") or ""
    if request.path.startswith("/api/v1/") and re.match(r"^\s*Bearer\s+rly_", authz, re.I):
        try:
            u, row = tokens.authenticate(authz, ip)
            g.org_user, g.org_via, g.org_token = u, "token", row
            return u
        except PermissionError as e:
            g.org_auth_error = str(e)
            return None
    h = {"username": request.headers.get("X-Authentik-Username") or request.headers.get("X-Forwarded-User") or "",
         "email": request.headers.get("X-Authentik-Email") or "", "name": request.headers.get("X-Authentik-Name") or "",
         "groups": request.headers.get("X-Authentik-Groups") or "", "uid": request.headers.get("X-Authentik-Uid") or ""}
    if OS.identity_mode(data):
        if h["username"] and OS.ip_trusted(ip, data):
            u = identity.upsert_from_headers(h, ip)
            if u:
                g.org_user, g.org_via = u, "proxy"
                _remember_base()
                return u
            g.org_auth_error = "Your sign-in is valid, but Relay does not create accounts automatically. Ask an owner to add you."
            return None
        if h["username"]:
            g.org_untrusted_headers = True
        if data["auth"].get("allow_local_fallback"):
            g.org_user, g.org_via = identity.local_owner(ip), "local"
            return g.org_user
        return None
    g.org_user, g.org_via = identity.local_owner(ip), "local"
    _remember_base()
    return g.org_user


def _remember_base():
    try:
        proto = request.headers.get("X-Forwarded-Proto") or request.scheme
        host = request.headers.get("X-Forwarded-Host") or request.host
        if host and OS.ip_trusted(client_ip()) or not OS.identity_mode():
            notify.remember_base(f"{proto}://{host}")
    except Exception:
        pass


def current_user() -> dict | None:
    return resolve()


def actor() -> dict:
    u = current_user() if _in_request() else None
    return u or {"username": "system", "role": "system"}


def _in_request() -> bool:
    from flask import has_request_context
    return has_request_context()


def role_now() -> str:
    u = current_user()
    if not u:
        return "anonymous"
    return rbac.effective_role(u.get("role"), (g.org_token or {}).get("scopes") if g.get("org_via") == "token" else None)


def need(role: str):
    def deco(fn):
        @functools.wraps(fn)
        def inner(*a, **k):
            if rbac.level(role_now()) < rbac.level(role):
                return jsonify({"error": f"This needs the {role} role (you are {role_now()})."}), 403
            return fn(*a, **k)
        return inner
    return deco


# ============================================================================ guard + audit
def _body_json():
    try:
        return request.get_json(silent=True) if request.data else None
    except Exception:
        return None


def _task_summary(tid):
    m = STATE["manager"]
    t = m.store.get(tid) if m else None
    if not t:
        return None
    keep = ("name", "status", "priority", "archived", "tags", "project_id", "depends_on", "created_by", "answered_by", "approved_by",
            "requirements", "repos", "connectors")
    out = {k: t.get(k) for k in keep if k in t}
    out["pending"] = (t.get("pending") or {}).get("kind")
    out["requirements"] = (out.get("requirements") or "")[:160]
    return out


def _snapshot(path: str, method: str, body):
    """What the object a request changes looks like now (masked). None when there is nothing to compare."""
    m = STATE["manager"]
    try:
        if mt := re.match(r"^/api/(?:v1/)?tasks/([^/]+)", path):
            return _task_summary(mt.group(1))
        if path == "/api/settings":
            return mask(C.public_view(m.cfg()))
        if path.startswith("/api/connectors"):
            return {c["name"]: mask(connectors.public(c)) for c in connectors.load_all()}
        if path.startswith("/api/stacks"):
            return {d["id"]: mask(stacks.public(d)) for d in stacks.list_defs()}
        if path.startswith("/api/github/sources"):
            return {r["id"]: r for r in github.load_sources()}
        if path.startswith("/api/org/projects"):
            return {p["id"]: p for p in projects.all_projects()}
        if path.startswith("/api/org/users"):
            return {u["username"]: identity.public(u) for u in identity.users()}
        if path.startswith("/api/org/settings") or path == "/api/org/budgets" or path.startswith("/api/org/integrations/webpush"):
            return OS.public()
        if path.startswith("/api/org/me/tokens") or path.startswith("/api/org/tokens"):
            u = current_user() or {}
            return {t["id"]: t for t in tokens.list_for(None if path.startswith("/api/org/tokens") else u.get("username"))}
        if path == "/api/org/me":
            u = current_user() or {}
            return identity.public(identity.get(u.get("username")), full=True)
        if mr := re.match(r"^/api/rules/([^/]+)$", path):
            from ..util import APP_DIR, read_text
            p = APP_DIR / "rules" / f"{mr.group(1)}.md"
            return {"length": len(read_text(p)) if p.exists() else 0}
    except Exception:
        return None
    return None


ACTION_WORDS = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}
TYPE_NAMES = {"tasks": "task", "connectors": "connector", "stacks": "stack", "lessons": "lesson", "repos": "repository", "worktrees": "worktree",
              "settings": "settings", "queue": "queue", "autopilot": "autopilot", "github": "github", "issues": "issue", "agents": "agent",
              "system": "system_map", "rules": "rule", "notifications": "notification", "projects": "project", "users": "user",
              "me": "profile", "tokens": "token", "integrations": "integration", "budgets": "budget", "onboarding": "onboarding", "audit": "audit",
              "run": "queue"}


def describe(method: str, path: str) -> tuple[str, dict]:
    parts = [p for p in path.split("/") if p][1:]  # drop "api"
    via_v1 = False
    if parts and parts[0] == "v1":
        parts, via_v1 = parts[1:], True
    if parts and parts[0] == "org":
        parts = parts[1:]
    if not parts:
        return "unknown", {}
    typ = TYPE_NAMES.get(parts[0], parts[0])
    obj = {"type": typ}
    verb = ACTION_WORDS.get(method, method.lower())
    if len(parts) >= 2:
        if parts[0] in ("queue", "autopilot", "github", "repos", "worktrees", "system", "onboarding", "integrations", "notifications", "audit", "issues"):
            verb = "_".join(parts[1:])
        elif parts[0] == "me":
            obj = {"type": "profile"}
            verb = "_".join(parts[1:]) + ("" if method == "POST" else "_" + ACTION_WORDS.get(method, ""))
            if len(parts) >= 2 and parts[1] == "tokens":
                obj = {"type": "token"}
                verb = "create" if method == "POST" else "revoke" if method == "DELETE" else verb
                if len(parts) >= 3:
                    obj["id"] = parts[2]
        else:
            obj["id"] = parts[1]
            if len(parts) >= 3:
                verb = "_".join(parts[2:])
    else:
        if parts[0] == "run":
            verb = "start"
    if obj.get("type") == "task" and obj.get("id"):
        t = STATE["manager"].store.get(obj["id"]) if STATE["manager"] else None
        if t:
            obj["name"] = t.get("name")
            obj["project"] = projects.project_of_task(t)
    action = f"{obj['type']}.{verb}".strip("._")
    if via_v1:
        obj["api"] = "v1"
    return action, obj


def _audit_request(resp):
    m = request.method
    if m in ("GET", "HEAD", "OPTIONS"):
        return
    path = request.path
    if not path.startswith("/api/") or path.startswith("/api/connect") or path == "/api/org/me/push/ping":
        return
    if path == "/api/notifications/read" and resp.status_code < 400:
        return  # marking notifications read is not a change worth auditing
    action, obj = describe(m, path)
    body = g.get("org_body")
    after = None
    if resp.status_code < 400:
        after = _snapshot(path, m, body)
        if path == "/api/tasks" and m == "POST" or path == "/api/v1/tasks" and m == "POST":
            try:
                created = json.loads(resp.get_data(as_text=True))
                created = created.get("task") or created
                obj.update({"id": created.get("id"), "name": created.get("name"), "project": created.get("project_id")})
                after = _task_summary(created.get("id"))
            except Exception:
                pass
    before = g.get("org_before")
    if isinstance(before, dict) and isinstance(after, dict) and obj.get("id") and obj["id"] in before and path.count("/") > 3:
        before, after = before.get(obj["id"]), after.get(obj["id"]) if obj["id"] in after else None
    outcome = "ok" if resp.status_code < 400 else ("denied" if resp.status_code in (401, 403) else "error")
    detail = ""
    if resp.status_code >= 400:
        try:
            detail = (json.loads(resp.get_data(as_text=True)) or {}).get("error") or ""
        except Exception:
            detail = ""
    u = current_user()
    via = g.get("org_via") or "anonymous"
    if via == "token":
        via = f"token:{(g.org_token or {}).get('id')}"
    req = {"method": m, "path": path, "ip": client_ip()}
    if isinstance(body, dict):
        req["body"] = {k: (v if len(json.dumps(v, default=str)) < 400 else "…") for k, v in list(body.items())[:30]}
    audit.record(u or {"username": "anonymous"}, action, obj, before=before if outcome == "ok" else None, after=after if outcome == "ok" else None,
                 request=req, status=resp.status_code, outcome=outcome, via=via, detail=detail)


def _json_response(data, status=200):
    resp = jsonify(data)
    resp.status_code = status
    return resp


def install(app, manager, broadcast):
    STATE["manager"] = manager
    _migrate(manager)
    _hook_manager(manager)
    STATE["dispatcher"] = notify.Dispatcher(manager)
    usage.Watch(manager)
    threading.Thread(target=_digest_loop, args=(manager,), name="relay-digests", daemon=True).start()
    threading.Thread(target=_merged_by_loop, args=(manager,), name="relay-merged-by", daemon=True).start()
    from . import api_v1
    api_v1.STATE = STATE
    app.register_blueprint(bp)
    app.register_blueprint(api_v1.bp)

    @app.before_request
    def _org_guard():
        path = request.path
        if not path.startswith("/api/") or path in PUBLIC_API or path.startswith("/api/connect"):
            return None
        u = resolve()
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            g.org_body = _body_json()
        authz = request.headers.get("Authorization") or ""
        if re.match(r"^\s*Bearer\s+rly_", authz, re.I) and not path.startswith("/api/v1/"):
            return _json_response({"error": "Access tokens work on /api/v1 only."}, 401)
        if not u:
            if g.get("org_untrusted_headers"):
                _untrusted_once()
            err = g.get("org_auth_error") or ("Send a personal access token as Authorization: Bearer rly_…" if path.startswith("/api/v1/")
                                              else "Sign in through Relay's address to continue.")
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                audit.record({"username": "anonymous"}, describe(request.method, path)[0], describe(request.method, path)[1],
                             request={"method": request.method, "path": path, "ip": client_ip()}, status=401, outcome="denied",
                             via="anonymous", detail=err)
            return _json_response({"error": err, "signed_out": True}, 401)
        # Theme, density and alert choices from people who may not change settings stay personal.
        if path == "/api/settings" and request.method == "POST" and isinstance(g.get("org_body"), dict) and g.org_body \
                and set(g.org_body) <= UI_KEYS and rbac.level(role_now()) < rbac.level("admin"):
            prefs = {}
            if "ui_theme" in g.org_body:
                prefs["theme"] = g.org_body["ui_theme"]
            if "ui_density" in g.org_body:
                prefs["density"] = g.org_body["ui_density"]
            if prefs:
                identity.save_prefs(u["username"], prefs)
            return _json_response(_config_for(u, C.public_view(manager.cfg())))
        role_needed, label = rbac.rule_for(request.method, path)
        scopes = (g.org_token or {}).get("scopes") if g.get("org_via") == "token" else None
        ok, reason = rbac.allowed(u, role_needed, request.method, path, task_lookup=manager.store.get, token_scopes=scopes)
        if not ok:
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                action, obj = describe(request.method, path)
                audit.record(u, action, obj, request={"method": request.method, "path": path, "ip": client_ip()}, status=403,
                             outcome="denied", via=g.get("org_via") or "", detail=f"{reason} ({label})")
                g.org_audited = True
            return _json_response({"error": reason, "needs": role_needed, "role": role_now()}, 403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            g.org_before = _snapshot(path, request.method, g.get("org_body"))
        return None

    @app.after_request
    def _org_after(resp):
        path = request.path
        if not path.startswith("/api/"):
            return resp
        try:
            if request.method not in ("GET", "HEAD", "OPTIONS") and not g.get("org_audited") and path not in PUBLIC_API:
                if hasattr(g, "org_resolved") or path.startswith("/api/"):
                    _audit_request(resp)
        except Exception as e:  # auditing must never break a request, but it must be visible
            app.logger.warning("audit failed on %s: %s", path, e)
        try:
            if request.method == "GET" and resp.status_code == 200 and resp.is_json and g.get("org_user"):
                resp = _scope_response(path, resp, manager)
        except Exception as e:
            app.logger.warning("project scoping failed on %s: %s", path, e)
        return resp


_untrusted = {"at": 0}


def _untrusted_once():
    """Identity headers from a peer that is not a trusted proxy: recorded, at most once a minute."""
    if time.time() - _untrusted["at"] < 60:
        return
    _untrusted["at"] = time.time()
    audit.record({"username": "anonymous"}, "auth.untrusted_headers", {"type": "auth"},
                 request={"method": request.method, "path": request.path, "ip": client_ip(),
                          "claimed": request.headers.get("X-Authentik-Username") or request.headers.get("X-Forwarded-User")},
                 outcome="denied", status=401, via="anonymous",
                 detail="Identity headers ignored: the request did not come from a trusted proxy.")


def _config_for(u, cfg: dict) -> dict:
    """Settings as a person sees them: their own theme, and agent secrets only for admins."""
    cfg = dict(cfg)
    p = (u or {}).get("prefs") or {}
    if p.get("theme"):
        cfg["ui_theme"] = p["theme"]
    if p.get("density"):
        cfg["ui_density"] = p["density"]
    if rbac.level((u or {}).get("role")) < rbac.level("admin"):
        env = cfg.get("agent_env") or {}
        cfg["agent_env"] = {a: {k: MASK for k in (vals or {})} for a, vals in env.items()}
    return cfg


def current_project_id() -> str | None:
    pid = (request.headers.get("X-Relay-Project") or request.args.get("project") or "").strip()
    if not pid or pid == "all":
        return None
    p = projects.get(pid)
    return p["id"] if p else None


def _scope_response(path, resp, manager):
    u = g.get("org_user")
    if path in ("/api/state", "/api/settings"):
        data = json.loads(resp.get_data(as_text=True))
        if path == "/api/state":
            data["config"] = _config_for(u, data.get("config") or {})
        else:
            data = _config_for(u, data)
        resp.set_data(json.dumps(data, ensure_ascii=False))
        return resp
    pid = current_project_id()
    if not pid:
        return resp
    scoped = {"/api/autopilot/inbox", "/api/digest", "/api/repos", "/api/connectors", "/api/stacks", "/api/lessons", "/api/dashboard", "/api/issues"}
    if path not in scoped:
        return resp
    rows = manager.store.list()
    sc = projects.Scope(pid, rows)
    data = json.loads(resp.get_data(as_text=True))
    if path == "/api/dashboard":
        data = _scoped_dashboard(manager, sc)
    elif path == "/api/repos":
        data["repos"] = [r for r in data.get("repos") or [] if sc.has_repo(r.get("path"))]
    elif path == "/api/connectors":
        data["connectors"] = [c for c in data.get("connectors") or [] if sc.has_connector(c)]
    elif path == "/api/stacks":
        data["stacks"] = [s for s in data.get("stacks") or [] if sc.has_stack(s)]
    elif path == "/api/lessons":
        for k in ("queue", "approved", "rejected"):
            data[k] = [x for x in data.get(k) or [] if not (x.get("repo") or x.get("proposed_repo")) or sc.has_repo(x.get("repo") or x.get("proposed_repo"))
                       or (x.get("task_id") and sc.has_task(x["task_id"]))]
        data["repos"] = [r for r in data.get("repos") or [] if sc.has_repo(r.get("key"))]
    elif path == "/api/issues":
        data["issues"] = [i for i in data.get("issues") or [] if not i.get("task_id") or sc.has_task(i["task_id"])]
    else:
        data = projects.filter_items(data, sc)
    data["project_scope"] = {"id": sc.pid, "name": (sc.project or {}).get("name")}
    resp.set_data(json.dumps(data, ensure_ascii=False))
    return resp


class _ScopedManager:
    """The manager seen through one project, so Manager.dashboard computes the project's own numbers."""

    def __init__(self, m, scope):
        self._m, self._scope = m, scope

        class _Store:
            def list(_):
                return [t for t in m.store.list() if scope.has_task(t["id"])]

            def __getattr__(_, k):
                return getattr(m.store, k)
        self.store = _Store()

    def __getattr__(self, k):
        return getattr(self._m, k)


def _scoped_dashboard(manager, sc):
    from ..manager import Manager
    view = _ScopedManager(manager, sc)
    return Manager.dashboard(view)


# ============================================================================ manager hooks
def _migrate(manager):
    try:
        cfg = manager.cfg()
        known = list(cfg.get("recent_repos") or [])
        for t in manager.store.list():
            if t.get("repo"):
                known.append(t["repo"])
        try:
            known += [str(p) for p in repos.candidate_repos()]
        except Exception:
            pass
        projects.migrate(known, [c["name"] for c in connectors.load_all()], [d["id"] for d in stacks.list_defs()],
                         manager.store.list(), lambda tid, pid: manager.store.update(tid, touch=False, project_id=pid))
    except Exception as e:
        print(f"Project migration skipped: {e}")


def _hook_manager(manager):
    orig_create, orig_answer, orig_emit = manager.create_task, manager.answer, manager._emit

    def create_task(payload: dict):
        payload = dict(payload or {})
        u = current_user() if _in_request() else None
        pid = str(payload.pop("project_id", "") or payload.pop("project", "") or "").strip()
        if not pid and _in_request():
            pid = current_project_id() or ""
        p = projects.get(pid) if pid else None
        if not p:
            p = projects.get(projects.project_for_repo(payload.get("repo") or "")) or projects.get(projects.DEFAULT_ID)
        over, why = usage.over_hard_cap(manager.store.list(), p["id"])
        if over:
            raise ValueError(why)
        d = p.get("defaults") or {}
        wf = dict(payload.get("workflow") or {})
        if d.get("preset") and not wf.get("preset") and not payload.get("preset") and not wf.get("roles"):
            wf["preset"] = d["preset"]
        if d.get("approval_before_delivery") is not None and "approval_before_delivery" not in wf:
            wf["approval_before_delivery"] = bool(d["approval_before_delivery"])
        if d.get("max_turns") and not wf.get("max_turns"):
            wf["max_turns"] = d["max_turns"]
        if wf:
            payload["workflow"] = wf
        if d.get("cost_cap_usd") and not payload.get("cost_cap_usd"):
            payload["cost_cap_usd"] = d["cost_cap_usd"]
        sample = bool(payload.pop("_sample", False))
        created = orig_create(payload)
        who = (u or {}).get("username") or payload.get("_created_by") or ("github-intake" if payload.get("issue") else "system")
        via = g.get("org_via") if _in_request() else "automation"
        patch = {"project_id": p["id"], "created_by": who, "created_via": via}
        if d.get("reviewers"):
            patch["reviewers"] = list(d["reviewers"])
        if sample:
            patch["sample"] = True
        manager.store.update(created["id"], touch=False, **patch)
        projects.add_repo(p["id"], payload.get("repo") or "")
        manager.emit_task(created["id"])
        return manager.get(created["id"])

    def answer(tid, qid, text, extra=None):
        t = manager.store.get(tid) or {}
        kind = (t.get("pending") or {}).get("kind")
        res = orig_answer(tid, qid, text, extra)
        who = g.get("org_actor_override") if _in_request() and g.get("org_actor_override") else (current_user() if _in_request() else None)
        stamp = {"username": (who or {}).get("username") or "system", "name": (who or {}).get("name") or "", "time": now_iso(),
                 "via": (g.get("org_via") if _in_request() else None) or "system"}
        if _in_request() and g.get("org_actor_override"):
            stamp["via"] = g.get("org_actor_via") or stamp["via"]
        patch = {}
        if extra and "approved" in extra:
            patch["approved_by" if extra.get("approved") else "changes_requested_by"] = stamp
        else:
            patch["answered_by"] = stamp
        hist = list(t.get("attribution") or [])[-40:]
        hist.append({**stamp, "action": "approved" if (extra or {}).get("approved") else "requested changes" if extra and "approved" in extra else "answered",
                     "kind": kind})
        patch["attribution"] = hist
        manager.store.update(tid, touch=False, **patch)
        manager.emit_task(tid)
        return res

    def emit(typ, payload):
        orig_emit(typ, payload)
        if typ == "notify" and STATE.get("dispatcher"):
            try:
                STATE["dispatcher"].on_notify(payload)
            except Exception as e:
                print(f"notification routing failed: {e}")

    manager.create_task = create_task
    manager.answer = answer
    manager._emit = emit


def _digest_loop(manager):
    """Each person's digest at their own digest time, on the channels they chose (in-app digests stay global)."""
    sent = {}
    from ..autopilot import summary_line
    while True:
        try:
            now = datetime.now()
            hhmm, day = now.strftime("%H:%M"), now.strftime("%Y-%m-%d")
            for u in identity.users():
                n = u["prefs"]["notifications"]
                chans = [c for c in n["events"].get("digest") or [] if c in notify.EXTERNAL]
                if u.get("disabled") or not chans or n.get("digest_time") != hhmm or sent.get(u["username"]) == day:
                    continue
                sent[u["username"]] = day
                d = manager.autopilot.digest(hours=24)
                msg = notify.build_message(manager, {"id": f"digest-{day}-{u['username']}", "kind": "digest", "level": "info",
                                                     "title": "Your Relay digest", "body": summary_line(d), "time": now_iso()}, "digest")
                STATE["dispatcher"].route(msg, only_user=u["username"])
        except Exception as e:
            print(f"digest scheduler: {e}")
        time.sleep(20)


def _merged_by_loop(manager):
    """Who merged a delivered pull request, looked up once per merged task."""
    import subprocess
    time.sleep(60)
    while True:
        try:
            for t in manager.store.list():
                pr = ((t.get("scorecard") or {}).get("pr") or {})
                if pr.get("state") == "merged" and t.get("pr_url") and not t.get("merged_by"):
                    p = subprocess.run(["gh", "pr", "view", t["pr_url"], "--json", "mergedBy,mergedAt"], capture_output=True, text=True, timeout=30)
                    info = json.loads(p.stdout or "{}") if p.returncode == 0 else {}
                    login = (info.get("mergedBy") or {}).get("login")
                    manager.store.update(t["id"], touch=False, merged_by={"username": login or "unknown", "time": info.get("mergedAt"), "via": "github"})
        except Exception:
            pass
        time.sleep(600)


# ============================================================================ /api/org: me
@bp.get("/api/org/me")
def me():
    u = current_user()
    data = OS.load()
    full = identity.public(identity.get(u["username"]) or u, full=True)
    return jsonify({
        "user": full, "via": g.get("org_via"), "role": role_now(), "identity_mode": OS.identity_mode(data),
        "can": {r: rbac.level(role_now()) >= rbac.level(r) for r in ("viewer", "member", "admin", "owner")},
        "projects": [_project_public(p) for p in projects.all_projects(include_archived=False)],
        "onboarding": {k: v for k, v in onboarding.checklist(STATE["manager"], u["username"]).items() if k in ("done", "total", "complete", "dismissed")},
        "channels_available": _channels_available(data),
        "events": [{"id": e, "label": notify.EVENT_LABEL[e]} for e in identity.NOTIFY_EVENTS],
        "telegram_link_code": notify.telegram_link_code(u["username"]) if data["integrations"]["telegram"].get("bot_token") else None,
        "webpush": {"available": webpush.AVAILABLE and bool(data["integrations"]["webpush"].get("public_key")),
                    "public_key": data["integrations"]["webpush"].get("public_key") or "", "subscriptions": notify.subscriptions(u["username"])},
    })


def _channels_available(data):
    i = data["integrations"]
    return {"inapp": True, "browser": True, "email": bool(i["smtp"].get("host")), "slack": True, "telegram": bool(i["telegram"].get("bot_token")),
            "discord": True, "webhook": True, "team_slack": bool(i["slack"].get("webhook_url")), "team_discord": bool(i["discord"].get("webhook_url"))}


def _project_public(p):
    m = STATE["manager"]
    rows = m.store.list()
    ids = {x["id"] for x in projects.all_projects()}
    mine = [t for t in rows if projects.project_of_task(t, ids) == p["id"]]
    return {**p, "task_count": len(mine), "active_count": len([t for t in mine if t.get("status") not in ("done", "failed", "stopped", "draft", "queued") and not t.get("archived")]),
            "attention_count": len([t for t in mine if t.get("pending") and not t.get("archived")])}


@bp.patch("/api/org/me")
def me_patch():
    u = current_user()
    b = request.get_json(silent=True) or {}
    if u.get("local") and "name" in b:
        identity.update_user(u["username"], {"name": b["name"]}, u)
    if "prefs" in b:
        identity.save_prefs(u["username"], b["prefs"])
    if "avatar_url" in b:
        url = str(b.get("avatar_url") or "").strip()
        if url and not re.match(r"^https://", url):
            return jsonify({"error": "Avatar address must start with https://"}), 400
        store = identity.raw_store()

        def fn(d):
            for x in d.get("users") or []:
                if x["username"] == u["username"]:
                    x["avatar_url"] = url
        store.update(fn)
    return me()


@bp.get("/api/org/me/notifications/latest")
def me_latest():
    m = STATE["manager"]
    rows = m.notifications[:5]
    return jsonify({"notifications": rows})


@bp.post("/api/org/me/channels/<channel>/test")
def me_test(channel):
    u = current_user()
    if channel not in notify.EXTERNAL:
        return jsonify({"error": "Unknown channel"}), 404
    user = identity.get(u["username"])
    target = STATE["dispatcher"]._target(user, channel, OS.load()["integrations"])
    if not target:
        return jsonify({"error": {"email": "Add your email address, and ask an admin to set up SMTP.", "telegram": "Add your chat id, and ask an admin to add the bot token.",
                                  "browser": "Allow browser notifications on this device first."}.get(channel, "Add an address for this channel first.")}), 400
    msg = _test_message(u)
    did = STATE["dispatcher"].enqueue(channel, target, msg, u["username"])
    return jsonify(STATE["dispatcher"].wait(did, timeout=25))


def _test_message(u):
    base = notify.base_url()
    return {"id": f"test-{int(time.time())}", "event": "delivered", "kind": "test", "level": "success", "title": "Test notification from Relay",
            "body": f"Sent by {u.get('name') or u['username']}. If you can read this, the channel works.", "time": now_iso(), "task": None,
            "links": {"relay": base or None}}


@bp.get("/api/org/me/deliveries")
def me_deliveries():
    return jsonify({"deliveries": STATE["dispatcher"].recent(100, current_user()["username"])})


@bp.get("/api/org/me/tokens")
def me_tokens():
    return jsonify({"tokens": tokens.list_for(current_user()["username"]), "scopes": [{"id": k, "role": v, "help": rbac.SCOPE_HELP[k]} for k, v in rbac.SCOPES.items()]})


@bp.post("/api/org/me/tokens")
def me_token_create():
    b = request.get_json(silent=True) or {}
    row, token = tokens.create(current_user(), b.get("name"), b.get("scopes") or [], b.get("expires_days"))
    return jsonify({"token": row, "secret": token, "note": "Copy this token now. Relay stores only its hash and cannot show it again."})


@bp.delete("/api/org/me/tokens/<tid>")
def me_token_revoke(tid):
    return jsonify(tokens.revoke(tid, current_user()))


@bp.post("/api/org/me/push")
def me_push_subscribe():
    b = request.get_json(silent=True) or {}
    notify.add_subscription(current_user()["username"], b.get("subscription") or {}, request.headers.get("User-Agent") or "")
    return jsonify({"ok": True, "subscriptions": notify.subscriptions(current_user()["username"])})


@bp.delete("/api/org/me/push")
def me_push_unsubscribe():
    b = request.get_json(silent=True) or {}
    notify.remove_subscription(current_user()["username"], b.get("endpoint") or "")
    return jsonify({"ok": True})


# ============================================================================ people
@bp.get("/api/org/users")
def users_list():
    lvl = rbac.level(role_now())
    rows = [identity.public(u) for u in identity.users()]
    if lvl < rbac.level("admin"):
        rows = [{k: r[k] for k in ("username", "name", "role", "initials", "color", "avatar_url", "local")} for r in rows]
    return jsonify({"users": rows, "roles": OS.ROLES, "identity_mode": OS.identity_mode()})


@bp.post("/api/org/users")
def users_invite():
    """Pre-create a person (for example before their first sign-in) with a role."""
    b = request.get_json(silent=True) or {}
    username = identity.norm_username(b.get("username"))
    if not username:
        return jsonify({"error": "Username is required (the Authentik username)."}), 400
    if identity.get(username):
        return jsonify({"error": f"{username} already exists."}), 400
    role = b.get("role") or "viewer"
    if role not in OS.ROLES:
        return jsonify({"error": "Unknown role"}), 400
    row = {"username": username, "name": str(b.get("name") or username)[:120], "email": str(b.get("email") or "")[:200], "role": role,
           "role_source": "manual", "first_seen": None, "groups": [], "invited_by": current_user()["username"], "invited_at": now_iso()}
    identity._save_user(row)
    return jsonify(identity.public(identity.get(username)))


@bp.patch("/api/org/users/<username>")
def users_patch(username):
    return jsonify(identity.public(identity.update_user(username, request.get_json(silent=True) or {}, current_user())))


@bp.delete("/api/org/users/<username>")
def users_delete(username):
    identity.delete_user(username, current_user())
    return jsonify({"ok": True})


@bp.get("/api/org/users/<username>")
def users_get(username):
    u = identity.get(username)
    if not u:
        return jsonify({"error": "No such user"}), 404
    return jsonify({**identity.public(u), "tokens": tokens.list_for(username)})


@bp.get("/api/org/tokens")
@need("owner")
def tokens_all():
    return jsonify({"tokens": tokens.list_for(None)})


@bp.delete("/api/org/tokens/<tid>")
def tokens_revoke_any(tid):
    return jsonify(tokens.revoke(tid, current_user()))


@bp.get("/api/org/access-matrix")
def access_matrix():
    return jsonify({"rules": rbac.matrix(), "roles": OS.ROLES, "scopes": rbac.SCOPES})


# ============================================================================ projects
@bp.get("/api/org/projects")
def projects_list():
    return jsonify({"projects": [_project_public(p) for p in projects.all_projects()], "default": projects.DEFAULT_ID})


@bp.post("/api/org/projects")
def projects_create():
    b = request.get_json(silent=True) or {}
    b.pop("id", None)
    return jsonify(_project_public(projects.save(b, current_user()["username"])))


@bp.patch("/api/org/projects/<pid>")
def projects_patch(pid):
    p = projects.get(pid)
    if not p:
        return jsonify({"error": "No such project"}), 404
    b = request.get_json(silent=True) or {}
    return jsonify(_project_public(projects.save({**p, **b, "id": p["id"]}, current_user()["username"])))


@bp.delete("/api/org/projects/<pid>")
def projects_delete(pid):
    m = STATE["manager"]

    def move(src, dst):
        for t in m.store.list():
            if t.get("project_id") == src:
                m.store.update(t["id"], touch=False, project_id=dst)
    projects.delete(pid, move)
    return jsonify({"ok": True})


@bp.post("/api/org/projects/<pid>/tasks")
def projects_move_tasks(pid):
    p = projects.get(pid)
    if not p:
        return jsonify({"error": "No such project"}), 404
    m = STATE["manager"]
    moved = []
    for tid in (request.get_json(silent=True) or {}).get("task_ids") or []:
        if m.store.get(tid):
            m.store.update(tid, touch=False, project_id=p["id"])
            m.emit_task(tid)
            moved.append(tid)
    return jsonify({"moved": moved})


# ============================================================================ organisation settings + integrations
@bp.get("/api/org/settings")
def org_settings():
    data = OS.public()
    return jsonify({**data, "identity_mode": OS.identity_mode(), "your_ip": client_ip(), "your_ip_trusted": OS.ip_trusted(client_ip()),
                    "webpush_available": webpush.AVAILABLE, "events": identity.NOTIFY_EVENTS})


@bp.put("/api/org/settings/auth")
def org_settings_auth():
    b = request.get_json(silent=True) or {}
    new = OS.save({"auth": b})
    return jsonify({**OS.public(new), "identity_mode": OS.identity_mode(new)})


@bp.put("/api/org/settings/integrations")
def org_settings_integrations():
    b = request.get_json(silent=True) or {}
    for w in b.get("webhooks") or []:
        w.setdefault("id", "wh_" + str(int(time.time() * 1000))[-8:])
        if w.get("url") and not re.match(r"^https?://", w["url"]):
            return jsonify({"error": "Webhook addresses start with http:// or https://"}), 400
    if b.get("public_url") and not re.match(r"^https?://", b["public_url"]):
        return jsonify({"error": "Relay's address starts with https://"}), 400
    return jsonify(OS.public(OS.save({"integrations": b})))


@bp.put("/api/org/budgets")
def org_budgets():
    b = request.get_json(silent=True) or {}
    if "org" in b:
        OS.save({"budgets": b["org"]})
    for pid, bud in (b.get("projects") or {}).items():
        p = projects.get(pid)
        if p:
            projects.save({**p, "budget": {**p["budget"], **bud}}, current_user()["username"])
    for un, amount in (b.get("users") or {}).items():
        identity.update_user(un, {"budget_monthly_usd": amount}, current_user())
    return jsonify({"ok": True})


@bp.post("/api/org/integrations/webpush/keys")
def org_webpush_keys():
    try:
        keys = notify.ensure_vapid()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"public_key": keys["public_key"]})


@bp.post("/api/org/integrations/test")
def org_integration_test():
    """Admin test-send to a team channel or organisation webhook."""
    b = request.get_json(silent=True) or {}
    ch = b.get("channel")
    org = OS.load()["integrations"]
    target = None
    if ch == "slack" and org["slack"].get("webhook_url"):
        target = {"key": org["slack"]["webhook_url"], "url": org["slack"]["webhook_url"], "label": "Slack (team)"}
    elif ch == "discord" and org["discord"].get("webhook_url"):
        target = {"key": org["discord"]["webhook_url"], "url": org["discord"]["webhook_url"], "label": "Discord (team)"}
    elif ch == "webhook":
        w = next((w for w in org.get("webhooks") or [] if w.get("id") == b.get("id")), None)
        if w:
            target = {"key": "org:" + w["id"], "url": w["url"], "secret": w.get("secret") or "", "label": w.get("name") or w["url"]}
    elif ch == "telegram" and org["telegram"].get("bot_token") and b.get("chat_id"):
        target = {"key": f"tg:{b['chat_id']}", "chat_id": str(b["chat_id"]), "label": f"Telegram {b['chat_id']}"}
    elif ch == "email" and org["smtp"].get("host") and b.get("address"):
        target = {"key": b["address"], "address": b["address"], "label": b["address"]}
    if not target:
        return jsonify({"error": "Configure this channel first (and give a chat id or address to test Telegram or email)."}), 400
    did = STATE["dispatcher"].enqueue(ch, target, _test_message(current_user()), current_user()["username"])
    return jsonify(STATE["dispatcher"].wait(did, timeout=25))


@bp.get("/api/org/deliveries")
def org_deliveries():
    return jsonify({"deliveries": STATE["dispatcher"].recent(int(request.args.get("limit") or 200))})


@bp.post("/api/org/integrations/telegram/webhook")
def telegram_webhook():
    """Telegram updates: /start and /link CODE in a chat, and presses on the inline buttons Relay sent."""
    tg = OS.load()["integrations"]["telegram"]
    secret = tg.get("webhook_secret") or ""
    if not tg.get("bot_token") or not secret or not _const_eq(request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "", secret):
        return jsonify({"error": "forbidden"}), 403
    up = request.get_json(silent=True) or {}
    api = (tg.get("api_base") or "https://api.telegram.org").rstrip("/") + f"/bot{tg['bot_token']}"
    d = STATE["dispatcher"]

    def call(method, payload):
        return d.http(f"{api}/{method}", json.dumps(payload).encode(), {"Content-Type": "application/json"})

    if up.get("message"):
        msg = up["message"]
        text = (msg.get("text") or "").strip()
        chat = (msg.get("chat") or {}).get("id")
        from_id = (msg.get("from") or {}).get("id")
        if text.startswith("/link"):
            u = notify.user_for_link_code(text.split(maxsplit=1)[1] if " " in text else "")
            if u:
                identity.link_telegram(u["username"], from_id)
                audit.record(u, "profile.telegram_link", {"type": "profile", "id": u["username"]}, via="telegram", detail=f"telegram user {from_id}")
                call("sendMessage", {"chat_id": chat, "text": f"Linked to Relay as {u.get('name') or u['username']}. Buttons on Relay messages now act as you."})
            else:
                call("sendMessage", {"chat_id": chat, "text": "That code is not valid. Copy it from Profile → Notifications in Relay."})
        elif text.startswith("/start"):
            call("sendMessage", {"chat_id": chat, "text": f"Relay notifications. Your chat id is {chat}: add it under Profile → Notifications, then send /link <code> from the same page so buttons act as you."})
        return jsonify({"ok": True})
    cq = up.get("callback_query")
    if not cq:
        return jsonify({"ok": True})
    data = str(cq.get("data") or "")
    act = notify.take_action(data[3:]) if data.startswith("rl:") else None
    user = identity.by_telegram((cq.get("from") or {}).get("id"))
    m = STATE["manager"]
    reply = ""
    if not act:
        reply = "This button has expired."
    elif not user:
        reply = "Link Telegram to your Relay account first (/link CODE)."
    elif rbac.level(user.get("role")) < rbac.level("member"):
        reply = "Answering needs the member role."
    else:
        g.org_actor_override, g.org_actor_via = user, "telegram"
        t = m.store.get(act["tid"]) or {}
        try:
            if (t.get("pending") or {}).get("id") != act["qid"]:
                raise ValueError("That question was already answered.")
            if act["action"] == "approve":
                m.approve(act["tid"], True, "Approved from Telegram")
                reply = "Approved"
            elif act["action"] == "reject":
                m.approve(act["tid"], False, "Changes requested from Telegram")
                reply = "Changes requested"
            else:
                m.answer(act["tid"], act["qid"], act["value"], {})
                reply = f"Answered: {act['value']}"
            audit.record(user, f"task.{act['action']}", {"type": "task", "id": act["tid"], "name": t.get("name"), "project": projects.project_of_task(t) if t else None},
                         via="telegram", detail=reply)
        except (ValueError, KeyError) as e:
            reply = str(e)
            audit.record(user, f"task.{act['action']}", {"type": "task", "id": act["tid"]}, via="telegram", outcome="error", detail=reply)
    call("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": reply[:190]})
    msgobj = cq.get("message") or {}
    if act and msgobj.get("message_id") and reply and user:
        call("editMessageReplyMarkup", {"chat_id": (msgobj.get("chat") or {}).get("id"), "message_id": msgobj["message_id"], "reply_markup": {"inline_keyboard": []}})
    return jsonify({"ok": True, "result": reply})


def _const_eq(a, b):
    import hmac
    return hmac.compare_digest(a.encode(), b.encode())


# ============================================================================ audit
@bp.get("/api/org/audit")
def audit_list():
    a = request.args
    return jsonify(audit.query(q=a.get("q") or "", actor=a.get("actor") or "", action=a.get("action") or "", obj_type=a.get("type") or "",
                               outcome=a.get("outcome") or "", since=a.get("since") or "", until=a.get("until") or "", project=a.get("project") or "",
                               limit=int(a.get("limit") or 200), offset=int(a.get("offset") or 0)))


@bp.get("/api/org/audit/export.csv")
def audit_csv():
    a = request.args
    rows = audit.query(q=a.get("q") or "", actor=a.get("actor") or "", action=a.get("action") or "", obj_type=a.get("type") or "",
                       outcome=a.get("outcome") or "", since=a.get("since") or "", until=a.get("until") or "", limit=100000)["entries"]
    audit.record(current_user(), "audit.export", {"type": "audit"}, via=g.get("org_via") or "", detail=f"{len(rows)} entries exported as CSV")
    return Response(audit.to_csv(rows), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=relay-audit-{datetime.now():%Y%m%d-%H%M}.csv"})


@bp.get("/api/org/audit/verify")
def audit_verify():
    return jsonify(audit.verify())


# ============================================================================ usage + onboarding
@bp.get("/api/org/usage")
def usage_get():
    a = request.args
    pid = a.get("project") or current_project_id()
    return jsonify(usage.summary(STATE["manager"].store.list(), a.get("month") or None, project=pid or None, user=a.get("user") or None))


@bp.get("/api/org/onboarding")
def onboarding_get():
    return jsonify(onboarding.checklist(STATE["manager"], current_user()["username"]))


@bp.post("/api/org/onboarding/dismiss")
def onboarding_dismiss():
    onboarding.dismiss(current_user()["username"], bool((request.get_json(silent=True) or {}).get("dismissed", True)))
    return onboarding_get()


@bp.post("/api/org/onboarding/team")
def onboarding_team():
    onboarding.choose_team((request.get_json(silent=True) or {}).get("preset") or "")
    STATE["manager"].config_changed()
    return onboarding_get()


@bp.post("/api/org/onboarding/sample-task")
def onboarding_sample():
    m = STATE["manager"]
    root = onboarding.ensure_sample_repo()
    from .. import agents as A
    health = A.agent_health(m.cfg())
    wf = {}
    if (health.get("claude") or {}).get("ok"):
        role = {"agent": "claude", "model": "haiku", "effort": "low"}
        wf = {"roles": {"supervisor": role, "worker": role, "reviewer": role}, "max_turns": 6}
    t = m.create_task({"repo": str(root), "requirements": onboarding.SAMPLE_REQUEST, "name": "Sample: greet() punctuation",
                       "template": "feature", "workflow": wf, "queue": True, "_sample": True,
                       "project_id": current_project_id() or projects.DEFAULT_ID})
    m.start()
    return jsonify({"task": t})
