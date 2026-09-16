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


def detect_setup(wt) -> str:
    """The repository's own dependency install, or "" when there is nothing to install."""
    wt = Path(wt)
    if not (wt / "package.json").exists():
        return ""
    if (wt / "pnpm-lock.yaml").exists():
        return "corepack pnpm install --frozen-lockfile"
    if (wt / "yarn.lock").exists():
        return "corepack yarn install --frozen-lockfile"
    if (wt / "package-lock.json").exists() or (wt / "npm-shrinkwrap.json").exists():
        return "npm ci --no-audit --no-fund"
    return "npm install --no-audit --no-fund"


def already_prepared(wt) -> bool:
    # A retried or follow-up task may reattach a worktree that already has its dependencies.
    return (Path(wt) / "node_modules").is_dir() and any((Path(wt) / "node_modules").iterdir())


def setup_command(task: dict, cfg: dict, wt) -> str:
    wf = task.get("workflow") or {}
    custom = (wf.get("setup_command") or "").strip()
    if custom:
        return custom
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
