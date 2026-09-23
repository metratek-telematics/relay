"""GitHub CLI helpers: auth, issue intake, draft pull requests."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from .util import GITHUB_SOURCES_FILE, MANAGED_REPOS_DIR, quiet, read_json, safe_slug, truncate, which, write_json


def gh_json(args, cwd=None, timeout=60):
    if not which("gh"):
        raise RuntimeError("GitHub CLI (gh) is not installed.")
    p = quiet(["gh", *args], cwd=cwd, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(((p.stdout or "") + (p.stderr or "")).strip())
    raw = (p.stdout or "").strip()
    return json.loads(raw) if raw else None


def gh_text(args, cwd=None, timeout=60):
    if not which("gh"):
        raise RuntimeError("GitHub CLI (gh) is not installed.")
    p = quiet(["gh", *args], cwd=cwd, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(((p.stdout or "") + (p.stderr or "")).strip())
    return (p.stdout or "").strip()


def auth_info() -> dict:
    if not which("gh"):
        return {"ready": False, "login": None, "error": "GitHub CLI (gh) is not installed"}
    try:
        login = gh_text(["api", "user", "--jq", ".login"], timeout=30)
        return {"ready": True, "login": login, "error": None}
    except Exception as e:
        return {"ready": False, "login": None, "error": str(e)[:300]}


def load_sources() -> list:
    return read_json(GITHUB_SOURCES_FILE, []) or []


def save_sources(rows) -> None:
    write_json(GITHUB_SOURCES_FILE, rows)


def normalize_repo_full_name(value) -> str:
    value = (value or "").strip()
    lowered = value.lower()
    for prefix in ("https://github.com/", "http://github.com/", "https://www.github.com/", "http://www.github.com/",
                   "ssh://git@github.com/", "git@github.com:", "github.com/", "www.github.com/"):
        if lowered.startswith(prefix):
            value = value[len(prefix):]
            # A pasted browser URL often points inside the repository (/issues/12, /tree/main).
            value = "/".join(value.split("?")[0].split("#")[0].split("/")[:2])
            break
    if value.endswith(".git"):
        value = value[:-4]
    return value.strip("/")


def remote_repo_name(repo):
    try:
        p = quiet(["git", "remote", "get-url", "origin"], cwd=repo)
        if p.returncode != 0:
            return None
        url = p.stdout.strip()
        if "github.com" not in url:
            return None
        return normalize_repo_full_name(url.split("github.com", 1)[1].lstrip(":/"))
    except Exception:
        return None


def ensure_local_repo(repo_full, local_path, runner=None):
    repo_full = normalize_repo_full_name(repo_full)
    if local_path and Path(local_path).exists():
        return Path(local_path).resolve()
    target = MANAGED_REPOS_DIR / safe_slug(repo_full.replace("/", "__"))
    if target.exists() and (target / ".git").exists():
        return target.resolve()
    p = quiet(["gh", "repo", "clone", repo_full, str(target)], cwd=MANAGED_REPOS_DIR, timeout=600)
    if p.returncode != 0:
        raise RuntimeError((p.stdout or "") + (p.stderr or ""))
    return target.resolve()


def issue_candidates(source) -> list:
    repo = normalize_repo_full_name(source.get("repo", ""))
    if not repo:
        return []
    found = {}
    fields = "number,title,body,url,labels,assignees"
    if source.get("assigned_to_me", True):
        try:
            for x in gh_json(["issue", "list", "--repo", repo, "--state", "open", "--assignee", "@me",
                              "--limit", "100", "--json", fields]) or []:
                found[x["number"]] = x
        except Exception:
            pass
    label = (source.get("label") or "").strip()
    if source.get("watch_label", True) and label:
        try:
            for x in gh_json(["issue", "list", "--repo", repo, "--state", "open", "--label", label,
                              "--limit", "100", "--json", fields]) or []:
                found[x["number"]] = x
        except Exception:
            pass
    return list(found.values())


def issue_view(number, cwd) -> dict:
    return gh_json(["issue", "view", str(number), "--json", "title,body,url,number,labels"], cwd=cwd) or {}


def pr_for_branch(repo_full, branch):
    try:
        rows = gh_json(["pr", "list", "--repo", repo_full, "--head", branch, "--state", "open",
                        "--json", "number,url,title,isDraft"]) or []
        return rows[0] if rows else None
    except Exception:
        return None


def pr_state(repo_full, number):
    """Merge state of one pull request, or None when GitHub is unavailable."""
    if not repo_full or not number:
        return None
    try:
        return gh_json(["pr", "view", str(number), "--repo", repo_full,
                        "--json", "state,mergedAt,mergeCommit"])
    except Exception:
        return None


def create_pr(runner, wt, repo_full, branch, title, body_file, base="", draft=True) -> dict:
    args = ["gh", "pr", "create", "--repo", repo_full, "--head", branch, "--title", title, "--body-file", str(body_file)]
    if base:
        args += ["--base", base]
    if draft:
        args.append("--draft")
    out = runner.run_shell_args(args, cwd=wt, role="github", title="Create draft pull request").strip()
    url = out.splitlines()[-1].strip() if out else None
    data = {"url": url, "number": None}
    try:
        pr = gh_json(["pr", "view", url, "--json", "number,url,isDraft,title"], cwd=wt)
        data["number"] = pr.get("number")
        data["url"] = pr.get("url") or url
    except Exception:
        pass
    return data


def clone_root() -> Path:
    """Where the New task wizard clones to.

    RELAY_REPOS is the folder the operator already chose for repositories (and in
    Docker it is the one mounted at the same path as the host), so clones land
    beside the repositories Relay already browses.
    """
    raw = os.environ.get("RELAY_REPOS") or os.environ.get("RELAY_BROWSE_ROOT")
    root = Path(raw).expanduser() if raw else MANAGED_REPOS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


_repo_cache = {"at": 0.0, "rows": []}


def accessible_repos(force=False) -> list:
    """Repositories the signed-in gh account owns or reaches through its organisations."""
    if not force and _repo_cache["rows"] and time.time() - _repo_cache["at"] < 300:
        return _repo_cache["rows"]
    owners = [gh_text(["api", "user", "--jq", ".login"], timeout=30)]
    try:
        owners += [o for o in gh_text(["api", "user/orgs", "--paginate", "--jq", ".[].login"], timeout=60).splitlines() if o]
    except RuntimeError:
        pass  # a token without read:org still lists the user's own repositories
    rows, seen = [], set()
    for owner in owners:
        try:
            found = gh_json(["repo", "list", owner, "--limit", "400", "--json", "nameWithOwner,description,isPrivate,updatedAt"], timeout=90) or []
        except RuntimeError:
            continue
        for r in found:
            if r["nameWithOwner"] not in seen:
                seen.add(r["nameWithOwner"])
                rows.append({"repo": r["nameWithOwner"], "description": r.get("description") or "",
                             "private": bool(r.get("isPrivate")), "updated": r.get("updatedAt") or ""})
    rows.sort(key=lambda r: r["updated"], reverse=True)
    _repo_cache.update(at=time.time(), rows=rows)
    return rows


def clone_repo(spec, name=None) -> dict:
    """Clone owner/repo or a git URL into clone_root(), reusing an existing checkout."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("Give a repository as owner/name or a git URL")
    is_github = "github.com" in spec or re.fullmatch(r"[\w.-]+/[\w.-]+", spec) is not None
    full = normalize_repo_full_name(spec) if is_github else spec
    base = (name or "").strip() or full.rstrip("/").split("/")[-1].split(":")[-1]
    if base.endswith(".git"):
        base = base[:-4]
    base = safe_slug(base, 100)
    if base in (".", "..") or base.startswith("."):
        raise ValueError("Invalid folder name")
    target = clone_root() / base
    if target.exists():
        if (target / ".git").exists():
            existing = remote_repo_name(target) if is_github else None
            if is_github and existing and existing.lower() != full.lower():
                raise ValueError(f"{target} already holds {existing}. Choose another folder name.")
            quiet(["git", "fetch", "--prune", "origin"], cwd=target, timeout=300)
            return {"path": str(target), "cloned": False}
        raise ValueError(f"{target} already exists and is not a git repository")
    if is_github:
        if "/" not in full:
            raise ValueError("Use owner/repository")
        args = ["gh", "repo", "clone", full, str(target)]
    else:
        # A leading dash would be read as a git option rather than a URL.
        if spec.startswith("-"):
            raise ValueError("Invalid repository URL")
        args = ["git", "clone", "--", spec, str(target)]
    p = quiet(args, cwd=clone_root(), timeout=900)
    if p.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(truncate(((p.stdout or "") + (p.stderr or "")).strip(), 800))
    return {"path": str(target), "cloned": True}
