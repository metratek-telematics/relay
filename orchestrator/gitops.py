"""Git worktree isolation, snapshots, diffs and delivery helpers."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .util import IS_WINDOWS, WORKTREES_DIR, quiet, safe_slug, truncate


def is_git_repo(path) -> bool:
    try:
        p = quiet(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, timeout=15)
        return p.returncode == 0 and "true" in (p.stdout or "")
    except Exception:
        return False


def repo_summary(path) -> dict:
    """Quick facts about a repository for the new-task wizard."""
    out = {"path": str(path), "is_git": is_git_repo(path), "branch": None, "remote": None, "dirty": 0,
           "detected_checks": [], "top_level": []}
    if not out["is_git"]:
        return out
    try:
        out["branch"] = quiet(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path).stdout.strip()
        out["remote"] = quiet(["git", "remote", "get-url", "origin"], cwd=path).stdout.strip() or None
        out["dirty"] = len([l for l in quiet(["git", "status", "--porcelain"], cwd=path).stdout.splitlines() if l.strip()])
    except Exception:
        pass
    try:
        out["top_level"] = sorted([p.name for p in Path(path).iterdir() if not p.name.startswith(".")])[:40]
    except Exception:
        pass
    out["detected_checks"] = detect_verify(path)
    return out


def detect_verify(repo) -> list[str]:
    repo = Path(repo)
    out = []
    if (repo / "package.json").exists():
        try:
            pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            pm = "pnpm" if (repo / "pnpm-lock.yaml").exists() else ("yarn" if (repo / "yarn.lock").exists() else "npm")
            for s in ["typecheck", "lint", "test", "build"]:
                if s in scripts:
                    out.append(f"npm run {s}" if pm == "npm" else f"{pm} {s}")
        except Exception:
            pass
    if (repo / "pom.xml").exists():
        out += ["mvn -q test"]
    if IS_WINDOWS and (repo / "gradlew.bat").exists():
        out += [r".\gradlew.bat test"]
    elif not IS_WINDOWS and (repo / "gradlew").exists():
        out += ["./gradlew test"]
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists() or (repo / "tests").is_dir():
        out += ["python -m pytest -q"]
    if (repo / "go.mod").exists():
        out += ["go test ./..."]
    if (repo / "Cargo.toml").exists():
        out += ["cargo test"]
    if (repo / "composer.json").exists() and (repo / "phpunit.xml").exists():
        out += ["vendor\\bin\\phpunit"]
    # dotnet
    if any(repo.glob("*.sln")) or any(repo.glob("*.csproj")):
        out += ["dotnet test"]
    return out


def snapshot_source(source, wt, run_dir, cfg, runner):
    """Carry the user's uncommitted work into the isolated worktree."""
    if not cfg.get("snapshot_working_tree", True):
        return
    patch = Path(run_dir) / "source.patch"
    with patch.open("wb") as f:
        p = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=source, stdout=f, stderr=subprocess.PIPE)
    if p.returncode == 0 and patch.exists() and patch.stat().st_size:
        a = subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch)], cwd=wt, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        if a.returncode != 0:
            raise RuntimeError("Could not apply local source snapshot:\n" + a.stderr)
        runner.timeline("git", "Local changes carried into worktree", f"{patch.stat().st_size} byte patch applied")
    else:
        patch.unlink(missing_ok=True)
    if cfg.get("copy_untracked_files", True):
        p = quiet(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=source)
        total = 0
        limit = int(cfg.get("max_untracked_copy_mb", 100)) * 1024 * 1024
        copied = 0
        for rel in [x for x in p.stdout.split("\0") if x][:2000]:
            s = Path(source) / rel
            if not s.is_file():
                continue
            if total + s.stat().st_size > limit:
                break
            d = Path(wt) / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            total += s.stat().st_size
            copied += 1
        if copied:
            runner.timeline("git", "Untracked files copied", f"{copied} file(s)")
    if cfg.get("copy_ignored_root_files", True):
        copy_ignored_root_files(source, wt, runner)


def copy_ignored_root_files(source, wt, runner=None) -> list[str]:
    """Copy small gitignored files from the repository's top level into the worktree.

    Projects keep what makes them run locally in gitignored files: `.env`,
    `.env.local`, dev auth cookies. A fresh worktree lacks them, so the app cannot
    reach its API and agents end up verifying against fake data. Only top-level
    regular files are copied, never directories, and Git still ignores them in the
    worktree, so they are never committed.
    """
    p = quiet(["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], cwd=source)
    copied = []
    for rel in p.stdout.splitlines():
        rel = rel.strip()
        if not rel or "/" in rel.rstrip("/") or rel.endswith("/"):
            continue  # top-level files only
        if rel.lower().endswith((".log", ".tmp", ".pid")):
            continue
        s = Path(source) / rel
        d = Path(wt) / rel
        try:
            if not s.is_file() or s.stat().st_size > 1024 * 1024 or d.exists():
                continue
            shutil.copy2(s, d)
            copied.append(rel)
        except OSError:
            continue
    if copied and runner:
        runner.timeline("git", "Local config copied into worktree", ", ".join(copied))
    return copied


def create_worktree(runner, task, cfg, run_dir):
    repo = Path(task["repo"]).resolve()
    if not is_git_repo(repo):
        raise RuntimeError(f"{repo} is not a Git repository. Initialize it with `git init` and make one commit first.")
    branch = f"{cfg.get('branch_prefix','agent')}/{safe_slug(task['name'], 40)}-{task['id'][-6:]}"
    wt = WORKTREES_DIR / f"{safe_slug(repo.name)}-{task['id'][-6:]}"
    if wt.exists():
        quiet(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, timeout=60)
        shutil.rmtree(wt, ignore_errors=True)
    quiet(["git", "worktree", "prune"], cwd=repo, timeout=60)
    # If the branch already exists (retry), reuse it.
    exists = quiet(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo).returncode == 0
    if exists:
        runner.run_shell_args(["git", "worktree", "add", str(wt), branch], cwd=repo, role="git", title="Attach worktree")
    else:
        runner.run_shell_args(["git", "worktree", "add", "-b", branch, str(wt), "HEAD"], cwd=repo, role="git", title="Create worktree")
    snapshot_source(repo, wt, run_dir, cfg, runner)
    return wt, branch


def remove_worktree(repo, wt) -> bool:
    try:
        if repo and Path(repo).exists():
            quiet(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, timeout=120)
            quiet(["git", "worktree", "prune"], cwd=repo, timeout=60)
        if Path(wt).exists():
            shutil.rmtree(wt, ignore_errors=True)
        return True
    except Exception:
        return False


def changed_files(wt) -> list:
    if not wt or not Path(wt).exists():
        return []
    p = quiet(["git", "status", "--short"], cwd=wt)
    out = []
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        out.append({"status": line[:2].strip() or "M", "path": line[3:].strip().strip('"')})
    return out


def diff_stat(wt) -> dict:
    if not wt or not Path(wt).exists():
        return {"files": 0, "insertions": 0, "deletions": 0}
    p = quiet(["git", "diff", "--shortstat", "HEAD"], cwd=wt)
    s = p.stdout.strip()
    import re
    files = int((re.search(r"(\d+) files? changed", s) or [0, 0])[1] or 0)
    ins = int((re.search(r"(\d+) insertions?", s) or [0, 0])[1] or 0)
    dele = int((re.search(r"(\d+) deletions?", s) or [0, 0])[1] or 0)
    untracked = len([l for l in quiet(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout.splitlines() if l.strip()])
    return {"files": files + untracked, "insertions": ins, "deletions": dele, "untracked": untracked}


def diff_file(wt, path) -> str:
    if not wt:
        return ""
    p = quiet(["git", "diff", "HEAD", "--", path], cwd=wt, timeout=30)
    if p.stdout.strip():
        return p.stdout
    fp = Path(wt) / path
    if fp.is_file():
        try:
            txt = fp.read_text(encoding="utf-8")
            return "".join(f"+{l}\n" for l in txt.splitlines()[:2000])
        except Exception:
            return "<binary or unreadable file>"
    return ""


def full_diff(wt, limit=60000) -> str:
    if not wt or not Path(wt).exists():
        return ""
    p = quiet(["git", "diff", "HEAD"], cwd=wt, timeout=60)
    txt = p.stdout or ""
    untracked = [l for l in quiet(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout.splitlines() if l.strip()]
    if untracked:
        txt += "\n\nUNTRACKED FILES:\n" + "\n".join(untracked)
    return truncate(txt, limit)


def commit_all(runner, wt, message) -> bool:
    status = runner.run_shell_args(["git", "status", "--porcelain"], cwd=wt, role="git", title="Check working tree", quiet_output=True).strip()
    if not status:
        return False
    runner.run_shell_args(["git", "add", "-A"], cwd=wt, role="git", title="Stage changes", quiet_output=True)
    runner.run_shell_args(["git", "commit", "-q", "-m", message], cwd=wt, role="git", title="Commit")
    return True


def push_branch(runner, wt, branch):
    runner.run_shell_args(["git", "push", "-u", "origin", branch], cwd=wt, role="git", title="Push branch")


def log_since_base(wt, n=20) -> str:
    p = quiet(["git", "log", f"-{n}", "--oneline"], cwd=wt)
    return p.stdout
