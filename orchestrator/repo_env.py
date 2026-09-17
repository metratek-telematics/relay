"""Per-repository environments: variables, secret files, services and the checks that prove the work.

Many projects cannot run their tests without configuration that is never committed: a `.env` with
database credentials, an API key, a cookies file. People keep that on their own machine; Relay keeps
it here, per repository, under its data folder, and hands it to every task in that repository:

  * variables   exported to agents, setup, services and verification, and (optionally) written to
                the worktree's `.env` so apps that load dotenv files see them too
  * files       written into the worktree at their relative path and excluded from git
  * system_packages  apt packages Relay installs (once, cached) before setup, e.g. unixodbc for pyodbc
  * setup       replaces the detected dependency install when set
  * services    commands started before the team works (a database, a mock server) and stopped after
  * checks      the commands that prove the work; required ones block delivery, optional ones inform

Secret values never reach the browser after they are saved (the API returns them masked) and are
replaced with ●●●● in logs, messages and artifacts.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .util import DATA_DIR, quiet, read_json, write_json

ENV_DIR = DATA_DIR / "repo_env"
MASK = "●●●●"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def repo_key(repo) -> str:
    """Stable key for a repository: its GitHub name when it has one, else its absolute path."""
    repo = Path(repo).resolve()
    url = (quiet(["git", "remote", "get-url", "origin"], cwd=repo, timeout=15).stdout or "").strip() if repo.exists() else ""
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    ident = m.group(1).lower() if m else str(repo)
    return hashlib.sha1(ident.encode()).hexdigest()[:16]


def source_repo(path) -> Path | None:
    """The repository a worktree belongs to (or the repository itself)."""
    try:
        common = (quiet(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path, timeout=15).stdout or "").strip()
    except Exception:
        return None
    if not common:
        return None
    p = Path(common)
    return p.parent if p.name == ".git" else p


def _path(repo) -> Path:
    return ENV_DIR / f"{repo_key(repo)}.json"


def empty() -> dict:
    return {"vars": [], "files": [], "write_dotenv": True, "setup": "", "services_up": "", "services_down": "", "checks": [],
            "system_packages": []}


def load(repo) -> dict:
    data = read_json(_path(repo), None) or {}
    out = empty()
    out.update({k: v for k, v in data.items() if k in out or k in ("repo", "updated")})
    return out


def _clean_rel(rel: str) -> str:
    rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or parts[0] == ".git":
        raise ValueError(f"Invalid file path: {rel!r}")
    return "/".join(parts)


def save(repo, incoming: dict) -> dict:
    """Store what the browser sent. A secret sent back as the mask keeps its stored value."""
    cur = load(repo)
    old_vars = {v["name"]: v for v in cur["vars"]}
    old_files = {f["path"]: f for f in cur["files"]}
    out = empty()
    seen = set()
    for v in incoming.get("vars") or []:
        name = str(v.get("name") or "").strip()
        if not name:
            continue
        if not _NAME.match(name):
            raise ValueError(f"Invalid variable name: {name!r}")
        if name in seen:
            raise ValueError(f"Variable {name} is listed twice")
        seen.add(name)
        value = v.get("value")
        if value == MASK and name in old_vars:
            value = old_vars[name]["value"]
        out["vars"].append({"name": name, "value": "" if value is None else str(value), "secret": bool(v.get("secret"))})
    for f in incoming.get("files") or []:
        if not str(f.get("path") or "").strip():
            continue
        rel = _clean_rel(f["path"])
        content = f.get("content")
        if content == MASK and rel in old_files:
            content = old_files[rel]["content"]
        out["files"].append({"path": rel, "content": str(content or ""), "secret": bool(f.get("secret", True))})
    out["write_dotenv"] = bool(incoming.get("write_dotenv", True))
    for k in ("setup", "services_up", "services_down"):
        out[k] = str(incoming.get(k) or "").strip()
    from . import syspkgs
    out["system_packages"] = syspkgs.parse(incoming.get("system_packages"))  # raises ValueError on a bad name
    for c in incoming.get("checks") or []:
        cmd = str(c.get("command") or "").strip()
        if cmd:
            out["checks"].append({"command": cmd, "required": bool(c.get("required", True)), "name": str(c.get("name") or "").strip()[:80]})
    from .util import now
    out["repo"] = str(Path(repo).resolve())
    out["updated"] = now()
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    write_json(_path(repo), out)
    try:
        _path(repo).chmod(0o600)
    except OSError:
        pass
    return out


def public(data: dict) -> dict:
    """What the browser may see: secret values masked, with a flag saying whether one is set."""
    pub = dict(data)
    pub["vars"] = [{**v, "value": MASK if v["secret"] and v["value"] else v["value"], "has_value": bool(v["value"])} for v in data["vars"]]
    pub["files"] = [{**f, "content": MASK if f.get("secret") and f["content"] else f["content"], "has_value": bool(f["content"])} for f in data["files"]]
    return pub


def parse_dotenv(text: str) -> list[dict]:
    """KEY=value lines from a pasted .env; names that look secret are marked secret."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name, value = name.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if _NAME.match(name):
            rows.append({"name": name, "value": value, "secret": looks_secret(name)})
    return rows


def looks_secret(name: str) -> bool:
    return bool(re.search(r"PASS|SECRET|TOKEN|KEY|PWD|CREDENTIAL|AUTH|COOKIE|TOTP|PRIVATE", name, re.I))


# ----------------------------------------------------------------------------- applying to a task
def env_vars(data: dict) -> dict:
    return {v["name"]: v["value"] for v in data["vars"]}


def for_path(path) -> dict:
    """The environment for whatever repository `path` (a worktree or repository) belongs to."""
    repo = source_repo(path)
    return load(repo) if repo else empty()


def secrets(data: dict) -> list[str]:
    vals = [v["value"] for v in data["vars"] if v["secret"] and len(v["value"]) >= 4]
    for f in data["files"]:
        if f.get("secret"):
            vals += [ln.split("=", 1)[1].strip().strip("\"'") for ln in f["content"].splitlines()
                     if "=" in ln and len(ln.split("=", 1)[1].strip().strip("\"'")) >= 6]
            # JSON/YAML style secrets: "key": "value" / key: value
            vals += [m.group(1) for m in re.finditer(r"[:=]\s*\"([^\"\s]{8,})\"", f["content"])]
    return sorted(set(vals), key=len, reverse=True)


def masker(data: dict):
    vals = secrets(data)
    if not vals:
        return lambda s: s
    rx = re.compile("|".join(re.escape(v) for v in vals))
    return lambda s: rx.sub(MASK, s) if isinstance(s, str) else s


def _exclude(wt: Path, patterns: list[str]) -> None:
    common = source_repo(wt)
    if not common:
        return
    info = common / ".git" / "info" if (common / ".git").is_dir() else common / "info"
    info.mkdir(parents=True, exist_ok=True)
    ex = info / "exclude"
    cur = ex.read_text(encoding="utf-8") if ex.exists() else ""
    lines = cur.splitlines()
    add = [f"/{p}" for p in patterns if f"/{p}" not in lines]
    if add:
        ex.write_text(cur + ("" if not cur or cur.endswith("\n") else "\n") + "\n".join(add) + "\n", encoding="utf-8")


def _tracked(wt: Path, rel: str) -> bool:
    return quiet(["git", "ls-files", "--error-unmatch", rel], cwd=wt, timeout=15).returncode == 0


def apply_to_worktree(wt, data: dict) -> list[str]:
    """Write the repository's files (and its .env) into a task worktree. Tracked files are never overwritten."""
    wt = Path(wt)
    written = []
    for f in data["files"]:
        rel = _clean_rel(f["path"])
        if _tracked(wt, rel):
            continue
        dest = wt / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f["content"], encoding="utf-8")
        written.append(rel)
    if data.get("write_dotenv", True) and data["vars"] and ".env" not in written and not _tracked(wt, ".env"):
        dest = wt / ".env"
        existing = dest.read_text(encoding="utf-8", errors="replace") if dest.exists() else ""
        # Keep lines the repository's own local .env had for names Relay does not set.
        ours = env_vars(data)
        kept = [ln for ln in existing.splitlines() if not re.match(rf"^\s*(?:export\s+)?({'|'.join(map(re.escape, ours))})\s*=", ln)]
        body = "\n".join(kept + [f"{k}={_quote(v)}" for k, v in ours.items()]).strip() + "\n"
        dest.write_text(body, encoding="utf-8")
        written.append(".env")
    if written:
        _exclude(wt, written)
    return written


def _quote(v: str) -> str:
    return f'"{v}"' if re.search(r"[\s#\"'$`\\]", v) else v


def describe(data: dict) -> str:
    """Names only, for agent prompts: agents learn what exists without ever seeing a value."""
    lines = []
    if data["vars"]:
        lines.append("- Environment variables set for this repository (already exported, and in `.env`): "
                     + ", ".join(f"`{v['name']}`" for v in data["vars"])
                     + ". Use them; never print their values, hard-code them or commit them.")
    if data["files"]:
        lines.append("- Local config files Relay placed in the worktree (git-ignored, never commit them): "
                     + ", ".join(f"`{f['path']}`" for f in data["files"]))
    if data.get("system_packages"):
        lines.append("- System packages Relay installed for this repository: " + ", ".join(f"`{p}`" for p in data["system_packages"])
                     + ". If another one is missing, record it as a blocked check naming the package; never run apt-get yourself.")
    if data.get("services_up"):
        lines.append(f"- Services Relay started for this task: `{data['services_up']}`.")
    if data["checks"]:
        lines.append("- The checks that prove the work (Relay runs them itself): "
                     + "; ".join(f"`{c['command']}`{'' if c['required'] else ' (optional)'}" for c in data["checks"]))
    return "\n".join(lines)
