"""Owner-configured redeploy for delivered tasks.

The command is a setting, never task data: it is not read from the task text, from
anything an agent produced, or from a request body. A task only redeploys when the
owner enabled redeploy *and* the task's workflow opted in.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import github
from .util import RUNTIME_DIR, kill_tree, now, popen_group_kwargs, truncate, write_text


OUTPUT_TAIL = 12_000
TRIGGERS = ("pr_merged", "delivered")
_UNSET = object()


def command(cfg: dict) -> str:
    return str(cfg.get("redeploy_command") or "").strip()


def opted_in(task: dict) -> bool:
    workflow = task.get("workflow") or {}
    return bool(workflow.get("redeploy_on_merge", False))


def merged_state(task: dict):
    """The task PR's state, without letting a GitHub error escape the watcher."""
    return github.pr_state(task.get("github_repo"), task.get("pr_number"))


def should_run(task: dict, cfg: dict, pr_state=_UNSET) -> bool:
    """Whether a task is eligible for an automatic redeploy.

    The optional pr_state lets the watcher reuse a state it already fetched.
    """
    if not cfg.get("redeploy_enabled") or not command(cfg) or not opted_in(task):
        return False
    if task.get("status") != "done":
        return False
    trigger = cfg.get("redeploy_trigger") or "pr_merged"
    if trigger not in TRIGGERS:
        return False
    if trigger == "pr_merged":
        if pr_state is _UNSET:
            pr_state = merged_state(task)
        return bool(pr_state) and str(pr_state.get("state") or "").upper() == "MERGED"
    return True


def working_dir(task: dict, cfg: dict) -> Path:
    configured = str(cfg.get("redeploy_working_dir") or "").strip()
    raw = configured or task.get("worktree") or task.get("repo") or (RUNTIME_DIR / str(task["id"]))
    return Path(raw).expanduser()


def _joined(stdout, stderr) -> str:
    stdout, stderr = stdout or "", stderr or ""
    return stdout + ("\n" if stdout and stderr else "") + stderr


def run(task: dict, cfg: dict, trigger="pr_merged", pr_state=_UNSET) -> dict:
    """Run the configured command and return the record stored on the task.

    The command runs through a shell (so existing deploy scripts and command chains
    work as configured) in its own process group, so a timeout kills the whole tree
    instead of leaving a half-finished deployment running detached.
    """
    if pr_state is _UNSET:
        pr_state = merged_state(task)
    cmd = command(cfg)
    if not cmd:
        raise ValueError("Redeploy command is not configured")
    cwd = working_dir(task, cfg)
    timeout_seconds = max(1, int(float(cfg.get("redeploy_timeout_minutes") or 10) * 60))
    record = {
        "status": "running", "trigger": trigger, "command": cmd, "cwd": str(cwd),
        "exit_code": None, "output": "", "started_at": now(), "finished_at": None,
        "pr_state": pr_state,
    }
    try:
        argv = ["cmd.exe", "/d", "/c", cmd] if os.name == "nt" else ["/bin/sh", "-c", cmd]
        proc = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", **popen_group_kwargs())
        try:
            output = _joined(*proc.communicate(timeout=timeout_seconds))
            record["exit_code"] = proc.returncode
            record["status"] = "succeeded" if proc.returncode == 0 else "failed"
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc)  # the whole process group, not just /bin/sh
            try:
                output = _joined(*proc.communicate(timeout=10))
            except Exception:
                output = _joined(exc.stdout, exc.stderr)
            output = output.rstrip() + ("\n" if output.strip() else "") + f"Redeploy timed out after {timeout_seconds} seconds."
            record["exit_code"] = -1
            record["status"] = "failed"
    except Exception as exc:
        output = str(exc)
        record["exit_code"] = -1
        record["status"] = "failed"
    record["finished_at"] = now()
    record["output"] = truncate(output, OUTPUT_TAIL, tail=True)
    log_path = RUNTIME_DIR / str(task["id"]) / "redeploy.log"
    write_text(log_path, output)
    record["log_path"] = str(log_path)
    return record
