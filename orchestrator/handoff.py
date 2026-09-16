"""Copy-paste commands for checking, running and accepting a task's result.

The task page shows these instead of raw git history: where the result is, how to
run it, how to get it onto your own machine, and how to accept or throw it away.
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path

from urllib.parse import quote, urlparse

from . import environment, gitops
from .util import IS_WINDOWS, quiet


def _q(value) -> str:
    s = str(value)
    if IS_WINDOWS:
        return f'"{s}"' if any(c in s for c in ' &()[]{}^=;!+,`~') else s
    return shlex.quote(s)


def _git(args, cwd, timeout=20) -> str:
    try:
        p = quiet(["git", *args], cwd=cwd, timeout=timeout)
        return (p.stdout or "").strip() if p.returncode == 0 else ""
    except Exception:
        return ""


def _run_commands(wt: Path) -> tuple[list[str], str]:
    """Best guess at how to start the project, from what is actually in the worktree."""
    pkg = wt / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except Exception:
            scripts = {}
        pm = "pnpm" if (wt / "pnpm-lock.yaml").exists() else "yarn" if (wt / "yarn.lock").exists() else "npm"
        install = f"{pm} install" if pm != "npm" else ("npm ci" if (wt / "package-lock.json").exists() else "npm install")
        for script in ("dev", "start", "serve", "preview"):
            if script in scripts:
                run = f"npm run {script}" if pm == "npm" else f"{pm} {script}"
                return [install, run], f"Starts the `{script}` script from package.json."
    if (wt / "manage.py").exists():
        return ["python manage.py runserver 0.0.0.0:8000"], "Django development server on port 8000."
    if (wt / "index.html").exists():
        return ["python3 -m http.server 8000"], "Static page: open http://localhost:8000."
    return [], ""


def ide_link(ide_url: str, folder) -> str:
    return f"{ide_url.rstrip('/')}/?folder={quote(str(folder))}" if ide_url else ""


def build(task: dict, cfg: dict | None = None) -> dict:
    ide_url = ((cfg or {}).get("ide_url") or "").strip()
    wt = Path(task.get("worktree") or "")
    repo = Path(task.get("repo") or "")
    branch = task.get("branch") or task.get("branch_name")
    if not branch or not task.get("worktree"):
        return {"ready": False, "reason": "The team has not created its branch yet."}

    wt_exists = wt.exists()
    base = gitops.default_branch(repo) if repo.exists() else "main"
    since = gitops.task_base(task) or base
    has_remote = bool(_git(["remote", "get-url", "origin"], repo)) if repo.exists() else False
    pushed = has_remote and bool(_git(["ls-remote", "--heads", "origin", branch], repo, timeout=15))
    pr = task.get("pr_number")
    stat = _git(["diff", "--shortstat", since], wt) if wt_exists else ""
    q_wt, q_repo, q_branch = _q(wt), _q(repo), _q(branch)

    sections = []
    if wt_exists:
        sections.append({
            "id": "look", "title": "See what changed",
            "text": f"Everything the team did is on `{branch}`, compared with `{base}`.",
            "commands": [f"cd {q_wt}", f"git log --oneline {base}..HEAD", f"git diff --stat {base}...HEAD", f"git diff {base}...HEAD"],
        })
        run, note = _run_commands(wt)
        if run:
            sections.append({"id": "run", "title": "Run it", "text": note + " Runs straight from the task's worktree, so your own checkout is untouched.",
                             "commands": [f"cd {q_wt}", *run]})
        dev = environment.dev_command(wt)
        if ide_url and dev:
            # relay-preview (in the IDE image) starts the dev server and bridges code-server's HTTP-only port
            # proxy to it, so HTTPS dev servers preview too; the bridge listens on the dev port + 1000.
            origin = "{0.scheme}://{0.netloc}".format(urlparse(ide_url))
            sections.insert(1, {"id": "preview", "title": "Preview in VS Code", "link": f"{origin}/absproxy/6173/",
                                "link_label": "Open preview",
                                "text": "Open the worktree in VS Code, run this in its terminal (Ctrl+`), wait for the preview line, then open the preview. Dependencies are already installed when Relay's setup succeeded. Ctrl+C stops it.",
                                "commands": [f"cd {q_wt}", "relay-preview"]})

    if pushed:
        local = [f"git fetch origin {q_branch}", f"git switch {q_branch}"]
        if pr:
            local = [f"gh pr checkout {pr}"]
        sections.append({"id": "local", "title": "Try it on your computer",
                         "text": "Run inside your own clone of the repository.", "commands": local})
    elif has_remote and wt_exists:
        sections.append({"id": "push", "title": "Share it", "text": "The branch is only on this machine. Push it to review it elsewhere or open a pull request.",
                         "commands": [f"cd {q_wt}", f"git push -u origin {q_branch}", f"gh pr create --draft --fill --head {q_branch}"]})

    if pr:
        sections.append({"id": "accept", "title": "Accept it", "text": f"Marks draft pull request #{pr} ready and squash-merges it.",
                         "commands": [f"gh pr ready {pr}", f"gh pr merge {pr} --squash --delete-branch"]})
    else:
        sections.append({"id": "accept", "title": "Accept it", "text": f"Merges the branch into `{base}` in the original repository.",
                         "commands": [f"cd {q_repo}", f"git switch {base}", f"git merge --no-ff {q_branch}"]})

    cleanup = []
    if wt_exists:
        cleanup.append(f"git -C {q_repo} worktree remove {q_wt}")
    cleanup.append(f"git -C {q_repo} branch -d {q_branch}")
    sections.append({"id": "cleanup", "title": "Clean up", "text": "After merging or when you no longer want the result. `branch -d` refuses to delete unmerged work; use `-D` to discard it.",
                     "commands": cleanup})

    return {"ready": True, "ide": ide_link(ide_url, wt) if wt_exists else "", "environment": task.get("environment"), "branch": branch, "base": base, "worktree": str(wt), "worktree_exists": wt_exists, "repo": str(repo),
            "pushed": pushed, "pr_url": task.get("pr_url"), "pr_number": pr, "diffstat": stat, "sections": sections}
