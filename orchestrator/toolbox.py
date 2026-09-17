"""Toolbox: tools the owner (and, with approval, the agents) add to every CLI Relay drives.

Three kinds of tool:
  mcp       an MCP server: a stdio command (installed by Relay or run through npx/uvx) or a remote
            streamable-HTTP URL with headers. Relay writes it into each CLI's own config format for one
            turn, in the task's run folder, and never touches the user's global CLI configuration.
  cli       a command-line program installed into Relay's tools folder, described to agents by a usage line.
  builtin   what Relay already ships (relay-connect, relay-stack, relay-screenshot, relay-tools, rg, jq …),
            listed for visibility only.

Registry: DATA_DIR/tools.json  {"tools": [...], "repos": {repo: {"enable": [], "disable": [], "dev": bool}},
                                "requests": [...]}
Usage:    DATA_DIR/tool_stats.json  per tool: calls, last use, per agent and role.
Installs: TOOLS_DIR (RELAY_TOOLS_DIR, default DATA_DIR/tools): npm prefix, one venv per pip tool, bin links.

Scope of a task, in order: the task's own list when it has one (New task → Advanced), otherwise every globally
enabled tool plus the repository's additions minus its removals; tools an agent requested for the task and the
owner approved are always added.

Secrets (env values and headers marked secret) stay in tools.json, are masked in every API response and log,
and reach a CLI only through its process environment at spawn: the per-run config files reference variables
(`${VAR}`, `{env:VAR}`, codex `env_vars`), never the values.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets as _secrets
import select
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .util import APP_DIR, DATA_DIR, now, read_json, truncate, write_json

PATH = DATA_DIR / "tools.json"
STATS = DATA_DIR / "tool_stats.json"
TOOLS_DIR = Path(os.environ.get("RELAY_TOOLS_DIR") or DATA_DIR / "tools").expanduser().resolve()
NPM_PREFIX = TOOLS_DIR / "npm"
VENVS = TOOLS_DIR / "venvs"
BIN = TOOLS_DIR / "bin"
MASK = "●●●●"
KINDS = ("mcp", "cli")
POLICIES = ("ask", "auto_catalog", "auto_dev", "off")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_lock = threading.RLock()
_jobs: dict[str, dict] = {}

# ============================================================================ catalog
# Reputable, widely used servers that help with coding. Every package name was checked against its registry
# (npm / PyPI) when it was added. Nothing is installed until the owner clicks Install (or approves a request).
CATALOG = [
    {"id": "filesystem", "kind": "mcp", "label": "Filesystem", "publisher": "Model Context Protocol (reference server)",
     "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
     "description": "Read, write, search and move files, limited to the task worktree.",
     "install": {"kind": "npm", "package": "@modelcontextprotocol/server-filesystem"}, "binary": "mcp-server-filesystem",
     "transport": "stdio", "args": ["{worktree}"], "fallback": "your CLI's own file tools and the shell"},
    {"id": "fetch", "kind": "mcp", "label": "Fetch", "publisher": "Model Context Protocol (reference server)",
     "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
     "description": "Fetch a URL and return it as markdown (docs, changelogs, API references).",
     "install": {"kind": "pip", "package": "mcp-server-fetch"}, "binary": "mcp-server-fetch",
     "transport": "stdio", "args": [], "fallback": "`curl -sL <url>`"},
    {"id": "git", "kind": "mcp", "label": "Git", "publisher": "Model Context Protocol (reference server)",
     "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/git",
     "description": "Structured git status, diff, log and blame for the task worktree.",
     "install": {"kind": "pip", "package": "mcp-server-git"}, "binary": "mcp-server-git",
     "transport": "stdio", "args": ["--repository", "{worktree}"], "fallback": "`git` in the shell"},
    {"id": "memory", "kind": "mcp", "label": "Memory", "publisher": "Model Context Protocol (reference server)",
     "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
     "description": "A small knowledge graph the agents of one task share (entities, relations, observations).",
     "install": {"kind": "npm", "package": "@modelcontextprotocol/server-memory"}, "binary": "mcp-server-memory",
     "transport": "stdio", "args": [], "env": [{"name": "MEMORY_FILE_PATH", "value": "{run_dir}/tools/memory.jsonl", "secret": False}],
     "fallback": "notes in your report"},
    {"id": "sequential-thinking", "kind": "mcp", "label": "Sequential thinking", "publisher": "Model Context Protocol (reference server)",
     "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
     "description": "Structured step-by-step reasoning for hard design or debugging problems.",
     "install": {"kind": "npm", "package": "@modelcontextprotocol/server-sequential-thinking"}, "binary": "mcp-server-sequential-thinking",
     "transport": "stdio", "args": [], "fallback": "think step by step"},
    {"id": "time", "kind": "mcp", "label": "Time", "publisher": "Model Context Protocol (reference server)",
     "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/time",
     "description": "Current time and time-zone conversion.",
     "install": {"kind": "pip", "package": "mcp-server-time"}, "binary": "mcp-server-time", "transport": "stdio", "args": [],
     "fallback": "`date`"},
    {"id": "playwright", "kind": "mcp", "label": "Playwright", "publisher": "Microsoft",
     "homepage": "https://github.com/microsoft/playwright-mcp",
     "description": "Drive a headless browser: navigate, click, fill forms, read the accessibility tree, take screenshots.",
     "install": {"kind": "npm", "package": "@playwright/mcp"}, "binary": "playwright-mcp",
     "transport": "stdio", "args": ["--headless", "--isolated", "--browser", "chromium", "--output-dir", "{run_dir}/screenshots"],
     "fallback": "`relay-screenshot <url> <out.png>`"},
    {"id": "context7", "kind": "mcp", "label": "Context7 docs", "publisher": "Upstash",
     "homepage": "https://github.com/upstash/context7",
     "description": "Up-to-date, version-specific library documentation and code examples, fetched on demand.",
     "install": {"kind": "npm", "package": "@upstash/context7-mcp"}, "binary": "context7-mcp", "transport": "stdio", "args": [],
     "fallback": "the library's own docs through `curl`"},
    {"id": "github", "kind": "mcp", "label": "GitHub (remote)", "publisher": "GitHub",
     "homepage": "https://github.com/github/github-mcp-server",
     "description": "Issues, pull requests, code search and Actions through GitHub's hosted MCP server. Needs a token.",
     "install": {"kind": "none"}, "transport": "http", "url": "https://api.githubcopilot.com/mcp/",
     "headers": [{"name": "Authorization", "value": "", "secret": True, "placeholder": "Bearer ghp_… (a fine-grained, read-only token is enough)"}],
     "fallback": "`gh` in the shell"},
    {"id": "postgres", "kind": "mcp", "label": "PostgreSQL (restricted)", "publisher": "Crystal DBA (postgres-mcp)",
     "homepage": "https://github.com/crystaldba/postgres-mcp",
     "description": "Schema, EXPLAIN plans, index advice and read-only SQL against a development database.",
     "install": {"kind": "pip", "package": "postgres-mcp"}, "binary": "postgres-mcp", "transport": "stdio", "args": ["--access-mode=restricted"],
     "env": [{"name": "DATABASE_URI", "value": "", "secret": True, "placeholder": "postgresql://readonly:…@db:5432/app"}],
     "fallback": "a postgres connector (`relay-connect sql`)"},
    {"id": "ast-grep", "kind": "cli", "label": "ast-grep", "publisher": "ast-grep",
     "homepage": "https://ast-grep.github.io/",
     "description": "Structural code search and rewrite with syntax-aware patterns.",
     "install": {"kind": "npm", "package": "@ast-grep/cli"}, "binary": "ast-grep",
     "usage": "`ast-grep run -p 'console.log($A)' -l ts src/` finds by syntax; add `-r '<rewrite>' -U` to rewrite"},
]
CATALOG_BY_ID = {c["id"]: c for c in CATALOG}

BUILTINS = [
    ("relay-connect", "Connectors: call the real API, database, logs and running app behind the code through Relay"),
    ("relay-stack", "Integration stack: start, check and read logs of the task's services"),
    ("relay-screenshot", "Headless browser screenshots with console errors"),
    ("relay-tools", "List the task's tools and request new ones"),
    ("rg", "ripgrep: fast code search"), ("jq", "JSON processor"), ("git", "Git"), ("gh", "GitHub CLI"),
    ("docker", "Docker client (integration stacks)"), ("node", "Node.js"), ("python3", "Python"), ("curl", "HTTP client"),
]

# How each CLI takes MCP servers for one run (verified against each CLI's --help): see mcp_for_turn().
MCP_SUPPORT = {
    "claude": "--mcp-config <run file>", "codex": "-c mcp_servers.<name>.* overrides", "gemini": "GEMINI_CLI_SYSTEM_SETTINGS_PATH run file",
    "qwen": "--mcp-config <run file>", "copilot": "--additional-mcp-config @<run file>", "amp": "--mcp-config <run file>",
    "opencode": "OPENCODE_CONFIG_CONTENT", "kilo": "KILO_CONFIG_CONTENT", "goose": "--with-extension / --with-streamable-http-extension",
}


# ============================================================================ storage
def _data() -> dict:
    d = read_json(PATH, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("tools", [])
    d.setdefault("repos", {})
    d.setdefault("requests", [])
    return d


def _write(d: dict) -> None:
    write_json(PATH, d)
    try:
        os.chmod(PATH, 0o600)  # secrets live here
    except OSError:
        pass


def load_all() -> list[dict]:
    with _lock:
        return _data()["tools"]


def get(name: str) -> dict | None:
    return next((t for t in load_all() if t["name"] == name), None)


def _pairs(rows, old_rows=None) -> list[dict]:
    """Env or header rows; a masked secret keeps the stored value."""
    old = {r.get("name"): r for r in old_rows or []}
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        if not name:
            continue
        value = str(r.get("value") if r.get("value") is not None else "")
        secret = bool(r.get("secret"))
        if secret and value == MASK:
            value = (old.get(name) or {}).get("value", "")
        out.append({"name": name[:100], "value": value[:4000], "secret": secret})
    return out


def normalize(incoming: dict, old: dict | None = None) -> dict:
    """Validate a tool definition from the browser, a catalog entry or an agent request."""
    old = old or {}
    name = str(incoming.get("name") or "").strip().lower()
    if not _NAME.match(name):
        raise ValueError("Name: lower-case letters, digits, - and _, up to 40 characters, starting with a letter or digit")
    kind = incoming.get("kind") or old.get("kind") or "mcp"
    if kind not in KINDS:
        raise ValueError(f"Kind must be one of {', '.join(KINDS)}")
    inst = incoming.get("install") if isinstance(incoming.get("install"), dict) else (old.get("install") or {"kind": "none"})
    ik = inst.get("kind") or "none"
    if ik not in ("npm", "pip", "script", "none"):
        raise ValueError("Install kind must be npm, pip, script or none")
    if ik in ("npm", "pip") and not re.match(r"^[@A-Za-z0-9][@A-Za-z0-9._/\-\[\]=<>~!,]*$", str(inst.get("package") or "")):
        raise ValueError("Install: a valid package name is required")
    if ik == "script" and not str(inst.get("command") or "").strip():
        raise ValueError("Install: the script command is required")
    t = {
        "name": name, "kind": kind, "label": str(incoming.get("label") or old.get("label") or name)[:80],
        "description": str(incoming.get("description") if incoming.get("description") is not None else old.get("description", ""))[:400],
        "source": incoming.get("source") or old.get("source") or "custom", "catalog_id": incoming.get("catalog_id") or old.get("catalog_id") or "",
        "install": {"kind": ik, **({"package": str(inst.get("package"))} if ik in ("npm", "pip") else {}),
                    **({"command": str(inst.get("command"))} if ik == "script" else {})},
        "binary": str(incoming.get("binary") if incoming.get("binary") is not None else old.get("binary", "")).strip()[:120],
        "enabled": bool(incoming.get("enabled", old.get("enabled", False))),
        "fallback": str(incoming.get("fallback") or old.get("fallback") or "")[:200],
        "created": old.get("created") or now(), "updated": now(), "created_by": old.get("created_by") or incoming.get("created_by") or "owner",
        "installed_at": old.get("installed_at"),
    }
    if kind == "mcp":
        transport = incoming.get("transport") or old.get("transport") or "stdio"
        if transport not in ("stdio", "http"):
            raise ValueError("Transport must be stdio or http")
        t["transport"] = transport
        if transport == "stdio":
            cmd = str(incoming.get("command") if incoming.get("command") is not None else old.get("command", "")).strip() or t["binary"]
            if not cmd:
                raise ValueError("A stdio MCP server needs a command (or an installed binary)")
            args = incoming.get("args") if incoming.get("args") is not None else old.get("args", [])
            if isinstance(args, str):
                args = shlex.split(args)
            t.update(command=cmd[:300], args=[str(a)[:500] for a in args or []][:40],
                     env=_pairs(incoming.get("env") if incoming.get("env") is not None else old.get("env"), old.get("env")))
            for e in t["env"]:
                if not _ENV_NAME.match(e["name"]):
                    raise ValueError(f"Environment variable name {e['name']!r} is not valid")
        else:
            url = str(incoming.get("url") or old.get("url") or "").strip()
            if not re.match(r"^https?://", url):
                raise ValueError("A remote MCP server needs an http(s) URL")
            t.update(url=url[:500], headers=_pairs(incoming.get("headers") if incoming.get("headers") is not None else old.get("headers"), old.get("headers")))
    else:
        if not t["binary"]:
            raise ValueError("A command-line tool needs the name of the command it installs")
        t["usage"] = str(incoming.get("usage") if incoming.get("usage") is not None else old.get("usage", ""))[:400]
    return t


def save(incoming: dict) -> dict:
    with _lock:
        d = _data()
        original = incoming.get("original_name") or incoming.get("name")
        old = next((t for t in d["tools"] if t["name"] == original), None)
        t = normalize(incoming, old)
        if t["name"] != original and any(x["name"] == t["name"] for x in d["tools"]):
            raise ValueError(f"A tool named {t['name']} already exists")
        d["tools"] = [x for x in d["tools"] if x["name"] != original] + [t]
        _write(d)
        return t


def delete(name: str) -> None:
    with _lock:
        d = _data()
        d["tools"] = [t for t in d["tools"] if t["name"] != name]
        for scope in d["repos"].values():
            for k in ("enable", "disable"):
                scope[k] = [n for n in scope.get(k) or [] if n != name]
        _write(d)


def from_catalog(cid: str, overrides: dict | None = None) -> dict:
    c = CATALOG_BY_ID.get(cid)
    if not c:
        raise ValueError(f"Unknown catalog tool {cid}")
    spec = {k: copy.deepcopy(v) for k, v in c.items() if k not in ("id", "publisher", "homepage")}
    spec.update(name=cid, source="catalog", catalog_id=cid)
    for rows in ("env", "headers"):
        for r in spec.get(rows) or []:
            r.pop("placeholder", None)
    spec.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return spec


def public(t: dict) -> dict:
    out = copy.deepcopy(t)
    for rows in ("env", "headers"):
        for r in out.get(rows) or []:
            if r.get("secret"):
                r["has_value"] = bool(r.get("value"))
                r["value"] = MASK if r.get("value") else ""
    out["installed"] = installed(t)
    out["missing_secrets"] = [r["name"] for rows in ("env", "headers") for r in t.get(rows) or [] if r.get("secret") and not r.get("value")]
    j = job(t["name"])
    if j:
        out["job"] = j
    return out


def secret_values(tools: list[dict]) -> list[str]:
    return [r["value"] for t in tools for rows in ("env", "headers") for r in t.get(rows) or []
            if r.get("secret") and len(str(r.get("value") or "")) >= 4]


def masker(tools: list[dict]):
    vals = sorted(set(secret_values(tools)), key=len, reverse=True)
    # "Bearer <token>" headers: the token alone can show up in logs too.
    vals += [v.split(" ", 1)[1] for v in vals if v.lower().startswith("bearer ") and len(v) > 12]

    def mask(text: str) -> str:
        for v in vals:
            if v and v in text:
                text = text.replace(v, MASK)
        return text
    return mask


# ============================================================================ install
def binary_path(t: dict) -> str:
    """Absolute path of the tool's command when Relay installed it, else the command as written."""
    b = t.get("binary") or ""
    if b:
        for d in (BIN, NPM_PREFIX / "bin"):
            p = d / b
            if p.exists():
                return str(p)
    return ""


def installed(t: dict) -> bool:
    ik = (t.get("install") or {}).get("kind") or "none"
    if ik == "none":
        if t.get("kind") == "cli":
            return bool(shutil.which(t.get("binary") or ""))
        return True
    return bool(binary_path(t))


def job(name: str) -> dict | None:
    with _lock:
        j = _jobs.get(name)
        return dict(j) if j else None


def _install_commands(t: dict, action: str) -> list[list[str]]:
    inst = t.get("install") or {}
    kind = inst.get("kind")
    if kind == "npm":
        pkg = inst["package"]
        base = pkg.rsplit("@", 1)[0] if pkg.count("@") > (1 if pkg.startswith("@") else 0) else pkg
        if action == "remove":
            return [["npm", "uninstall", "-g", "--prefix", str(NPM_PREFIX), base]]
        return [["npm", "install", "-g", "--no-audit", "--no-fund", "--prefix", str(NPM_PREFIX), pkg if base != pkg else f"{pkg}@latest"]]
    if kind == "pip":
        venv = VENVS / t["name"]
        if action == "remove":
            return []
        cmds = [] if (venv / "bin" / "python").exists() else [["python3", "-m", "venv", str(venv)]]
        return cmds + [[str(venv / "bin" / "pip"), "install", "--disable-pip-version-check", "--upgrade", inst["package"]]]
    if kind == "script":
        return [] if action == "remove" else [["bash", "-lc", inst["command"]]]
    return []


def install(name: str, action: str = "install", on_done=None) -> dict:
    """Install, update or remove a tool in the background (npm / pip / script into TOOLS_DIR)."""
    t = get(name)
    if not t:
        raise ValueError(f"No tool named {name}")
    if action not in ("install", "update", "remove"):
        raise ValueError("Unknown action")
    with _lock:
        running = _jobs.get(name)
        if running and running.get("state") == "running":
            return dict(running)
        cmds = _install_commands(t, "remove" if action == "remove" else "install")
        _jobs[name] = {"tool": name, "action": action, "state": "running", "started": time.time(), "log": "", "error": None}

    def run():
        log, ok, err = [], True, None
        env = os.environ.copy()
        env.update({"PREFIX": str(TOOLS_DIR), "BIN_DIR": str(BIN), "INSTALL_DIR": str(BIN), "CI": "1", "NO_COLOR": "1",
                    "npm_config_update_notifier": "false"})
        try:
            for d in (BIN, NPM_PREFIX, VENVS):
                d.mkdir(parents=True, exist_ok=True)
            for cmd in cmds:
                log.append("$ " + " ".join(cmd))
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=env)
                log.append(truncate((p.stdout or "") + (p.stderr or ""), 6000, tail=True))
                if p.returncode != 0:
                    ok, err = False, f"exit {p.returncode}"
                    break
            kind = (t.get("install") or {}).get("kind")
            b = t.get("binary") or ""
            if ok and kind == "pip" and b:
                src, dst = VENVS / name / "bin" / b, BIN / b
                if action == "remove":
                    shutil.rmtree(VENVS / name, ignore_errors=True)
                    dst.unlink(missing_ok=True)
                elif src.exists():
                    if dst.exists() or dst.is_symlink():
                        dst.unlink()
                    dst.symlink_to(src)
            if ok and action != "remove" and kind != "none" and not binary_path(t):
                ok, err = False, f"installed, but `{b}` was not found in {BIN} or {NPM_PREFIX / 'bin'}"
        except Exception as e:  # a failed install must end the job, not leave it "running"
            ok, err = False, str(e)
        with _lock:
            _jobs[name].update(state="done" if ok else "failed", error=err, log="\n".join(log)[-20000:], finished=time.time())
            if ok and action != "remove":
                d = _data()
                for x in d["tools"]:
                    if x["name"] == name:
                        x["installed_at"] = now()
                _write(d)
        if on_done:
            try:
                on_done(name, ok)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name=f"tool-install-{name}").start()
    return job(name)


def wait_installed(name: str, timeout: float = 600) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        j = job(name)
        if not j or j["state"] != "running":
            return bool(get(name) and installed(get(name)))
        time.sleep(1)
    return False


# ============================================================================ scope
def _repo_id(repo) -> str:
    try:
        return str(Path(repo).expanduser().resolve()) if repo else ""
    except OSError:
        return str(repo or "")


def repo_scope(repo) -> dict:
    with _lock:
        s = _data()["repos"].get(_repo_id(repo)) or {}
    return {"enable": list(s.get("enable") or []), "disable": list(s.get("disable") or []), "dev": bool(s.get("dev"))}


def set_repo_scope(repo, enable=None, disable=None, dev=None) -> dict:
    rid = _repo_id(repo)
    if not rid:
        raise ValueError("repo is required")
    with _lock:
        d = _data()
        known = {t["name"] for t in d["tools"]}
        s = d["repos"].setdefault(rid, {})
        if enable is not None:
            s["enable"] = [n for n in dict.fromkeys(enable) if n in known]
        if disable is not None:
            s["disable"] = [n for n in dict.fromkeys(disable) if n in known]
        if dev is not None:
            s["dev"] = bool(dev)
        _write(d)
    return repo_scope(repo)


def approved_for_task(tid: str) -> list[str]:
    with _lock:
        return [r["tool"] for r in _data()["requests"] if r.get("task") == tid and r.get("status") == "approved" and r.get("tool")]


def default_names(repo) -> list[str]:
    scope = repo_scope(repo)
    names = [t["name"] for t in load_all() if t.get("enabled")]
    names += [n for n in scope["enable"] if n not in names]
    return [n for n in names if n not in scope["disable"]]


def names_for_task(task: dict) -> list[str]:
    known = {t["name"] for t in load_all()}
    sel = task.get("tools")
    names = [n for n in sel if n in known] if isinstance(sel, list) else default_names(task.get("repo"))
    names += [n for n in approved_for_task(task.get("id") or "") if n not in names and n in known]
    return names


def tools_for_task(task: dict) -> list[dict]:
    names = names_for_task(task)
    by = {t["name"]: t for t in load_all()}
    return [by[n] for n in names if n in by]


# ============================================================================ per-CLI translation
def _placeholders(text: str, ctx: dict) -> str:
    return (str(text).replace("{worktree}", str(ctx.get("worktree") or "."))
            .replace("{run_dir}", str(ctx.get("run_dir") or ".")).replace("{task_id}", str(ctx.get("task_id") or "")))


def _var(tool: str, name: str) -> str:
    return re.sub(r"[^A-Z0-9_]", "_", f"RELAY_TOOL_{tool}_{name}".upper())


def _servers(tools: list[dict], ctx: dict) -> tuple[list[dict], dict]:
    """Resolved servers and the secret variables to export: [{name, transport, command, args, env, secret_env, url, headers}]."""
    out, env = [], {}
    for t in tools:
        if t.get("kind") != "mcp" or not installed(t):
            continue
        s = {"name": t["name"], "transport": t.get("transport", "stdio"), "description": t.get("description", "")}
        if s["transport"] == "stdio":
            s["command"] = binary_path(t) or _placeholders(t.get("command") or t.get("binary") or "", ctx)
            s["args"] = [_placeholders(a, ctx) for a in t.get("args") or []]
            s["env"], s["secret_env"] = {}, []
            for r in t.get("env") or []:
                if r.get("secret"):
                    if not r.get("value"):
                        continue
                    env[r["name"]] = r["value"]
                    s["secret_env"].append(r["name"])
                else:
                    s["env"][r["name"]] = _placeholders(r.get("value") or "", ctx)
        else:
            s["url"] = t["url"]
            s["headers"], s["secret_headers"] = {}, {}
            for r in t.get("headers") or []:
                if r.get("secret"):
                    if not r.get("value"):
                        continue
                    var = _var(t["name"], r["name"])
                    env[var] = r["value"]
                    s["secret_headers"][r["name"]] = var
                else:
                    s["headers"][r["name"]] = r.get("value") or ""
        out.append(s)
    return out, env


def _toml(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _toml_table(d: dict) -> str:
    return "{" + ", ".join(f"{_toml(k)} = {_toml(v)}" for k, v in d.items()) + "}"


def _json_servers(servers, style: str) -> dict:
    """mcpServers objects in the dialects of Claude (claude), Gemini/Qwen (gemini), Copilot (copilot), Amp (amp)."""
    ref = "${%s}"
    out = {}
    for s in servers:
        if s["transport"] == "stdio":
            env = {**s["env"], **{k: ref % k for k in s["secret_env"]}}
            row = {"command": s["command"], "args": s["args"]}
            if env:
                row["env"] = env
            if style == "claude":
                row = {"type": "stdio", **row}
            elif style == "copilot":
                row = {"type": "local", **row, "tools": ["*"]}
            elif style == "gemini":
                row["timeout"] = 120000
        else:
            headers = {**s["headers"], **{h: ref % v for h, v in s["secret_headers"].items()}}
            if style == "gemini":
                row = {"httpUrl": s["url"]}
            elif style == "claude":
                row = {"type": "http", "url": s["url"]}
            elif style == "copilot":
                row = {"type": "http", "url": s["url"], "tools": ["*"]}
            else:
                row = {"url": s["url"]}
            if headers:
                row["headers"] = headers
        out[s["name"]] = row
    return out


def _write_run_file(ctx: dict, name: str, data) -> Path:
    d = Path(ctx["run_dir"]) / "tools"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def mcp_for_turn(agent: str, tools: list[dict], ctx: dict, cfg: dict | None = None, base_env: dict | None = None) -> dict:
    """What one agent turn needs to see these MCP servers.

    Returns {"args": [...], "env": {...}, "servers": [names], "unsupported": [names], "files": [paths]}.
    ctx: {"run_dir", "worktree", "task_id", "role"}. Files go to <run_dir>/tools/, never to a CLI's home folder.
    """
    cfg = cfg or {}
    servers, secret_env = _servers(tools, ctx)
    res = {"args": [], "env": dict(secret_env), "servers": [s["name"] for s in servers], "unsupported": [], "files": []}
    if not servers:
        res["env"] = {}
        return res
    role = ctx.get("role") or "agent"
    if agent == "claude":
        p = _write_run_file(ctx, f"mcp-{role}-claude.json", {"mcpServers": _json_servers(servers, "claude")})
        res["files"].append(str(p))
        res["args"] += ["--mcp-config", str(p)]
        if cfg.get("tools_isolate_user_mcp"):
            res["args"].append("--strict-mcp-config")
        if cfg.get("claude_permission", "bypass") == "acceptEdits":
            res["args"] += ["--allowedTools", " ".join(f"mcp__{s['name']}" for s in servers)]
    elif agent == "codex":
        for s in servers:
            key = f"mcp_servers.{s['name']}"
            if s["transport"] == "stdio":
                res["args"] += ["-c", f"{key}.command={_toml(s['command'])}", "-c", f"{key}.args={_toml(s['args'])}",
                                "-c", f"{key}.startup_timeout_sec=60"]
                if s["env"]:
                    res["args"] += ["-c", f"{key}.env={_toml_table(s['env'])}"]
                if s["secret_env"]:
                    res["args"] += ["-c", f"{key}.env_vars={_toml(s['secret_env'])}"]
            else:
                res["args"] += ["-c", f"{key}.url={_toml(s['url'])}"]
                if s["headers"]:
                    res["args"] += ["-c", f"{key}.http_headers={_toml_table(s['headers'])}"]
                env_headers = dict(s["secret_headers"])
                auth = next((h for h in env_headers if h.lower() == "authorization"), None)
                if auth and str(secret_env.get(env_headers[auth], "")).lower().startswith("bearer "):
                    var = env_headers.pop(auth)
                    res["env"][var] = secret_env[var].split(" ", 1)[1]
                    res["args"] += ["-c", f"{key}.bearer_token_env_var={_toml(var)}"]
                if env_headers:
                    res["args"] += ["-c", f"{key}.env_http_headers={_toml_table(env_headers)}"]
    elif agent == "gemini":
        data = {}
        real = Path((base_env or {}).get("GEMINI_CLI_SYSTEM_SETTINGS_PATH") or "/etc/gemini-cli/settings.json")
        try:
            data = json.loads(real.read_text(encoding="utf-8")) if real.is_file() else {}
        except (OSError, ValueError):
            data = {}
        data["mcpServers"] = {**(data.get("mcpServers") or {}), **_json_servers(servers, "gemini")}
        p = _write_run_file(ctx, f"gemini-{role}-settings.json", data)
        res["files"].append(str(p))
        res["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(p)
    elif agent == "qwen":
        p = _write_run_file(ctx, f"mcp-{role}-qwen.json", {"mcpServers": _json_servers(servers, "gemini")})
        res["files"].append(str(p))
        res["args"] += ["--mcp-config", str(p)]
    elif agent == "copilot":
        p = _write_run_file(ctx, f"mcp-{role}-copilot.json", {"mcpServers": _json_servers(servers, "copilot")})
        res["files"].append(str(p))
        res["args"].append(f"--additional-mcp-config=@{p}")
        if secret_env:
            res["args"].append("--secret-env-vars=" + ",".join(sorted(secret_env)))
    elif agent == "amp":
        p = _write_run_file(ctx, f"mcp-{role}-amp.json", _json_servers(servers, "amp"))
        res["files"].append(str(p))
        res["args"] += ["--mcp-config", str(p)]
    elif agent in ("opencode", "kilo"):
        var = "KILO_CONFIG_CONTENT" if agent == "kilo" else "OPENCODE_CONFIG_CONTENT"
        try:
            content = json.loads((base_env or {}).get(var) or "{}")
        except ValueError:
            content = {}
        mcp = dict(content.get("mcp") or {})
        for s in servers:
            if s["transport"] == "stdio":
                row = {"type": "local", "command": [s["command"], *s["args"]], "enabled": True}
                env = {**s["env"], **{k: "{env:%s}" % k for k in s["secret_env"]}}
                if env:
                    row["environment"] = env
            else:
                row = {"type": "remote", "url": s["url"], "enabled": True}
                headers = {**s["headers"], **{h: "{env:%s}" % v for h, v in s["secret_headers"].items()}}
                if headers:
                    row["headers"] = headers
            mcp[s["name"]] = row
        content["mcp"] = mcp
        res["env"][var] = json.dumps(content, ensure_ascii=False)
    elif agent == "goose":
        for s in servers:
            if s["transport"] == "stdio":
                env = " ".join(f"{k}={shlex.quote(v)}" for k, v in s["env"].items())
                res["args"] += ["--with-extension", f"{s['name']}:{(env + ' ') if env else ''}{shlex.join([s['command'], *s['args']])}"]
            elif not s["secret_headers"] and not s["headers"]:
                res["args"] += ["--with-streamable-http-extension", s["url"]]
            else:
                res["unsupported"].append(s["name"])
                res["servers"].remove(s["name"])
    else:
        res["unsupported"] = list(res["servers"])
        res["servers"] = []
        res["env"] = {}
    return res


def inject_args(agent: str, argv: list, extra: list) -> list:
    """Put extra flags right after the CLI binary (after `run` for goose), where every CLI accepts global options."""
    if not extra:
        return argv
    from . import config as C
    binary = (C.AGENTS.get(agent) or {}).get("binary") or agent
    idx = next((i for i, a in enumerate(argv) if Path(str(a)).name.split(".")[0] == binary), 0)
    at = idx + 1
    if agent == "goose" and at < len(argv) and argv[at] == "run":
        at += 1
    return [*argv[:at], *extra, *argv[at:]]


# ============================================================================ prompts
def describe(tools: list[dict], agent: str, turn: dict | None = None, policy: str = "ask") -> str:
    """The short block agents read about their tools. Empty when there is nothing to say."""
    turn = turn or {}
    served = set(turn.get("servers") or [])
    lines = []
    mcp_ok = [t for t in tools if t.get("kind") == "mcp" and t["name"] in served]
    mcp_no = [t for t in tools if t.get("kind") == "mcp" and t["name"] not in served]
    clis = [t for t in tools if t.get("kind") == "cli" and installed(t)]
    if mcp_ok:
        lines.append("  MCP servers loaded as native tools this turn: " + "; ".join(
            f"`{t['name']}` ({truncate(t.get('description') or t.get('label') or '', 90)})" for t in mcp_ok))
    if mcp_no:
        lines.append("  Not loadable in this CLI (use the equivalent instead): " + "; ".join(
            f"`{t['name']}` → {t.get('fallback') or 'the shell'}" for t in mcp_no))
    if clis:
        lines.append("  Command-line tools on PATH: " + "; ".join(
            f"`{t['binary']}`" + (f" {truncate(t.get('usage') or t.get('description') or '', 140)}" if (t.get("usage") or t.get("description")) else "") for t in clis))
    if policy != "off":
        lines.append("  Missing a tool that would clearly save work? `relay-tools catalog`, then `relay-tools request <name> \"<why>\"` "
                     "(or a `tool_request` field in your envelope). It is usable from your next turn once approved; keep working meanwhile.")
    if not lines:
        return ""
    return "TOOLS\n" + "\n".join(lines)


# ============================================================================ agent requests
def _clean_request(spec: dict) -> dict:
    spec = spec if isinstance(spec, dict) else {"name": str(spec)}
    name = re.sub(r"[^a-z0-9_-]", "-", str(spec.get("name") or spec.get("catalog") or "").strip().lower())[:40].strip("-")
    out = {"name": name, "why": truncate(str(spec.get("why") or spec.get("reason") or ""), 500)}
    for k in ("catalog", "npm", "pip", "command", "url", "usage", "binary", "description"):
        if spec.get(k):
            out[k] = truncate(str(spec[k]), 500)
    if isinstance(spec.get("args"), list):
        out["args"] = [str(a)[:300] for a in spec["args"][:20]]
    return out


def request_to_tool(req: dict) -> dict:
    """The registry entry an approved request becomes."""
    cid = req.get("catalog") or (req["name"] if req["name"] in CATALOG_BY_ID else "")
    if cid in CATALOG_BY_ID:
        spec = from_catalog(cid)
        spec["name"] = req["name"] if _NAME.match(req["name"] or "") else cid
        return {**spec, "source": "agent", "created_by": f"{req.get('role')}/{req.get('agent')}"}
    install = {"kind": "npm", "package": req["npm"]} if req.get("npm") else {"kind": "pip", "package": req["pip"]} if req.get("pip") else {"kind": "none"}
    base = {"name": req["name"], "label": req["name"], "description": req.get("description") or req.get("why", ""), "install": install,
            "source": "agent", "created_by": f"{req.get('role')}/{req.get('agent')}", "enabled": False}
    if req.get("url"):
        return {**base, "kind": "mcp", "transport": "http", "url": req["url"], "headers": []}
    if req.get("command") and not req.get("usage"):
        parts = shlex.split(req["command"])
        return {**base, "kind": "mcp", "transport": "stdio", "command": parts[0], "args": parts[1:] + list(req.get("args") or []),
                "binary": req.get("binary") or (parts[0] if install["kind"] != "none" else "")}
    return {**base, "kind": "cli", "binary": req.get("binary") or req["name"], "usage": req.get("usage") or ""}


def requests(status: str | None = None, task: str | None = None) -> list[dict]:
    with _lock:
        rows = _data()["requests"]
    return [r for r in rows if (not status or r.get("status") == status) and (not task or r.get("task") == task)]


def get_request(rid: str) -> dict | None:
    return next((r for r in requests() if r["id"] == rid), None)


def _update_request(rid: str, **patch) -> dict:
    with _lock:
        d = _data()
        for r in d["requests"]:
            if r["id"] == rid:
                r.update(patch)
                _write(d)
                return dict(r)
    raise ValueError("No such tool request")


def request(task: dict, role: str, agent: str, spec: dict, policy: str = "ask", dev_project: bool | None = None) -> dict:
    """An agent asks for a tool. Returns the request row; its status says what happened (pending, approved, available, refused)."""
    req = _clean_request(spec)
    tid = task.get("id") or ""
    if not req["name"]:
        return {"status": "refused", "error": "a tool name is required"}
    if policy == "off":
        return {"status": "refused", "error": "tool requests are turned off in Relay's settings"}
    if req["name"] in names_for_task(task) and get(req["name"]):
        return {"status": "available", "tool": req["name"], "note": "already available to this task"}
    with _lock:
        d = _data()
        dup = next((r for r in d["requests"] if r.get("task") == tid and r.get("name") == req["name"] and r.get("status") == "pending"), None)
        if dup:
            return dict(dup)
        row = {"id": "tr_" + _secrets.token_hex(5), "task": tid, "task_name": task.get("name"), "repo": task.get("repo"),
               "role": role, "agent": agent, **req, "status": "pending", "time": now(),
               "catalog_match": req.get("catalog") in CATALOG_BY_ID or req["name"] in CATALOG_BY_ID}
        try:
            row["proposal"] = public(normalize(request_to_tool(row)))
        except ValueError as e:
            row["proposal_error"] = str(e)
        d["requests"] = (d["requests"] + [row])[-500:]
        _write(d)
    dev = repo_scope(task.get("repo"))["dev"] if dev_project is None else dev_project
    auto = (policy == "auto_catalog" and row["catalog_match"]) or (policy == "auto_dev" and (row["catalog_match"] or dev))
    if auto and not row.get("proposal_error"):
        return approve(row["id"], by="policy")
    return row


def approve(rid: str, by: str = "owner", edits: dict | None = None, scope: str = "task") -> dict:
    """Approve a request: add the tool (or reuse one with that name), install it, and give it to the task (or wider)."""
    r = get_request(rid)
    if not r:
        raise ValueError("No such tool request")
    if r["status"] not in ("pending",):
        return r
    existing = get(r["name"])
    tool = existing or save({**request_to_tool(r), **(edits or {})})
    if scope == "global":
        save({**tool, "enabled": True})
    elif scope == "repo" and r.get("repo"):
        s = repo_scope(r["repo"])
        set_repo_scope(r["repo"], enable=s["enable"] + [tool["name"]])
    row = _update_request(rid, status="approved", tool=tool["name"], decided_by=by, decided=now(), scope=scope)
    if not installed(tool) and (tool.get("install") or {}).get("kind") != "none":
        install(tool["name"])
        row["installing"] = True
    return row


def deny(rid: str, note: str = "", by: str = "owner") -> dict:
    return _update_request(rid, status="denied", note=truncate(note, 300), decided_by=by, decided=now())


def inbox_items() -> list[dict]:
    """Pending requests as Needs-you items (orchestrator/autopilot.py inbox shape)."""
    out = []
    for r in requests("pending"):
        what = (f"catalog tool `{r.get('catalog') or r['name']}`" if r.get("catalog_match") else
                f"MCP server `{r['name']}` ({r.get('url') or r.get('command') or ''})" if (r.get("url") or r.get("command")) else
                f"command-line tool `{r['name']}`" + (f" (npm {r['npm']})" if r.get("npm") else f" (pip {r['pip']})" if r.get("pip") else ""))
        out.append({"id": r["id"], "kind": "tool_request", "task_id": r.get("task"), "task": r.get("task_name"),
                    "repo": Path(r.get("repo") or "").name, "from": r.get("role"), "agent": r.get("agent"), "time": r.get("time"),
                    "question": f"Add {what} to this task?\n\nWhy: {r.get('why') or '(no reason given)'}", "options": [],
                    "tool": r["name"], "catalog_match": r.get("catalog_match"), "proposal": r.get("proposal")})
    return out


# ============================================================================ usage
def classify(tool_name: str, category: str, summary: str, tools: list[dict]) -> tuple[str, str] | None:
    """(registry or builtin name, pretty label) for a tool call, or None when it is not a Relay tool."""
    t = str(tool_name or "")
    low = t.lower()
    if category == "mcp" or low.startswith("mcp") or "." in t or "__" in t:
        for tool in tools:
            if tool.get("kind") != "mcp":
                continue
            n = tool["name"].lower()
            for prefix in (f"mcp__{n}__", f"mcp.{n}.", f"{n}.", f"{n}__", f"mcp_{n}_", f"{n}_"):
                if low.startswith(prefix):
                    return tool["name"], f"{tool['name']} · {t[len(prefix):]}"
    if category == "shell" and summary:
        clis = {tool["binary"]: tool["name"] for tool in tools if tool.get("kind") == "cli" and tool.get("binary")}
        known = {b for b, _ in BUILTINS if b.startswith("relay-")}  # Relay's own tools; git, rg and friends are not worth counting
        for seg in re.split(r"&&|\|\||;|\|", str(summary)):
            words = [w for w in seg.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
            if words and words[0] in ("sudo", "env", "time", "npx", "exec") and len(words) > 1:
                words = words[1:]
            head = Path(words[0]).name if words else ""
            if head in clis:
                return clis[head], clis[head]
            if head in known:
                return head, head
    return None


def record_use(name: str, tid: str, role: str, agent: str, label: str = "") -> None:
    with _lock:
        d = read_json(STATS, {})
        if not isinstance(d, dict):
            d = {}
        s = d.setdefault(name, {"calls": 0, "tasks": [], "agents": {}, "roles": {}, "tools": {}})
        s["calls"] = int(s.get("calls") or 0) + 1
        s["last"] = now()
        if tid and tid not in s["tasks"]:
            s["tasks"] = (s["tasks"] + [tid])[-50:]
        for key, val in (("agents", agent), ("roles", role), ("tools", label.split(" · ", 1)[-1] if " · " in label else "")):
            if val:
                s[key][val] = int(s[key].get(val) or 0) + 1
        write_json(STATS, d)


def stats() -> dict:
    d = read_json(STATS, {})
    return d if isinstance(d, dict) else {}


def builtins() -> list[dict]:
    rows = []
    for b, desc in BUILTINS:
        path = shutil.which(b) or (str(APP_DIR / "tools" / "bin" / b) if (APP_DIR / "tools" / "bin" / b).exists() else "")
        rows.append({"name": b, "kind": "builtin", "description": desc, "available": bool(path), "path": path})
    return rows


def catalog() -> list[dict]:
    names = {t.get("catalog_id") or t["name"] for t in load_all()}
    return [{**{k: v for k, v in c.items()}, "added": c["id"] in names} for c in CATALOG]


# ============================================================================ turn hook (runner.py)
def prepare_turn(task: dict, agent: str, argv: list, env: dict, cfg: dict, run_dir, role: str, cwd) -> dict:
    """Add the task's tools to one agent turn: PATH, MCP config for this CLI, secrets in the environment.

    Returns {"argv", "servers", "unsupported", "tools", "mask"}; env is changed in place.
    """
    info = {"argv": argv, "servers": [], "unsupported": [], "tools": [], "mask": None}
    try:
        tools = tools_for_task(task or {})
    except Exception:
        tools = []
    if env is not None:
        extra = [str(p) for p in (BIN, NPM_PREFIX / "bin") if p.is_dir()]
        if extra:
            env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    info["tools"] = tools
    if not tools or env is None:
        return info
    ctx = {"run_dir": str(run_dir), "worktree": str(cwd), "task_id": (task or {}).get("id"), "role": role}
    plan = mcp_for_turn(agent, tools, ctx, cfg, env)
    env.update(plan["env"])
    info.update(argv=inject_args(agent, argv, plan["args"]), servers=plan["servers"], unsupported=plan["unsupported"],
                mask=masker(tools) if secret_values(tools) else None)
    return info


# ============================================================================ probe
def _rpc(method: str, rid: int | None, params: dict | None = None) -> dict:
    msg = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        msg["id"] = rid
    if params is not None:
        msg["params"] = params
    return msg


_INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "relay-toolbox", "version": "1"}}


def probe(name: str, timeout: float = 45, ctx: dict | None = None) -> dict:
    """Start the server, run initialize + tools/list, and report the tools it offers."""
    t = get(name)
    if not t:
        raise ValueError(f"No tool named {name}")
    if t.get("kind") != "mcp":
        path = binary_path(t) or shutil.which(t.get("binary") or "")
        return {"ok": bool(path), "summary": f"`{t.get('binary')}` {'found at ' + path if path else 'not found'}", "tools": []}
    if not installed(t):
        return {"ok": False, "summary": "Not installed yet", "tools": []}
    if not ctx:
        # A scratch git repository stands in for the worktree (servers such as git refuse anything else).
        probe_dir = DATA_DIR / "runtime" / "_toolprobe"
        repo = probe_dir / "repo"
        if not (repo / ".git").exists():
            repo.mkdir(parents=True, exist_ok=True)
            subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True, timeout=30)
            subprocess.run(["git", "-c", "user.name=relay", "-c", "user.email=relay@localhost", "commit", "-q", "--allow-empty", "-m", "probe"],
                           cwd=repo, capture_output=True, timeout=30)
        ctx = {"run_dir": str(probe_dir), "worktree": str(repo), "task_id": "probe"}
    Path(ctx["run_dir"], "tools").mkdir(parents=True, exist_ok=True)
    servers, secret_env = _servers([t], ctx)
    if not servers:
        return {"ok": False, "summary": "Missing a required secret", "tools": []}
    s = servers[0]
    mask = masker([t])
    started = time.time()
    try:
        if s["transport"] == "http":
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **s["headers"],
                       **{h: secret_env[v] for h, v in s["secret_headers"].items()}}

            def post(body, session=None):
                h = dict(headers)
                if session:
                    h["Mcp-Session-Id"] = session
                req = urllib.request.Request(s["url"], data=json.dumps(body).encode(), headers=h, method="POST")
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = r.read().decode("utf-8", "replace")
                    sid = r.headers.get("Mcp-Session-Id")
                datas = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")] or [raw]
                for x in datas:
                    try:
                        return json.loads(x), sid
                    except ValueError:
                        continue
                return {}, sid
            init, sid = post(_rpc("initialize", 1, _INIT))
            if "error" in init:
                raise RuntimeError(json.dumps(init["error"]))
            try:
                post(_rpc("notifications/initialized", None), sid)
            except urllib.error.HTTPError:
                pass
            listed, _ = post(_rpc("tools/list", 2, {}), sid)
        else:
            env = os.environ.copy()
            env.update(s["env"])
            env.update({k: secret_env[k] for k in s["secret_env"]})
            p = subprocess.Popen([s["command"], *s["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=env, cwd=ctx["worktree"], text=True, bufsize=1)
            try:
                p.stdin.write(json.dumps(_rpc("initialize", 1, _INIT)) + "\n")
                p.stdin.flush()
                listed, init = {}, None
                deadline = time.time() + timeout
                while time.time() < deadline:
                    ready, _, _ = select.select([p.stdout], [], [], 0.5)
                    if not ready:
                        if p.poll() is not None:
                            raise RuntimeError("exited: " + truncate(p.stderr.read() or "", 600))
                        continue
                    line = p.stdout.readline()
                    if not line:
                        raise RuntimeError("closed its output: " + truncate(p.stderr.read() or "", 600))
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("id") == 1:
                        init = msg
                        p.stdin.write(json.dumps(_rpc("notifications/initialized", None)) + "\n")
                        p.stdin.write(json.dumps(_rpc("tools/list", 2, {})) + "\n")
                        p.stdin.flush()
                    elif msg.get("id") == 2:
                        listed = msg
                        break
                if init is None:
                    raise RuntimeError(f"no answer to initialize within {int(timeout)} s")
            finally:
                p.kill()
        names = [x.get("name") for x in ((listed or {}).get("result") or {}).get("tools") or []]
        server = ((init or {}).get("result") or {}).get("serverInfo") or {}
        return {"ok": bool(names), "tools": names, "server": server, "duration": round(time.time() - started, 2),
                "summary": f"{len(names)} tools" + (f" from {server.get('name')} {server.get('version') or ''}".rstrip() if server else "")}
    except Exception as e:
        return {"ok": False, "tools": [], "duration": round(time.time() - started, 2), "summary": mask(truncate(str(e), 500))}
