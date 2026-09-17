"""Lessons learned from earlier tasks, reviewed by a person before any agent sees them.

Retrospectives (orchestrator/retro.py) propose lessons into a review queue. Nothing
in the queue reaches a prompt. Once approved, a lesson is filed either under its
repository (keyed like scorecards: the GitHub name, else the local path) or
globally, and future supervisor and worker kickoff prompts on that repository
carry it in a short "Lessons from earlier tasks" section (protocol.with_lessons).

Layout under DATA_DIR/lessons:
    queue.json           proposed and rejected lessons (rejected ones stop re-proposals)
    global.json          approved lessons for every repository
    repos/<slug>.json    {"repo": key, "lessons": [...]} approved for one repository
"""
from __future__ import annotations

import hashlib
import re
import threading

from .util import DATA_DIR, new_id, now, read_json, safe_slug, truncate, write_json

LESSONS_DIR = DATA_DIR / "lessons"
QUEUE_FILE = LESSONS_DIR / "queue.json"
GLOBAL_FILE = LESSONS_DIR / "global.json"
REPOS_DIR = LESSONS_DIR / "repos"
MAX_TEXT = 300
MAX_REJECTED = 300

_lock = threading.RLock()


def _norm(text) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _clean_text(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1].rstrip() + "…"


def _repo_file(key: str):
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
    # The end of a path names the repository; the hash keeps two with the same name apart.
    return REPOS_DIR / f"{safe_slug(key.replace('/', '__').strip('_')[-60:]).strip('_')}-{digest}.json"


def _read_queue() -> list:
    return read_json(QUEUE_FILE, []) or []


def _read_global() -> list:
    return read_json(GLOBAL_FILE, []) or []


def _read_repo(key: str) -> list:
    return (read_json(_repo_file(key), {}) or {}).get("lessons") or []


def _write_repo(key: str, rows: list):
    path = _repo_file(key)
    if rows:
        write_json(path, {"repo": key, "lessons": rows})
    elif path.exists():
        path.unlink()


def _all_repo_files() -> list[tuple[str, list]]:
    out = []
    if REPOS_DIR.is_dir():
        for p in sorted(REPOS_DIR.glob("*.json")):
            d = read_json(p, {}) or {}
            if d.get("repo"):
                out.append((d["repo"], d.get("lessons") or []))
    return out


def approved(repo: str | None = None) -> list[dict]:
    """Approved lessons: those for one repository plus global ones, or all of them when repo is None."""
    with _lock:
        rows = [dict(x, scope="global", repo="") for x in _read_global()]
        if repo is None:
            for key, items in _all_repo_files():
                rows += [dict(x, scope="repo", repo=key) for x in items]
        elif repo:
            rows += [dict(x, scope="repo", repo=repo) for x in _read_repo(repo)]
        return rows


def listing() -> dict:
    with _lock:
        q = _read_queue()
        return {"queue": [x for x in q if x.get("status") == "proposed"],
                "rejected": [x for x in q if x.get("status") == "rejected"][-50:],
                "approved": sorted(approved(), key=lambda x: x.get("approved_at") or x.get("created_at") or "", reverse=True)}


def pending_count() -> int:
    with _lock:
        return sum(1 for x in _read_queue() if x.get("status") == "proposed")


def _known(scope: str, repo: str) -> set:
    rows = _read_queue() + [dict(x) for x in _read_global()]
    if scope == "repo" and repo:
        rows += _read_repo(repo)
    # An approved lesson may have been reworded; its original wording still counts as known.
    return {_norm(x.get(k)) for x in rows for k in ("text", "original_text") if x.get(k)}


def propose(items: list[dict], task: dict, repo: str, repo_label: str = "") -> list[dict]:
    """Queue lessons from a retrospective. Duplicates of queued, rejected or approved lessons are dropped."""
    added = []
    with _lock:
        q = _read_queue()
        for it in items or []:
            text = _clean_text(it.get("text"))
            scope = "global" if it.get("scope") == "global" or not repo else "repo"
            if len(_norm(text)) < 12 or _norm(text) in _known(scope, repo) | {_norm(x["text"]) for x in added}:
                continue
            row = {"id": new_id("l_"), "status": "proposed", "scope": scope, "repo": repo if scope == "repo" else "",
                   "proposed_repo": repo, "repo_label": repo_label, "text": text, "evidence": truncate(str(it.get("evidence") or ""), 500),
                   "task_id": task.get("id"), "task_name": task.get("name"), "source": "retro", "created_at": now()}
            added.append(row)
        if added:
            q += added
            rejected = [x for x in q if x.get("status") == "rejected"]
            if len(rejected) > MAX_REJECTED:
                drop = {x["id"] for x in rejected[:-MAX_REJECTED]}
                q = [x for x in q if x["id"] not in drop]
            write_json(QUEUE_FILE, q)
    return added


def _file_approved(row: dict):
    row = {k: v for k, v in row.items() if k not in ("status", "scope", "repo")}
    return row


def _find_approved(lid):
    """(scope, repo, rows, index) of an approved lesson, or None."""
    rows = _read_global()
    for i, x in enumerate(rows):
        if x.get("id") == lid:
            return "global", "", rows, i
    for key, items in _all_repo_files():
        for i, x in enumerate(items):
            if x.get("id") == lid:
                return "repo", key, items, i
    return None


def _save_approved(scope, repo, rows):
    if scope == "global":
        write_json(GLOBAL_FILE, rows)
    else:
        _write_repo(repo, rows)


def _place(row: dict, scope: str, repo: str):
    if scope == "repo" and not repo:
        raise ValueError("A repository lesson needs a repository.")
    rows = _read_global() if scope == "global" else _read_repo(repo)
    rows.append(_file_approved(row))
    _save_approved(scope, repo, rows)


def approve(lid: str, text: str | None = None, scope: str | None = None, repo: str | None = None) -> dict:
    with _lock:
        q = _read_queue()
        row = next((x for x in q if x.get("id") == lid), None)
        if not row:
            raise KeyError("Lesson not found in the review queue")
        text = _clean_text(text if text is not None else row["text"])
        if len(_norm(text)) < 3:
            raise ValueError("The lesson is empty.")
        scope = scope or row.get("scope") or "repo"
        repo = (repo if repo is not None else (row.get("repo") or row.get("proposed_repo") or "")) if scope == "repo" else ""
        out = {**row, "text": text, "approved_at": now(), "edited": text != row["text"]}
        if out["edited"]:
            out["original_text"] = row["text"]
        _place(out, scope, repo)
        write_json(QUEUE_FILE, [x for x in q if x.get("id") != lid])
        return {**_file_approved(out), "scope": scope, "repo": repo, "status": "approved"}


def reject(lid: str) -> dict:
    with _lock:
        q = _read_queue()
        row = next((x for x in q if x.get("id") == lid), None)
        if not row:
            raise KeyError("Lesson not found in the review queue")
        row.update(status="rejected", rejected_at=now())
        write_json(QUEUE_FILE, q)
        return row


def update(lid: str, patch: dict) -> dict:
    """Edit text, scope or repository of a queued or approved lesson; `enabled` switches an approved one off."""
    with _lock:
        q = _read_queue()
        row = next((x for x in q if x.get("id") == lid), None)
        if row:
            if "text" in patch:
                row["text"] = _clean_text(patch["text"])
            if patch.get("scope") in ("repo", "global"):
                row["scope"] = patch["scope"]
                row["repo"] = (row.get("proposed_repo") or "") if patch["scope"] == "repo" else ""
            row["updated_at"] = now()
            write_json(QUEUE_FILE, q)
            return row
        hit = _find_approved(lid)
        if not hit:
            raise KeyError("Lesson not found")
        scope, repo, rows, i = hit
        cur = dict(rows[i])
        if "text" in patch:
            cur["text"] = _clean_text(patch["text"])
            cur["edited"] = True
        if "enabled" in patch:
            cur["enabled"] = bool(patch["enabled"])
        cur["updated_at"] = now()
        new_scope = patch.get("scope") if patch.get("scope") in ("repo", "global") else scope
        new_repo = (patch.get("repo") or repo or cur.get("proposed_repo") or "") if new_scope == "repo" else ""
        if (new_scope, new_repo) != (scope, repo):
            if new_scope == "repo" and not new_repo:
                raise ValueError("A repository lesson needs a repository.")
            del rows[i]
            _save_approved(scope, repo, rows)
            _place(cur, new_scope, new_repo)
        else:
            rows[i] = cur
            _save_approved(scope, repo, rows)
        return {**cur, "scope": new_scope, "repo": new_repo, "status": "approved"}


def delete(lid: str):
    with _lock:
        q = _read_queue()
        if any(x.get("id") == lid for x in q):
            write_json(QUEUE_FILE, [x for x in q if x.get("id") != lid])
            return
        hit = _find_approved(lid)
        if not hit:
            raise KeyError("Lesson not found")
        scope, repo, rows, i = hit
        del rows[i]
        _save_approved(scope, repo, rows)


def add(text: str, scope: str = "global", repo: str = "") -> dict:
    """A lesson written by a person goes straight to the approved list."""
    text = _clean_text(text)
    if len(_norm(text)) < 3:
        raise ValueError("The lesson is empty.")
    scope = scope if scope in ("repo", "global") else "global"
    with _lock:
        row = {"id": new_id("l_"), "text": text, "evidence": "", "source": "manual", "created_at": now(), "approved_at": now(),
               "proposed_repo": repo if scope == "repo" else ""}
        _place(row, scope, repo if scope == "repo" else "")
        return {**row, "scope": scope, "repo": repo if scope == "repo" else "", "status": "approved"}


def for_prompt(repo: str, limit: int = 15) -> list[dict]:
    """What a new task on this repository is told: its own lessons first (newest first), then global ones."""
    with _lock:
        own = [dict(x, scope="repo") for x in _read_repo(repo)] if repo else []
        glob = [dict(x, scope="global") for x in _read_global()]
    pick = lambda rows: sorted((x for x in rows if x.get("enabled", True)), key=lambda x: x.get("approved_at") or "", reverse=True)
    return (pick(own) + pick(glob))[:max(0, int(limit))]


def prompt_block(rows: list[dict]) -> str:
    if not rows:
        return ""
    own = [x for x in rows if x.get("scope") == "repo"]
    glob = [x for x in rows if x.get("scope") != "repo"]
    lines = ["LESSONS FROM EARLIER TASKS ON THIS REPOSITORY",
             "Reviewed and approved by the operator after earlier runs. Apply them where they are relevant; "
             "the task request and the rules take precedence if they conflict."]
    lines += [f"- {x['text']}" for x in own]
    if glob:
        lines += (["From tasks on other repositories:"] if own else []) + [f"- {x['text']}" for x in glob]
    return "\n".join(lines)


def kickoff_block(manager, task: dict, cfg: dict) -> str:
    """The lessons section for a task's kickoff prompts; records which lessons it used on the task."""
    if not cfg.get("lessons_inject", True):
        return ""
    from .scorecard import repo_key
    try:
        rows = for_prompt(repo_key(task), int(cfg.get("lessons_max_in_prompt") or 15))
    except Exception:  # an unreadable lessons file must not stop a task
        return ""
    manager.set_meta(task["id"], lessons_used=[x["id"] for x in rows])
    if rows:
        manager.timeline(task["id"], "system", "Lessons from earlier tasks", f"{len(rows)} approved lesson(s) added to the kickoff prompts")
    return prompt_block(rows)
