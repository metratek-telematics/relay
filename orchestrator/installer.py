"""Install, update and remove agent CLIs from the Agents page.

Agents go into AGENTS_DIR under Relay's data folder rather than the image, so an
agent installed from the browser survives image rebuilds and container restarts,
and the browser VS Code container can mount the same folder to run their logins.

  npm packages   -> AGENTS_DIR/npm            (npm install -g --prefix)
  Python tools   -> AGENTS_DIR/venvs/<agent>  (own virtualenv, binary linked into AGENTS_DIR/bin)
  install script -> run with HOME and a prefix pointing at AGENTS_DIR, binary expected in AGENTS_DIR/bin
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .util import DATA_DIR, truncate

AGENTS_DIR = Path(os.environ.get("RELAY_AGENTS_DIR") or DATA_DIR / "agents").resolve()
NPM_PREFIX = AGENTS_DIR / "npm"
BIN_DIR = AGENTS_DIR / "bin"
VENVS = AGENTS_DIR / "venvs"

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def extend_path() -> None:
    """Put installed agents first on PATH for Relay and every process it starts."""
    for d in (BIN_DIR, NPM_PREFIX / "bin"):
        d.mkdir(parents=True, exist_ok=True)
    parts = os.environ.get("PATH", "").split(os.pathsep)
    # Vendor install scripts (Cursor) link their binary into ~/.local/bin under Relay's home folder.
    local_bin = str(Path.home() / ".local" / "bin")
    for d in (local_bin, str(NPM_PREFIX / "bin"), str(BIN_DIR)):
        if d not in parts:
            parts.insert(0, d)
    os.environ["PATH"] = os.pathsep.join(parts)


def managed_path(binary: str) -> str | None:
    """Where a Relay-installed binary lives, if it does."""
    for d in (BIN_DIR, NPM_PREFIX / "bin", Path.home() / ".local" / "bin"):
        p = d / binary
        if p.exists():
            return str(p)
    return None


def job(agent: str) -> dict | None:
    with _lock:
        j = _jobs.get(agent)
        return dict(j) if j else None


def _commands(spec: dict, action: str) -> list[list[str]]:
    inst = spec.get("install") or {}
    kind = inst.get("kind")
    if kind == "npm":
        pkg = inst["package"]
        if action == "remove":
            return [["npm", "uninstall", "-g", "--prefix", str(NPM_PREFIX), pkg.rsplit("@", 1)[0] if pkg.count("@") > 1 else pkg]]
        return [["npm", "install", "-g", "--no-audit", "--no-fund", "--prefix", str(NPM_PREFIX), f"{pkg}@latest" if "@" not in pkg[1:] else pkg]]
    if kind == "pip":
        venv = VENVS / spec["id"]
        if action == "remove":
            return []
        cmds = []
        if not (venv / "bin" / "python").exists():
            cmds.append(["python3", "-m", "venv", str(venv)])
        cmds.append([str(venv / "bin" / "pip"), "install", "--upgrade", inst["package"]])
        return cmds
    if kind == "script":
        if action == "remove":
            return []
        return [["bash", "-lc", inst["command"]]]
    raise ValueError(f"{spec.get('label', spec.get('id'))} cannot be installed automatically")


def _link_pip_binary(spec: dict) -> None:
    src = VENVS / spec["id"] / "bin" / spec["binary"]
    dst = BIN_DIR / spec["binary"]
    if src.exists():
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        dst.symlink_to(src)


def start(spec: dict, action: str, on_done=None) -> dict:
    """Run an install, update or removal in the background; progress is polled through job()."""
    agent = spec["id"]
    if action not in ("install", "update", "remove"):
        raise ValueError("Unknown action")
    with _lock:
        running = _jobs.get(agent)
        if running and running.get("state") == "running":
            raise ValueError(f"{spec['label']} is already being {running['action']}ed")
        cmds = _commands(spec, "remove" if action == "remove" else "install")
        _jobs[agent] = {"agent": agent, "action": action, "state": "running", "started": time.time(), "log": "", "error": None}

    def run():
        log, ok, err = [], True, None
        env = os.environ.copy()
        env.update({"NPM_CONFIG_PREFIX": str(NPM_PREFIX), "PREFIX": str(AGENTS_DIR), "BIN_DIR": str(BIN_DIR),
                    "INSTALL_DIR": str(BIN_DIR), "CI": "1", "NO_COLOR": "1"})
        try:
            AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            for cmd in cmds:
                log.append("$ " + " ".join(cmd))
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=env)
                log.append(truncate((p.stdout or "") + (p.stderr or ""), 6000, tail=True))
                if p.returncode != 0:
                    ok, err = False, f"exit {p.returncode}"
                    break
            if ok and action != "remove" and (spec.get("install") or {}).get("kind") == "pip":
                _link_pip_binary(spec)
            if ok and action == "remove":
                kind = (spec.get("install") or {}).get("kind")
                if kind == "pip":
                    shutil.rmtree(VENVS / agent, ignore_errors=True)
                    (BIN_DIR / spec["binary"]).unlink(missing_ok=True)
                elif kind == "script":
                    (BIN_DIR / spec["binary"]).unlink(missing_ok=True)
            if ok and action != "remove" and not shutil.which(spec["binary"]) and not managed_path(spec["binary"]):
                ok, err = False, f"installed, but `{spec['binary']}` was not found afterwards"
        except Exception as e:  # a failed install must end the job, not leave it "running"
            ok, err = False, str(e)
        with _lock:
            _jobs[agent].update(state="done" if ok else "failed", error=err, log="\n".join(log)[-20000:], finished=time.time())
        if on_done:
            on_done(agent)

    threading.Thread(target=run, daemon=True, name=f"install-{agent}").start()
    return job(agent)
