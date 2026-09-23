"""System map: the components of the software system, what each provides and what depends on what.

A task used to know one repository. Real features cross repositories: a frontend button that relies on an
endpoint of an API that keeps state in memory, an API that calls SQL functions of a database schema. The map
lets Relay suggest related repositories for a task and tell the supervisor which components a request
reaches end to end.

Stored in DATA_DIR/systems.json:

  components  {id, name, repo (owner/name), path, kind, description, runs, provides, locked}
  edges       {id, from, to, via (http|sql|queue|file|lib), details, status (proposed|approved|rejected),
               evidence [{file, line, text}], source (scan|manual)}

Scanning is deterministic: HTTP calls and base-URL config keys in client code, route definitions (FastAPI,
Flask, Express), SQL function definitions and calls (including PostgREST /rpc/ names) and docker compose
service names. Calls are matched to providers and proposed as edges with file:line evidence. Nothing a scan
proposes is used for planning until a person approves it.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .util import DATA_DIR, new_id, now, quiet, read_json, safe_slug, truncate, write_json

PATH = DATA_DIR / "systems.json"
KINDS = ("frontend", "api", "worker", "database", "library", "infra")
VIAS = ("http", "sql", "queue", "file", "lib")
STATUSES = ("proposed", "approved", "rejected")
EDITABLE = ("name", "repo", "kind", "description", "runs", "path")

SKIP_DIRS = {".git", "node_modules", "dist", "build", ".venv", "venv", "env", "__pycache__", ".next", ".nuxt", "coverage",
             ".pytest_cache", ".mypy_cache", "vendor", "target", ".idea", ".vscode", "site-packages", ".orchestrator_refs",
             "bower_components", ".cache", "out", "tmp"}
CODE_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".go", ".java", ".kt", ".rb", ".php", ".cs"}
SQL_EXT = {".sql"}
MAX_FILES = 6000
MAX_BYTES = 400_000
MAX_EVIDENCE = 16
# Paths too generic to prove a dependency on their own.
GENERIC = {"", "api", "v1", "v2", "v3", "health", "healthz", "ping", "status", "login", "logout", "auth", "token", "me", "index",
           "static", "assets", "docs", "openapi.json", "metrics", "config", "version", "ws", "{}"}
NAME_NOISE = {"service", "services", "api", "app", "server", "web", "backend", "frontend", "client", "ui", "the", "and", "core",
              "main", "repo", "vue", "react", "svc", "worker", "http", "base", "url"}

_lock = threading.RLock()


# ----------------------------------------------------------------------------- storage
def empty() -> dict:
    return {"version": 1, "components": [], "edges": [], "scanned_at": None, "updated": None}


def load() -> dict:
    data = read_json(PATH, None) or {}
    out = empty()
    out.update({k: v for k, v in data.items() if k in out})
    return out


def save(data: dict) -> dict:
    data["updated"] = now()
    PATH.parent.mkdir(parents=True, exist_ok=True)
    write_json(PATH, data)
    return data


def view(data: dict | None = None) -> dict:
    """The map as the browser and prompts see it: each component also lists its dependencies."""
    data = data or load()
    comps = [dict(c) for c in data["components"]]
    for c in comps:
        c["depends_on"] = [{"component": e["to"], "via": e["via"], "details": e.get("details", ""), "status": e["status"], "edge": e["id"]}
                           for e in data["edges"] if e["from"] == c["id"] and e["status"] != "rejected"]
        c["used_by"] = [{"component": e["from"], "via": e["via"], "details": e.get("details", ""), "status": e["status"], "edge": e["id"]}
                        for e in data["edges"] if e["to"] == c["id"] and e["status"] != "rejected"]
        c["cloned"] = bool(c.get("path") and Path(c["path"]).is_dir())
    return {**data, "components": comps,
            "counts": {"components": len(comps), "approved": sum(e["status"] == "approved" for e in data["edges"]),
                       "proposed": sum(e["status"] == "proposed" for e in data["edges"])}}


def component(data: dict, cid: str) -> dict | None:
    return next((c for c in data["components"] if c["id"] == cid), None)


def _same_path(a, b) -> bool:
    try:
        return bool(a) and bool(b) and Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def find(data: dict, ref: str) -> dict | None:
    """A component by id, name, owner/name, repository name or local path (case-insensitive)."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    low = ref.lower().rstrip("/")
    tail = low.split("/")[-1].removesuffix(".git")
    for c in data["components"]:
        if low in (c["id"].lower(), (c.get("name") or "").lower(), (c.get("repo") or "").lower()) or _same_path(c.get("path"), ref):
            return c
    for c in data["components"]:
        if tail and tail in (c["id"].lower(), (c.get("repo") or "").lower().split("/")[-1], Path(c.get("path") or "x").name.lower()):
            return c
    return None


def for_repo(data: dict, path) -> dict | None:
    """The component a local repository (or one of its worktrees) is."""
    if not path:
        return None
    for c in data["components"]:
        if _same_path(c.get("path"), path):
            return c
    from .github import remote_repo_name
    full = remote_repo_name(path) if Path(path).exists() else None
    if full:
        for c in data["components"]:
            if (c.get("repo") or "").lower() == full.lower():
                return c
    return None


# ----------------------------------------------------------------------------- scanning helpers
def _files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in CODE_EXT | SQL_EXT and not _config_file(name) and not _compose_file(name):
                continue
            try:
                if p.stat().st_size > MAX_BYTES:
                    continue
            except OSError:
                continue
            if name.endswith((".min.js", ".bundle.js", ".map")):
                continue
            count += 1
            if count > MAX_FILES:
                return
            yield p


def _config_file(name: str) -> bool:
    return name.startswith(".env") or name in ("config.js", "config.json", "runtime-config.js", "env.js", "settings.json")


def _compose_file(name: str) -> bool:
    return bool(re.match(r"^(docker-)?compose[\w.-]*\.ya?ml$", name)) or name in ("navistack.yml", "stack.yml")


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def normalize_path(raw: str) -> str:
    """'/api/v1/operation/status/${id}?x=1' → '/api/v1/operation/status/{}'. Empty when it is not a path."""
    s = str(raw or "").strip().strip("'\"`")
    s = re.sub(r"^\$\{[^}]*\}", "", s)            # template base: ${API_BASE}/...
    s = re.sub(r"^https?://[^/]+", "", s)            # absolute URL
    s = s.split("?")[0].split("#")[0]
    if not s.startswith("/") or " " in s or len(s) > 200:
        return ""
    trailing = s.endswith("/") and len(s) > 1
    parts = []
    for seg in s.strip("/").split("/"):
        if not seg:
            continue
        if re.fullmatch(r"\$\{[^}]*\}|\{[^}]*\}|:[A-Za-z_]\w*|<[^>]*>|\d+|[0-9a-f-]{32,36}", seg):
            parts.append("{}")
        elif "${" in seg or "{" in seg:
            parts.append("{}")
        elif re.fullmatch(r"[A-Za-z0-9_.~-]+", seg):
            parts.append(seg.lower())
        else:
            return ""
    if trailing:
        parts.append("{}")  # '/items/' + id
    return "/" + "/".join(parts)


def _literal_segments(norm: str) -> list[str]:
    return [s for s in norm.strip("/").split("/") if s and s != "{}"]


def path_match(call: str, route: str) -> int:
    """How strongly a called path matches a route: 0 no match, else the number of literal segments matched.

    The route must line up with the end of the call, so a proxy or base prefix in front of the call
    ('/lidar' + '/api/v1/operation/start') still matches. A route parameter matches any segment; a value
    interpolated into the call only matches a route parameter, never a fixed word.
    """
    c = call.strip("/").split("/")
    r = route.strip("/").split("/")
    if not route.strip("/") or len(r) > len(c):
        return 0
    tail = c[len(c) - len(r):]
    score = distinct = 0
    for a, b in zip(tail, r):
        if a == b:
            if a != "{}":
                score += 1
                distinct += a not in GENERIC
        elif b == "{}":
            continue
        else:
            return 0
    if not distinct:
        return 0
    # One word ('/archive') is weak evidence when the call has more in front of it; require two matching words then.
    if score < 2 and len(r) != len(c):
        return 0
    return score


_ROUTE_DECOR = re.compile(r"@(\w+)\.(get|post|put|patch|delete|options|head|route|api_route|websocket)\(\s*(?:path\s*=\s*)?[rbf]?['\"]([^'\"]*)['\"]", re.I)
_ROUTER_PREFIX = re.compile(r"(\w+)\s*=\s*(?:APIRouter|Blueprint)\s*\(([^)]*)\)", re.S)
_PREFIX_ARG = re.compile(r"(?:prefix|url_prefix)\s*=\s*['\"]([^'\"]*)['\"]")
_INCLUDE = re.compile(r"(?:include_router|register_blueprint)\(\s*([\w.]+)\s*(?:,([^)]*))?\)")
_EXPRESS = re.compile(r"\b(app|router|server|api)\.(get|post|put|patch|delete|all)\(\s*['\"`](/[^'\"`]*)['\"`]")
_SQL_DEF = re.compile(r"create\s+(?:or\s+replace\s+)?function\s+(?:\"?(\w+)\"?\.)?\"?(\w+)\"?\s*\(", re.I)
_RPC_CALL = re.compile(r"(?:/rpc/|\.rpc\(\s*['\"`])(\w+)")
_SQL_CALL = re.compile(r"\b(?:select|perform|call)\s+(?:\*\s+from\s+)?(?:(\w+)\.)?(\w+)\s*\(", re.I)
_STRING = re.compile(r"`([^`\n]{2,240})`|'([^'\n]{2,240})'|\"([^\"\n]{2,240})\"")
_CALL_HINT = re.compile(r"fetch|axios|\$http|http[A-Z.]|request|\bky\b|superagent|\.(?:get|post|put|patch|delete)\s*\(|url|endpoint|api|requests\.|httpx|urljoin|_BASE|baseURL", re.I)
_CONFIG_KEY = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:API|URL|BASE|HOST|ENDPOINT|SERVICE)[A-Za-z0-9_]*)\b\s*[:=]\s*['\"`]?([^'\"`\s,;]*)", re.I)
_SQL_SKIP = {"now", "count", "coalesce", "max", "min", "sum", "avg", "exists", "array_agg", "json_build_object", "jsonb_build_object",
             "to_char", "date_trunc", "st_makepoint", "st_setsrid", "st_distance", "row_number", "lower", "upper", "set_config",
             "current_setting", "pg_sleep", "gen_random_uuid", "nextval", "json_agg", "jsonb_agg", "cast", "extract", "round", "abs",
             "greatest", "least", "concat", "length", "trim", "unnest", "generate_series", "time_bucket", "version", "from", "distinct"}


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _ev(root: Path, p: Path, line: int, text: str) -> dict:
    return {"file": str(p.relative_to(root)), "line": line, "text": truncate(text.strip(), 160)}


def scan_repo(path) -> dict:
    """Evidence from one repository: what it provides and what it calls."""
    root = Path(path).resolve()
    routes, functions, services, calls, rpc_calls, config = [], [], [], [], [], []
    includes: dict[str, str] = {}
    py_files = []
    frontend_signals = 0
    for p in _files(root):
        text = _read(p)
        if not text:
            continue
        ext = p.suffix.lower()
        name = p.name
        if _compose_file(name):
            m = re.search(r"^services:\s*\n((?:[ \t]+.*\n?|\s*\n)*)", text, re.M)
            if m:
                for sm in re.finditer(r"^( {2}|\t)([A-Za-z0-9_.-]+):\s*$", m.group(1), re.M):
                    services.append({"name": sm.group(2), **_ev(root, p, _line_of(text, m.start(1) + sm.start()), sm.group(0))})
            continue
        if ext in SQL_EXT:
            for m in _SQL_DEF.finditer(text):
                functions.append({"name": m.group(2).lower(), "schema": (m.group(1) or "").lower(), **_ev(root, p, _line_of(text, m.start()), m.group(0))})
            continue
        if _config_file(name) or name in ("vite.config.js", "vite.config.ts"):
            for m in _CONFIG_KEY.finditer(text):
                config.append({"key": m.group(1), "value": m.group(2)[:200], **_ev(root, p, _line_of(text, m.start()), m.group(0))})
            if ext not in CODE_EXT:
                continue
        if ext in (".vue", ".svelte", ".jsx", ".tsx"):
            frontend_signals += 1
        if ext == ".py":
            py_files.append((p, text))
            for m in _INCLUDE.finditer(text):
                pm = _PREFIX_ARG.search(m.group(2) or "")
                includes[m.group(1).split(".")[0]] = pm.group(1) if pm else ""
        # Routes this file defines.
        prefixes = {}
        for m in _ROUTER_PREFIX.finditer(text):
            pm = _PREFIX_ARG.search(m.group(2))
            prefixes[m.group(1)] = pm.group(1) if pm else ""
        for m in _ROUTE_DECOR.finditer(text):
            obj, method, rpath = m.group(1), m.group(2).upper(), m.group(3)
            if method in ("ROUTE", "API_ROUTE"):
                method = "ANY"
            if not rpath.startswith("/") and rpath:
                continue
            routes.append({"method": method, "path": (prefixes.get(obj, "") + rpath) or "/", "module": p.stem, "router": obj,
                           **_ev(root, p, _line_of(text, m.start()), m.group(0))})
        if ext in (".js", ".mjs", ".cjs", ".ts"):
            for m in _EXPRESS.finditer(text):
                routes.append({"method": m.group(2).upper(), "path": m.group(3), "module": p.stem, "router": m.group(1),
                               **_ev(root, p, _line_of(text, m.start()), m.group(0))})
        # SQL defined inline in code (migrations as strings).
        for m in _SQL_DEF.finditer(text):
            functions.append({"name": m.group(2).lower(), "schema": (m.group(1) or "").lower(), **_ev(root, p, _line_of(text, m.start()), m.group(0))})
        # Calls this file makes.
        lines = text.splitlines()
        for i, line in enumerate(lines, 1):
            if len(line) > 600:
                continue
            for m in _RPC_CALL.finditer(line):
                rpc_calls.append({"name": m.group(1).lower(), **_ev(root, p, i, line)})
            if re.search(r"\b(select|perform|call)\b", line, re.I):
                for m in _SQL_CALL.finditer(line):
                    fn = m.group(2).lower()
                    if fn not in _SQL_SKIP and len(fn) > 3:
                        rpc_calls.append({"name": fn, **_ev(root, p, i, line)})
            if "/" not in line or not _CALL_HINT.search(line):
                continue
            if _ROUTE_DECOR.search(line) or _EXPRESS.search(line):
                continue
            for m in _STRING.finditer(line):
                raw = next(g for g in m.groups() if g is not None)
                norm = normalize_path(raw)
                if norm and len(_literal_segments(norm)) >= 1:
                    base = re.match(r"^\$\{([^}]*)\}", raw.strip())
                    calls.append({"path": norm, "raw": raw[:200], "base": base.group(1) if base else "", **_ev(root, p, i, line)})
    # FastAPI/Flask: include_router(module.router, prefix="/api/v1") prefixes every route of that module.
    for r in routes:
        if r["module"] in includes and includes[r["module"]]:
            r["path"] = includes[r["module"]].rstrip("/") + ("/" + r["path"].lstrip("/") if r["path"] not in ("", "/") else "")
    # A service does not depend on itself: drop calls that match its own routes.
    own = [normalize_path(r["path"]) for r in routes]
    calls = [c for c in calls if not any(o and path_match(c["path"], o) for o in own)]
    own_fns = {f["name"] for f in functions}
    rpc_calls = [c for c in rpc_calls if c["name"] not in own_fns]
    return {"path": str(root), "routes": routes, "functions": functions, "services": services, "calls": calls,
            "rpc_calls": rpc_calls, "config": config, "frontend_signals": frontend_signals}


def _package(root: Path) -> dict:
    try:
        return json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guess_kind(root: Path, ev: dict) -> str:
    pkg = _package(root)
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if any(k in deps for k in ("vue", "react", "svelte", "@angular/core", "vite", "next", "nuxt")) or ev["frontend_signals"] > 3:
        return "frontend"
    if not ev["routes"] and ((root / "index.html").exists() or (root / "public" / "index.html").exists()) and ev["calls"]:
        return "frontend"
    if len(ev["routes"]) >= 2:
        return "api"
    if len(ev["functions"]) >= 3:
        return "database"
    reqs = _read(root / "requirements.txt").lower() + _read(root / "pyproject.toml").lower()
    if any(w in reqs for w in ("celery", "rq", "kafka", "pika", "paho-mqtt", "apscheduler")):
        return "worker"
    if ev["services"] and not ev["routes"]:
        return "infra"
    return "library"


def guess_description(root: Path) -> str:
    pkg = _package(root)
    if pkg.get("description"):
        return truncate(pkg["description"], 300)
    for name in ("README.md", "readme.md", "README.rst", "README"):
        text = _read(root / name)
        if not text:
            continue
        for para in re.split(r"\n\s*\n", text):
            para = para.strip()
            if para and not para.startswith(("#", "!", "[", "<", "```", "|", "---", "=")):
                return truncate(re.sub(r"\s+", " ", para), 300)
    return ""


def guess_runs(root: Path) -> str:
    from . import environment, gitops
    parts = []
    dev = environment.dev_command(root)
    if dev:
        parts.append(f"run: {dev}")
    elif (root / "run.py").exists():
        parts.append("run: python run.py")
    checks = gitops.detect_verify(root)
    if checks:
        parts.append("test: " + "; ".join(checks))
    return " · ".join(parts)


def _common_prefix(paths: list[str]) -> str:
    split = [p.strip("/").split("/") for p in paths if p]
    if not split:
        return ""
    out = []
    for segs in zip(*split):
        if len(set(segs)) == 1 and segs[0] != "{}":
            out.append(segs[0])
        else:
            break
    return "/" + "/".join(out) if out else ""


def endpoint_group(norm: str) -> str:
    """'/api/v1/operation/status/{}' → '/api/v1/operation/*': the path up to its first distinctive segment."""
    segs = norm.strip("/").split("/")
    for i, seg in enumerate(segs):
        if seg != "{}" and seg not in GENERIC and not re.fullmatch(r"v\d+", seg):
            return "/" + "/".join(segs[:i + 1]) + ("/*" if i + 1 < len(segs) else "")
    return norm


def endpoint_groups(paths: list[str]) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    groups = [endpoint_group(p) for p in paths]
    for g in groups:
        if not g.endswith("/*") and g + "/*" in groups:
            g += "/*"  # '/api/items' belongs with '/api/items/{id}'
        counts[g] = counts.get(g, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _name_tokens(comp: dict) -> set:
    raw = " ".join([comp["id"], comp.get("name") or "", (comp.get("repo") or "").split("/")[-1]])
    return {t for t in re.split(r"[^a-z0-9]+", raw.lower()) if len(t) >= 4 and t not in NAME_NOISE}


def match_edges(components: list[dict], evidence: dict[str, dict]) -> list[dict]:
    """Proposed edges from call evidence to providers. `evidence` maps component id → scan_repo() output."""
    by_id = {c["id"]: c for c in components}
    found: dict[tuple, dict] = {}

    def add(src, dst, via, ev, key_detail):
        if src == dst:
            return
        e = found.setdefault((src, dst, via), {"from": src, "to": dst, "via": via, "evidence": [], "_details": set(), "score": 0})
        e["_details"].add(key_detail)
        e["score"] += 1
        group = endpoint_group(key_detail) if key_detail.startswith("/") else key_detail
        per_group = sum(1 for x in e["evidence"] if x.get("group") == group)
        if len(e["evidence"]) < MAX_EVIDENCE and per_group < 3 and not any(x["file"] == ev["file"] and x["line"] == ev["line"] for x in e["evidence"]):
            e["evidence"].append({**{k: ev[k] for k in ("file", "line", "text")}, "group": group, "matches": key_detail})

    providers = []
    for cid, ev in evidence.items():
        for r in ev["routes"]:
            norm = normalize_path(r["path"])
            if norm:
                providers.append((cid, norm, r))
    fn_owner: dict[str, set] = {}
    for cid, ev in evidence.items():
        for f in ev["functions"]:
            fn_owner.setdefault(f["name"], set()).add(cid)

    for cid, ev in evidence.items():
        for call in ev["calls"]:
            best, hits = 0, []
            for pid, norm, r in providers:
                if pid == cid:
                    continue
                s = path_match(call["path"], norm)
                if not s and call.get("base"):
                    # The base URL may already carry the API prefix: ${LIDAR_URL}/operation/start → /api/v1/operation/start.
                    s = path_match(norm, call["path"])
                    s = s if s >= 2 else 0
                if s > best:
                    best, hits = s, [(pid, norm)]
                elif s and s == best:
                    hits.append((pid, norm))
            owners = {pid for pid, _ in hits}
            if len(owners) > 1 and call.get("base"):
                # Several services expose the same path: the base variable's name decides (LIDAR_BASE → lidar-service).
                base = call["base"].lower()
                named = {pid for pid in owners if any(t in base for t in _name_tokens(by_id[pid]))}
                owners = named or owners
            for pid, norm in hits:
                if pid in owners:
                    add(cid, pid, "http", call, norm)
        for rc in ev["rpc_calls"]:
            for pid in fn_owner.get(rc["name"], ()):
                add(cid, pid, "sql", rc, rc["name"])
        # Config keys that name another component (LIDAR_API_URL → lidar-service).
        for cf in ev["config"]:
            key = cf["key"].lower()
            for pid, comp in by_id.items():
                if pid != cid and any(t in key for t in _name_tokens(comp)):
                    add(cid, pid, "http", cf, f"config {cf['key']}")

    out = []
    for e in found.values():
        details = sorted(e.pop("_details"))
        paths = [d for d in details if d.startswith("/")]
        cfg_keys = [d.removeprefix("config ") for d in details if d.startswith("config ")]
        names = [d for d in details if not d.startswith(("/", "config "))]
        if e["via"] == "http" and not paths and len(cfg_keys) == len(details) and e["score"] < 1:
            continue
        text = []
        if paths:
            groups = endpoint_groups(paths)
            text.append(", ".join(f"{g}" + (f" ({n})" if n > 1 else "") for g, n in groups[:5]) + (" endpoints" if len(groups) <= 5 else f" and {len(groups) - 5} more endpoint groups"))
        if names:
            pre = os.path.commonprefix(names)
            text.append(f"{pre}* SQL functions ({len(names)})" if len(pre) >= 3 and len(names) > 1 else ", ".join(names[:4]) + " (SQL)")
        if cfg_keys:
            text.append("base URL " + ", ".join(sorted(set(cfg_keys))[:3]))
        e["details"] = "; ".join(text)
        e["matched"] = details[:40]
        out.append(e)
    return out


# ----------------------------------------------------------------------------- scan into the map
def local_repositories(extra=()) -> list[Path]:
    from . import repos
    seen, out = set(), []
    for p in [*repos.candidate_repos(), *[Path(x) for x in extra if x]]:
        try:
            r = Path(p).resolve()
        except OSError:
            continue
        if r not in seen and (r / ".git").exists():
            seen.add(r)
            out.append(r)
    return out


def _component_id(path: Path, taken: set) -> str:
    base = safe_slug(path.name.lower(), 60) or "component"
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base}-{n}", n + 1
    return cid


def scan(paths=None, extra=()) -> dict:
    """Rescan local repositories, refresh components and proposed edges. Approved and rejected decisions stay."""
    from .github import remote_repo_name
    with _lock:
        data = load()
        targets = [Path(p).resolve() for p in paths] if paths else local_repositories(extra)
        # Every known component with a local clone takes part in matching, even when only some are rescanned.
        known = {str(Path(c["path"]).resolve()): c for c in data["components"] if c.get("path") and Path(c["path"]).is_dir()}
        for t in targets:
            known.setdefault(str(t), None)
        evidence: dict[str, dict] = {}
        taken = {c["id"] for c in data["components"]}
        for path_str in sorted(known):
            path = Path(path_str)
            if not (path / ".git").exists():
                continue
            comp = known[path_str] or for_repo(data, path)
            full = remote_repo_name(path) or ""
            if not comp:
                comp = {"id": _component_id(path, taken), "name": path.name, "repo": full, "path": str(path), "kind": "library",
                        "description": "", "runs": "", "provides": {}, "locked": [], "source": "scan"}
                taken.add(comp["id"])
                data["components"].append(comp)
            ev = scan_repo(path)
            evidence[comp["id"]] = ev
            locked = set(comp.get("locked") or [])
            comp["path"] = str(path)
            if "repo" not in locked and full:
                comp["repo"] = full
            if "kind" not in locked:
                comp["kind"] = guess_kind(path, ev)
            if "description" not in locked and not comp.get("description_agent"):
                comp["description"] = guess_description(path)
            if "runs" not in locked:
                comp["runs"] = guess_runs(path)
            comp["provides"] = {
                "endpoints": sorted({f"{r['method']} {normalize_path(r['path']) or r['path']}" for r in ev["routes"]})[:300],
                "sql_functions": sorted({(f["schema"] + "." if f["schema"] else "") + f["name"] for f in ev["functions"]})[:300],
                "services": sorted({s["name"] for s in ev["services"]})[:60],
            }
            comp["scan"] = {"time": now(), "calls": len(ev["calls"]), "routes": len(ev["routes"]), "functions": len(ev["functions"]),
                            "config_keys": sorted({c["key"] for c in ev["config"]})[:40]}
        proposed = match_edges(data["components"], evidence)
        index = {(e["from"], e["to"], e["via"]): e for e in data["edges"]}
        scanned = set(evidence)
        for p in proposed:
            key = (p["from"], p["to"], p["via"])
            cur = index.get(key)
            if cur:
                cur["evidence"] = p["evidence"]
                cur["matched"] = p["matched"]
                if cur.get("source") != "manual" and not cur.get("details_locked"):
                    cur["details"] = p["details"]
                cur["seen"] = now()
            else:
                row = {"id": new_id("edge"), "from": p["from"], "to": p["to"], "via": p["via"], "details": p["details"], "status": "proposed",
                       "evidence": p["evidence"], "matched": p["matched"], "source": "scan", "seen": now()}
                data["edges"].append(row)
                index[key] = row
        # A proposed edge whose evidence vanished from a rescanned repository is dropped; decisions are kept.
        live = {(p["from"], p["to"], p["via"]) for p in proposed}
        data["edges"] = [e for e in data["edges"] if e["status"] != "proposed" or e.get("source") == "manual"
                         or e["from"] not in scanned or (e["from"], e["to"], e["via"]) in live]
        data["scanned_at"] = now()
        return view(save(data))


def _stored_evidence(comp: dict) -> dict:
    """Provider-side evidence rebuilt from a component's stored `provides`, so one repository can be rescanned alone."""
    prov = comp.get("provides") or {}
    routes = []
    for ep in prov.get("endpoints") or []:
        method, _, path = str(ep).partition(" ")
        routes.append({"method": method, "path": path or method})
    fns = []
    for f in prov.get("sql_functions") or []:
        schema, _, name = str(f).rpartition(".")
        fns.append({"schema": schema, "name": name})
    return {"routes": routes, "functions": fns, "calls": [], "rpc_calls": [], "config": [], "services": []}


def rescan_repo(repo: str, path) -> dict:
    """Rescan ONE repository from a checkout that is not its component's clone (the knowledge refresh's shallow clone).

    Updates that component's `provides` and its outgoing proposed edges; the component's path, description and every
    approved or rejected decision stay. Returns what changed.
    """
    path = Path(path)
    with _lock:
        data = load()
        comp = find(data, repo)
        if not comp:
            return {"component": None, "note": "not in the system map"}
        ev = scan_repo(path)
        before = {k: set(v) for k, v in (comp.get("provides") or {}).items() if isinstance(v, list)}
        comp["provides"] = {
            "endpoints": sorted({f"{r['method']} {normalize_path(r['path']) or r['path']}" for r in ev["routes"]})[:300],
            "sql_functions": sorted({(f["schema"] + "." if f["schema"] else "") + f["name"] for f in ev["functions"]})[:300],
            "services": sorted({s["name"] for s in ev["services"]})[:60],
        }
        comp["scan"] = {"time": now(), "calls": len(ev["calls"]), "routes": len(ev["routes"]), "functions": len(ev["functions"]),
                        "config_keys": sorted({c["key"] for c in ev["config"]})[:40], "from": "knowledge refresh"}
        evidence = {c["id"]: _stored_evidence(c) for c in data["components"] if c["id"] != comp["id"]}
        evidence[comp["id"]] = ev
        proposed = [p for p in match_edges(data["components"], evidence) if p["from"] == comp["id"]]
        index = {(e["from"], e["to"], e["via"]): e for e in data["edges"]}
        added = []
        for p in proposed:
            key = (p["from"], p["to"], p["via"])
            cur = index.get(key)
            if cur:
                cur["evidence"], cur["matched"], cur["seen"] = p["evidence"], p["matched"], now()
                if cur.get("source") != "manual" and not cur.get("details_locked"):
                    cur["details"] = p["details"]
            else:
                row = {"id": new_id("edge"), "from": p["from"], "to": p["to"], "via": p["via"], "details": p["details"], "status": "proposed",
                       "evidence": p["evidence"], "matched": p["matched"], "source": "scan", "seen": now()}
                data["edges"].append(row)
                added.append(f"{p['from']} → {p['to']} ({p['via']})")
        live = {(p["from"], p["to"], p["via"]) for p in proposed}
        dropped = [f"{e['from']} → {e['to']} ({e['via']})" for e in data["edges"]
                   if e["from"] == comp["id"] and e["status"] == "proposed" and e.get("source") != "manual" and (e["from"], e["to"], e["via"]) not in live]
        data["edges"] = [e for e in data["edges"] if not (e["from"] == comp["id"] and e["status"] == "proposed" and e.get("source") != "manual"
                                                          and (e["from"], e["to"], e["via"]) not in live)]
        after = comp["provides"]
        diff = {k: {"added": len(set(after.get(k) or []) - before.get(k, set())), "removed": len(before.get(k, set()) - set(after.get(k) or []))}
                for k in after}
        save(data)
        return {"component": comp["id"], "provides": diff, "edges_added": added, "edges_dropped": dropped}


# ----------------------------------------------------------------------------- edits
def update_component(cid: str, patch: dict) -> dict:
    with _lock:
        data = load()
        c = component(data, cid)
        if not c:
            raise KeyError("Component not found")
        locked = set(c.get("locked") or [])
        for k in EDITABLE:
            if k in patch:
                v = str(patch[k] or "").strip()
                if k == "kind" and v not in KINDS:
                    raise ValueError(f"Kind must be one of {', '.join(KINDS)}")
                c[k] = v
                locked.add(k)
        c["locked"] = sorted(locked)
        return view(save(data))


def add_component(body: dict) -> dict:
    with _lock:
        data = load()
        name = str(body.get("name") or body.get("repo") or "").strip()
        if not name:
            raise ValueError("Give the component a name")
        taken = {c["id"] for c in data["components"]}
        cid = _component_id(Path(name.split("/")[-1]), taken)
        kind = body.get("kind") if body.get("kind") in KINDS else "library"
        data["components"].append({"id": cid, "name": name.split("/")[-1], "repo": str(body.get("repo") or "").strip(),
                                   "path": str(body.get("path") or "").strip(), "kind": kind, "description": str(body.get("description") or ""),
                                   "runs": str(body.get("runs") or ""), "provides": {}, "locked": list(EDITABLE), "source": "manual"})
        return view(save(data))


def remove_component(cid: str) -> dict:
    with _lock:
        data = load()
        data["components"] = [c for c in data["components"] if c["id"] != cid]
        data["edges"] = [e for e in data["edges"] if cid not in (e["from"], e["to"])]
        return view(save(data))


def add_edge(body: dict) -> dict:
    with _lock:
        data = load()
        src, dst = str(body.get("from") or ""), str(body.get("to") or "")
        if not component(data, src) or not component(data, dst) or src == dst:
            raise ValueError("Pick two different components")
        via = body.get("via") if body.get("via") in VIAS else "http"
        for e in data["edges"]:
            if (e["from"], e["to"], e["via"]) == (src, dst, via):
                e.update(status="approved", details=str(body.get("details") or e.get("details") or ""), details_locked=True)
                return view(save(data))
        data["edges"].append({"id": new_id("edge"), "from": src, "to": dst, "via": via, "details": str(body.get("details") or ""),
                              "status": "approved", "evidence": [], "source": "manual", "details_locked": True, "seen": now()})
        return view(save(data))


def set_edge(eid: str, patch: dict) -> dict:
    with _lock:
        data = load()
        e = next((x for x in data["edges"] if x["id"] == eid), None)
        if not e:
            raise KeyError("Dependency not found")
        if "status" in patch:
            if patch["status"] not in STATUSES:
                raise ValueError("Status must be proposed, approved or rejected")
            e["status"] = patch["status"]
            e["decided"] = now()
        if "details" in patch:
            e["details"] = str(patch["details"] or "")
            e["details_locked"] = True
        if "via" in patch and patch["via"] in VIAS:
            e["via"] = patch["via"]
        return view(save(data))


def remove_edge(eid: str) -> dict:
    with _lock:
        data = load()
        data["edges"] = [e for e in data["edges"] if e["id"] != eid]
        return view(save(data))


# ----------------------------------------------------------------------------- discovery
def discover(force=False) -> dict:
    """Repositories the gh account can reach, marked with whether Relay has a clone and a component for them."""
    from . import github
    rows = github.accessible_repos(force=force)
    data = load()
    local = {}
    for p in local_repositories():
        full = github.remote_repo_name(p)
        if full:
            local[full.lower()] = str(p)
    out = []
    for r in rows:
        comp = next((c for c in data["components"] if (c.get("repo") or "").lower() == r["repo"].lower()), None)
        out.append({**r, "local_path": local.get(r["repo"].lower(), ""), "component": comp["id"] if comp else ""})
    return {"repos": out, "root": str(github.clone_root())}


def clone_and_scan(names: list[str]) -> dict:
    from . import github
    cloned, errors = [], []
    for n in names[:30]:
        try:
            res = github.clone_repo(n)
            cloned.append({"repo": n, **res})
        except Exception as e:
            errors.append({"repo": n, "error": truncate(str(e), 300)})
    result = scan() if cloned else view()
    return {**result, "cloned": cloned, "errors": errors}


def ensure_local(ref: str) -> tuple[dict | None, str]:
    """A component (or owner/name) with a local clone, cloning it when needed. Returns (component, path)."""
    from . import github
    data = load()
    comp = find(data, ref)
    if comp and comp.get("path") and Path(comp["path"]).is_dir():
        return comp, comp["path"]
    spec = (comp or {}).get("repo") or ref
    if Path(str(ref)).is_dir() and (Path(ref) / ".git").exists():
        return comp, str(Path(ref).resolve())
    if "/" not in str(spec) or Path(str(spec)).is_absolute():
        raise ValueError(f"Relay has no local clone of {ref} and no GitHub name to clone it from")
    res = github.clone_repo(spec)
    if comp:
        with _lock:
            data = load()
            c = component(data, comp["id"])
            if c:
                c["path"] = res["path"]
                save(data)
    return comp, res["path"]


# ----------------------------------------------------------------------------- tasks
def related(repo_path, include_proposed=True) -> dict:
    """Repositories a task on `repo_path` probably also needs: approved edges first (pre-ticked), then proposed ones."""
    data = load()
    me = for_repo(data, repo_path)
    if not me:
        return {"component": None, "suggestions": []}
    out = {}
    for e in data["edges"]:
        if e["status"] == "rejected" or (e["status"] == "proposed" and not include_proposed):
            continue
        if me["id"] not in (e["from"], e["to"]):
            continue
        other = component(data, e["to"] if e["from"] == me["id"] else e["from"])
        if not other:
            continue
        direction = "depends on" if e["from"] == me["id"] else "is used by"
        reason = f"{me['name']} {direction} {other['name']} via {e['via']}" + (f": {e['details']}" if e.get("details") else "")
        cur = out.get(other["id"])
        row = {"component": other["id"], "name": other["name"], "repo": other.get("repo") or "", "path": other.get("path") or "",
               "cloned": bool(other.get("path") and Path(other["path"]).is_dir()), "kind": other.get("kind"),
               "status": e["status"], "reason": reason, "checked": e["status"] == "approved"}
        if not cur or (cur["status"] != "approved" and e["status"] == "approved"):
            out[other["id"]] = row
    rows = sorted(out.values(), key=lambda r: (not r["checked"], r["name"].lower()))
    return {"component": me["id"], "suggestions": rows}


def prompt_block(repo_paths: list, task_repos: list | None = None) -> str:
    """The slice of the map a supervisor needs: components the task's repositories are, and their neighbours."""
    data = load()
    if not data["components"]:
        return ""
    mine = [c for c in (for_repo(data, p) for p in repo_paths) if c]
    ids = {c["id"] for c in mine}
    edges = [e for e in data["edges"] if e["status"] != "rejected" and (e["from"] in ids or e["to"] in ids)]
    neighbour_ids = {e["from"] for e in edges} | {e["to"] for e in edges}
    lines = ["SYSTEM MAP (Relay's record of the system; approved dependencies are confirmed by a person, proposed ones come from a code scan)"]
    for cid in sorted(neighbour_ids | ids):
        c = component(data, cid)
        if not c:
            continue
        where = "IN THIS TASK" if cid in ids else ("NOT in this task, local clone " + c["path"] if c.get("path") and Path(c["path"]).is_dir()
                                                   else "NOT in this task, not cloned")
        lines.append(f"- {c['id']} ({c.get('kind') or 'component'}{', ' + c['repo'] if c.get('repo') else ''}) · {where}")
        if c.get("description"):
            lines.append(f"  {truncate(c['description'], 220)}")
        prov = c.get("provides") or {}
        if cid not in ids and prov.get("endpoints"):
            lines.append("  endpoints: " + truncate(", ".join(prov["endpoints"][:25]), 900))
        if cid not in ids and prov.get("sql_functions"):
            lines.append("  sql functions: " + truncate(", ".join(prov["sql_functions"][:25]), 600))
    if edges:
        lines.append("Dependencies:")
        for e in edges:
            lines.append(f"- {e['from']} → {e['to']} via {e['via']}: {e.get('details') or '(no details)'} [{e['status']}]")
    others = [c for c in data["components"] if c["id"] not in neighbour_ids | ids]
    if others:
        lines.append("Other known components: " + ", ".join(f"{c['id']} ({c.get('kind')})" for c in others[:30]))
    return "\n".join(lines)


# ----------------------------------------------------------------------------- optional agent descriptions
def describe_with_agent(cfg: dict, ids=None) -> dict:
    """One cheap agent turn that writes a one-sentence description per component from the scanned facts.

    Optional: scanning never needs it. Components a person edited keep their description.
    """
    from . import config as C, protocol, retro
    from .util import RUNTIME_DIR
    data = load()
    comps = [c for c in data["components"] if (not ids or c["id"] in ids) and "description" not in (c.get("locked") or [])]
    if not comps:
        return view(data)
    roles = cfg.get("roles") or {}
    agent = (roles.get("supervisor") or {}).get("agent") or ""
    if not agent or agent not in C.AGENTS:
        raise RuntimeError("No supervisor agent is configured, so descriptions come from each repository's README only.")
    model = ((cfg.get("subagent_models") or {}).get(agent) or "").strip()
    efforts = C.AGENTS.get(agent, {}).get("efforts") or []
    facts = []
    for c in comps:
        p = c.get("provides") or {}
        readme = guess_description(Path(c["path"])) if c.get("path") and Path(c["path"]).is_dir() else ""
        facts.append(f"- id: {c['id']} · kind: {c.get('kind')} · repo: {c.get('repo') or '(local)'}\n  readme: {truncate(readme, 300)}\n"
                     f"  endpoints: {truncate(', '.join((p.get('endpoints') or [])[:20]), 600)}\n"
                     f"  sql functions: {truncate(', '.join((p.get('sql_functions') or [])[:15]), 300)}\n  runs: {c.get('runs') or ''}")
    text = ("You describe software components for a system map. For each component below write ONE plain sentence (max 25 words) "
            "saying what it does for the system, from these facts only. Do not use tools.\n\n" + "\n".join(facts)
            + '\n\nReply with only a fenced json block: {"type":"descriptions","descriptions":{"<id>":"<sentence>"}}')
    res = retro.run_agent(agent, model, efforts[0] if efforts else "", text, cfg, RUNTIME_DIR / "systemmap", 240)
    env = protocol.parse_envelope(res.get("text") or "")
    if not env or not isinstance(env.get("descriptions"), dict):
        raise RuntimeError("The agent did not return descriptions: " + truncate(res.get("error") or res.get("text") or "", 300))
    with _lock:
        data = load()
        for c in data["components"]:
            d = env["descriptions"].get(c["id"])
            if d and "description" not in (c.get("locked") or []):
                c["description"] = truncate(str(d), 300)
                c["description_agent"] = True
        return view(save(data))
