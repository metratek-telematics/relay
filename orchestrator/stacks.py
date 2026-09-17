"""Integration stacks: the services of a task, built from the task's own branches and run together.

A task can change several services at once. Unit tests in each repository cannot show that they still
work together, so a repository (or a task) can name a *stack*: the containers that make up the system,
the simulators and fixtures it needs, and end-to-end checks that prove cross-service behaviour.

    definition (DATA_DIR/stacks.json)
      services   [{name, role: service|simulator, repo | image, build {context, dockerfile}, command,
                   ports [container ports], env [{name, value, secret}], env_from_repo, healthcheck
                   {http, port, command, timeout}, depends_on [names], memory}]
      vars       stack-level variables, usable as ${NAME} in service env
      fixtures   [{name, service, command, stdin}]   run inside a service once it is (re)created
      checks     [{name, required, kind: command|http|browser, command | service+path(+method, body,
                   expect_status, expect_text)}]

    lifecycle (per task, `relay-stack up|down|status|logs|url|restart|check`)
      * each service with a repository is built from the task's worktree for that repository (other
        repositories use their checkout), so the task's changes are tested together
      * one docker network and project name per task; every container, network and image carries
        relay.instance / relay.task labels so Relay only ever touches its own resources
      * host ports are allocated per task and published on 127.0.0.1 only
      * Relay in Docker attaches itself to the task network, so agents and checks reach services at
        their container address; STACK_<SERVICE>_URL is exported to agents and commands
      * health is awaited with timeouts, logs are captured to the run folder, and the stack is torn
        down when the task ends, stops or is deleted; orphans are collected on startup

Docker is optional. Without a reachable Docker daemon every operation fails with one clear message
and verification records the stack checks as blocked instead of guessing.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from . import repo_env
from .util import DATA_DIR, IN_DOCKER, RUNTIME_DIR, STATE_DIR, new_id, now, read_json, truncate, write_json

DEFS_FILE = DATA_DIR / "stacks.json"
STACK_STATE_DIR = STATE_DIR / "stacks"
MASK = repo_env.MASK
# Resources of this Relay installation. Two Relays sharing a Docker host never collect each other's stacks.
INSTANCE = hashlib.sha1(str(DATA_DIR).encode()).hexdigest()[:12]
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
_VAR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_lock = threading.RLock()


class StackError(RuntimeError):
    pass


# ============================================================================ definitions
def _defs() -> list[dict]:
    return list((read_json(DEFS_FILE, None) or {}).get("stacks") or [])


def list_defs() -> list[dict]:
    return _defs()


def get_def(sid: str) -> dict | None:
    return next((d for d in _defs() if d.get("id") == sid), None)


def _clean_rel(rel: str, allow_root=True) -> str:
    rel = str(rel or "").strip().replace("\\", "/")
    if rel in ("", ".", "./"):
        if allow_root:
            return "."
        raise ValueError("A path is required")
    parts = [p for p in rel.lstrip("/").split("/") if p not in ("", ".")]
    if ".." in parts or rel.startswith("/") or (parts and parts[0] == ".git"):
        raise ValueError(f"Invalid relative path: {rel!r}")
    return "/".join(parts) or "."


def _env_rows(rows, old_rows) -> list[dict]:
    old = {r["name"]: r for r in old_rows or []}
    out, seen = [], set()
    for r in rows or []:
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        if not _VAR.match(name):
            raise ValueError(f"Invalid variable name: {name!r}")
        if name in seen:
            raise ValueError(f"Variable {name} is listed twice")
        seen.add(name)
        value = r.get("value")
        if value == MASK and name in old:
            value = old[name]["value"]
        secret = bool(r.get("secret")) if "secret" in r else repo_env.looks_secret(name)
        out.append({"name": name, "value": "" if value is None else str(value), "secret": secret})
    return out


def _ports(raw) -> list[int]:
    out = []
    items = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
    for p in items:
        if p in ("", None):
            continue
        try:
            n = int(str(p).strip())
        except ValueError:
            raise ValueError(f"Invalid port: {p!r}")
        if not 1 <= n <= 65535:
            raise ValueError(f"Invalid port: {p!r}")
        if n not in out:
            out.append(n)
    return out


def normalize(incoming: dict, old: dict | None = None) -> dict:
    """Validate a definition from the browser or an import. Secrets sent back as the mask keep their value."""
    old = old or {}
    name = str(incoming.get("name") or "").strip()[:80]
    if not name:
        raise ValueError("The stack needs a name")
    d = {"id": old.get("id") or str(incoming.get("id") or "") or new_id("stk"), "name": name,
         "description": str(incoming.get("description") or "").strip()[:500],
         "vars": _env_rows(incoming.get("vars"), old.get("vars")), "services": [], "fixtures": [], "checks": []}
    old_svcs = {s["name"]: s for s in old.get("services") or []}
    names = []
    for s in incoming.get("services") or []:
        sname = str(s.get("name") or "").strip().lower()
        if not sname:
            continue
        if not _NAME.match(sname):
            raise ValueError(f"Invalid service name {sname!r}: lowercase letters, digits, - and _")
        if sname in names:
            raise ValueError(f"Service {sname} is listed twice")
        names.append(sname)
        repo = str(s.get("repo") or "").strip()
        image = str(s.get("image") or "").strip()
        b = s.get("build") or {}
        build = None
        if repo:
            build = {"context": _clean_rel(b.get("context") or "."), "dockerfile": _clean_rel(b.get("dockerfile") or "Dockerfile", False)}
        elif not image:
            raise ValueError(f"Service {sname}: choose a repository to build or an image to run")
        hc = s.get("healthcheck") or {}
        health = {"http": str(hc.get("http") or "").strip(), "command": str(hc.get("command") or "").strip(),
                  "port": _ports([hc["port"]])[0] if hc.get("port") else None,
                  "timeout": max(5, min(1800, int(hc.get("timeout") or 120)))}
        if health["http"] and not health["http"].startswith("/"):
            health["http"] = "/" + health["http"]
        cmd = s.get("command")
        d["services"].append({
            "name": sname, "role": "simulator" if s.get("role") == "simulator" else "service",
            "repo": str(Path(repo).expanduser()) if repo else "", "image": "" if repo else image, "build": build,
            "command": shlex.join(cmd) if isinstance(cmd, list) else str(cmd or "").strip(),
            "ports": _ports(s.get("ports")),
            "env": _env_rows(s.get("env"), (old_svcs.get(sname) or {}).get("env")),
            "env_from_repo": bool(s.get("env_from_repo", True)),
            "healthcheck": health,
            "depends_on": [str(x).strip().lower() for x in (s.get("depends_on") or []) if str(x).strip()],
            "memory": str(s.get("memory") or "").strip(),
        })
    if not d["services"]:
        raise ValueError("The stack needs at least one service")
    for s in d["services"]:
        for dep in s["depends_on"]:
            if dep not in names:
                raise ValueError(f"Service {s['name']} depends on unknown service {dep}")
        if s["memory"] and not re.match(r"^\d+(\.\d+)?[bkmg]?$", s["memory"], re.I):
            raise ValueError(f"Service {s['name']}: memory limit like 512m or 1g")
    order(d["services"])  # raises on cycles
    for f in incoming.get("fixtures") or []:
        cmd = str(f.get("command") or "").strip()
        if not cmd:
            continue
        svc = str(f.get("service") or "").strip().lower()
        if svc not in names:
            raise ValueError(f"Fixture {f.get('name') or cmd}: unknown service {svc!r}")
        d["fixtures"].append({"name": str(f.get("name") or cmd).strip()[:80], "service": svc, "command": cmd,
                              "stdin": str(f.get("stdin") or "")})
    for c in incoming.get("checks") or []:
        kind = c.get("kind") or ("http" if c.get("path") and not c.get("command") else "command")
        item = {"name": str(c.get("name") or "").strip()[:80], "kind": kind, "required": bool(c.get("required", True))}
        if kind == "command":
            item["command"] = str(c.get("command") or "").strip()
            if not item["command"]:
                continue
        elif kind in ("http", "browser"):
            svc = str(c.get("service") or "").strip().lower()
            if svc not in names:
                raise ValueError(f"Check {item['name'] or kind}: unknown service {svc!r}")
            path = str(c.get("path") or "/").strip()
            item.update(service=svc, path=path if path.startswith("/") else "/" + path,
                        expect_text=str(c.get("expect_text") or ""),
                        timeout=max(1, min(600, int(c.get("timeout") or 30))))
            if kind == "http":
                item.update(method=str(c.get("method") or "GET").upper(), body=str(c.get("body") or ""),
                            expect_status=int(c.get("expect_status") or 200))
        else:
            raise ValueError(f"Unknown check kind {kind!r}")
        item["name"] = item["name"] or item.get("command") or f"{kind} {item.get('service')}{item.get('path')}"
        d["checks"].append(item)
    d["updated"] = now()
    return d


def save_def(incoming: dict) -> dict:
    with _lock:
        rows = _defs()
        old = next((x for x in rows if x.get("id") and x.get("id") == incoming.get("id")), None)
        d = normalize(incoming, old)
        rows = [x for x in rows if x.get("id") != d["id"]] + [d]
        write_json(DEFS_FILE, {"stacks": rows})
        try:
            DEFS_FILE.chmod(0o600)
        except OSError:
            pass
        return d


def delete_def(sid: str) -> None:
    with _lock:
        write_json(DEFS_FILE, {"stacks": [x for x in _defs() if x.get("id") != sid]})


def public(d: dict) -> dict:
    """What the browser may see: secret values masked."""
    if not d:
        return d
    mask = lambda rows: [{**r, "value": MASK if r.get("secret") and r.get("value") else r.get("value"), "has_value": bool(r.get("value"))} for r in rows or []]
    return {**d, "vars": mask(d.get("vars")), "services": [{**s, "env": mask(s.get("env"))} for s in d.get("services") or []]}


def order(services: list[dict]) -> list[dict]:
    """Services in dependency order; raises on a cycle."""
    by = {s["name"]: s for s in services}
    out, state = [], {}

    def visit(n, path):
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise ValueError("Services depend on each other in a cycle: " + " → ".join(path + [n]))
        state[n] = 1
        for dep in by[n].get("depends_on") or []:
            if dep in by:
                visit(dep, path + [n])
        state[n] = 2
        out.append(by[n])

    for s in services:
        visit(s["name"], [])
    return out


# ============================================================================ compose import
def _compose_port(p) -> int | None:
    if isinstance(p, int):
        return p
    if isinstance(p, dict):
        return int(p["target"]) if p.get("target") else None
    s = str(p).split("/")[0]
    target = s.rsplit(":", 1)[-1]
    target = target.split("-")[0]
    return int(target) if target.isdigit() else None


def import_compose(text: str, repo: str = "", compose_dir: str = ".") -> dict:
    """A draft definition from a docker-compose file. Nothing is saved; the user maps repositories and saves.

    `compose_dir` is where the file lives inside `repo`, so relative build contexts stay relative to the repo root.
    """
    import yaml
    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Not a valid compose file: {str(e).splitlines()[0]}")
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise ValueError("No services: found in that compose file")
    warnings, services = [], []
    base = _clean_rel(compose_dir)
    for name, s in data["services"].items():
        s = s or {}
        sname = re.sub(r"[^a-z0-9_-]", "-", str(name).lower())[:40]
        if not sname[:1].isalpha():
            sname = "s-" + sname
        svc = {"name": sname, "role": "service", "repo": "", "image": str(s.get("image") or ""), "build": None,
               "command": "", "ports": [], "env": [], "env_from_repo": True, "depends_on": [], "memory": "",
               "healthcheck": {"http": "", "command": "", "port": None, "timeout": 120}}
        b = s.get("build")
        if b:
            ctx = b if isinstance(b, str) else (b.get("context") or ".")
            dockerfile = "Dockerfile" if isinstance(b, str) else (b.get("dockerfile") or "Dockerfile")
            joined = os.path.normpath(os.path.join(base, ctx)).replace("\\", "/")
            if joined.startswith(".."):
                warnings.append(f"{sname}: build context {ctx} is outside the repository; pick the repository that holds it")
                joined = "."
            svc["build"] = {"context": joined, "dockerfile": dockerfile}
            svc["repo"] = repo
            svc["image"] = ""
        cmd = s.get("command")
        svc["command"] = shlex.join(str(x) for x in cmd) if isinstance(cmd, list) else str(cmd or "")
        for p in list(s.get("ports") or []) + list(s.get("expose") or []):
            port = _compose_port(p)
            if port and port not in svc["ports"]:
                svc["ports"].append(port)
        env = s.get("environment") or {}
        pairs = env.items() if isinstance(env, dict) else [(e.split("=", 1) + [""])[:2] for e in env]
        for k, v in pairs:
            k = str(k).strip()
            if _VAR.match(k):
                svc["env"].append({"name": k, "value": "" if v is None else str(v), "secret": repo_env.looks_secret(k)})
        if s.get("env_file"):
            warnings.append(f"{sname}: env_file is not imported; put those values in the repository environment or the service env")
        dep = s.get("depends_on") or []
        svc["depends_on"] = [re.sub(r"[^a-z0-9_-]", "-", str(x).lower()) for x in (dep.keys() if isinstance(dep, dict) else dep)]
        hc = s.get("healthcheck") or {}
        test = hc.get("test")
        if isinstance(test, list) and test:
            if test[0] == "CMD-SHELL":
                svc["healthcheck"]["command"] = " ".join(str(x) for x in test[1:])
            elif test[0] == "CMD":
                svc["healthcheck"]["command"] = shlex.join(str(x) for x in test[1:])
        elif isinstance(test, str) and test:
            svc["healthcheck"]["command"] = test
        mem = s.get("mem_limit") or (((s.get("deploy") or {}).get("resources") or {}).get("limits") or {}).get("memory")
        svc["memory"] = str(mem or "").lower()
        if s.get("volumes"):
            warnings.append(f"{sname}: volumes are not imported (stacks are disposable); use fixtures to seed data")
        services.append(svc)
    return {"name": str(data.get("name") or (Path(repo).name if repo else "imported stack")), "description": "Imported from docker compose",
            "vars": [], "services": services, "fixtures": [], "checks": [], "warnings": warnings}


# ============================================================================ which stack a task uses
def def_for_task(task: dict) -> dict | None:
    wf = task.get("workflow") or {}
    sid = wf.get("stack")
    if sid == "none":
        return None
    if not sid and task.get("repo"):
        try:
            sid = repo_env.load(task["repo"]).get("stack") or ""
        except Exception:
            sid = ""
    return get_def(sid) if sid else None


def _same(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except Exception:
        return False


def source_for(svc: dict, task: dict) -> tuple[Path | None, str]:
    """Where a service is built from: the task's worktree for its repository, else that repository's checkout."""
    if not svc.get("repo"):
        return None, "image"
    # Multi-repository tasks record one worktree per related repository (orchestrator/multirepo.py).
    for w in (task.get("repo_worktrees") or {}).values():
        if w.get("repo") and _same(w["repo"], svc["repo"]) and w.get("worktree") and Path(w["worktree"]).exists():
            return Path(w["worktree"]), "task worktree"
    for repo, wt in (task.get("worktrees") or {}).items():
        if _same(repo, svc["repo"]) and wt and Path(wt).exists():
            return Path(wt), "task worktree"
    if task.get("repo") and _same(task["repo"], svc["repo"]) and task.get("worktree") and Path(task["worktree"]).exists():
        return Path(task["worktree"]), "task worktree"
    return Path(svc["repo"]), "repository checkout"


def masker(d: dict | None):
    vals = []
    if d:
        vals += [r["value"] for r in d.get("vars") or [] if r.get("secret") and len(r.get("value") or "") >= 4]
        for s in d.get("services") or []:
            vals += [r["value"] for r in s.get("env") or [] if r.get("secret") and len(r.get("value") or "") >= 4]
            if s.get("repo"):
                try:
                    vals += repo_env.secrets(repo_env.load(s["repo"]))
                except Exception:
                    pass
    vals = sorted(set(vals), key=len, reverse=True)
    if not vals:
        return lambda s: s
    rx = re.compile("|".join(re.escape(v) for v in vals))
    return lambda s: rx.sub(MASK, s) if isinstance(s, str) else s


# ============================================================================ docker
def _docker(args, timeout=120, input=None, check=False) -> subprocess.CompletedProcess:
    exe = shutil.which("docker")
    if not exe:
        raise StackError("The docker CLI is not installed in this environment.")
    env = {**os.environ, "DOCKER_CLI_HINTS": "false"}
    try:
        res = subprocess.run([exe, *[str(a) for a in args]], capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout, input=input, env=env)
    except subprocess.TimeoutExpired:
        raise StackError(f"docker {args[0]} timed out after {int(timeout)}s")
    if check and res.returncode != 0:
        raise StackError(f"docker {' '.join(str(a) for a in args[:2])} failed: {truncate((res.stderr or res.stdout).strip(), 800, tail=True)}")
    return res


_status_cache = {"at": 0.0, "value": None}


def docker_status(force=False) -> dict:
    """Whether stacks can run here, with the reason when they cannot."""
    if not force and _status_cache["value"] and time.time() - _status_cache["at"] < 30:
        return _status_cache["value"]
    out = {"available": False, "client": bool(shutil.which("docker")), "server": "", "reason": "", "self_container": ""}
    if not out["client"]:
        out["reason"] = "The docker CLI is not installed. Rebuild the Relay image (the Dockerfile installs it) or install Docker."
    else:
        try:
            res = _docker(["version", "--format", "{{.Server.Version}}"], timeout=15)
            if res.returncode == 0 and res.stdout.strip():
                out.update(available=True, server=res.stdout.strip())
            else:
                err = (res.stderr or "").strip().splitlines()
                msg = err[-1] if err else "no response"
                hint = ("Mount the Docker socket into the Relay container (see docker-compose.yml) and give the relay user access to it."
                        if IN_DOCKER else "Start Docker and make sure this user may use it.")
                out["reason"] = f"Docker is not reachable ({truncate(msg, 200)}). {hint}"
        except StackError as e:
            out["reason"] = str(e)
    if out["available"] and IN_DOCKER:
        out["self_container"] = self_container_id()
    _status_cache.update(at=time.time(), value=out)
    return out


def require_docker():
    st = docker_status()
    if not st["available"]:
        raise StackError(st["reason"])


def self_container_id() -> str:
    """The container Relay itself runs in (never guessed from the hostname, which another container may share)."""
    if os.environ.get("RELAY_SELF_CONTAINER"):
        return os.environ["RELAY_SELF_CONTAINER"]
    if not IN_DOCKER:
        return ""
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(r"/containers/([0-9a-f]{64})/(?:hostname|resolv\.conf|hosts)\b", text)
    if not m:
        return ""
    res = _docker(["inspect", "-f", "{{.Id}}", m.group(1)], timeout=15)
    return m.group(1) if res.returncode == 0 and res.stdout.strip() == m.group(1) else ""


# ============================================================================ names, ports, health
def settings(cfg: dict) -> dict:
    cfg = cfg or {}
    lo, hi = 20000, 29999
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", str(cfg.get("stack_port_range") or ""))
    if m and 1024 <= int(m.group(1)) < int(m.group(2)) <= 65535:
        lo, hi = int(m.group(1)), int(m.group(2))
    return {"prefix": re.sub(r"[^a-z0-9-]", "-", str(cfg.get("stack_prefix") or "relay-stack").lower()).strip("-") or "relay-stack",
            "max": max(1, int(cfg.get("stack_max_concurrent") or 2)), "memory": str(cfg.get("stack_memory_limit") or "1g"),
            "lo": lo, "hi": hi, "build_timeout": float(cfg.get("stack_build_timeout_minutes") or 15) * 60,
            "keep": bool(cfg.get("stack_keep_after_task", False)), "start": cfg.get("stack_start") or "prepare"}


def project_name(tid: str, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", tid.lower())[-12:]
    return f"{prefix}-{slug}"


def env_name(service: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", service.upper())


def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def allocate_ports(count: int, taken, lo=20000, hi=29999, is_free=_port_free, start=None) -> list[int]:
    """`count` host ports in [lo, hi] that no other stack holds and nothing on this machine listens on."""
    taken = set(taken or ())
    span = hi - lo + 1
    offset = random.randrange(span) if start is None else (start - lo) % span
    out = []
    for i in range(span):
        if len(out) == count:
            break
        p = lo + (offset + i) % span
        if p in taken or p in out or not is_free(p):
            continue
        out.append(p)
    if len(out) < count:
        raise StackError(f"No free host ports left in {lo}-{hi}")
    return out


def wait_until(probe, timeout: float, interval: float = 1.0, alive=None, clock=time.monotonic, sleep=time.sleep) -> tuple[bool, str]:
    """Poll `probe() -> (ok, detail)` until it passes, the container dies (`alive() -> (ok, why)`) or time runs out."""
    deadline = clock() + timeout
    last = ""
    while True:
        if alive:
            ok_alive, why = alive()
            if not ok_alive:
                return False, why
        try:
            ok, detail = probe()
        except Exception as e:  # a probe that throws is just not healthy yet
            ok, detail = False, str(e)
        if ok:
            return True, detail
        last = detail or last
        if clock() >= deadline:
            return False, f"not healthy after {int(timeout)}s" + (f": {last}" if last else "")
        sleep(interval)


def http_get(url: str, timeout=5, method="GET", data: str = "") -> tuple[int, str]:
    req = urllib.request.Request(url, method=method, data=data.encode() if data else None,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(200000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(200000).decode("utf-8", "replace") if e.fp else ""


# ============================================================================ state
def _state_path(tid: str) -> Path:
    return STACK_STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', tid)}.json"


def load_state(tid: str) -> dict:
    return read_json(_state_path(tid), None) or {}


def _save_state(tid: str, st: dict) -> None:
    STACK_STATE_DIR.mkdir(parents=True, exist_ok=True)
    st["updated"] = now()
    write_json(_state_path(tid), st)


@contextmanager
def task_lock(tid: str, timeout=1800):
    """One stack operation per task at a time, across the web process and `relay-stack` run by agents."""
    STACK_STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STACK_STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', tid)}.lock"
    f = open(path, "a+")
    try:
        try:
            import fcntl
        except ImportError:  # Windows: stacks need Docker on Linux anyway
            yield
            return
        deadline = time.time() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    raise StackError("Another stack operation for this task is still running")
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def _labels(project: str, tid: str, service: str = "") -> list[str]:
    out = ["--label", f"relay.instance={INSTANCE}", "--label", f"relay.task={tid}", "--label", f"relay.stack={project}"]
    if service:
        out += ["--label", f"relay.service={service}"]
    return out


def owned_resources() -> dict:
    """Containers and networks of this Relay installation, grouped by task id."""
    out: dict[str, dict] = {}
    res = _docker(["ps", "-a", "--filter", f"label=relay.instance={INSTANCE}", "--format", '{{.ID}}\t{{.Names}}\t{{.Label "relay.task"}}\t{{.State}}'], timeout=30)
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            out.setdefault(parts[2], {"containers": [], "networks": []})["containers"].append(parts[1])
    res = _docker(["network", "ls", "--filter", f"label=relay.instance={INSTANCE}", "--format", '{{.Name}}\t{{.Label "relay.task"}}'], timeout=30)
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            out.setdefault(parts[1], {"containers": [], "networks": []})["networks"].append(parts[0])
    return out


def _taken_ports(exclude_tid: str) -> set[int]:
    taken = set()
    for p in STACK_STATE_DIR.glob("*.json") if STACK_STATE_DIR.exists() else []:
        st = read_json(p, None) or {}
        if st.get("tid") == exclude_tid or st.get("status") == "down":
            continue
        for svc in (st.get("services") or {}).values():
            taken.update(int(v) for v in (svc.get("ports") or {}).values())
    res = _docker(["ps", "--format", "{{.Ports}}"], timeout=30)
    taken.update(int(m) for m in re.findall(r":(\d+)->", res.stdout))
    return taken


def _inspect(container: str) -> dict:
    res = _docker(["inspect", container], timeout=30)
    if res.returncode != 0:
        return {}
    try:
        return (json.loads(res.stdout) or [{}])[0]
    except ValueError:
        return {}


def _container_ip(container: str, network: str) -> str:
    info = _inspect(container)
    return (((info.get("NetworkSettings") or {}).get("Networks") or {}).get(network) or {}).get("IPAddress") or ""


def service_logs(container: str, tail=200) -> str:
    """stdout and stderr interleaved as the service wrote them."""
    exe = shutil.which("docker")
    if not exe:
        raise StackError("The docker CLI is not installed in this environment.")
    try:
        res = subprocess.run([exe, "logs", "--tail", str(int(tail)), container], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        return "(docker logs timed out)"
    return res.stdout or ""


def _substitute(value: str, scope: dict) -> str:
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: scope.get(m.group(1), m.group(0)), value)


def _service_env(svc: dict, d: dict, st: dict) -> dict:
    scope = {}
    if svc.get("repo"):
        try:
            repo_vars = repo_env.env_vars(repo_env.load(svc["repo"]))
        except Exception:
            repo_vars = {}
        scope.update(repo_vars)
    scope.update({r["name"]: r["value"] for r in d.get("vars") or []})
    for other in d.get("services") or []:
        if other.get("ports"):  # how services reach each other inside the task network
            scope[f"STACK_{env_name(other['name'])}_INTERNAL_URL"] = f"http://{other['name']}:{other['ports'][0]}"
    env = dict(repo_vars) if svc.get("repo") and svc.get("env_from_repo", True) else {}
    env.update({k: v for k, v in scope.items() if k.startswith("STACK_")})
    for r in svc.get("env") or []:
        env[r["name"]] = _substitute(r["value"], scope)
    return env


def _write_env_file(path: Path, env: dict, log) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in env.items():
        if "\n" in v:
            log(f"  warning: {k} has a line break and cannot be passed to a container; skipped")
            continue
        lines.append(f"{k}={v}")
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _running(container: str) -> tuple[bool, str]:
    info = _inspect(container)
    state = info.get("State") or {}
    if not info:
        return False, "container is gone"
    if state.get("Status") in ("exited", "dead"):
        return False, f"container exited with code {state.get('ExitCode')}"
    return True, ""


# ============================================================================ lifecycle
def up(task: dict, cfg: dict, log=print, only: list[str] | None = None, owner="relay") -> dict:
    """Build and start the task's stack (reusing containers whose image did not change) and wait until healthy."""
    d = def_for_task(task)
    if not d:
        raise StackError("This task has no integration stack. Choose one in Repositories → Environment.")
    require_docker()
    conf = settings(cfg)
    tid = task["id"]
    mask = masker(d)
    say = lambda s: log(mask(s))
    with task_lock(tid):
        owned = owned_resources()
        others = [k for k, v in owned.items() if k != tid and v["containers"]]
        if tid not in owned and len(others) >= conf["max"]:
            raise StackError(f"{len(others)} integration stack(s) are already running (limit {conf['max']}, Settings → Verification). "
                             "Wait for another task to finish or raise the limit.")
        prev = load_state(tid)
        project = prev.get("project") if prev.get("status") not in (None, "down") and prev.get("project") else project_name(tid, conf["prefix"])
        network = f"{project}-net"
        st = {"tid": tid, "stack_id": d["id"], "stack_name": d["name"], "project": project, "network": network,
              "instance": INSTANCE, "status": "starting", "started": prev.get("started") if prev.get("status") == "up" else now(),
              "owner": owner, "error": "", "services": dict(prev.get("services") or {}) if prev.get("project") == project else {},
              "checks": prev.get("checks"), "self_attached": False}
        _save_state(tid, st)
        started = time.time()
        try:
            if _docker(["network", "inspect", network], timeout=20).returncode != 0:
                _docker(["network", "create", *_labels(project, tid), network], timeout=60, check=True)
                say(f"Created network {network}")
            me = self_container_id() if IN_DOCKER else ""
            if me:
                res = _docker(["network", "connect", network, me], timeout=30)
                if res.returncode != 0 and "already exists" not in (res.stderr or "") and "already" not in (res.stderr or ""):
                    raise StackError(f"Could not attach Relay to the stack network: {res.stderr.strip()}")
                st.update(self_attached=True, self_container=me)
            elif IN_DOCKER:
                say("warning: Relay runs in Docker but could not find its own container; service URLs use host ports and may be unreachable from here")
            taken = _taken_ports(tid)
            recreated = set()
            for svc in order(d["services"]):
                if only and svc["name"] not in only and svc["name"] in st["services"]:
                    continue
                st["services"][svc["name"]] = _up_service(task, d, svc, st, conf, taken, say, recreated)
                _save_state(tid, st)
            for fx in d.get("fixtures") or []:
                if fx["service"] not in recreated:
                    continue
                say(f"Fixture {fx['name']} → {fx['service']}")
                c = st["services"][fx["service"]]["container"]
                res = _docker(["exec", "-i", c, "sh", "-c", fx["command"]], timeout=600, input=fx.get("stdin") or None)
                out = (res.stdout or "") + (res.stderr or "")
                if out.strip():
                    say(truncate(out.strip(), 2000, tail=True))
                if res.returncode != 0:
                    raise StackError(f"Fixture {fx['name']} failed (exit {res.returncode})")
            st.update(status="up", error="", ready_seconds=round(time.time() - started, 1))
            say(f"Stack {d['name']} is up ({st['ready_seconds']}s): " + ", ".join(
                f"{n} {s.get('url') or ''}".strip() for n, s in st["services"].items()))
        except Exception as e:
            st.update(status="failed", error=mask(str(e)))
            say(f"Stack failed: {e}")
            _save_state(tid, st)
            raise StackError(mask(str(e))) from None
        _save_state(tid, st)
        return st


def _up_service(task, d, svc, st, conf, taken, say, recreated) -> dict:
    name, project, network, tid = svc["name"], st["project"], st["network"], task["id"]
    container = f"{project}-{name}"
    prev = st["services"].get(name) or {}
    info = {"name": name, "role": svc.get("role", "service"), "container": container, "ports": {}, "url": "", "host_url": "",
            "status": "starting", "health": "", "error": ""}
    src, kind = source_for(svc, task)
    t0 = time.time()
    if src:
        image = f"{project}-{name}:task"
        ctx = (src / svc["build"]["context"]).resolve()
        dockerfile = (src / svc["build"]["dockerfile"]).resolve()
        if not ctx.is_dir():
            raise StackError(f"{name}: build context {svc['build']['context']} does not exist in {src}")
        say(f"Building {name} from the {kind} ({src})")
        res = _docker(["build", "-q", "-t", image, "-f", dockerfile, *_labels(project, tid, name), ctx], timeout=conf["build_timeout"])
        if res.returncode != 0:
            raise StackError(f"{name}: image build failed\n{truncate((res.stderr or res.stdout).strip(), 3000, tail=True)}")
        info.update(source=str(src), source_kind=kind, image=image)
    else:
        image = svc["image"]
        if _docker(["image", "inspect", image], timeout=30).returncode != 0:
            say(f"Pulling {image}")
            _docker(["pull", image], timeout=conf["build_timeout"], check=True)
        info.update(source=image, source_kind="image", image=image)
    image_id = _docker(["image", "inspect", "-f", "{{.Id}}", image], timeout=30).stdout.strip()
    env = _service_env(svc, d, st)
    env_file = STACK_STATE_DIR / tid / f"{name}.env"
    env_hash = hashlib.sha1(json.dumps([env, svc], sort_keys=True).encode()).hexdigest()
    existing = _inspect(container)
    reuse = bool(existing) and existing.get("Image") == image_id and (existing.get("State") or {}).get("Running") \
        and prev.get("env_hash") == env_hash and "ports" in prev
    if reuse:
        info["ports"] = prev["ports"]
        say(f"{name}: unchanged, reusing the running container")
    else:
        if existing:
            _docker(["rm", "-f", container], timeout=60)
        _write_env_file(env_file, env, say)
        run_base = ["run", "-d", "--name", container, "--network", network, "--network-alias", name,
                    *_labels(project, tid, name), "--memory", svc.get("memory") or conf["memory"], "--pids-limit", "1024",
                    "--env-file", env_file]
        cmd = shlex.split(svc["command"]) if svc.get("command") else []
        for attempt in range(5):
            ports = allocate_ports(len(svc["ports"]), taken, conf["lo"], conf["hi"]) if svc["ports"] else []
            publish = [a for cp, hp in zip(svc["ports"], ports) for a in ("-p", f"127.0.0.1:{hp}:{cp}")]
            res = _docker([*run_base, *publish, image, *cmd], timeout=120)
            if res.returncode == 0:
                info["ports"] = {str(cp): hp for cp, hp in zip(svc["ports"], ports)}
                taken.update(ports)
                break
            _docker(["rm", "-f", container], timeout=60)
            if re.search(r"port is already allocated|address already in use", res.stderr or "", re.I):
                taken.update(ports)
                continue
            raise StackError(f"{name}: container did not start\n{truncate(res.stderr.strip(), 1500, tail=True)}")
        else:
            raise StackError(f"{name}: could not find free host ports")
        recreated.add(name)
    info["env_hash"] = env_hash
    if svc["ports"]:
        cport = svc["ports"][0]
        info["host_url"] = f"http://127.0.0.1:{info['ports'][str(cport)]}"
        if st.get("self_attached"):
            ip = _container_ip(container, network)
            info["url"] = f"http://{ip}:{cport}" if ip else info["host_url"]
        else:
            info["url"] = info["host_url"]
    hc = svc.get("healthcheck") or {}
    alive = lambda: _running(container)
    if hc.get("http") and svc["ports"]:
        port = hc.get("port") or svc["ports"][0]
        base = info["url"] if port == svc["ports"][0] else (
            f"http://{_container_ip(container, network)}:{port}" if st.get("self_attached") else f"http://127.0.0.1:{info['ports'].get(str(port))}")

        def probe():
            code, _ = http_get(base + hc["http"], timeout=3)
            return code < 400, f"HTTP {code}"
    elif hc.get("command"):
        def probe():
            res = _docker(["exec", container, "sh", "-c", hc["command"]], timeout=30)
            return res.returncode == 0, truncate((res.stdout + res.stderr).strip(), 200, tail=True)
    else:
        def probe():
            time.sleep(1)
            ok, why = _running(container)
            return ok, why or "running"
    ok, detail = wait_until(probe, hc.get("timeout") or 120, 1.0, alive)
    if not ok:
        tail = service_logs(container, 60)
        info.update(status="unhealthy", error=detail)
        st["services"][name] = info
        raise StackError(f"{name} did not become healthy: {detail}\n--- last log lines of {name} ---\n{truncate(tail, 3000, tail=True)}")
    info.update(status="healthy", health=detail, ready_seconds=round(time.time() - t0, 1))
    say(f"{name}: healthy ({info['ready_seconds']}s){' at ' + info['url'] if info['url'] else ''}")
    return info


def down(tid: str, log=print, logs_dir: Path | None = None, reason="") -> dict:
    """Stop and remove everything this installation created for the task, saving service logs first."""
    require_docker()
    logs_dir = logs_dir or (RUNTIME_DIR / tid / "stack-logs")
    removed = {"containers": [], "networks": [], "images": []}
    with task_lock(tid):
        st = load_state(tid)
        d = get_def(st.get("stack_id")) if st.get("stack_id") else None
        mask = masker(d)
        res = _docker(["ps", "-a", "--filter", f"label=relay.instance={INSTANCE}", "--filter", f"label=relay.task={tid}",
                       "--format", '{{.Names}}\t{{.Label "relay.service"}}'], timeout=30)
        for line in res.stdout.splitlines():
            name, _, svc = line.partition("\t")
            if not name:
                continue
            try:
                text = service_logs(name, 5000)
                if text.strip() and logs_dir.parent.exists():
                    logs_dir.mkdir(parents=True, exist_ok=True)
                    (logs_dir / f"{svc or name}.log").write_text(mask(text), encoding="utf-8")
            except Exception:
                pass
            _docker(["rm", "-f", "-v", name], timeout=120)
            removed["containers"].append(name)
        me = self_container_id() if IN_DOCKER else ""
        res = _docker(["network", "ls", "--filter", f"label=relay.instance={INSTANCE}", "--filter", f"label=relay.task={tid}", "--format", "{{.Name}}"], timeout=30)
        for net in res.stdout.split():
            if me:
                _docker(["network", "disconnect", "-f", net, me], timeout=30)
            r = _docker(["network", "rm", net], timeout=60)
            if r.returncode == 0:
                removed["networks"].append(net)
        res = _docker(["images", "--filter", f"label=relay.instance={INSTANCE}", "--filter", f"label=relay.task={tid}", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30)
        for img in res.stdout.split():
            if _docker(["rmi", img], timeout=120).returncode == 0:
                removed["images"].append(img)
        shutil.rmtree(STACK_STATE_DIR / re.sub(r"[^A-Za-z0-9_.-]", "_", tid), ignore_errors=True)
        if st:
            for s in (st.get("services") or {}).values():
                s["status"] = "stopped"
            st.update(status="down", stopped=now(), stop_reason=reason, logs_dir=str(logs_dir) if logs_dir.exists() else "")
            _save_state(tid, st)
    if removed["containers"] or removed["networks"]:
        log(f"Stack removed: {len(removed['containers'])} container(s), {len(removed['networks'])} network(s), {len(removed['images'])} image(s)")
    return removed


def teardown_quietly(tid: str, reason="") -> None:
    """For task end/stop/delete: never raises, does nothing when Docker or the stack is absent."""
    try:
        st = load_state(tid)
        if not st or st.get("status") == "down":
            return
        if not docker_status()["available"]:
            return
        down(tid, log=lambda s: None, reason=reason)
    except Exception:
        pass


def gc(keep: set[str], log=print) -> list[str]:
    """Remove stacks of this installation whose task is not in `keep` (Relay restarted, task deleted, crash)."""
    if not docker_status(force=True)["available"]:
        return []
    removed = []
    for tid, res in owned_resources().items():
        if tid and tid not in keep:
            down(tid, log=log, reason="orphan collected on startup")
            removed.append(tid)
    for p in STACK_STATE_DIR.glob("*.json") if STACK_STATE_DIR.exists() else []:
        st = read_json(p, None) or {}
        if st.get("tid") and st.get("tid") not in keep and st.get("status") not in ("down", None):
            st.update(status="down", stop_reason="orphan collected on startup")
            _save_state(st["tid"], st)
    return removed


def restart(task: dict, cfg: dict, service: str, log=print) -> dict:
    """Rebuild one service from its source and recreate it (a restart that also picks up code changes)."""
    st = load_state(task["id"])
    if service not in (st.get("services") or {}):
        raise StackError(f"Unknown service {service!r}")
    require_docker()
    c = st["services"][service]["container"]
    _docker(["rm", "-f", c], timeout=60)
    return up(task, cfg, log=log, only=[service])


def ensure_attached(st: dict) -> dict:
    """After Relay's container is recreated, rejoin running stack networks so service URLs keep working."""
    if not st or st.get("status") not in ("up", "starting", "failed") or not st.get("self_attached") or not IN_DOCKER:
        return st
    me = self_container_id()
    if me and st.get("self_container") != me:
        res = _docker(["network", "connect", st["network"], me], timeout=30)
        if res.returncode == 0 or "already" in (res.stderr or ""):
            st["self_container"] = me
            _save_state(st["tid"], st)
    return st


def status(tid: str) -> dict:
    """The saved state refreshed with what Docker reports right now."""
    st = load_state(tid)
    if not st or st.get("status") == "down" or not docker_status()["available"]:
        return st
    ensure_attached(st)
    for name, svc in (st.get("services") or {}).items():
        ok, why = _running(svc["container"])
        if not ok and svc.get("status") == "healthy":
            svc.update(status="exited", error=why)
    return st


def logs(tid: str, service: str, tail=300) -> str:
    st = load_state(tid)
    svc = (st.get("services") or {}).get(service)
    if not svc:
        raise StackError(f"Unknown service {service!r}")
    d = get_def(st.get("stack_id")) if st.get("stack_id") else None
    if st.get("status") == "down":
        saved = Path(st.get("logs_dir") or RUNTIME_DIR / tid / "stack-logs") / f"{service}.log"
        return saved.read_text(encoding="utf-8", errors="replace")[-200000:] if saved.exists() else "(the stack is down and no logs were saved)"
    require_docker()
    return masker(d)(service_logs(svc["container"], tail))


def env_for_task(tid: str) -> dict:
    """STACK_* variables for agents and commands while the task's stack is running."""
    st = load_state(tid)
    if not st or st.get("status") not in ("up", "starting", "failed"):
        return {}
    env = {"STACK_PROJECT": st.get("project", ""), "STACK_NETWORK": st.get("network", "")}
    for name, svc in (st.get("services") or {}).items():
        if svc.get("url"):
            env[f"STACK_{env_name(name)}_URL"] = svc["url"]
            env[f"STACK_{env_name(name)}_HOST_URL"] = svc.get("host_url", "")
    return env


# ============================================================================ end-to-end checks
_BROWSER_JS = r"""
const { chromium } = require("playwright-core");
(async () => {
  const [url, expect, timeout] = process.argv.slice(2);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const problems = [];
  page.on("pageerror", (e) => problems.push(`page error: ${e.message}`));
  page.on("console", (m) => m.type() === "error" && problems.push(`console error: ${m.text()}`));
  let ok = true;
  try { await page.goto(url, { waitUntil: "networkidle", timeout: Number(timeout) * 1000 }); }
  catch (e) { problems.push(`navigation: ${e.message.split("\n")[0]}`); ok = false; }
  const text = ok ? await page.innerText("body").catch(() => "") : "";
  if (expect && !text.includes(expect)) { ok = false; problems.push(`page does not contain: ${expect}`); }
  console.log(text.slice(0, 1500));
  problems.forEach((p) => console.log(p));
  await browser.close();
  process.exit(ok && !problems.some((p) => p.startsWith("page error")) ? 0 : 1);
})();
"""


def _run_http(check, st) -> dict:
    svc = (st.get("services") or {}).get(check["service"]) or {}
    if not svc.get("url"):
        return {"ok": False, "rc": 1, "output": f"service {check['service']} has no URL (no ports or not running)"}
    url = svc["url"] + check["path"]
    try:
        code, text = http_get(url, timeout=check.get("timeout") or 30, method=check.get("method") or "GET", data=check.get("body") or "")
    except Exception as e:
        return {"ok": False, "rc": 1, "output": f"{check.get('method', 'GET')} {check['path']} → {e}"}
    problems = []
    if code != int(check.get("expect_status") or 200):
        problems.append(f"expected HTTP {check.get('expect_status') or 200}, got {code}")
    if check.get("expect_text") and check["expect_text"] not in text:
        problems.append(f"response does not contain {check['expect_text']!r}")
    out = f"{check.get('method', 'GET')} {check['service']}{check['path']} → HTTP {code}\n{truncate(text, 1500)}"
    if problems:
        out += "\n" + "\n".join(problems)
    return {"ok": not problems, "rc": 0 if not problems else 1, "output": out}


def _run_browser(check, st) -> dict:
    svc = (st.get("services") or {}).get(check["service"]) or {}
    node = shutil.which("node")
    if not node:
        return {"ok": False, "rc": 127, "cannot_run": True, "output": "node is not installed, so the browser check cannot run"}
    if not svc.get("url"):
        return {"ok": False, "rc": 1, "output": f"service {check['service']} has no URL"}
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as f:
        f.write(_BROWSER_JS)
        script = f.name
    try:
        env = {**os.environ, "NODE_PATH": os.environ.get("NODE_PATH") or "/usr/local/lib/node_modules"}
        res = subprocess.run([node, script, svc["url"] + check["path"], check.get("expect_text") or "", str(check.get("timeout") or 30)],
                             capture_output=True, text=True, timeout=(check.get("timeout") or 30) + 60, env=env)
        out = (res.stdout + res.stderr).strip()
        cannot = "Cannot find module 'playwright-core'" in out or "Executable doesn't exist" in out
        return {"ok": res.returncode == 0, "rc": 127 if cannot else res.returncode, "cannot_run": cannot, "output": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "output": "browser check timed out"}
    finally:
        os.unlink(script)


def run_checks(task: dict, d: dict, cfg: dict, run_command=None, log=print) -> list[dict]:
    """Run the stack's end-to-end checks against the running stack.

    `run_command(cmd, cwd, timeout) -> {ok, rc, output, duration}` runs command checks (the pipeline passes its runner so
    they appear in the conversation); by default they run here with the STACK_* variables exported.
    """
    tid = task["id"]
    st = ensure_attached(load_state(tid))
    mask = masker(d)
    cwd = task.get("worktree") or task.get("repo") or os.getcwd()
    timeout = float((cfg or {}).get("verification_timeout_minutes") or 20) * 60
    items = []
    for c in d.get("checks") or []:
        t0 = time.time()
        if c["kind"] == "command":
            if run_command:
                res = run_command(c["command"], cwd, timeout)
            else:
                env = {**os.environ, **env_for_task(tid), "RELAY_TASK_ID": tid}
                try:
                    p = subprocess.run(["bash", "-lc", c["command"]], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
                    res = {"ok": p.returncode == 0, "rc": p.returncode, "output": p.stdout + p.stderr}
                except subprocess.TimeoutExpired:
                    res = {"ok": False, "rc": -1, "output": f"timed out after {int(timeout)}s"}
            res["cannot_run"] = res.get("rc") in (126, 127)
        elif c["kind"] == "http":
            res = _run_http(c, st)
        else:
            res = _run_browser(c, st)
        item = {"name": c["name"], "kind": c["kind"], "required": c["required"], "passed": bool(res["ok"]),
                "rc": res.get("rc"), "cannot_run": bool(res.get("cannot_run")), "duration": round(res.get("duration") or time.time() - t0, 1),
                "output": mask(truncate(res.get("output") or "", 4000, tail=True))}
        if not res["ok"]:
            item["logs"] = failing_logs(st, d)
        log(f"{'PASS' if res['ok'] else 'FAIL'} {c['name']}")
        items.append(item)
    st = load_state(tid)
    if st:
        st["checks"] = {"ok": all(i["passed"] or not i["required"] for i in items), "items": items, "time": now()}
        _save_state(tid, st)
    return items


def failing_logs(st: dict, d: dict | None, lines=40) -> dict:
    """Recent log lines of every service, for a failing check: the cause is usually in another service."""
    out = {}
    mask = masker(d)
    for name, svc in (st.get("services") or {}).items():
        try:
            out[name] = mask(truncate(service_logs(svc["container"], lines), 2500, tail=True))
        except Exception:
            pass
    return out


# ============================================================================ prompts
def describe(task: dict, d: dict | None = None) -> str:
    d = d or def_for_task(task)
    if not d:
        return ""
    st = load_state(task["id"])
    lines = [f"- Integration stack `{d['name']}`: Relay builds these services from this task's branches and runs them together in Docker:"]
    for s in d["services"]:
        src = "image " + s["image"] if s.get("image") else ("this worktree" if task.get("repo") and _same(s["repo"], task["repo"]) else f"repository {Path(s['repo']).name}")
        live = (st.get("services") or {}).get(s["name"]) or {}
        url = f", URL `$STACK_{env_name(s['name'])}_URL`" + (f" ({live['url']})" if live.get("url") and st.get("status") != "down" else "") if s.get("ports") else ""
        inside = f", other services reach it at `http://{s['name']}:{s['ports'][0]}`" if s.get("ports") else ""
        lines.append(f"  - `{s['name']}`{' (simulator)' if s.get('role') == 'simulator' else ''} from {src}{url}{inside}")
    if d.get("checks"):
        lines.append("  End-to-end checks Relay runs against the stack at verification: "
                     + "; ".join(f"`{c['name']}`{'' if c['required'] else ' (optional)'}" for c in d["checks"]) + ".")
    lines.append("  Prove cross-service behaviour through the stack, not with mocks: `relay-stack up` rebuilds changed services from "
                 "your edits, `relay-stack status`, `relay-stack logs <service>`, `relay-stack url <service>`, `relay-stack check` runs the "
                 "end-to-end checks. Do not run `docker` directly.")
    return "\n".join(lines)


# ============================================================================ pipeline hooks
def pipeline_prepare(p) -> None:
    """Called once the worktree and environment are ready. Failures are recorded, never fatal."""
    p.stack = def_for_task({**p.task, "worktree": str(p.wt)})
    if not p.stack:
        return
    p.m.set_meta(p.tid, stack={"id": p.stack["id"], "name": p.stack["name"]})
    if settings(p.cfg)["start"] != "prepare":
        return
    try:
        _pipeline_up(p, "Starting integration stack")
    except StackError:
        pass


def _pipeline_up(p, title) -> dict:
    task = {**p.task, "id": p.tid, "worktree": str(p.wt), "repo_worktrees": (p.task_meta() or {}).get("repo_worktrees") or {}}
    mask = masker(p.stack)
    m = p.r.msg(role="setup", agent=None, kind="command", content=f"relay-stack up ({p.stack['name']})", title=f"{title} · {p.stack['name']}", status="running")
    lines = []

    def log(s):
        lines.append(mask(s))
        p.r.rawlog(mask(s), "stack")
        p.r.msg_update(m["id"], output=truncate("\n".join(lines), 12000, tail=True))

    t0 = time.time()
    try:
        st = up(task, p.cfg, log=log, owner="pipeline")
    except StackError as e:
        p.r.msg_update(m["id"], status="error", output=truncate("\n".join(lines), 12000, tail=True), duration=time.time() - t0, rc=1)
        p.record_blocked([{"check": f"Integration stack {p.stack['name']}", "action_required": not docker_status()["available"],
                           "reason": truncate(str(e).splitlines()[0], 300),
                           "impact": "end-to-end checks cannot prove the services work together until the stack starts"}])
        p.r.timeline("system", "Integration stack failed", truncate(str(e).splitlines()[0], 200))
        raise
    p.r.msg_update(m["id"], status="ok", output=truncate("\n".join(lines), 12000, tail=True), duration=time.time() - t0, rc=0)
    p.r.timeline("system", "Integration stack up", ", ".join(f"{n}: {s.get('status')}" for n, s in st["services"].items()))
    return st


def pipeline_verify(p) -> tuple[list[dict], list[str]]:
    """Bring the stack up from the current worktree (reusing unchanged containers) and run its end-to-end checks."""
    d = getattr(p, "stack", None)
    if not d or not d.get("checks"):
        if d:
            try:
                _pipeline_up(p, "Rebuilding integration stack")
            except StackError:
                pass
        return [], []
    items, parts = [], []
    try:
        _pipeline_up(p, "Rebuilding integration stack")
    except StackError as e:
        reason = str(e)
        docker_missing = not docker_status()["available"]
        logs_ = failing_logs(load_state(p.tid), d) if not docker_missing else {}
        for c in d["checks"]:
            items.append({"command": f"e2e · {c['name']}", "kind": "e2e", "ok": not c["required"], "passed": False, "rc": 1,
                          "skipped": False, "pre_existing": False, "optional": not c["required"], "duration": 0,
                          "cannot_run": docker_missing, "stack": True})
        parts.append(f"$ integration stack {d['name']}\nFAIL (the stack did not start, so no end-to-end check could run)\n"
                     + truncate(reason, 3000, tail=True) + _logs_text(logs_))
        _record_checks(p, d, [{"name": c["name"], "kind": c["kind"], "required": c["required"], "passed": False, "rc": 1,
                               "cannot_run": docker_missing, "duration": 0, "output": truncate(reason, 1500)} for c in d["checks"]])
        return items, parts
    task = {**p.task, "id": p.tid, "worktree": str(p.wt), "repo_worktrees": (p.task_meta() or {}).get("repo_worktrees") or {}}

    def run_command(cmd, cwd, timeout):
        return p.r.run_shell(cmd, cwd, "verify", timeout=timeout, title=f"End-to-end · {cmd}")

    results = run_checks(task, d, p.cfg, run_command=run_command, log=lambda s: p.r.rawlog(s, "stack"))
    fail_chars = int(p.cfg.get("budget_verify_chars") or 3500)
    for r in results:
        optional = not r["required"]
        items.append({"command": f"e2e · {r['name']}", "kind": "e2e", "ok": r["passed"] or optional, "passed": r["passed"], "rc": r["rc"],
                      "skipped": False, "pre_existing": False, "optional": optional, "duration": r["duration"],
                      "cannot_run": r["cannot_run"], "stack": True})
        verdict = "PASS" if r["passed"] else ("FAIL (optional check; reported, does not block)" if optional else "FAIL")
        head = f"$ e2e · {r['name']} ({r['kind']}, integration stack {d['name']})\n{verdict} (exit {r['rc']}, {round(r['duration'])}s)"
        body = "" if r["passed"] else "\n" + truncate(r["output"], fail_chars, tail=True) + _logs_text(r.get("logs") or {})
        parts.append(head + body)
        if r["cannot_run"]:
            p.record_blocked([{"check": f"e2e · {r['name']}", "action_required": True, "reason": truncate(r["output"].strip().splitlines()[-1] if r["output"].strip() else "cannot run", 300),
                               "impact": "this end-to-end check could not run, so it proves nothing"}])
    _record_checks(p, d, results)
    return items, parts


def _logs_text(logs_: dict) -> str:
    if not logs_:
        return ""
    return "\n" + "\n".join(f"--- last log lines of {n} ---\n{t.strip()}" for n, t in logs_.items() if (t or "").strip())


def _record_checks(p, d, results):
    st = load_state(p.tid)
    ok = all(r["passed"] or not r["required"] for r in results)
    summary = {"id": d["id"], "name": d["name"], "ok": ok, "time": now(),
               "checks": [{k: r[k] for k in ("name", "kind", "required", "passed", "duration", "cannot_run")} for r in results],
               "status": st.get("status")}
    if st:
        st["checks"] = {"ok": ok, "items": results, "time": now()}
        _save_state(p.tid, st)
    p.m.set_meta(p.tid, stack=summary)


def pipeline_teardown(p) -> None:
    if not getattr(p, "stack", None) or settings(p.cfg)["keep"]:
        return
    st = load_state(p.tid)
    if not st or st.get("status") == "down":
        return
    try:
        down(p.tid, log=lambda s: p.r.timeline("system", "Integration stack stopped", s), logs_dir=Path(p.run_dir) / "stack-logs",
             reason="task ended")
        summary = p.task_meta().get("stack")
        if summary:
            p.m.set_meta(p.tid, stack={**summary, "status": "down"})
    except Exception as e:  # stopping must never mask the task's own outcome
        try:
            p.r.timeline("system", "Integration stack teardown failed", str(e)[:200])
        except Exception:
            pass
