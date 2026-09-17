"""Relay local backend: HTTP API + Server-Sent Events for the browser workspace."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from orchestrator import agents, config as C, github, gitops, handoff, history, repos  # noqa: E402
from orchestrator import installer, lessons  # noqa: E402
from orchestrator.scorecard import repo_label  # noqa: E402

# Agents installed from the Agents page must be found by health checks, runs and verification alike.
installer.extend_path()
from orchestrator.manager import Manager  # noqa: E402
from orchestrator.util import IN_DOCKER, IS_WINDOWS, RUNTIME_DIR, quiet, read_text  # noqa: E402

# 127.0.0.1 keeps Relay private to this machine. Inside Docker it must listen on
# 0.0.0.0, and the compose file publishes the port on the host's loopback only.
HOST = os.environ.get("RELAY_HOST", "0.0.0.0" if IN_DOCKER else "127.0.0.1")
PORT = int(os.environ.get("RELAY_PORT", "8767"))


def ensure_single_instance():
    """Refuse to start a second orchestrator against the same state directory.

    Two instances would each hold their own task list in memory and overwrite
    each other's file, which loses tasks.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.6)
    already = probe.connect_ex(("127.0.0.1" if HOST in ("0.0.0.0", "") else HOST, PORT)) == 0
    probe.close()
    if already:
        print(f"Relay is already running at http://{HOST}:{PORT}.")
        print("Open that window instead, or close it before starting a new one.")
        sys.exit(1)

app = Flask(__name__, static_folder=str(ROOT / "web"), static_url_path="")
app.json.sort_keys = False

# ----------------------------------------------------------------------------- SSE fan-out
subs: list[queue.Queue] = []
subs_lock = threading.RLock()


def broadcast(typ, payload):
    event = {"type": typ, "payload": payload, "ts": time.time()}
    data = json.dumps(event, ensure_ascii=False)
    with subs_lock:
        dead = []
        for q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            if q in subs:
                subs.remove(q)


manager = Manager(broadcast)


@app.after_request
def no_cache(resp):
    if request.path == "/" or request.path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return resp


# ----------------------------------------------------------------------------- helpers
def body():
    if not request.data:
        return {}
    data = request.get_json(silent=True)
    if data is None:
        raise ValueError("Request body is not valid JSON")
    return data or {}


def task_or_404(tid):
    t = manager.get(tid)
    if not t:
        raise KeyError("Task not found")
    return t


def task_root(tid):
    t = task_or_404(tid)
    root = t.get("worktree") or t.get("repo")
    if not root:
        raise FileNotFoundError("Task has no repository or worktree yet")
    return Path(root).resolve()


def safe_repo_path(tid, rel):
    root = task_root(tid)
    rel = (rel or "").replace("\\", "/").lstrip("/")
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise PermissionError("Path escapes repository")
    return root, p


IGNORED_DIRS = {".git", "node_modules", "dist", "build", ".next", ".nuxt", "coverage", ".idea", ".venv", "venv",
                "__pycache__", ".pytest_cache", ".mypy_cache", "target", "bin", "obj", ".orchestrator_refs"}


def repo_tree(path, root, depth, max_depth, budget):
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    rel = str(path.relative_to(root)).replace("\\", "/") if path != root else ""
    node = {"name": path.name or root.name, "path": rel, "type": "dir" if path.is_dir() else "file"}
    if path.is_file():
        try:
            node["size"] = path.stat().st_size
        except Exception:
            pass
    if path.is_dir() and depth < max_depth:
        children = []
        try:
            entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            entries = []
        for child in entries:
            if child.name in IGNORED_DIRS:
                continue
            c = repo_tree(child, root, depth + 1, max_depth, budget)
            if c:
                children.append(c)
        node["children"] = children
    return node


# ----------------------------------------------------------------------------- static
@app.get("/")
def index():
    return send_from_directory(str(ROOT / "web"), "index.html")


# ----------------------------------------------------------------------------- meta
# Changes on every server start, so an open browser can tell it is running code from before a deploy.
BOOT_ID = f"{time.time():.0f}"


@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "service": "Relay", "build": C.BUILD, "boot": BOOT_ID, "port": PORT, "time": time.time()})


@app.get("/api/build")
def build():
    return jsonify({"build": C.BUILD, "name": C.APP_NAME, "port": PORT})


@app.get("/api/state")
def state():
    cfg = manager.cfg()
    return jsonify({
        "build": C.BUILD,
        "tasks": manager.tasks,
        "config": C.public_view(cfg),
        "agents": agents.agent_health(cfg),
        "agent_meta": C.AGENTS,
        "presets": C.PRESETS,
        "templates": C.TASK_TEMPLATES,
        "github": {**manager.github_status, "sources": github.load_sources()},
        "queue": manager.queue_state(),
        "notifications": manager.notifications[:30],
        "lessons_pending": lessons.pending_count(),
    })


@app.get("/api/events")
def events():
    def gen():
        q = queue.Queue(maxsize=5000)
        with subs_lock:
            subs.append(q)
        try:
            yield "event: ping\ndata: {}\n\n"
            while True:
                try:
                    data = q.get(timeout=15)
                    yield "data: " + data + "\n\n"
                except queue.Empty:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            with subs_lock:
                if q in subs:
                    subs.remove(q)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/health")
def health():
    cfg = manager.cfg()
    force = request.args.get("force") == "1"
    return jsonify({"agents": agents.agent_health(cfg, force=force), "github": github.auth_info() if force else manager.github_status})


@app.get("/api/dashboard")
def dashboard():
    return jsonify(manager.dashboard())


# ----------------------------------------------------------------------------- agents
@app.get("/api/agents")
def agents_get():
    return jsonify({"health": agents.agent_health(manager.cfg(), force=request.args.get("force") == "1"), "meta": C.AGENTS})


@app.post("/api/agents/<name>/install")
def agent_install(name):
    """Install, update or remove a pack agent's CLI in the background (poll /api/agents/jobs)."""
    spec = C.AGENTS.get(name)
    if not spec or not spec.get("install"):
        return jsonify({"error": f"{name} cannot be installed from Relay"}), 400
    action = body().get("action") or "install"

    def done(agent):
        agents.invalidate_health()
        broadcast("agents", {"agent": agent, "job": installer.job(agent)})
    try:
        return jsonify(installer.start({"id": name, **spec}, action, on_done=done))
    except ValueError as e:
        return jsonify({"error": str(e)}), 409


@app.get("/api/agents/jobs")
def agent_jobs():
    return jsonify({name: installer.job(name) for name in C.AGENTS if installer.job(name)})


@app.post("/api/agents/<name>/test")
def agent_test(name):
    """Run a one-line prompt through the agent to prove auth + structured output work."""
    cfg = manager.cfg()
    ad = agents.adapter(name)
    from orchestrator.agents import TurnContext
    scratch = RUNTIME_DIR / "_agent_tests"
    scratch.mkdir(parents=True, exist_ok=True)
    if not (scratch / ".git").exists():
        quiet(["git", "init", "-q"], cwd=scratch, timeout=30)
    args, env, stdin, session = ad.build("Reply with exactly the single word: pong", scratch, cfg, body().get("model") or "", None, scratch, "test")
    ctx = TurnContext()
    started = time.time()
    lines = []
    try:
        p = subprocess.run(args, cwd=str(scratch), input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        for line in out.splitlines():
            lines.append(line)
            try:
                ad.parse_line(line, ctx)
            except Exception:
                pass
        res = ad.finalize(ctx, p.returncode, scratch, session)
        txt = (res.get("last_message") or res.get("text") or "").strip()
        ok = p.returncode == 0 and "pong" in txt.lower()
        agents.invalidate_health()
        return jsonify({"ok": ok, "rc": p.returncode, "seconds": round(time.time() - started, 1), "reply": txt[:300],
                        "session_id": res.get("session_id"), "model": res.get("model"), "usage": res.get("usage"),
                        "error": None if ok else (res.get("error") or "\n".join([l for l in lines if l.strip()][-8:]))})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timed out after 180 s", "seconds": 180}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ----------------------------------------------------------------------------- settings
@app.get("/api/settings")
def settings_get():
    return jsonify(C.public_view(manager.cfg()))


@app.post("/api/settings")
def settings_post():
    cfg = C.update(body())
    manager.config_changed()
    agents.invalidate_health()
    broadcast("config", C.public_view(cfg))
    return jsonify(C.public_view(cfg))


@app.get("/api/presets")
def presets():
    return jsonify({"presets": C.PRESETS, "templates": C.TASK_TEMPLATES, "agents": C.AGENTS})


@app.get("/api/rules")
def rules_list():
    out = []
    for p in sorted((ROOT / "rules").glob("*.md")):
        out.append({"name": p.stem, "size": p.stat().st_size})
    return jsonify(out)


@app.get("/api/rules/<name>")
def rules_get(name):
    p = ROOT / "rules" / f"{Path(name).stem}.md"
    if not p.exists():
        return jsonify({"error": "Not found"}), 404
    return jsonify({"name": p.stem, "text": read_text(p)})


@app.put("/api/rules/<name>")
def rules_put(name):
    p = ROOT / "rules" / f"{Path(name).stem}.md"
    text = body().get("text")
    if text is None:
        return jsonify({"error": "Missing text"}), 400
    p.write_text(text, encoding="utf-8")
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------- filesystem
@app.get("/api/fs/browse")
def fs_browse():
    raw = (request.args.get("path") or "").strip()
    if not raw:
        if IS_WINDOWS:
            drives = [f"{d}:\\" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{d}:\\").exists()]
            home = str(Path.home())
            return jsonify({"path": "", "parent": None, "dirs": [{"name": d, "path": d, "git": False} for d in drives]
                            + [{"name": "Home · " + home, "path": home, "git": False}], "is_git": False})
        # On Linux start somewhere useful: RELAY_BROWSE_ROOT (the mounted repos in
        # Docker) or the user's home, instead of the filesystem root.
        raw = os.environ.get("RELAY_BROWSE_ROOT") or str(Path.home())
    p = Path(raw).expanduser()
    if not p.exists() or not p.is_dir():
        return jsonify({"error": "Folder not found"}), 404
    dirs = []
    try:
        for c in sorted(p.iterdir(), key=lambda x: x.name.lower()):
            if c.is_dir() and not c.name.startswith(".") and c.name.lower() not in ("node_modules", "$recycle.bin", "system volume information"):
                dirs.append({"name": c.name, "path": str(c), "git": (c / ".git").exists()})
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else ("" if IS_WINDOWS else None)
    return jsonify({"path": str(p), "parent": parent, "dirs": dirs[:400], "is_git": (p / ".git").exists()})


@app.get("/api/fs/repo-info")
def fs_repo_info():
    raw = (request.args.get("path") or "").strip().strip('"')
    if not raw or not Path(raw).exists():
        return jsonify({"error": "Folder not found"}), 404
    return jsonify(gitops.repo_summary(raw))


# ----------------------------------------------------------------------------- tasks
@app.post("/api/tasks")
def task_create():
    try:
        return jsonify(manager.create_task(body()))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/tasks")
def task_list():
    return jsonify(manager.tasks)


@app.get("/api/tasks/<tid>")
def task_get(tid):
    return jsonify(task_or_404(tid))


@app.patch("/api/tasks/<tid>")
def task_patch(tid):
    return jsonify(manager.update_task(tid, body()))


@app.delete("/api/tasks/<tid>")
def task_delete(tid):
    manager.delete(tid, delete_worktree=request.args.get("worktree") == "1")
    return jsonify({"ok": True})


@app.post("/api/tasks/<tid>/<action>")
def task_action(tid, action):
    b = body()
    task_or_404(tid)
    try:
        if action == "start":
            return jsonify({"ok": True, "task": manager.start_task(tid)})
        elif action == "stop":
            manager.stop(tid)
        elif action == "pause":
            manager.pause(tid)
        elif action == "resume":
            manager.resume(tid)
        elif action == "retry":
            manager.retry(tid, fresh=bool(b.get("fresh")))
        elif action == "answer":
            manager.answer(tid, b.get("id"), (b.get("text") or "").strip(), b.get("extra") or {})
        elif action == "approve":
            manager.approve(tid, True, b.get("note") or "")
        elif action == "reject":
            manager.approve(tid, False, b.get("note") or "")
        elif action == "guidance":
            return jsonify(manager.guidance(tid, b.get("text"), b.get("to") or "next", b.get("mode") or "queue"))
        elif action == "archive":
            manager.archive(tid, b.get("archived", True))
        elif action == "duplicate":
            return jsonify(manager.duplicate(tid))
        elif action == "queue":
            return jsonify(manager.update_task(tid, {"status": "queued"}))
        else:
            return jsonify({"error": f"Unknown action {action}"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "task": manager.get(tid)})


@app.get("/api/tasks/<tid>/messages")
def task_messages(tid):
    task_or_404(tid)
    after = int(request.args.get("after", "0") or 0)
    limit = int(request.args.get("limit", "0") or 0)
    return jsonify({"messages": manager.store.messages(tid, after=after, limit=limit), "count": manager.store.message_count(tid)})


@app.get("/api/tasks/<tid>/files")
def task_files(tid):
    t = task_or_404(tid)
    base = gitops.task_base(t)
    files, stat = gitops.changed_files(t.get("worktree"), base), gitops.diff_stat(t.get("worktree"), base)
    if t.get("status") in ("done", "failed", "stopped") and Path(t.get("worktree") or "").exists() and t.get("diffstat") != stat:
        # Tasks delivered before base-aware counts stored 0 files; correct them when they are looked at.
        manager.set_meta(tid, diffstat=stat, changed_count=len(files), base_commit=base)
    return jsonify({"files": files, "stat": stat})


@app.get("/api/tasks/<tid>/handoff")
def task_handoff(tid):
    return jsonify(handoff.build(task_or_404(tid), manager.cfg()))


@app.get("/api/tasks/<tid>/commits")
def task_commits(tid):
    return jsonify(history.commits(task_or_404(tid)))


@app.get("/api/tasks/<tid>/commits/<sha>/diff")
def task_commit_diff(tid, sha):
    return jsonify(history.commit_diff(task_or_404(tid), sha, request.args.get("path", "")))


@app.get("/api/tasks/<tid>/work")
def task_work(tid):
    return jsonify(history.work_segments(task_or_404(tid), manager.store.messages(tid)))


@app.get("/api/tasks/<tid>/pr")
def task_pr(tid):
    t = task_or_404(tid)
    try:
        return jsonify(history.pr_status(t, force=request.args.get("force") == "1"))
    except Exception as e:  # the header polls this; a GitHub hiccup must read as a message, not a server error
        return jsonify({"ok": False, "error": str(e) or e.__class__.__name__})


@app.get("/api/branch-name")
def branch_name():
    a = request.args
    repo = (a.get("repo") or "").strip()
    name = (a.get("name") or "").strip()
    requirements = a.get("requirements") or ""
    if not name:
        name = requirements.strip().splitlines()[0][:60] if requirements.strip() else ""
    return jsonify({"branch": gitops.suggest_branch(manager.cfg(), name, requirements, a.get("template") or "feature",
                                                     (a.get("issue") or "").strip().lstrip("#"), repo or None,
                                                     manager.taken_branches(repo))})


@app.get("/api/tasks/<tid>/diff")
def task_diff(tid):
    t = task_or_404(tid)
    path = request.args.get("path", "")
    if path:
        return jsonify({"text": gitops.diff_file(t.get("worktree"), path, gitops.task_base(t))})
    return jsonify({"text": gitops.full_diff(t.get("worktree"), 400000, gitops.task_base(t))})


@app.get("/api/tasks/<tid>/artifact/<kind>")
def task_artifact(tid, kind):
    t = task_or_404(tid)
    names = {"plan": "PLAN.md", "acceptance": "ACCEPTANCE.md", "implementation": "IMPLEMENTATION.md", "verification": "VERIFICATION.md",
             "review": "REVIEW.md", "report": "REPORT.md", "raw": "raw.log", "pr_body": "PR_BODY.md", "issue": "ISSUE.json", "retro": "RETRO.md"}
    cands = []
    art = (t.get("artifacts") or {}).get(kind)
    if art:
        cands.append(Path(art))
    if t.get("run_dir") and kind in names:
        cands.append(Path(t["run_dir"]) / names[kind])
    cands.append(RUNTIME_DIR / tid / names.get(kind, kind))
    for p in cands:
        if p.exists():
            txt = read_text(p)
            if kind == "raw":
                tail = int(request.args.get("tail", "0") or 0)
                if tail and len(txt) > tail:
                    txt = txt[-tail:]
            return jsonify({"text": txt, "path": str(p), "size": p.stat().st_size})
    return jsonify({"text": "", "path": None, "size": 0})


@app.get("/api/tasks/<tid>/export")
def task_export(tid):
    t = task_or_404(tid)
    run = RUNTIME_DIR / tid
    parts = [f"# {t['name']}\n", f"- Status: {t['status']} · {t.get('detail','')}", f"- Repository: {t['repo']}",
             f"- Branch: {t.get('branch') or '-'}", f"- PR: {t.get('pr_url') or '-'}", f"- Created: {t['created_at']}", ""]
    for kind, name in (("Plan", "PLAN.md"), ("Acceptance", "ACCEPTANCE.md"), ("Latest report", "IMPLEMENTATION.md"),
                       ("Verification", "VERIFICATION.md"), ("Review", "REVIEW.md"), ("Final report", "REPORT.md")):
        p = run / name
        if p.exists():
            parts += [f"\n## {kind}\n", read_text(p)]
    parts.append("\n## Conversation\n")
    for m in manager.store.messages(tid):
        who = (m.get("agent") or m.get("role") or "system")
        k = m.get("kind")
        if k in ("text", "handoff", "plan", "decision", "review", "user", "question", "error", "complete"):
            head = f"**{who} · {k}**" + (f" → {m.get('to')}" if m.get("to") else "") + (f" · {m.get('title')}" if m.get("title") else "")
            parts += [head, "", str(m.get("content") or m.get("summary") or ""), ""]
        elif k == "tool":
            parts += [f"- `{m.get('tool')}` {m.get('summary','')} ({m.get('status','')})"]
        elif k == "command":
            parts += [f"- `$ {m.get('content')}` → {m.get('status','')}"]
    md = "\n".join(parts)
    return Response(md, mimetype="text/markdown", headers={"Content-Disposition": f"attachment; filename={tid}.md"})


@app.get("/api/tasks/<tid>/repo-tree")
def task_repo_tree(tid):
    root = task_root(tid)
    depth = max(1, min(10, int(request.args.get("depth", "6"))))
    return jsonify(repo_tree(root, root, 0, depth, [6000]))


@app.get("/api/tasks/<tid>/repo-file")
def task_repo_file(tid):
    root, p = safe_repo_path(tid, request.args.get("path", ""))
    if not p.exists() or not p.is_file():
        return jsonify({"error": "File not found"}), 404
    if p.stat().st_size > 3_000_000:
        return jsonify({"error": "File is too large to open in the browser"}), 413
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "Binary or non-UTF-8 file"}), 415
    return jsonify({"path": str(p.relative_to(root)).replace("\\", "/"), "text": text, "size": p.stat().st_size,
                    "mtime": p.stat().st_mtime})


@app.put("/api/tasks/<tid>/repo-file")
def task_repo_file_save(tid):
    b = body()
    text = b.get("text")
    if text is None:
        return jsonify({"error": "Missing text"}), 400
    root, p = safe_repo_path(tid, b.get("path", ""))
    create = bool(b.get("create"))
    if not p.exists() and not create:
        return jsonify({"error": "File does not exist (pass create=true to create it)"}), 404
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    rel = str(p.relative_to(root)).replace("\\", "/")
    manager.timeline(tid, "user", "File edited in browser", rel)
    manager.message(tid, {"role": "user", "kind": "file_edit", "content": rel})
    return jsonify({"ok": True, "path": rel})


@app.post("/api/tasks/<tid>/open/<what>")
def task_open(tid, what):
    t = task_or_404(tid)
    if what == "pr" and t.get("pr_url"):
        webbrowser.open(t["pr_url"])
        return jsonify({"ok": True})
    if what == "issue" and t.get("github_issue_url"):
        webbrowser.open(t["github_issue_url"])
        return jsonify({"ok": True})
    path = {"worktree": t.get("worktree"), "vscode": t.get("worktree"), "code": t.get("worktree"),
            "run": t.get("run_dir") or str(RUNTIME_DIR / tid), "repo": t.get("repo")}.get(what)
    if IN_DOCKER and what in ("worktree", "run", "repo", "vscode", "code"):
        where = f"The folder is {path}." if path else "The task has not created its worktree yet."
        return jsonify({"error": f"Relay runs on a server, so it cannot open windows on your desktop. {where} "
                                 "Set Settings → Verification → Browser VS Code URL to open folders in the browser."}), 400
    if path and Path(path).exists():
        if IS_WINDOWS:
            os.startfile(path)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", path])
        return jsonify({"ok": True})
    if what in ("vscode", "code") and t.get("worktree"):
        subprocess.Popen(["cmd.exe", "/d", "/c", "code", t["worktree"]] if IS_WINDOWS else ["code", t["worktree"]])
        return jsonify({"ok": True})
    return jsonify({"error": "Unavailable"}), 404


# ----------------------------------------------------------------------------- scorecards + lessons
@app.get("/api/tasks/<tid>/scorecard")
def task_scorecard(tid):
    t = task_or_404(tid)
    return jsonify({"scorecard": t.get("scorecard") or manager.learning.cards.get(tid), "retro": t.get("retro")})


@app.post("/api/tasks/<tid>/scorecard/refresh")
def task_scorecard_refresh(tid):
    """Recompute the scorecard now, asking GitHub about the pull request first."""
    task_or_404(tid)
    if not manager.learning.score(tid):
        return jsonify({"error": "The task has not finished yet."}), 400
    manager.learning.refresh_prs(force_tid=tid)
    return jsonify({"scorecard": manager.get(tid).get("scorecard")})


@app.post("/api/tasks/<tid>/retro")
def task_retro(tid):
    t = task_or_404(tid)
    if t.get("status") not in ("done", "failed", "stopped") or not t.get("started_at"):
        return jsonify({"error": "A retrospective needs a task that ran and has finished."}), 400
    if (t.get("retro") or {}).get("status") in ("queued", "running"):
        return jsonify({"error": "A retrospective is already queued or running."}), 409
    manager.learning.score(tid)
    manager.learning.queue_retro(tid)
    return jsonify({"ok": True})


def lessons_view():
    data = lessons.listing()
    labels = {}
    for t in manager.store.list():
        card = t.get("scorecard") or {}
        if card.get("repo"):
            labels[card["repo"]] = card.get("repo_label") or card["repo"]
    for row in data["queue"] + data["approved"] + data["rejected"]:
        key = row.get("repo") or row.get("proposed_repo")
        if key and key not in labels:
            labels[key] = row.get("repo_label") or repo_label(key)
    return {**data, "repos": [{"key": k, "label": v} for k, v in sorted(labels.items(), key=lambda kv: kv[1].lower())]}


@app.get("/api/lessons")
def lessons_list():
    return jsonify(lessons_view())


@app.post("/api/lessons")
def lessons_add():
    b = body()
    lessons.add(b.get("text"), b.get("scope") or "global", (b.get("repo") or "").strip())
    broadcast("lessons", {"pending": lessons.pending_count()})
    return jsonify(lessons_view())


@app.post("/api/lessons/<lid>/<action>")
def lessons_action(lid, action):
    b = body()
    if action == "approve":
        lessons.approve(lid, b.get("text"), b.get("scope"), b.get("repo"))
    elif action == "reject":
        lessons.reject(lid)
    else:
        return jsonify({"error": f"Unknown action {action}"}), 404
    broadcast("lessons", {"pending": lessons.pending_count()})
    return jsonify(lessons_view())


@app.patch("/api/lessons/<lid>")
def lessons_patch(lid):
    lessons.update(lid, body())
    return jsonify(lessons_view())


@app.delete("/api/lessons/<lid>")
def lessons_delete(lid):
    lessons.delete(lid)
    broadcast("lessons", {"pending": lessons.pending_count()})
    return jsonify(lessons_view())


# ----------------------------------------------------------------------------- queue
@app.post("/api/queue/start")
def queue_start():
    n = body().get("parallel")
    if n:
        C.update({"max_parallel": max(1, int(n))})
        manager.config_changed()
    manager.start(int(n) if n else None)
    return jsonify(manager.queue_state())


@app.post("/api/queue/stop")
def queue_stop():
    manager.stop_queue()
    return jsonify(manager.queue_state())


@app.get("/api/queue")
def queue_get():
    return jsonify(manager.queue_state())


@app.post("/api/run")  # v13 compatibility
def run_compat():
    return queue_start()


# ----------------------------------------------------------------------------- notifications
@app.get("/api/notifications")
def notifications_get():
    return jsonify(manager.notifications)


@app.post("/api/notifications/read")
def notifications_read():
    for n in manager.notifications:
        n["read"] = True
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------- github
@app.get("/api/github/status")
def gh_status():
    return jsonify({**github.auth_info(), **{"watcher": manager.github_watcher, **manager.github_status}})


@app.get("/api/github/repos")
def gh_repos():
    try:
        return jsonify({"repos": github.accessible_repos(force=request.args.get("force") == "1"), "root": str(github.clone_root())})
    except RuntimeError as e:
        return jsonify({"error": str(e), "repos": [], "root": str(github.clone_root())}), 502


@app.post("/api/github/clone")
def gh_clone():
    b = body()
    try:
        return jsonify(github.clone_repo(b.get("repo"), b.get("name")))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502


@app.get("/api/github/sources")
def gh_sources_get():
    return jsonify(github.load_sources())


@app.post("/api/github/sources")
def gh_sources_post():
    b = body()
    raw = str(b.get("repo") or "").strip()
    repo = github.normalize_repo_full_name(raw)
    # Say exactly what is wrong and which field it belongs to; the form shows it inline.
    if not raw:
        return jsonify({"error": "Enter a repository as owner/repository.", "field": "repo"}), 400
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}", repo):
        return jsonify({"error": f"“{raw}” is not a GitHub repository. Use owner/repository (for example acme/web) or paste its GitHub URL.",
                        "field": "repo"}), 400
    local_path = (b.get("local_path") or "").strip().strip('"')
    if local_path and not b.get("id") and not Path(local_path).expanduser().is_dir():
        return jsonify({"error": f"{local_path} is not a folder on this machine. Leave it blank to use a managed clone.",
                        "field": "local_path"}), 400
    try:
        max_rounds = int(b.get("max_turns") or manager.cfg().get("max_turns") or 12)
    except (TypeError, ValueError):
        return jsonify({"error": "Max turns must be a whole number.", "field": "max_turns"}), 400
    rows = github.load_sources()
    if any(r.get("repo", "").lower() == repo.lower() and r.get("id") != b.get("id") for r in rows):
        return jsonify({"error": f"{repo} is already watched.", "field": "repo"}), 400
    row = {"id": b.get("id") or f"src_{int(time.time())}", "repo": repo, "local_path": local_path,
           "label": (b.get("label") or manager.cfg().get("github_default_label") or "agent").strip(),
           "assigned_to_me": bool(b.get("assigned_to_me", True)), "watch_label": bool(b.get("watch_label", True)),
           "enabled": bool(b.get("enabled", True)), "auto_queue": bool(b.get("auto_queue", True)),
           "preset": b.get("preset") or "", "max_rounds": max_rounds}
    rows = [r for r in rows if r.get("id") != row["id"]]
    rows.append(row)
    github.save_sources(rows)
    if manager.cfg().get("github_intake_enabled", True):
        manager.start_github_watcher()
    return jsonify(rows)


@app.delete("/api/github/sources/<sid>")
def gh_sources_delete(sid):
    rows = [r for r in github.load_sources() if r.get("id") != sid]
    github.save_sources(rows)
    return jsonify(rows)


@app.post("/api/github/poll")
def gh_poll():
    manager.start_github_watcher()
    return jsonify({"ok": True})


# ----------------------------------------------------------------------------- repositories + worktrees
def task_busy(t):
    return t.get("status") in repos.BUSY or t.get("id") in manager.runners


def repo_arg(raw):
    # Task repositories chosen outside the repositories folder stay reachable, but
    # only the exact paths Relay already recorded, never anything the client names.
    return repos.resolve_repo(raw, [t["repo"] for t in manager.tasks if t.get("repo")])


@app.get("/api/repos")
def repos_list():
    return jsonify(repos.list_repos(manager.tasks))


@app.post("/api/repos/<action>")
def repos_action(action):
    b = body()
    path = repo_arg(b.get("path"))
    try:
        if action == "fetch":
            return jsonify(repos.fetch(path, manager.tasks))
        if action == "pull":
            return jsonify(repos.pull(path, manager.tasks))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"error": f"Unknown action {action}"}), 404


@app.get("/api/repos/graph")
def repos_graph():
    return jsonify(repos.branch_graph(repo_arg(request.args.get("path")), manager.tasks))


@app.get("/api/worktrees")
def worktrees_list():
    return jsonify(repos.list_worktrees(manager.tasks, task_busy))


@app.get("/api/worktrees/size")
def worktrees_size():
    p = repos.resolve_worktree(request.args.get("path"))
    return jsonify({"path": str(p), "size": repos.disk_size(p) if p.is_dir() else None})


@app.post("/api/worktrees/remove")
def worktrees_remove():
    b = body()
    try:
        return jsonify(repos.remove_worktree(repos.resolve_worktree(b.get("path")), manager.tasks, task_busy, bool(b.get("discard"))))
    except PermissionError as e:
        return jsonify({"error": str(e)}), 409
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/worktrees/delete-branch")
def worktrees_delete_branch():
    b = body()
    try:
        return jsonify(repos.delete_branch(repo_arg(b.get("repo")), b.get("branch"), manager.tasks, task_busy))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/worktrees/cleanup")
def worktrees_cleanup_preview():
    return jsonify({"candidates": repos.cleanup_candidates(manager.tasks, task_busy)})


@app.post("/api/worktrees/cleanup")
def worktrees_cleanup():
    return jsonify(repos.cleanup(body().get("paths") or [], manager.tasks, task_busy))


# ----------------------------------------------------------------------------- errors
@app.errorhandler(404)
def not_found(exc):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found", "path": request.path}), 404
    return ("Not found", 404)


@app.errorhandler(Exception)
def unhandled(exc):
    if isinstance(exc, KeyError):
        return jsonify({"error": str(exc).strip("'")}), 404
    if isinstance(exc, (FileNotFoundError, PermissionError, ValueError)):
        return jsonify({"error": str(exc)}), 400
    if request.path.startswith("/api/"):
        app.logger.exception("API error on %s", request.path)
        return jsonify({"error": str(exc), "type": exc.__class__.__name__}), 500
    app.logger.exception("Request error on %s", request.path)
    return ("Internal Server Error", 500)


def launch_browser():
    time.sleep(1.0)
    try:
        webbrowser.open(f"http://{HOST}:{PORT}")
    except Exception:
        pass


if __name__ == "__main__":
    ensure_single_instance()
    if "--no-browser" not in sys.argv:
        threading.Thread(target=launch_browser, daemon=True).start()
    from waitress import serve
    try:
        resumed = manager.recover_on_start()
        if resumed:
            print(f"Resuming {len(resumed)} interrupted task(s): " + ", ".join(resumed))
    except Exception as exc:  # never block startup on recovery
        app.logger.warning("Startup recovery failed: %s", exc)
    manager.learning.start()  # backfill scorecards, then keep pull request outcomes current
    shown = "127.0.0.1" if HOST in ("0.0.0.0", "") else HOST
    print(f"Relay {C.BUILD} · http://{shown}:{PORT}" + (" (inside Docker)" if IN_DOCKER else ""))
    serve(app, host=HOST, port=PORT, threads=16, channel_timeout=3600)
