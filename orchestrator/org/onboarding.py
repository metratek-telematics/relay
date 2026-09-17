"""First-run setup: a checklist that fills itself in from what Relay can observe, plus a sample task to try the loop."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .. import agents, config as C, github
from ..util import DATA_DIR
from .common import JsonStore, now_iso
from . import projects

_state = JsonStore("onboarding.json", {"dismissed_by": [], "team_preset": None, "completed_at": None})
SAMPLE_DIR = DATA_DIR / "sample-repos" / "hello-relay"


def _github_ready(manager) -> bool:
    if (manager.github_status or {}).get("login"):
        return True
    try:
        return bool(github.load_sources())
    except Exception:
        return False


def checklist(manager, username: str) -> dict:
    cfg = manager.cfg()
    tasks = manager.store.list()
    st = _state.read()
    try:
        health = agents.agent_health(cfg)
    except Exception:
        health = {}
    ready_agents = [k for k, v in health.items() if k not in ("git", "gh") and v.get("ok")]
    repos_known = {r for p in projects.all_projects() for r in p["repos"]} | set(cfg.get("recent_repos") or [])
    env_dir = DATA_DIR / "repo_env"
    has_env = (env_dir.is_dir() and any(env_dir.glob("*.json"))) or (DATA_DIR / "stacks.json").exists()
    steps = [
        {"id": "github", "title": "Connect GitHub", "done": _github_ready(manager),
         "why": "Relay pushes branches, opens draft pull requests and picks up labelled issues.",
         "action": {"label": "Open GitHub", "href": "#/github"}},
        {"id": "agents", "title": "Sign in your agents", "done": bool(ready_agents),
         "detail": ", ".join(health[a].get("label") or a for a in ready_agents[:4]) if ready_agents else "",
         "why": "Codex, Claude, Gemini and the pack agents run with your own subscriptions or API keys.",
         "action": {"label": "Open Agents", "href": "#/agents"}},
        {"id": "repo", "title": "Add your first repository", "done": bool(repos_known) or bool(tasks),
         "why": "Clone from GitHub or point at a folder; it joins the current project.",
         "action": {"label": "Open Repositories", "href": "#/repos"}},
        {"id": "environment", "title": "Describe its environment", "done": bool(has_env),
         "why": "Variables, setup commands and an integration stack let agents run and test the real thing.",
         "action": {"label": "Environment", "href": "#/repos/env"}},
        {"id": "team", "title": "Choose a team preset", "done": bool(st.get("team_preset")) or cfg.get("workflow_preset") != C.DEFAULTS["workflow_preset"],
         "detail": st.get("team_preset") or cfg.get("workflow_preset"),
         "why": "Who supervises, who writes the code and who reviews it.", "action": {"label": "Choose", "do": "team"}},
        {"id": "sample", "title": "Run a sample task", "done": any(t.get("status") == "done" for t in tasks),
         "running": any(t.get("sample") and t.get("status") not in ("done", "failed", "stopped") for t in tasks),
         "why": "A tiny repository and a two-minute change, to watch the supervisor, worker and reviewer loop end to end.",
         "action": {"label": "Run sample", "do": "sample"}},
    ]
    done = sum(1 for s in steps if s["done"])
    if done == len(steps) and not st.get("completed_at"):
        _state.update(lambda d: {**d, "completed_at": now_iso()})
    return {"steps": steps, "done": done, "total": len(steps), "complete": done == len(steps),
            "dismissed": username in (st.get("dismissed_by") or []), "presets": C.PRESETS}


def dismiss(username: str, dismissed: bool = True):
    def fn(d):
        rows = set(d.get("dismissed_by") or [])
        (rows.add if dismissed else rows.discard)(username)
        d["dismissed_by"] = sorted(rows)
    _state.update(fn)


def choose_team(preset_id: str):
    if not C.preset(preset_id):
        raise ValueError("Unknown team preset.")
    p = C.preset(preset_id)
    roles = {r: {"agent": v.get("agent", "")} for r, v in p["roles"].items()}
    C.update({"workflow_preset": preset_id, "roles": roles})
    _state.update(lambda d: {**d, "team_preset": preset_id})


def ensure_sample_repo() -> Path:
    """A tiny, self-contained Python project with a test, created once under DATA_DIR/sample-repos."""
    root = SAMPLE_DIR
    if (root / ".git").exists():
        return root
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# hello-relay\n\nA tiny project for trying Relay.\n", encoding="utf-8")
    (root / "greet.py").write_text('def greet(name):\n    return f"Hello, {name}!"\n', encoding="utf-8")
    (root / "test_greet.py").write_text("from greet import greet\n\n\ndef test_greet():\n    assert greet(\"Relay\") == \"Hello, Relay!\"\n", encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "Relay", "GIT_AUTHOR_EMAIL": "relay@localhost", "GIT_COMMITTER_NAME": "Relay", "GIT_COMMITTER_EMAIL": "relay@localhost",
           "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home())}
    for args in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-qm", "Initial commit"]):
        subprocess.run(args, cwd=root, check=True, capture_output=True, env=env, timeout=30)
    return root


SAMPLE_REQUEST = ("Add an optional `punctuation` argument to greet() in greet.py (default \"!\") so greet(\"Relay\", \"?\") returns "
                  "\"Hello, Relay?\". Keep the existing behaviour, add a test for the new argument, and run the tests with "
                  "`python3 -m pytest -q` or `python3 -m unittest` if pytest is missing.")
