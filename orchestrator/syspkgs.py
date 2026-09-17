"""System (apt) packages a repository needs before its own install and tests can work.

Real evidence (docs/RUN_INSIGHTS.md): a Python repository imported `pyodbc`, whose shared library
`libodbc.so.2` comes from the `unixodbc` system package. The image did not have it, so every test
module that touched the database failed to import in three tasks in a row, the full suite never ran,
and verification still "passed" as a pre-existing failure.

This module:
  * validates apt package names saved in a repository environment (`system_packages`);
  * detects common needs from the repository's manifests (pyodbc -> unixodbc, psycopg2 -> libpq-dev …)
    so the environment editor can suggest them;
  * maps "cannot open shared object file" errors to the package that provides the library;
  * installs a package set once (dpkg is asked what is already there; apt runs only for what is missing),
    serialised across tasks, and explains clearly when Relay is not root and cannot install anything.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

MAX_PACKAGES = 40
# Debian policy: lowercase letters, digits, + - . ; at least two characters; starts with a letter or digit.
_PKG = re.compile(r"^[a-z0-9][a-z0-9+.-]{1,99}$")

_LOCK = threading.Lock()
_DONE: set[tuple] = set()  # package sets installed (or verified present) by this process


class PackageError(ValueError):
    pass


def parse(value) -> list[str]:
    """A list or a free-form string ("unixodbc, unixodbc-dev libpq-dev") into validated, de-duplicated names."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[\s,;]+", str(value))
    out = []
    for raw in items:
        name = str(raw or "").strip()
        if not name:
            continue
        if not _PKG.match(name):
            raise PackageError(f"Invalid system package name: {name!r} (apt names use lowercase letters, digits, '+', '-' and '.')")
        if name not in out:
            out.append(name)
    if len(out) > MAX_PACKAGES:
        raise PackageError(f"Too many system packages ({len(out)}); the limit is {MAX_PACKAGES}.")
    return out


# ----------------------------------------------------------------------------- detection
# Python distribution name (normalised: lowercase, _ and . become -) -> (apt packages, why)
PYTHON_NEEDS = {
    "pyodbc": (["unixodbc", "unixodbc-dev"], "pyodbc loads libodbc.so.2 from unixodbc"),
    "psycopg2": (["libpq-dev", "gcc", "python3-dev"], "psycopg2 (not -binary) builds against libpq"),
    "lxml": (["libxml2-dev", "libxslt1-dev"], "lxml needs libxml2 and libxslt when no wheel fits"),
    "pillow": (["libjpeg-dev", "zlib1g-dev"], "Pillow needs libjpeg and zlib when it builds from source"),
    "mysqlclient": (["default-libmysqlclient-dev", "pkg-config", "gcc", "python3-dev"], "mysqlclient builds against the MySQL client library"),
    "python-ldap": (["libldap2-dev", "libsasl2-dev", "gcc", "python3-dev"], "python-ldap builds against OpenLDAP"),
    "pycairo": (["libcairo2-dev", "pkg-config", "python3-dev"], "pycairo builds against cairo"),
    "cairosvg": (["libcairo2"], "CairoSVG loads libcairo at runtime"),
    "weasyprint": (["libpango-1.0-0", "libpangoft2-1.0-0"], "WeasyPrint loads Pango at runtime"),
    "pyaudio": (["portaudio19-dev", "gcc", "python3-dev"], "PyAudio builds against PortAudio"),
    "opencv-python": (["libgl1", "libglib2.0-0"], "opencv-python loads libGL and GLib at runtime"),
    "python-magic": (["libmagic1"], "python-magic loads libmagic at runtime"),
    "pyzbar": (["libzbar0"], "pyzbar loads libzbar at runtime"),
}
# npm package -> (apt packages, why)
NODE_NEEDS = {
    "canvas": (["build-essential", "libcairo2-dev", "libpango1.0-dev", "libjpeg-dev", "libgif-dev", "librsvg2-dev"],
               "node-canvas builds against cairo and pango"),
    "odbc": (["unixodbc", "unixodbc-dev", "build-essential"], "the odbc addon builds against unixODBC"),
    "oracledb": (["libaio1"], "node-oracledb thick mode loads libaio"),
}
# shared library (prefix, as printed by the loader) -> apt package that provides it
LIBRARY_PACKAGES = {
    "libodbc.so": "unixodbc", "libodbcinst.so": "unixodbc", "libpq.so": "libpq5", "libGL.so": "libgl1",
    "libgthread-2.0.so": "libglib2.0-0", "libglib-2.0.so": "libglib2.0-0", "libxml2.so": "libxml2", "libxslt.so": "libxslt1.1",
    "libjpeg.so": "libjpeg62-turbo", "libmagic.so": "libmagic1", "libzbar.so": "libzbar0", "libcairo.so": "libcairo2",
    "libpango-1.0.so": "libpango-1.0-0", "libaio.so": "libaio1", "libmysqlclient.so": "libmariadb3", "libmariadb.so": "libmariadb3",
    "libsndfile.so": "libsndfile1", "libportaudio.so": "libportaudio2", "libgomp.so": "libgomp1", "libsqlite3.so": "libsqlite3-0",
}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _python_requirements(repo: Path) -> dict[str, str]:
    """Distribution name -> manifest file, from requirements*.txt, pyproject.toml, setup.py/cfg and Pipfile."""
    found: dict[str, str] = {}

    def add(name, src):
        n = _norm(name)
        if n and n not in found:
            found[n] = src

    files = sorted(repo.glob("requirements*.txt")) + sorted(repo.glob("requirements/*.txt"))
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
            if m:
                add(m.group(1), f.relative_to(repo).as_posix())
    for name in ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile"):
        f = repo / name
        if not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Quoted requirement strings ("pyodbc>=5", 'lxml') and Pipfile keys (pyodbc = "*").
        for m in re.finditer(r"""["']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;].*?)?["']""", text):
            add(m.group(1), name)
        if name == "Pipfile":
            for m in re.finditer(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", text, re.M):
                add(m.group(1), name)
    return found


def _node_dependencies(repo: Path) -> dict[str, str]:
    f = repo / "package.json"
    try:
        pkg = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    deps = {}
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        for name in (pkg.get(key) or {}):
            deps.setdefault(name, "package.json")
    return deps


def detect(repo) -> list[dict]:
    """Suggestions like {"dependency": "pyodbc", "packages": ["unixodbc", "unixodbc-dev"], "reason": "...", "source": "requirements.txt"}."""
    repo = Path(repo)
    if not repo.is_dir():
        return []
    out = []
    py = _python_requirements(repo)
    for dep, (pkgs, why) in PYTHON_NEEDS.items():
        if dep in py:
            out.append({"dependency": dep, "packages": list(pkgs), "reason": why, "source": py[dep]})
    node = _node_dependencies(repo)
    for dep, (pkgs, why) in NODE_NEEDS.items():
        if dep in node:
            out.append({"dependency": dep, "packages": list(pkgs), "reason": why, "source": node[dep]})
    return out


def missing_libraries(text: str) -> list[dict]:
    """Shared libraries a loader could not find in some output, with the package that provides them."""
    out = []
    seen = set()
    for m in re.finditer(r"([A-Za-z0-9_.+-]+\.so(?:\.[0-9]+)*): cannot open shared object file", text or ""):
        lib = m.group(1)
        if lib in seen:
            continue
        seen.add(lib)
        pkg = next((p for prefix, p in LIBRARY_PACKAGES.items() if lib.startswith(prefix)), "")
        out.append({"library": lib, "package": pkg})
    return out


def missing_library_hint(text: str) -> str:
    libs = missing_libraries(text)
    if not libs:
        return ""
    parts = [f"{x['library']} (package {x['package']})" if x["package"] else x["library"] for x in libs]
    return ("missing system library: " + ", ".join(parts)
            + ". Add the package under Repositories → Environment → System packages and retry.")


# ----------------------------------------------------------------------------- installing
def installed(pkg: str) -> bool:
    from .util import quiet
    if not shutil.which("dpkg-query"):
        return False
    p = quiet(["dpkg-query", "-W", "-f=${db:Status-Abbrev}", pkg], timeout=30)
    return p.returncode == 0 and (p.stdout or "").startswith("ii")


def capability() -> dict:
    """Whether this Relay can install system packages, and why not when it cannot."""
    if os.name == "nt" or not shutil.which("apt-get"):
        return {"ok": False, "sudo": False,
                "reason": "apt-get is not available where Relay runs, so system packages cannot be installed. Install them on the host yourself."}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return {"ok": True, "sudo": False, "reason": ""}
    from .util import quiet
    if shutil.which("sudo") and quiet(["sudo", "-n", "true"], timeout=15).returncode == 0:
        return {"ok": True, "sudo": True, "reason": ""}
    uid = os.geteuid() if hasattr(os, "geteuid") else "?"
    return {"ok": False, "sudo": False,
            "reason": (f"Relay runs as a non-root user (uid {uid}) without passwordless sudo, so it cannot install system packages. "
                       "Add them to the image (apt-get install in the Dockerfile) or run the Relay container as root.")}


def _lists_stale(max_age_hours: float = 24) -> bool:
    lists = Path("/var/lib/apt/lists")
    try:
        entries = [p for p in lists.iterdir() if p.is_file() and p.name not in ("lock",)]
    except OSError:
        return True
    if not entries:
        return True
    newest = max(p.stat().st_mtime for p in entries)
    return time.time() - newest > max_age_hours * 3600


def install_command(missing: list[str], sudo: bool = False, update: bool = True) -> str:
    pre = "sudo -n " if sudo else ""
    steps = []
    if update:
        steps.append(f"{pre}apt-get update -q")
    steps.append(f"{pre}env DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends {' '.join(missing)}")
    return " && ".join(steps)


def ensure(packages, run_shell, timeout: float = 900) -> dict:
    """Install whatever of `packages` is missing. `run_shell(cmd, timeout)` returns {"ok", "output", "rc"}.

    Returns {"ok", "packages", "missing", "installed", "skipped", "reason", "command", "output"}.
    Only one install runs at a time across all tasks; a set already verified by this process is not re-checked.
    """
    pkgs = parse(packages)
    result = {"ok": True, "packages": pkgs, "missing": [], "installed": [], "skipped": False, "reason": "", "command": "", "output": ""}
    if not pkgs:
        result["skipped"] = True
        return result
    key = tuple(sorted(pkgs))
    with _LOCK:
        if key in _DONE and all(installed(p) for p in pkgs):
            result.update(skipped=True, reason="already installed")
            return result
        missing = [p for p in pkgs if not installed(p)]
        result["missing"] = missing
        if not missing:
            _DONE.add(key)
            result.update(skipped=True, reason="already installed")
            return result
        cap = capability()
        if not cap["ok"]:
            result.update(ok=False, reason=cap["reason"])
            return result
        cmd = install_command(missing, cap["sudo"], update=_lists_stale())
        result["command"] = cmd
        res = run_shell(cmd, timeout)
        if not res.get("ok") and "update" not in cmd:
            # Package lists may be outdated even when recent (a new package, a moved mirror): refresh once.
            cmd = install_command(missing, cap["sudo"], update=True)
            result["command"] = cmd
            res = run_shell(cmd, timeout)
        still = [p for p in missing if not installed(p)]
        if res.get("ok") and not still:
            _DONE.add(key)
            result["installed"] = missing
            return result
        out = (res.get("output") or "").strip()
        reason = ""
        m = re.search(r"E: (Unable to locate package [^\n]+|Package '[^']+' has no installation candidate|[^\n]+)", out)
        if m:
            reason = m.group(1).strip()
        result.update(ok=False, installed=[p for p in missing if p not in still], output=out[-1500:],
                      reason=reason or (f"apt-get exited with {res.get('rc')}" if not res.get("ok") else f"still missing: {', '.join(still)}"))
        return result
