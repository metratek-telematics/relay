"""GitHub CLI helpers: auth, issue intake, draft pull requests."""
from __future__ import annotations

import json
from pathlib import Path

from .util import GITHUB_SOURCES_FILE, MANAGED_REPOS_DIR, quiet, read_json, safe_slug, which, write_json


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
    for prefix in ("https://github.com/", "http://github.com/", "git@github.com:"):
        if value.startswith(prefix):
            value = value[len(prefix):]
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
