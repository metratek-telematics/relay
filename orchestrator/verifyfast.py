"""Faster verification without proving less.

- A build runs only when something the build consumes changed (tests, docs and specs alone do not).
- Checks that do not write what other checks read (lint, tests, type checks) run side by side; a build runs alone.
- The same tree with the same commands is never verified twice: the fingerprint of the worktree keys the results.
- "Does it also fail on the starting commit?" is answered once per repository, commit and command, across tasks.
- node_modules is reused across tasks of a repository while its lockfile is unchanged (hard links from a warm copy,
  so a new worktree has its dependencies in about a second instead of a fresh `npm ci`).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .util import DATA_DIR

CACHE_DIR = DATA_DIR / "cache"
_lock = threading.Lock()

# ----------------------------------------------------------------------------- what a command is
_BUILD = re.compile(r"(^|\s|/)(npm run|pnpm|yarn)\s+(build|build:[\w-]+)\b|\bvite build\b|\bnext build\b|\bwebpack\b|\btsc\b(?!.*--noEmit)|"
                    r"\bgradlew?\b.*\b(assemble|build)\b|\bmvn\b.*\b(package|install)\b|\bcargo build\b|\bgo build\b|\bdotnet build\b|\bmake\b", re.I)
_LINTISH = re.compile(r"\blint\b|\beslint\b|\bprettier\b|\bruff\b|\bflake8\b|\bmypy\b|\btypecheck\b|--noEmit|\bstylelint\b", re.I)

# Changes a build never reads: tests, specs, docs and repository metadata.
_NOT_BUILD_INPUT = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|e2e|cypress|playwright|docs?)/|\.(spec|test)\.[cm]?[jt]sx?$|_test\.(py|go)$|(^|/)test_[^/]*\.py$|"
    r"\.(md|mdx|rst|txt)$|(^|/)\.github/|(^|/)(LICENSE|CHANGELOG|CONTRIBUTING)[^/]*$|(^|/)\.relay_mockups/|(^|/)\.orchestrator_refs/",
    re.I)


def is_build(cmd: str) -> bool:
    return bool(_BUILD.search(cmd or "")) and not _LINTISH.search(cmd or "")


def build_needed(changed_paths: list[str]) -> tuple[bool, list[str]]:
    """(needed, the paths that make it needed). No changes at all still builds (nothing to compare against)."""
    paths = [p for p in changed_paths if p]
    if not paths:
        return True, []
    inputs = [p for p in paths if not _NOT_BUILD_INPUT.search(p)]
    return bool(inputs), inputs[:5]


def plan_groups(cmds: list[str], parallel: bool) -> list[list[str]]:
    """Run order: independent checks together first, then each build on its own (a build rewrites files others read)."""
    if not parallel:
        return [[c] for c in cmds]
    others = [c for c in cmds if not is_build(c)]
    builds = [c for c in cmds if is_build(c)]
    groups = [others] if others else []
    groups += [[b] for b in builds]
    return groups


def run_group(cmds: list[str], fn, workers: int = 3) -> dict:
    """{cmd: fn(cmd)} running the commands of a group side by side."""
    if len(cmds) <= 1:
        return {c: fn(c) for c in cmds}
    out = {}
    errors = {}

    def one(c):
        try:
            out[c] = fn(c)
        except BaseException as e:  # Stopped must reach the caller
            errors[c] = e
    threads = [threading.Thread(target=one, args=(c,), daemon=True) for c in cmds[:max(1, workers)]]
    rest = cmds[max(1, workers):]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for c in rest:
        one(c)
    if errors:
        raise next(iter(errors.values()))
    return out


# ----------------------------------------------------------------------------- baseline results across tasks
def _baseline_file() -> Path:
    return CACHE_DIR / "baseline.json"


def baseline_get(repo: str, base: str, cmd: str) -> dict | None:
    try:
        data = json.loads(_baseline_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    return data.get(f"{repo}|{base}|{cmd}")


def baseline_put(repo: str, base: str, cmd: str, result: dict):
    with _lock:
        try:
            data = json.loads(_baseline_file().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data[f"{repo}|{base}|{cmd}"] = {**result, "time": time.time()}
        if len(data) > 500:  # keep the newest
            data = dict(sorted(data.items(), key=lambda kv: kv[1].get("time", 0))[-400:])
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _baseline_file().with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, _baseline_file())
        except Exception:
            pass


# ----------------------------------------------------------------------------- generated files
def _generated_file() -> Path:
    return CACHE_DIR / "generated_files.json"


def generated_get(repo: str) -> list[str]:
    try:
        return list(json.loads(_generated_file().read_text(encoding="utf-8")).get(str(repo)) or [])
    except Exception:
        return []


def generated_add(repo: str, paths: list[str]):
    """Tracked files Relay's own checks rewrote (build stamps, generated maps): learned per repository."""
    if not paths:
        return
    with _lock:
        try:
            data = json.loads(_generated_file().read_text(encoding="utf-8"))
        except Exception:
            data = {}
        cur = list(dict.fromkeys((data.get(str(repo)) or []) + list(paths)))[:40]
        data[str(repo)] = cur
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _generated_file().write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception:
            pass


# ----------------------------------------------------------------------------- dependency cache (node_modules)
def _lock_files(wt: Path) -> list[Path]:
    return [wt / n for n in ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "package.json", ".npmrc", ".nvmrc")
            if (wt / n).is_file()]


def deps_key(wt) -> str:
    """Hash of everything that decides what `npm ci` installs, plus the node version."""
    wt = Path(wt)
    locks = _lock_files(wt)
    if not any(p.name in ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock") for p in locks):
        return ""
    h = hashlib.sha1()
    for p in locks:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    try:
        node = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        node = ""
    h.update(node.encode())
    return h.hexdigest()[:20]


def _repo_slug(repo: str) -> str:
    return re.sub(r"[^\w.-]+", "_", str(Path(repo).resolve()))[-80:]


def deps_dir(repo: str, key: str) -> Path:
    return CACHE_DIR / "deps" / _repo_slug(repo) / key


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _link_tree(src: Path, dst: Path) -> bool:
    """cp -al (hard links: instant, no extra disk). Falls back to a copy across file systems."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".relay-tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    link = _same_fs(src, dst.parent)
    cmd = ["cp", "-al" if link else "-a", str(src), str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    if r.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return False
    os.replace(tmp, dst)
    return True


def deps_restore(repo: str, wt) -> dict | None:
    """Put a cached node_modules into a new worktree. {key, seconds} or None when there is no usable cache."""
    wt = Path(wt)
    if (wt / "node_modules").exists():
        return None
    key = deps_key(wt)
    if not key:
        return None
    src = deps_dir(repo, key) / "node_modules"
    if not (src / ".relay-complete").is_file():
        return None
    started = time.time()
    if not _link_tree(src, wt / "node_modules"):
        return None
    try:
        (wt / "node_modules" / ".relay-complete").unlink()
    except OSError:
        pass
    try:
        os.utime(deps_dir(repo, key), None)   # most recently used
    except OSError:
        pass
    return {"key": key, "seconds": round(time.time() - started, 1)}


def deps_save(repo: str, wt, keep: int = 3) -> dict | None:
    """After a successful install, keep a hard-linked copy for the next task (no extra disk on the same file system)."""
    wt = Path(wt)
    key = deps_key(wt)
    nm = wt / "node_modules"
    if not key or not nm.is_dir():
        return None
    target = deps_dir(repo, key) / "node_modules"
    if (target / ".relay-complete").is_file():
        return {"key": key, "cached": True}
    shutil.rmtree(target, ignore_errors=True)
    if not _link_tree(nm, target):
        return None
    # Tool caches inside node_modules belong to one worktree.
    for sub in (".cache", ".vite", ".vitest"):
        shutil.rmtree(target / sub, ignore_errors=True)
    (target / ".relay-complete").write_text(str(time.time()))
    # Prune: newest `keep` trees per repository.
    parent = deps_dir(repo, key).parent
    try:
        trees = sorted((p for p in parent.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in trees[max(1, keep):]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    return {"key": key, "cached": False}
