"""Prepare a task's worktree before any agent runs in it.

Agents must not improvise installs (rules/CORE.md), so Relay itself runs the
repository's own install command once, with the credentials the operator mounted
(~/.npmrc and friends) and a shared package cache. A failure is not fatal: it is
recorded as a blocked check and the team carries on without dependencies.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path


def _node_setup(wt: Path) -> str:
    if not (wt / "package.json").exists():
        return ""
    if (wt / "pnpm-lock.yaml").exists():
        return "corepack pnpm install --frozen-lockfile"
    if (wt / "yarn.lock").exists():
        return "corepack yarn install --frozen-lockfile"
    if (wt / "package-lock.json").exists() or (wt / "npm-shrinkwrap.json").exists():
        return "npm ci --no-audit --no-fund"
    return "npm install --no-audit --no-fund"


def is_python(wt: Path) -> bool:
    return any((wt / f).exists() for f in ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile")) \
        or any(wt.glob("requirements*.txt")) or any(wt.glob("*.py")) or (wt / "src").is_dir() and any((wt / "src").rglob("*.py"))


def _python_setup(wt: Path) -> str:
    """A worktree-local .venv with the project's requirements and pytest, put on PATH for agents and checks."""
    if not is_python(wt):
        return ""
    steps = ["python3 -m venv .venv", ".venv/bin/python -m pip install -q --upgrade pip"]
    reqs = sorted(p.name for p in wt.glob("requirements*.txt"))
    for name in reqs:
        if name == "requirements.txt":
            steps.append(f".venv/bin/pip install -q -r {name}")
        else:  # optional sets (dev, docs, a platform extra) must not sink the whole setup
            steps.append(f"(.venv/bin/pip install -q -r {name} || echo 'warning: {name} did not install')")
    if (wt / "pyproject.toml").exists() or (wt / "setup.py").exists():
        # Dev/test extras when the project declares them, plain editable install otherwise.
        steps.append("(.venv/bin/pip install -q -e '.[dev,test]' || .venv/bin/pip install -q -e .)")
    steps.append(".venv/bin/pip install -q pytest")
    return " && ".join(steps)


def _other_setup(wt: Path) -> list[str]:
    out = []
    if (wt / "go.mod").exists() and shutil.which("go"):
        out.append("go mod download")
    if (wt / "Cargo.toml").exists() and shutil.which("cargo"):
        out.append("cargo fetch")
    if (wt / "Gemfile").exists() and shutil.which("bundle"):
        out.append("bundle install")
    if (wt / "composer.json").exists() and shutil.which("composer"):
        out.append("composer install --no-interaction")
    return out


def detect_setup(wt) -> str:
    """The repository's own dependency installs for every ecosystem it uses, or "" when there is nothing."""
    wt = Path(wt)
    steps = [c for c in (_node_setup(wt), _python_setup(wt), *_other_setup(wt)) if c]
    if _python_setup(wt):
        exclude_from_git(wt, ".venv/")
    if is_python(wt):
        exclude_python_artifacts(wt)
    return " && ".join(f"( {c} )" if len(steps) > 1 else c for c in steps)


# Written by Relay's own setup and checks, never by the change: `pip install -e .` leaves *.egg-info, pytest leaves
# __pycache__. Without a .gitignore entry Relay's `git add -A` committed them (a real run had to untrack an egg-info).
PYTHON_ARTIFACTS = ("__pycache__/", "*.egg-info/", ".pytest_cache/")


def exclude_python_artifacts(wt) -> None:
    for pattern in PYTHON_ARTIFACTS:
        exclude_from_git(Path(wt), pattern)


def exclude_from_git(wt: Path, pattern: str) -> None:
    """Keep Relay's own setup output (.venv) out of `git status` without touching the repo's .gitignore."""
    try:
        from .util import quiet
        common = (quiet(["git", "rev-parse", "--git-common-dir"], cwd=wt, timeout=20).stdout or "").strip()
        if not common:
            return
        info = (wt / common if not Path(common).is_absolute() else Path(common)) / "info"
        info.mkdir(parents=True, exist_ok=True)
        ex = info / "exclude"
        cur = ex.read_text(encoding="utf-8") if ex.exists() else ""
        if pattern not in cur.splitlines():
            ex.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + pattern + "\n", encoding="utf-8")
    except Exception:
        pass


def already_prepared(wt) -> bool:
    # A retried or follow-up task may reattach a worktree that already has its dependencies.
    wt = Path(wt)
    node_ok = not (wt / "package.json").exists() or ((wt / "node_modules").is_dir() and any((wt / "node_modules").iterdir()))
    py_ok = not is_python(wt) or (wt / ".venv" / "bin" / "python").exists()
    return node_ok and py_ok and not _other_setup(wt)


def venv_bin(wt) -> str:
    """The worktree's .venv/bin when Relay created one, for PATH."""
    p = Path(wt) / ".venv" / "bin"
    return str(p) if (p / "python").exists() else ""


def setup_command(task: dict, cfg: dict, wt) -> str:
    wf = task.get("workflow") or {}
    custom = (wf.get("setup_command") or "").strip()
    if custom:
        return custom
    from . import repo_env
    saved = repo_env.for_path(wt).get("setup") or ""
    if saved:
        return saved
    if not cfg.get("env_prepare", True):
        return ""
    return detect_setup(wt)


def screenshot_tool() -> str:
    """Path of the screenshot command when a headless browser is installed in this environment."""
    return shutil.which("relay-screenshot") or ""


def dev_command(wt) -> str:
    """The script a person or agent would use to run the app locally."""
    pkg = Path(wt) / "package.json"
    if not pkg.exists():
        return ""
    try:
        scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
    except (OSError, ValueError):
        return ""
    for name in ("dev", "start", "serve"):
        if name in scripts:
            return f"npm run {name}"
    return ""


def uses_vite(wt) -> bool:
    wt = Path(wt)
    if any((wt / f).exists() for f in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.mts")):
        return True
    try:
        pkg = json.loads((wt / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return "vite" in deps
