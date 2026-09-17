"""Connectors: named, typed windows onto the real environments behind the code.

Agents only see a repository. A connector lets them look at (and, when allowed, change) the service
the code talks to: an HTTP API, a PostgreSQL database, container logs, container state, or the running
web app in a headless browser. Definitions and their credentials live in DATA_DIR/connectors.json.

The agent never receives a credential. For a task Relay issues a short-lived token scoped to the
connectors that task may use and exports RELAY_CONNECT_URL + RELAY_CONNECT_TOKEN; the `relay-connect`
CLI sends each call to /api/connect/<name>/<op>, Relay performs it server side, enforces the access
rules, masks secrets, truncates output and records the call in the task.

Access rules
  * access "read" (default): only safe operations (GET/HEAD, read-only SQL, logs, status, navigation).
  * access "write": also unsafe operations, still limited by the connector's own allowlists.
  * prod: a write connector must be explicitly write-enabled with a confirmation when saved, and every
    write call must carry confirm_prod. Prod connectors are never in a task's default scope.
"""
from __future__ import annotations

import base64
import fnmatch
import http.client
import json
import os
import posixpath
import re
import secrets as _secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .util import APP_DIR, DATA_DIR, now, read_json, write_json

PATH = DATA_DIR / "connectors.json"
CALLS = DATA_DIR / "connector_calls.jsonl"
MASK = "●●●●"
TYPES = ("http", "postgres", "logs", "docker", "browser")
ENVIRONMENTS = ("dev", "staging", "prod")
BIN_DIR = APP_DIR / "tools" / "bin"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
ALL_METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE")
SECRET_KEYS = {
    "http": ("auth_token", "auth_password", "auth_value"),
    "postgres": ("password",),
    "logs": ("loki_token", "loki_password"),
    "docker": (),
    "browser": ("password",),
}
DEFAULTS = {
    "http": {"base_url": "", "auth_type": "none", "auth_token": "", "auth_user": "", "auth_password": "", "auth_header": "",
             "auth_value": "", "headers": [], "allowed_methods": ["GET", "HEAD"], "path_allowlist": [], "health_path": "/",
             "timeout": 20, "verify_tls": True},
    "postgres": {"host": "", "port": 5432, "database": "", "user": "", "password": "", "sslmode": "prefer",
                 "allowed_schemas": [], "row_limit": 200, "statement_timeout_ms": 15000},
    "logs": {"source": "docker", "containers": [], "loki_url": "", "loki_query": "", "loki_token": "", "loki_user": "",
             "loki_password": "", "max_lines": 500},
    "docker": {"containers": [], "allow_restart": False},
    "browser": {"base_url": "", "username": "", "password": "", "login_steps": "", "width": 1440, "height": 900},
}
MAX_OUTPUT = 16000
_lock = threading.RLock()


class Refused(Exception):
    """The call breaks an access rule. Nothing was sent to the environment."""


class Unavailable(Exception):
    """The connector cannot work here (no Docker socket, no driver, no browser)."""


# ============================================================================ storage
def _data() -> dict:
    d = read_json(PATH, None) or {}
    return {"connectors": list(d.get("connectors") or []), "repo_defaults": dict(d.get("repo_defaults") or {})}


def _write(d: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(PATH, d)
    try:
        PATH.chmod(0o600)
    except OSError:
        pass


def load_all() -> list[dict]:
    return _data()["connectors"]


def get(name: str) -> dict | None:
    return next((c for c in load_all() if c.get("name") == name), None)


def _list(value) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[\n,]", value)
    return [str(x).strip() for x in (value or []) if str(x).strip()]


def _clean_config(ctype: str, incoming: dict, old: dict) -> dict:
    cfg = dict(DEFAULTS[ctype])
    for k in cfg:
        if k in incoming:
            cfg[k] = incoming[k]
    for k in SECRET_KEYS[ctype]:
        v = cfg.get(k)
        cfg[k] = (old.get(k) or "") if v == MASK else ("" if v is None else str(v))
    if ctype == "http":
        cfg["base_url"] = str(cfg["base_url"]).strip().rstrip("/")
        if cfg["base_url"] and not re.match(r"^https?://[^/\s]+", cfg["base_url"]):
            raise ValueError("Base URL must start with http:// or https://")
        if cfg["auth_type"] not in ("none", "bearer", "basic", "header"):
            raise ValueError("Auth must be none, bearer, basic or header")
        methods = [m.upper() for m in _list(cfg["allowed_methods"])] or ["GET", "HEAD"]
        bad = [m for m in methods if m not in ALL_METHODS]
        if bad:
            raise ValueError(f"Unknown HTTP method(s): {', '.join(bad)}")
        cfg["allowed_methods"] = list(dict.fromkeys(methods))
        cfg["path_allowlist"] = _list(cfg["path_allowlist"])
        old_headers = {h.get("name"): h for h in old.get("headers") or []}
        headers = []
        for h in cfg["headers"] or []:
            name = str(h.get("name") or "").strip()
            if not name:
                continue
            if not re.match(r"^[A-Za-z0-9-]+$", name):
                raise ValueError(f"Invalid header name: {name!r}")
            value = h.get("value")
            if value == MASK and name in old_headers:
                value = old_headers[name].get("value")
            headers.append({"name": name, "value": str(value or ""), "secret": bool(h.get("secret"))})
        cfg["headers"] = headers
        cfg["health_path"] = "/" + str(cfg["health_path"] or "/").lstrip("/")
        cfg["timeout"] = max(1, min(120, int(cfg["timeout"] or 20)))
        cfg["verify_tls"] = bool(cfg["verify_tls"])
    elif ctype == "postgres":
        cfg["port"] = int(cfg["port"] or 5432)
        cfg["allowed_schemas"] = _list(cfg["allowed_schemas"])
        cfg["row_limit"] = max(1, min(5000, int(cfg["row_limit"] or 200)))
        cfg["statement_timeout_ms"] = max(100, min(300000, int(cfg["statement_timeout_ms"] or 15000)))
        if cfg["sslmode"] not in ("disable", "allow", "prefer", "require", "verify-ca", "verify-full"):
            raise ValueError("Invalid sslmode")
        for k in ("host", "database", "user"):
            cfg[k] = str(cfg[k] or "").strip()
    elif ctype == "logs":
        if cfg["source"] not in ("docker", "loki"):
            raise ValueError("Log source must be docker or loki")
        cfg["containers"] = _list(cfg["containers"])
        cfg["loki_url"] = str(cfg["loki_url"] or "").strip().rstrip("/")
        cfg["max_lines"] = max(10, min(5000, int(cfg["max_lines"] or 500)))
    elif ctype == "docker":
        cfg["containers"] = _list(cfg["containers"])
        cfg["allow_restart"] = bool(cfg["allow_restart"])
    elif ctype == "browser":
        cfg["base_url"] = str(cfg["base_url"]).strip().rstrip("/")
        if cfg["base_url"] and not re.match(r"^https?://[^/\s]+", cfg["base_url"]):
            raise ValueError("Base URL must start with http:// or https://")
        parse_steps(cfg["login_steps"])  # validates
        cfg["width"] = max(320, min(3840, int(cfg["width"] or 1440)))
        cfg["height"] = max(320, min(3000, int(cfg["height"] or 900)))
    return cfg


def save(incoming: dict) -> dict:
    """Create or replace one connector. A secret sent back as the mask keeps its stored value."""
    name = str(incoming.get("name") or "").strip().lower()
    if not _NAME.match(name):
        raise ValueError("Name: lowercase letters, digits, dot, dash or underscore (for example items-api)")
    ctype = incoming.get("type")
    if ctype not in TYPES:
        raise ValueError(f"Type must be one of {', '.join(TYPES)}")
    env = incoming.get("environment") or "dev"
    if env not in ENVIRONMENTS:
        raise ValueError("Environment must be dev, staging or prod")
    access = incoming.get("access") or "read"
    if access not in ("read", "write"):
        raise ValueError("Access must be read or write")
    original = str(incoming.get("original_name") or name).strip().lower()
    with _lock:
        d = _data()
        old = next((c for c in d["connectors"] if c.get("name") == original), None) or {}
        if name != original or not old:
            if any(c.get("name") == name for c in d["connectors"]):
                raise ValueError(f"A connector named {name} already exists")
        old_cfg = old.get("config") or {} if old.get("type") == ctype else {}
        prod_write = False
        if env == "prod" and access == "write":
            # Writing to production is never a default: it takes an explicit switch plus a confirmation.
            already = bool(old.get("prod_write_enabled")) and old.get("environment") == "prod" and old.get("access") == "write"
            if not incoming.get("prod_write_enabled"):
                raise ValueError("A prod connector with write access needs “Allow writes to production” switched on")
            if not already and incoming.get("confirm_prod_write") is not True:
                raise ValueError("Confirm that agents may write to production (confirm_prod_write)")
            prod_write = True
        c = {
            "name": name, "type": ctype, "environment": env, "access": access, "prod_write_enabled": prod_write,
            "description": str(incoming.get("description") or "").strip()[:300],
            "repos": _list(incoming.get("repos")), "components": _list(incoming.get("components")),
            "config": _clean_config(ctype, incoming.get("config") or {}, old_cfg),
            "created": old.get("created") or now(), "updated": now(),
        }
        d["connectors"] = [x for x in d["connectors"] if x.get("name") not in (original, name)] + [c]
        d["connectors"].sort(key=lambda x: x["name"])
        if name != original:
            for k, names in d["repo_defaults"].items():
                d["repo_defaults"][k] = [name if n == original else n for n in names]
        _write(d)
    return c


def delete(name: str) -> None:
    with _lock:
        d = _data()
        d["connectors"] = [c for c in d["connectors"] if c.get("name") != name]
        for k, names in d["repo_defaults"].items():
            d["repo_defaults"][k] = [n for n in names if n != name]
        _write(d)


def public(c: dict) -> dict:
    """What the browser may see: secrets masked, with has_<key> flags."""
    cfg = dict(c.get("config") or {})
    for k in SECRET_KEYS.get(c.get("type"), ()):
        cfg[f"has_{k}"] = bool(cfg.get(k))
        cfg[k] = MASK if cfg.get(k) else ""
    if "headers" in cfg:
        cfg["headers"] = [{**h, "value": MASK if h.get("secret") and h.get("value") else h.get("value"), "has_value": bool(h.get("value"))}
                          for h in cfg["headers"]]
    return {**c, "config": cfg, "target": target(c)}


def target(c: dict) -> str:
    """One line saying what the connector points at (no secrets)."""
    cfg = c.get("config") or {}
    t = c.get("type")
    if t in ("http", "browser"):
        return cfg.get("base_url") or ""
    if t == "postgres":
        return f"{cfg.get('user') or '?'}@{cfg.get('host') or '?'}:{cfg.get('port') or 5432}/{cfg.get('database') or '?'}"
    if t == "logs":
        return ("loki " + (cfg.get("loki_url") or "") + (f" {cfg.get('loki_query')}" if cfg.get("loki_query") else "")) if cfg.get("source") == "loki" \
            else "containers " + ", ".join(cfg.get("containers") or [])
    if t == "docker":
        return "containers " + ", ".join(cfg.get("containers") or [])
    return ""


def secret_values(c: dict) -> list[str]:
    cfg = c.get("config") or {}
    vals = [str(cfg.get(k) or "") for k in SECRET_KEYS.get(c.get("type"), ())]
    vals += [h.get("value") or "" for h in cfg.get("headers") or [] if h.get("secret")]
    if c.get("type") == "http" and cfg.get("auth_type") == "basic" and cfg.get("auth_password"):
        vals.append(base64.b64encode(f"{cfg.get('auth_user') or ''}:{cfg['auth_password']}".encode()).decode())
    if c.get("type") == "logs" and cfg.get("loki_password"):
        vals.append(base64.b64encode(f"{cfg.get('loki_user') or ''}:{cfg['loki_password']}".encode()).decode())
    return [v for v in vals if len(v) >= 4]


def masker(conns: list[dict], extra: list[str] | None = None):
    vals = sorted({v for c in conns for v in secret_values(c)} | {v for v in (extra or []) if v and len(v) >= 4}, key=len, reverse=True)
    if not vals:
        return lambda s: s
    rx = re.compile("|".join(re.escape(v) for v in vals))
    return lambda s: rx.sub(MASK, s) if isinstance(s, str) else s


# ============================================================================ scope
def _repo_id(repo) -> str:
    try:
        return str(Path(repo).expanduser().resolve())
    except Exception:
        return str(repo or "")


def _linked(c: dict, repo) -> bool:
    rid = _repo_id(repo)
    return any(_repo_id(r) == rid for r in c.get("repos") or [])


def repo_defaults(repo) -> list[str] | None:
    """The saved default connector names for a repository, or None when it follows the automatic rule."""
    d = _data()
    names = d["repo_defaults"].get(_repo_id(repo))
    return None if names is None else [n for n in names if any(c["name"] == n for c in d["connectors"])]


def set_repo_defaults(repo, names) -> list[str] | None:
    with _lock:
        d = _data()
        key = _repo_id(repo)
        if names is None:
            d["repo_defaults"].pop(key, None)
        else:
            known = {c["name"] for c in d["connectors"]}
            d["repo_defaults"][key] = [n for n in _list(names) if n in known]
        _write(d)
    return repo_defaults(repo)


def default_names(repo) -> list[str]:
    """Saved per-repository defaults, else every non-prod connector linked to the repository."""
    saved = repo_defaults(repo) if repo else None
    if saved is not None:
        return saved
    return [c["name"] for c in load_all() if repo and _linked(c, repo) and c.get("environment") != "prod"]


def names_for_task(task: dict) -> list[str]:
    known = {c["name"] for c in load_all()}
    sel = task.get("connectors")
    if isinstance(sel, list):
        return [n for n in sel if n in known]
    return default_names(task.get("repo"))


# ============================================================================ tokens
_tokens: dict[str, dict] = {}
TOKEN_TTL = 24 * 3600


def issue_token(tid: str, names: list[str], ttl: float = TOKEN_TTL) -> str:
    with _lock:
        for tok, info in list(_tokens.items()):
            if info["tid"] == tid:
                _tokens.pop(tok, None)  # one live token per task
        tok = "rc_" + _secrets.token_urlsafe(32)
        _tokens[tok] = {"tid": tid, "names": list(names), "expires": time.time() + ttl}
        return tok


def revoke_task(tid: str) -> None:
    with _lock:
        for tok, info in list(_tokens.items()):
            if info["tid"] == tid:
                _tokens.pop(tok, None)


def check_token(header: str) -> dict:
    tok = (header or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    with _lock:
        info = _tokens.get(tok)
        if not info:
            raise PermissionError("Invalid or expired connector token: connectors only work while the task runs")
        if time.time() > info["expires"]:
            _tokens.pop(tok, None)
            raise PermissionError("Connector token expired")
        return dict(info)


def relay_url() -> str:
    return os.environ.get("RELAY_CONNECT_URL") or f"http://127.0.0.1:{os.environ.get('RELAY_PORT', '8767')}"


# ============================================================================ access helpers
def _guard_write(c: dict, what: str, confirm_prod: bool) -> None:
    if c.get("access") != "write":
        raise Refused(f"{what} is a write and connector {c['name']} is read-only")
    if c.get("environment") == "prod":
        if not c.get("prod_write_enabled"):
            raise Refused(f"{what} would write to production and writes are not enabled for {c['name']}")
        if not confirm_prod:
            raise Refused(f"{what} would write to production: it needs --confirm-prod, and only when the task explicitly asks for it")


def _truncate(text: str, limit: int = MAX_OUTPUT) -> tuple[str, bool]:
    text = text or ""
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n…[truncated {len(text) - limit} characters]", True


def _pick_container(c: dict, requested: str | None) -> str:
    names = (c.get("config") or {}).get("containers") or []
    if not names:
        raise Refused(f"Connector {c['name']} lists no containers")
    if not requested:
        if len(names) > 1:
            raise Refused(f"Choose --container: {', '.join(names)}")
        return names[0]
    if requested not in names:
        raise Refused(f"Container {requested} is not allowed for {c['name']} (allowed: {', '.join(names)})")
    return requested


def parse_since(value, default: float = 600) -> float:
    """'10m', '2h', '30s', '1d' or an ISO time → seconds ago as a unix timestamp."""
    v = str(value or "").strip()
    if not v:
        return time.time() - default
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhd]?)", v)
    if m:
        mult = {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return time.time() - float(m.group(1)) * mult
    from datetime import datetime
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        raise Refused(f"Cannot read --since {v!r}: use 10m, 2h, 1d or an ISO time")


def _grep(lines: list[str], pattern: str | None) -> list[str]:
    if not pattern:
        return lines
    try:
        rx = re.compile(pattern, re.I)
        return [ln for ln in lines if rx.search(ln)]
    except re.error:
        return [ln for ln in lines if pattern.lower() in ln.lower()]


# ============================================================================ http
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # credentials must never follow a redirect to another host
        return None


def clean_path(path: str) -> str:
    p = str(path or "/").strip()
    if re.match(r"^[a-z][a-z0-9+.-]*:", p, re.I) or p.startswith("//") or "\\" in p:
        raise Refused("Give a path relative to the connector's base URL (for example /items), not a full URL")
    if not p.startswith("/"):
        p = "/" + p
    raw_path, _, query = p.partition("?")
    decoded = urllib.parse.unquote(raw_path)
    if ".." in decoded.split("/"):
        raise Refused("Paths with .. are not allowed")
    norm = posixpath.normpath(raw_path)
    norm = norm if norm != "." else "/"
    if raw_path.endswith("/") and not norm.endswith("/"):
        norm += "/"
    return norm + (("?" + query) if query else "")


def check_http(c: dict, method: str, path: str, confirm_prod: bool = False) -> tuple[str, str]:
    cfg = c["config"]
    method = str(method or "GET").upper()
    if method not in ALL_METHODS:
        raise Refused(f"Unknown HTTP method {method}")
    allowed = cfg.get("allowed_methods") or ["GET", "HEAD"]
    if method not in allowed:
        raise Refused(f"{method} is not allowed for {c['name']} (allowed: {', '.join(allowed)})")
    if method not in SAFE_METHODS:
        _guard_write(c, f"{method} {path}", confirm_prod)
    p = clean_path(path)
    allow = cfg.get("path_allowlist") or []
    bare = p.split("?", 1)[0]
    if allow and not any(fnmatch.fnmatchcase(bare, pat) or bare == pat.rstrip("*").rstrip("/") for pat in allow):
        raise Refused(f"{bare} is outside the path allowlist of {c['name']} ({', '.join(allow)})")
    return method, p


def _http_headers(cfg: dict) -> dict:
    h = {"User-Agent": "relay-connect/1", "Accept": "application/json, text/plain, */*"}
    for row in cfg.get("headers") or []:
        h[row["name"]] = row["value"]
    at = cfg.get("auth_type")
    if at == "bearer" and cfg.get("auth_token"):
        h["Authorization"] = f"Bearer {cfg['auth_token']}"
    elif at == "basic":
        h["Authorization"] = "Basic " + base64.b64encode(f"{cfg.get('auth_user') or ''}:{cfg.get('auth_password') or ''}".encode()).decode()
    elif at == "header" and cfg.get("auth_header"):
        h[cfg["auth_header"]] = cfg.get("auth_value") or ""
    return h


def http_call(c: dict, method: str, path: str, body=None, headers: dict | None = None, confirm_prod: bool = False) -> dict:
    cfg = c["config"]
    method, p = check_http(c, method, path, confirm_prod)
    if not cfg.get("base_url"):
        raise Unavailable(f"Connector {c['name']} has no base URL")
    h = _http_headers(cfg)
    reserved = {"authorization", "cookie", "proxy-authorization", "host", str(cfg.get("auth_header") or "").lower()}
    reserved |= {r["name"].lower() for r in cfg.get("headers") or [] if r.get("secret")}
    for k, v in (headers or {}).items():
        if str(k).lower() in reserved:
            raise Refused(f"Header {k} is set by the connector and cannot be overridden")
        h[str(k)] = str(v)
    data = None
    if body is not None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        h.setdefault("Content-Type", "application/json")
    url = cfg["base_url"] + p
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    ctx = None if cfg.get("verify_tls", True) else ssl._create_unverified_context()
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    started = time.time()
    try:
        resp = opener.open(req, timeout=cfg.get("timeout") or 20)
        status, rheaders, raw = resp.status, dict(resp.headers), resp.read(2_000_000)
    except urllib.error.HTTPError as e:
        status, rheaders, raw = e.code, dict(e.headers or {}), (e.read(2_000_000) if e.fp else b"")
    except (urllib.error.URLError, OSError) as e:
        raise Unavailable(f"{method} {url} failed: {getattr(e, 'reason', e)}")
    ms = int((time.time() - started) * 1000)
    ctype = rheaders.get("Content-Type") or rheaders.get("content-type") or ""
    text = raw.decode("utf-8", errors="replace")
    parsed = None
    if "json" in ctype or text[:1] in "[{":
        try:
            parsed = json.loads(text)
            text = json.dumps(parsed, indent=2, ensure_ascii=False)
        except ValueError:
            parsed = None
    keep = {k: v for k, v in rheaders.items() if k.lower() in ("content-type", "content-length", "location", "cache-control", "etag", "www-authenticate")}
    out, cut = _truncate(text)
    shape = ""
    if isinstance(parsed, list):
        shape = f"array of {len(parsed)}" + (f", item keys: {', '.join(list(parsed[0].keys())[:20])}" if parsed and isinstance(parsed[0], dict) else "")
    elif isinstance(parsed, dict):
        shape = "object keys: " + ", ".join(list(parsed.keys())[:20])
    head = f"HTTP {status} · {ms} ms · {ctype or 'no content type'}" + (f" · {shape}" if shape else "")
    return {"ok": status < 400, "http_status": status, "headers": keep, "summary": f"{method} {p} → {status}",
            "output": head + "\n" + "\n".join(f"{k}: {v}" for k, v in keep.items()) + "\n\n" + out, "truncated": cut}


# ============================================================================ postgres
_WRITE_WORDS = ("insert", "update", "delete", "merge", "truncate", "drop", "alter", "create", "grant", "revoke", "copy",
                "vacuum", "reindex", "cluster", "refresh", "lock", "comment", "security", "import", "call", "do")
_READ_START = ("select", "with", "show", "explain", "values", "table")
# Functions with side effects that a read-only transaction does not stop.
_UNSAFE_FUNCS = ("set_config", "dblink", "dblink_exec", "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
                 "pg_rotate_logfile", "lo_import", "lo_export", "lo_unlink", "pg_read_file", "pg_read_binary_file", "pg_ls_dir",
                 "pg_advisory_lock", "pg_advisory_xact_lock", "pg_notify", "pg_switch_wal", "pg_create_restore_point")


def _strip_sql(sql: str, idents: bool = True) -> str:
    """Remove comments and string literals (and quoted identifiers when idents) so keywords can be checked."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif ch == "'" or (ch == '"' and idents):
            j = i + 1
            while j < n:
                if sql[j] == ch and j + 1 < n and sql[j + 1] == ch:
                    j += 2
                    continue
                if sql[j] == ch:
                    break
                j += 1
            out.append(" '' " if ch == "'" else ' "" ')
            i = j + 1
        elif ch == "$":
            m = re.match(r"\$([A-Za-z_]\w*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                i = n if j < 0 else j + len(tag)
                out.append(" '' ")
            else:
                out.append(ch)
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def check_sql(c: dict, sql: str, write: bool = False, confirm_prod: bool = False, schemas: list[str] | None = None) -> str:
    """Reject anything that is not one read statement (unless a permitted write). The database enforces it again."""
    sql = str(sql or "").strip()
    if not sql:
        raise Refused("Empty SQL")
    bare = _strip_sql(sql).strip()
    while bare.endswith(";"):
        bare = bare[:-1].rstrip()
        sql = sql.rstrip().rstrip(";").rstrip()
    if ";" in bare:
        raise Refused("One statement per call")
    words = re.findall(r"[a-z_]+", bare.lower())
    if not words:
        raise Refused("No SQL statement found")
    if write:
        _guard_write(c, "This SQL", confirm_prod)
    else:
        found = [w for w in _WRITE_WORDS if w in words and not (w in ("do", "call", "comment", "lock", "security", "import") and words[0] in _READ_START)]
        found += [w for w in _UNSAFE_FUNCS if w in words]
        if words[0] not in _READ_START or found:
            what = words[0] if words[0] not in _READ_START else found[0]
            raise Refused(f"Refused: `{what.upper()}` is not read-only. The connector runs read-only SQL "
                          + ("(the connector has write access: add --write to run a write)" if c.get("access") == "write"
                             else "and this connector is read-only") + ("" if words[0] == what else ". If it is a column or table name, quote it"))
    allowed = [s.lower() for s in (c["config"].get("allowed_schemas") or [])]
    if allowed and schemas:
        low = _strip_sql(sql, idents=False).lower()
        for s in schemas:
            s = s.lower()
            if s in allowed or s in ("pg_catalog", "information_schema"):
                continue
            if re.search(rf'(?<![\w"]){re.escape(s)}\s*\.|"{re.escape(s)}"\s*\.', low):
                raise Refused(f"Schema {s} is not allowed for {c['name']} (allowed: {', '.join(allowed)})")
    return sql


def _pg_connect(c: dict, write: bool):
    try:
        import psycopg  # type: ignore
    except ImportError:
        raise Unavailable("PostgreSQL connectors need the psycopg package in Relay's Python (pip install 'psycopg[binary]')")
    cfg = c["config"]
    opts = [f"-c statement_timeout={int(cfg.get('statement_timeout_ms') or 15000)}", "-c idle_in_transaction_session_timeout=60000"]
    if not write:
        opts.append("-c default_transaction_read_only=on")
    if cfg.get("allowed_schemas"):
        opts.append("-c search_path=" + ",".join(re.sub(r"[^\w]", "", s) for s in cfg["allowed_schemas"]))
    try:
        conn = psycopg.connect(host=cfg.get("host"), port=int(cfg.get("port") or 5432), dbname=cfg.get("database"),
                               user=cfg.get("user"), password=cfg.get("password") or None, sslmode=cfg.get("sslmode") or "prefer",
                               connect_timeout=10, options=" ".join(opts), application_name="relay-connect")
    except Exception as e:
        raise Unavailable(f"Could not connect to {target(c)}: {str(e).strip().splitlines()[0] if str(e).strip() else e}")
    conn.read_only = not write
    return conn


def _fmt_table(cols: list[str], rows: list) -> str:
    def cell(v):
        if v is None:
            return "NULL"
        s = v.isoformat() if hasattr(v, "isoformat") else str(v)
        return s.replace("\n", "\\n")[:200]
    body = [[cell(v) for v in r] for r in rows]
    widths = [min(60, max([len(c)] + [len(r[i]) for r in body])) for i, c in enumerate(cols)]
    line = lambda vals: " | ".join(v[:w].ljust(w) for v, w in zip(vals, widths))  # noqa: E731
    return "\n".join([line(cols), "-+-".join("-" * w for w in widths)] + [line(r) for r in body])


def sql_call(c: dict, sql: str, write: bool = False, confirm_prod: bool = False, limit: int | None = None, max_rows: int | None = None) -> dict:
    cfg = c["config"]
    check_sql(c, sql, write, confirm_prod)  # before connecting: refused SQL never reaches the database
    conn = _pg_connect(c, write)
    try:
        cur = conn.cursor()
        if cfg.get("allowed_schemas"):
            cur.execute("select nspname from pg_namespace")
            sql = check_sql(c, sql, write, confirm_prod, [r[0] for r in cur.fetchall()])
        else:
            sql = check_sql(c, sql, write, confirm_prod)
        ceiling = int(max_rows or cfg.get("row_limit") or 200)
        cap = max(1, min(int(limit or ceiling), ceiling))
        started = time.time()
        try:
            cur.execute(sql, prepare=False)
        except Exception as e:
            conn.rollback()
            msg = str(e).strip()
            return {"ok": False, "summary": "SQL error", "output": msg, "error": msg.splitlines()[0] if msg else "SQL error"}
        ms = int((time.time() - started) * 1000)
        if cur.description:
            cols = [d.name for d in cur.description]
            rows = cur.fetchmany(cap + 1)
            more = len(rows) > cap
            rows = rows[:cap]
            text = _fmt_table(cols, rows)
            info = f"{len(rows)} row(s){' (row limit reached, more rows exist)' if more else ''} · {ms} ms"
            data = {"columns": cols, "rows": [[(v.isoformat() if hasattr(v, "isoformat") else v if isinstance(v, (int, float, str, bool, type(None))) else str(v)) for v in r] for r in rows]}
        else:
            text, info, data, more = "", f"{cur.statusmessage} · {ms} ms", None, False
        if write:
            conn.commit()
        else:
            conn.rollback()
        out, cut = _truncate(info + "\n" + text)
        return {"ok": True, "summary": info, "output": out, "truncated": cut or more, "data": data}
    finally:
        conn.close()


def schema_sql(table: str | None) -> str:
    if table:
        parts = [re.sub(r"[^\w]", "", x) for x in table.split(".", 1)]
        sch = f"and table_schema = '{parts[0]}'" if len(parts) == 2 else ""
        tbl = parts[-1]
        return ("select table_schema, column_name, data_type, is_nullable, column_default from information_schema.columns "
                f"where table_name = '{tbl}' {sch} order by table_schema, ordinal_position")
    return ("select table_schema, table_name, table_type from information_schema.tables "
            "where table_schema not in ('pg_catalog', 'information_schema') order by 1, 2")


# ============================================================================ docker
def docker_socket() -> str:
    return os.environ.get("RELAY_DOCKER_SOCKET") or "/var/run/docker.sock"


def docker_status() -> dict:
    p = docker_socket()
    if not os.path.exists(p):
        return {"available": False, "message": f"No Docker socket at {p}. Mount it read-only into Relay to use logs and docker connectors (see docker-compose.yml)."}
    if not os.access(p, os.R_OK | os.W_OK):
        return {"available": False, "message": f"Relay's user cannot open {p}. Add the socket's group to the container (group_add)."}
    return {"available": True, "message": f"Docker socket at {p}"}


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path, timeout=20):
        super().__init__("localhost", timeout=timeout)
        self._sock_path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._sock_path)
        self.sock = s


def _docker(method: str, path: str, timeout: float = 20) -> tuple[int, bytes]:
    st = docker_status()
    if not st["available"]:
        raise Unavailable(st["message"])
    conn = _UnixConn(docker_socket(), timeout)
    try:
        conn.request(method, path, headers={"Host": "docker"})
        r = conn.getresponse()
        return r.status, r.read(5_000_000)
    except OSError as e:
        raise Unavailable(f"Docker API error: {e}")
    finally:
        conn.close()


def _inspect(name: str) -> dict:
    status, raw = _docker("GET", f"/containers/{urllib.parse.quote(name)}/json")
    if status == 404:
        raise Unavailable(f"Container {name} does not exist")
    if status >= 400:
        raise Unavailable(f"Docker API {status}: {raw[:300].decode(errors='replace')}")
    return json.loads(raw)


def _demux(raw: bytes) -> str:
    """Docker multiplexes stdout/stderr frames when the container has no TTY."""
    out, i = [], 0
    if len(raw) >= 8 and raw[0] in (0, 1, 2) and raw[1:4] == b"\x00\x00\x00":
        while i + 8 <= len(raw):
            size = int.from_bytes(raw[i + 4:i + 8], "big")
            out.append(raw[i + 8:i + 8 + size])
            i += 8 + size
        return b"".join(out).decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def docker_logs(c: dict, container: str | None, since: str | None, tail: int | None, grep: str | None) -> dict:
    cfg = c["config"]
    name = _pick_container(c, container)
    info = _inspect(name)
    n = max(1, min(int(tail or cfg.get("max_lines") or 500), int(cfg.get("max_lines") or 500)))
    q = urllib.parse.urlencode({"stdout": 1, "stderr": 1, "timestamps": 1, "since": int(parse_since(since)), "tail": n if not grep else 5000})
    status, raw = _docker("GET", f"/containers/{urllib.parse.quote(name)}/logs?{q}", timeout=30)
    if status >= 400:
        raise Unavailable(f"Docker API {status}: {raw[:300].decode(errors='replace')}")
    lines = _grep(_demux(raw).splitlines(), grep)[-n:]
    state = (info.get("State") or {}).get("Status")
    out, cut = _truncate("\n".join(lines), MAX_OUTPUT)
    return {"ok": True, "summary": f"{len(lines)} line(s) from {name} ({state})", "output": out or "(no log lines in that window)", "truncated": cut}


def loki_logs(c: dict, since: str | None, tail: int | None, grep: str | None) -> dict:
    cfg = c["config"]
    if not cfg.get("loki_url") or not cfg.get("loki_query"):
        raise Unavailable(f"Connector {c['name']} needs a Loki URL and query")
    n = max(1, min(int(tail or cfg.get("max_lines") or 500), int(cfg.get("max_lines") or 500)))
    start = int(parse_since(since) * 1e9)
    q = urllib.parse.urlencode({"query": cfg["loki_query"], "start": start, "end": int(time.time() * 1e9), "limit": 5000 if grep else n, "direction": "backward"})
    h = {"User-Agent": "relay-connect/1"}
    if cfg.get("loki_token"):
        h["Authorization"] = f"Bearer {cfg['loki_token']}"
    elif cfg.get("loki_user"):
        h["Authorization"] = "Basic " + base64.b64encode(f"{cfg['loki_user']}:{cfg.get('loki_password') or ''}".encode()).decode()
    req = urllib.request.Request(f"{cfg['loki_url']}/loki/api/v1/query_range?{q}", headers=h)
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=30) as r:
            data = json.loads(r.read(10_000_000))
    except urllib.error.HTTPError as e:
        raise Unavailable(f"Loki answered {e.code}: {(e.read(300) if e.fp else b'').decode(errors='replace')}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Unavailable(f"Loki query failed: {getattr(e, 'reason', e)}")
    entries = []
    for stream in (data.get("data") or {}).get("result") or []:
        for ts, line in stream.get("values") or []:
            entries.append((int(ts), line))
    entries.sort()
    from datetime import datetime, timezone
    lines = [f"{datetime.fromtimestamp(ts / 1e9, timezone.utc).isoformat()} {ln}" for ts, ln in entries]
    lines = _grep(lines, grep)[-n:]
    out, cut = _truncate("\n".join(lines))
    return {"ok": True, "summary": f"{len(lines)} line(s) from Loki", "output": out or "(no log lines in that window)", "truncated": cut}


def _docker_summary(info: dict) -> dict:
    st = info.get("State") or {}
    return {"name": (info.get("Name") or "").lstrip("/"), "image": (info.get("Config") or {}).get("Image"), "status": st.get("Status"),
            "running": st.get("Running"), "health": (st.get("Health") or {}).get("Status"), "started_at": st.get("StartedAt"),
            "finished_at": st.get("FinishedAt"), "exit_code": st.get("ExitCode"), "restart_count": info.get("RestartCount"),
            "oom_killed": st.get("OOMKilled"), "error": st.get("Error") or None}


def _sanitize_inspect(info: dict) -> dict:
    """Inspect output without environment values or secret-looking labels: those often hold credentials."""
    info = json.loads(json.dumps(info))
    cfg = info.get("Config") or {}
    cfg["Env"] = [e.split("=", 1)[0] + "=" + MASK for e in cfg.get("Env") or []]
    labels = cfg.get("Labels") or {}
    for k in list(labels):
        if re.search(r"pass|secret|token|key|auth|credential", k, re.I):
            labels[k] = MASK
    for k in ("GraphDriver", "ExecIDs"):
        info.pop(k, None)
    return info


def docker_call(c: dict, action: str, container: str | None, confirm_prod: bool = False) -> dict:
    name = _pick_container(c, container)
    if action == "status":
        rows = []
        targets = [name] if container or len(c["config"]["containers"]) == 1 else c["config"]["containers"]
        for n in targets:
            try:
                rows.append(_docker_summary(_inspect(n)))
            except Unavailable as e:
                rows.append({"name": n, "status": "unavailable", "error": str(e)})
        ok = all(r.get("running") for r in rows)
        return {"ok": True, "summary": ", ".join(f"{r['name']} {r.get('status')}{'/' + r['health'] if r.get('health') else ''}" for r in rows),
                "output": json.dumps(rows, indent=2), "healthy": ok}
    if action == "inspect":
        out, cut = _truncate(json.dumps(_sanitize_inspect(_inspect(name)), indent=2, ensure_ascii=False))
        return {"ok": True, "summary": f"inspect {name}", "output": out, "truncated": cut}
    if action == "restart":
        if not c["config"].get("allow_restart"):
            raise Refused(f"Restart is not permitted for {c['name']}")
        _guard_write(c, f"Restarting {name}", confirm_prod)
        status, raw = _docker("POST", f"/containers/{urllib.parse.quote(name)}/restart?t=10", timeout=60)
        if status >= 400:
            return {"ok": False, "summary": f"restart {name} failed", "output": raw[:500].decode(errors="replace")}
        return {"ok": True, "summary": f"restarted {name}", "output": json.dumps(_docker_summary(_inspect(name)), indent=2)}
    raise Refused(f"Unknown docker action {action}: use status, inspect or restart")


# ============================================================================ browser
_STEP = re.compile(r"^(goto|fill|click|press|wait|waitfor)\s*(.*)$", re.I)


def parse_steps(text) -> list[dict]:
    """Login steps, one per line: goto /login · fill #user {{username}} · click button[type=submit] · wait 1000 · waitfor .dashboard"""
    steps = []
    if isinstance(text, list):
        return text
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _STEP.match(line)
        if not m:
            raise ValueError(f"Login step not understood: {line!r} (use goto, fill, click, press, wait, waitfor)")
        action, rest = m.group(1).lower(), m.group(2).strip()
        if action in ("fill", "press"):
            sel, _, value = rest.partition(" ")
            if not sel:
                raise ValueError(f"{action} needs a selector: {line!r}")
            steps.append({"action": action, "selector": sel, "value": value.strip()})
        elif action == "goto":
            steps.append({"action": "goto", "path": rest or "/"})
        elif action == "wait":
            steps.append({"action": "wait", "ms": int(rest or 1000)})
        else:
            steps.append({"action": action, "selector": rest})
    return steps


def browser_available() -> dict:
    node = shutil.which("node")
    if not node:
        return {"available": False, "message": "Node.js is not installed, so browser connectors cannot run"}
    r = subprocess.run([node, "-e", "require.resolve('playwright-core')"], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return {"available": False, "message": "playwright-core is not installed (the Relay image ships it)"}
    return {"available": True, "message": "headless Chromium via playwright-core"}


def browser_call(c: dict, goto: str = "/", actions: list[dict] | None = None, screenshot: bool = False, login: bool = True,
                 text_selector: str | None = None, width=None, height=None, theme: str = "light", wait: int = 1000) -> dict:
    cfg = c["config"]
    if not cfg.get("base_url"):
        raise Unavailable(f"Connector {c['name']} has no base URL")
    actions = actions or []
    if any(a.get("action") in ("click", "fill", "press") for a in actions) and c.get("environment") == "prod" and c.get("access") != "write":
        raise Refused("Clicking or typing in a production app can change data: this prod connector is read-only (navigation and screenshots only)")
    for a in actions:
        if a.get("action") not in ("click", "fill", "press", "wait", "waitfor", "goto"):
            raise Refused(f"Unknown browser action {a.get('action')}")
        if a.get("action") == "goto":
            a["path"] = clean_path(a.get("path") or "/")
    st = browser_available()
    if not st["available"]:
        raise Unavailable(st["message"])
    steps = parse_steps(cfg.get("login_steps")) if login else []
    for s in steps:
        if s.get("action") == "goto":
            s["path"] = clean_path(s.get("path") or "/")
    job = {"base_url": cfg["base_url"], "login_steps": steps, "username": cfg.get("username") or "", "password": cfg.get("password") or "",
           "goto": clean_path(goto or "/"), "actions": actions, "screenshot": bool(screenshot), "text_selector": text_selector or "",
           "width": int(width or cfg.get("width") or 1440), "height": int(height or cfg.get("height") or 900),
           "theme": "dark" if theme == "dark" else "light", "wait": max(0, min(int(wait or 0), 15000))}
    env = os.environ.copy()
    env.setdefault("NODE_PATH", "/usr/local/lib/node_modules")
    try:
        r = subprocess.run(["node", str(APP_DIR / "tools" / "connect-browser.cjs")], input=json.dumps(job), capture_output=True,
                           text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "summary": "browser timed out", "output": "The browser check took longer than 120 s"}
    try:
        res = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "summary": "browser failed", "output": (r.stderr or r.stdout)[-2000:]}
    lines = [f"{res.get('status') or '?'} {res.get('url')}", f"title: {res.get('title')!r}"]
    if res.get("login"):
        lines.append(f"login: {res['login']}")
    for p in (res.get("problems") or [])[:30]:
        lines.append(p)
    if res.get("text") is not None:
        lines.append("\ntext:\n" + str(res["text"])[:6000])
    out, cut = _truncate("\n".join(lines))
    return {"ok": bool(res.get("ok")), "summary": f"{job['goto']} → {res.get('status') or 'error'}"
            + (f" · {len(res.get('problems') or [])} problem(s)" if res.get("problems") else ""),
            "output": out, "truncated": cut, "screenshot_png_base64": res.get("screenshot"), "error": res.get("error")}


# ============================================================================ dispatch
def execute(c: dict, op: str, args: dict) -> dict:
    """Run one connector operation. Raises Refused / Unavailable; returns {ok, summary, output, ...}."""
    t = c.get("type")
    confirm = bool(args.get("confirm_prod"))
    if op == "http":
        if t != "http":
            raise Refused(f"{c['name']} is a {t} connector, not http")
        return http_call(c, args.get("method") or "GET", args.get("path") or "/", args.get("body"), args.get("headers"), confirm)
    if op in ("sql", "schema"):
        if t != "postgres":
            raise Refused(f"{c['name']} is a {t} connector, not postgres")
        sql = schema_sql(args.get("table")) if op == "schema" else args.get("sql")
        # Catalogue listings are metadata, so they are not held to the data row limit.
        return sql_call(c, sql, bool(args.get("write")) and op == "sql", confirm, args.get("limit"), 1000 if op == "schema" else None)
    if op == "logs":
        if t == "logs" and c["config"].get("source") == "loki":
            return loki_logs(c, args.get("since"), args.get("tail"), args.get("grep"))
        if t in ("logs", "docker"):
            return docker_logs(c, args.get("container"), args.get("since"), args.get("tail"), args.get("grep"))
        raise Refused(f"{c['name']} is a {t} connector, not logs")
    if op == "docker":
        if t != "docker":
            raise Refused(f"{c['name']} is a {t} connector, not docker")
        return docker_call(c, args.get("action") or "status", args.get("container"), confirm)
    if op == "browser":
        if t != "browser":
            raise Refused(f"{c['name']} is a {t} connector, not browser")
        return browser_call(c, args.get("goto") or "/", args.get("actions"), bool(args.get("screenshot")), args.get("login", True) is not False,
                            args.get("text"), args.get("width"), args.get("height"), args.get("theme") or "light", args.get("wait") or 1000)
    raise Refused(f"Unknown operation {op}")


def operation_label(op: str, args: dict) -> str:
    if op == "http":
        return f"{str(args.get('method') or 'GET').upper()} {args.get('path') or '/'}"
    if op == "sql":
        return ("SQL (write) " if args.get("write") else "SQL ") + re.sub(r"\s+", " ", str(args.get("sql") or ""))[:160]
    if op == "schema":
        return f"schema {args.get('table') or ''}".strip()
    if op == "logs":
        return "logs" + "".join(f" --{k} {args[k]}" for k in ("container", "since", "tail", "grep") if args.get(k))
    if op == "docker":
        return f"docker {args.get('action') or 'status'}" + (f" {args['container']}" if args.get("container") else "")
    if op == "browser":
        return f"browser {args.get('goto') or '/'}" + (" + screenshot" if args.get("screenshot") else "")
    return op


def run(c: dict, op: str, args: dict) -> dict:
    """execute() plus timing, status and masking: the shape recorded for every call."""
    started = time.time()
    mask = masker([c])
    try:
        res = execute(c, op, dict(args or {}))
        status = "ok" if res.get("ok") else "error"
    except Refused as e:
        res, status = {"ok": False, "refused": True, "summary": "refused", "output": str(e), "error": str(e)}, "refused"
    except Unavailable as e:
        res, status = {"ok": False, "unavailable": True, "summary": "unavailable", "output": str(e), "error": str(e)}, "error"
    except Exception as e:  # a connector bug must come back as an answer, not a 500 the agent cannot read
        res, status = {"ok": False, "summary": "error", "output": f"{type(e).__name__}: {e}", "error": str(e)}, "error"
    res = {k: (mask(v) if isinstance(v, str) else v) for k, v in res.items()}
    res.update(call_status=status, duration=round(time.time() - started, 3), connector=c["name"], type=c["type"],
               environment=c["environment"], operation=mask(operation_label(op, args or {})))
    return res


def test(c: dict) -> dict:
    """Health check from the Settings page."""
    t = c["type"]
    cfg = c["config"]
    try:
        if t == "http":
            path = cfg.get("health_path") or "/"
            saved = dict(c, config={**cfg, "allowed_methods": list(set((cfg.get("allowed_methods") or []) + ["GET"])), "path_allowlist": []})
            return run(saved, "http", {"method": "GET", "path": path})
        if t == "postgres":
            return run(c, "sql", {"sql": "select current_user, current_database(), current_setting('transaction_read_only') as read_only, "
                                         "(select count(*) from information_schema.tables where table_schema not in ('pg_catalog','information_schema')) as tables, "
                                         "split_part(version(), ' ', 2) as server_version"})
        if t == "logs" and cfg.get("source") == "loki":
            return run(c, "logs", {"since": "5m", "tail": 5})
        if t in ("logs", "docker"):
            st = docker_status()
            if not st["available"]:
                return {"ok": False, "call_status": "error", "summary": "Docker socket unavailable", "output": st["message"], "duration": 0}
            rows = []
            for n in cfg.get("containers") or []:
                try:
                    rows.append(_docker_summary(_inspect(n)))
                except Unavailable as e:
                    rows.append({"name": n, "status": "missing", "error": str(e)})
            ok = bool(rows) and all(r.get("running") for r in rows)
            return {"ok": ok, "call_status": "ok" if ok else "error", "summary": ", ".join(f"{r['name']} {r.get('status')}" for r in rows) or "no containers listed",
                    "output": json.dumps(rows, indent=2), "duration": 0}
        if t == "browser":
            res = run(c, "browser", {"goto": "/", "screenshot": False})
            res.pop("screenshot_png_base64", None)
            return res
    except Exception as e:
        return {"ok": False, "call_status": "error", "summary": "error", "output": str(e), "duration": 0}
    return {"ok": False, "call_status": "error", "summary": "unknown type", "output": "", "duration": 0}


# ============================================================================ call log
def record_call(entry: dict) -> None:
    line = json.dumps({"time": now(), **entry}, ensure_ascii=False)
    with _lock:
        CALLS.parent.mkdir(parents=True, exist_ok=True)
        with CALLS.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        try:
            if CALLS.stat().st_size > 2_000_000:
                keep = CALLS.read_text(encoding="utf-8").splitlines()[-2000:]
                CALLS.write_text("\n".join(keep) + "\n", encoding="utf-8")
        except OSError:
            pass


def recent_calls(name: str | None = None, limit: int = 50) -> list[dict]:
    if not CALLS.exists():
        return []
    out = []
    for line in reversed(CALLS.read_text(encoding="utf-8", errors="replace").splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if name and e.get("connector") != name:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


# ============================================================================ prompts
def describe(names: list[str], screenshots_dir=None) -> str:
    conns = [c for c in load_all() if c["name"] in names]
    if not conns:
        return ""
    rows = []
    for c in conns:
        cfg = c["config"]
        extra = ""
        if c["type"] == "http":
            extra = f" · methods {', '.join(m for m in cfg.get('allowed_methods') or [] if m in SAFE_METHODS or c['access'] == 'write')}"
            if cfg.get("path_allowlist"):
                extra += f" · paths {', '.join(cfg['path_allowlist'])}"
        elif c["type"] == "postgres" and cfg.get("allowed_schemas"):
            extra = f" · schemas {', '.join(cfg['allowed_schemas'])}"
        elif c["type"] == "docker" and cfg.get("allow_restart") and c["access"] == "write":
            extra = " · restart allowed"
        env = c["environment"].upper() if c["environment"] == "prod" else c["environment"]
        rows.append(f"  - `{c['name']}` · {c['type']} · {env} · {c['access']} · {target(c)}{extra}"
                    + (f" — {c['description']}" if c.get("description") else ""))
    types = {c["type"] for c in conns}
    cmds = ["`relay-connect list`"]
    if "http" in types:
        cmds.append("`relay-connect http <name> GET /path` (`--json '{…}'` for a body)")
    if "postgres" in types:
        cmds += ["`relay-connect schema <name> [table]`", "`relay-connect sql <name> \"select …\"`"]
    if types & {"logs", "docker"}:
        cmds.append("`relay-connect logs <name> --since 10m --grep error`")
    if "docker" in types:
        cmds.append("`relay-connect docker <name> status`")
    if "browser" in types:
        shot = f"{screenshots_dir}/page.png" if screenshots_dir else "out.png"
        cmds.append(f"`relay-connect browser <name> --goto /path --screenshot {shot}` (prints console errors)")
    return ("- Connectors to the real environments behind this code. Relay performs each call, keeps the credentials, "
            "enforces the access level and records the call in the task:\n" + "\n".join(rows)
            + "\n  Call them from your shell: " + " · ".join(cmds) + ".\n"
            "  Before you build on or fix behaviour that depends on one of these services, check the real thing (response shape, "
            "columns, errors in the logs) instead of guessing. Never write to a PROD connector unless the task explicitly asks. "
            "Put the command you ran and what it showed in your report as evidence.")
