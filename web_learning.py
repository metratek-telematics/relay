"""HTTP API of the learning engine (orchestrator/learning_engine.py), as a Flask blueprint.

    GET    /api/learning                          everything the Learning page shows
    GET    /api/shipped                           the honest success record (#65)
    POST   /api/shipped/backfill                  rebuild it from the task records and scorecards
    GET    /api/learning/outcomes?limit=&repo=    the outcome dataset (latest record per run)
    POST   /api/learning/preflight                recommendation + risk for a draft task
    POST   /api/learning/backfill                 outcome records for scored runs that have none
    POST   /api/learning/proposals/<id>/apply     one-click improvement
    POST   /api/learning/proposals/<id>/dismiss
    POST   /api/learning/lessons/<id>/retire      switch a lesson off with the reason
    POST   /api/learning/pin                      {repo, team_key|null}: pin or unpin a team for a repository
    GET    /api/learning/playbook?repo=
    POST   /api/learning/playbook/seed            {repo, repo_path?}
    POST   /api/learning/playbook/refresh         {repo}: cheap agent turn in the background
    PATCH  /api/learning/playbook                 {repo, section, text | accept_suggestion}
"""
from __future__ import annotations

import threading

from flask import Blueprint, jsonify, request

from orchestrator import config as C, lessons, outcomes, playbooks

bp = Blueprint("learning", __name__)
_manager = None
_broadcast = None


def init(manager, broadcast):
    global _manager, _broadcast
    _manager, _broadcast = manager, broadcast
    return bp


def _engine():
    return _manager.learning.engine


def _body():
    return request.get_json(silent=True) or {}


@bp.get("/api/learning")
def learning_view():
    return jsonify(_engine().view())


@bp.get("/api/shipped")
def shipped_view():
    """The honest success record (#65): merged, deployed and untouched, computed from facts."""
    return jsonify(_manager.learning.shipped_view())


@bp.post("/api/shipped/backfill")
def shipped_backfill():
    n = _manager.learning.shipped_backfill()
    return jsonify({"rebuilt": n, **_manager.learning.shipped_view()})


@bp.get("/api/learning/outcomes")
def learning_outcomes():
    rows = _engine().records()
    repo = request.args.get("repo")
    if repo:
        rows = [r for r in rows if r.get("repo") == repo]
    limit = max(1, min(5000, int(request.args.get("limit") or 500)))
    return jsonify({"outcomes": rows[-limit:], "total": len(rows)})


@bp.post("/api/learning/preflight")
def learning_preflight():
    return jsonify(_engine().preflight(_body()))


@bp.post("/api/learning/backfill")
def learning_backfill():
    return jsonify({"added": _engine().backfill()})


@bp.post("/api/learning/proposals/<pid>/<action>")
def learning_proposal(pid, action):
    if action == "apply":
        p = _engine().apply_proposal(pid)
    elif action == "dismiss":
        p = _engine().dismiss_proposal(pid)
    else:
        return jsonify({"error": f"Unknown action {action}"}), 404
    _broadcast("learning", {"proposals": _engine().open_proposals_count()})
    return jsonify({"proposal": p})


@bp.post("/api/learning/lessons/<lid>/retire")
def learning_retire(lid):
    row = lessons.update(lid, {"retire": True, "reason": _body().get("reason") or "retired from Learning"})
    return jsonify({"lesson": row})


@bp.post("/api/learning/pin")
def learning_pin():
    b = _body()
    repo = (b.get("repo") or "").strip()
    if not repo:
        return jsonify({"error": "Missing repo"}), 400
    teams = dict(_engine().settings().get("repo_teams") or {})
    if b.get("team_key"):
        teams[repo] = b["team_key"]
    else:
        teams.pop(repo, None)
    # A nested dict update merges keys, so an unpin writes the whole map back.
    cfg = C.load()
    cfg.setdefault("learning", {})["repo_teams"] = teams
    C.save(cfg)
    _manager.config_changed()
    return jsonify({"repo_teams": teams, "label": outcomes.team_label(b.get("team_key") or "", C.AGENTS)})


@bp.get("/api/learning/playbook")
def learning_playbook():
    pb = playbooks.load(request.args.get("repo") or "")
    if not pb:
        return jsonify({"error": "No playbook for this repository yet"}), 404
    return jsonify({**pb, "labels": playbooks.SECTION_LABEL})


@bp.post("/api/learning/playbook/seed")
def learning_playbook_seed():
    b = _body()
    return jsonify({**_engine().seed_playbook(b.get("repo") or "", b.get("repo_path") or ""), "labels": playbooks.SECTION_LABEL})


@bp.post("/api/learning/playbook/refresh")
def learning_playbook_refresh():
    repo = _body().get("repo") or ""
    if not repo:
        return jsonify({"error": "Missing repo"}), 400

    def run():
        try:
            _engine().refresh_playbook_with_agent(repo)
            _broadcast("learning", {"playbook": repo})
        except Exception as e:
            _broadcast("learning", {"playbook": repo, "error": str(e)[:300]})
    threading.Thread(target=run, name="relay-playbook", daemon=True).start()
    return jsonify({"ok": True, "queued": True}), 202


@bp.patch("/api/learning/playbook")
def learning_playbook_edit():
    b = _body()
    pb = _engine().edit_playbook(b.get("repo") or "", b.get("section") or "", b.get("text") or "", bool(b.get("accept_suggestion")))
    return jsonify({**pb, "labels": playbooks.SECTION_LABEL})
