"""HTTP API of the knowledge docs (orchestrator/knowledge.py), as a Flask blueprint.

    GET    /api/knowledge/docs                  every doc (platform + repos/) with its staleness, and whether you may edit
    GET    /api/knowledge/doc?path=             one doc's markdown and sha
    GET    /api/knowledge/search?q=             docs containing every word, with matching lines
    POST   /api/knowledge/docs/save             {path, text, base_sha}: owners and admins; audited (org/web.py)
    POST   /api/knowledge/refresh               {repo?, force?}: check (and refresh stale) repository docs in the background

Writes need the admin role (org/rbac.py); the audit log records who saved which doc with the before and after sha.
"""
from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

from orchestrator import knowledge

bp = Blueprint("knowledge", __name__)
_manager = None
_broadcast = None


def init(manager, broadcast):
    global _manager, _broadcast
    _manager, _broadcast = manager, broadcast
    return bp


def _body():
    return request.get_json(silent=True) or {}


def _can_edit() -> bool:
    try:
        from orchestrator.org import rbac, web as org_web
        return rbac.level(org_web.role_now()) >= rbac.level("admin")
    except Exception:
        return False


def _user() -> str:
    try:
        from orchestrator.org import web as org_web
        return (org_web.current_user() or {}).get("username") or ""
    except Exception:
        return ""


@bp.get("/api/knowledge/docs")
def knowledge_docs():
    state = knowledge.load_state()
    return jsonify({"docs": knowledge.list_docs(), "root": str(knowledge.KNOWLEDGE_DIR), "can_edit": _can_edit(),
                    "busy": knowledge.busy(), "last_run": state.get("last_run"),
                    "settings": knowledge.settings(_manager.cfg() if _manager else {})})


@bp.get("/api/knowledge/doc")
def knowledge_doc():
    try:
        return jsonify(knowledge.read_doc(request.args.get("path") or ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e).strip("'")}), 404


@bp.get("/api/knowledge/search")
def knowledge_search():
    return jsonify({"q": request.args.get("q") or "", "results": knowledge.search(request.args.get("q") or "")})


@bp.post("/api/knowledge/docs/save")
def knowledge_save():
    b = _body()
    try:
        doc = knowledge.save_doc(b.get("path") or "", b.get("text") or "", b.get("base_sha") or "", by=_user())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409
    _broadcast("knowledge", {"path": doc["path"], "saved": True})
    return jsonify({**doc, "id": doc["path"]})


@bp.post("/api/knowledge/refresh")
def knowledge_refresh():
    b = _body()
    repo, force = str(b.get("repo") or "").strip(), bool(b.get("force"))
    if repo and not knowledge.doc_path_for(repo):
        return jsonify({"error": f"No knowledge doc for {repo}"}), 404

    def run():
        try:
            res = knowledge.check_all(_manager.cfg(), refresh=True, force=force, only=repo,
                                      estimate_cost=getattr(_manager, "estimate_cost", None))
            _broadcast("knowledge", {"refreshed": [{k: r.get(k) for k in ("repo", "status", "behind", "error", "sections_changed")} for r in res]})
        except Exception as e:
            _broadcast("knowledge", {"error": str(e)[:300]})
    threading.Thread(target=run, name="relay-knowledge-refresh", daemon=True).start()
    return jsonify({"ok": True, "queued": True, "id": repo or "all"}), 202
