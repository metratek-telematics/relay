"""Shared helpers: paths, time, subprocess, filesystem."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
RULES_DIR = APP_DIR / "rules"
RUNTIME_DIR = APP_DIR / "runtime"
WORKTREES_DIR = APP_DIR / "worktrees"
STATE_DIR = APP_DIR / "state"
MANAGED_REPOS_DIR = APP_DIR / "managed-repos"
TASKS_FILE = STATE_DIR / "tasks.json"
GITHUB_SOURCES_FILE = STATE_DIR / "github_sources.json"
DELETED_FILE = STATE_DIR / "deleted_tasks.json"
CONFIG_PATH = APP_DIR / "config.json"

for _p in (RUNTIME_DIR, WORKTREES_DIR, STATE_DIR, MANAGED_REPOS_DIR):
    _p.mkdir(parents=True, exist_ok=True)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IS_WINDOWS = os.name == "nt"


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ts() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def new_task_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def safe_slug(s: str, n: int = 56) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s or "").strip()).strip("-")[:n] or "task"


def read_text(p) -> str:
    return Path(p).read_text(encoding="utf-8", errors="replace")


def write_text(p, t) -> None:
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(t or "", encoding="utf-8")


def append_line(p, line: str) -> None:
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    with Path(p).open("a", encoding="utf-8", errors="replace") as f:
        f.write(line.rstrip("\n") + "\n")


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p, obj) -> None:
    tmp = Path(str(p) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def which(name: str):
    return shutil.which(name)


def windows_cli(name: str, args=None) -> list[str]:
    """Resolve a CLI (node .cmd shims included) into an argv list."""
    args = list(args or [])
    r = shutil.which(name)
    if not r:
        raise RuntimeError(f"{name} was not found in PATH.")
    if IS_WINDOWS and Path(r).suffix.lower() in (".cmd", ".bat"):
        return ["cmd.exe", "/d", "/c", "call", r, *args]
    return [r, *args]


def quiet(args, cwd=None, timeout=30, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=CREATE_NO_WINDOW, env=env,
    )


def kill_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and its children (node shims spawn grandchildren)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=15)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def truncate(s, n: int = 4000, tail: bool = False) -> str:
    s = str(s or "")
    if len(s) <= n:
        return s
    if tail:
        return "…[" + str(len(s) - n) + " chars omitted]…\n" + s[-n:]
    return s[:n] + "\n…[" + str(len(s) - n) + " chars omitted]…"


def fmt_duration(seconds) -> str:
    try:
        seconds = max(0, int(seconds or 0))
    except Exception:
        seconds = 0
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def is_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except Exception:
        return False
