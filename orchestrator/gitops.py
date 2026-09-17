"""Git worktree isolation, snapshots, diffs and delivery helpers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unicodedata
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


def has_python_tests(repo: Path) -> bool:
    # A bare tests/ folder is common in JavaScript repositories too; pytest there
    # collects nothing and exits 5, which used to read as a failed verification.
    if (repo / "pytest.ini").exists() or (repo / "conftest.py").exists():
        return True
    for name in ("pyproject.toml", "setup.cfg", "tox.ini"):
        p = repo / name
        if p.exists() and "pytest" in p.read_text(encoding="utf-8", errors="replace"):
            return True
    tests = repo / "tests"
    return tests.is_dir() and any(True for _ in (*tests.rglob("test_*.py"), *tests.rglob("*_test.py")))


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
    if has_python_tests(repo):
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
    # The repository's saved environment (Repositories → Environment) wins over whatever the clone had.
    from . import repo_env
    written = repo_env.apply_to_worktree(wt, repo_env.load(source))
    if written:
        runner.timeline("git", "Repository environment applied", ", ".join(written))


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


def create_worktree(runner, task, cfg, run_dir, repo=None, wt_path=None):
    """The task's isolated worktree and branch. `repo`/`wt_path` place a multi-repository task's other repositories."""
    repo = Path(repo or task["repo"]).resolve()
    if not is_git_repo(repo):
        raise RuntimeError(f"{repo} is not a Git repository. Initialize it with `git init` and make one commit first.")
    # Tasks created before readable names existed keep their original scheme so a retry reattaches.
    branch = task.get("branch_name") or f"{cfg.get('branch_prefix','agent')}/{safe_slug(task['name'], 40)}-{task['id'][-6:]}"
    wt = Path(wt_path) if wt_path else WORKTREES_DIR / f"{safe_slug(repo.name)}-{task['id'][-6:]}"
    if wt.exists():
        quiet(["git", "worktree", "remove", "--force", str(wt)], cwd=repo, timeout=60)
        shutil.rmtree(wt, ignore_errors=True)
    quiet(["git", "worktree", "prune"], cwd=repo, timeout=60)
    # If the branch already exists (retry), reuse it.
    exists = quiet(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo).returncode == 0
    if exists:
        holder = worktree_holding(repo, branch)
        managed = holder and WORKTREES_DIR.resolve() in Path(holder).resolve().parents
        if managed and Path(holder).resolve() != wt.resolve():
            # A follow-up builds on a finished task's branch, which is still checked out in that task's
            # worktree. Detaching it at the same commit leaves its files untouched and frees the branch.
            # Only Relay's own worktrees are touched, never the person's checkout.
            runner.run_shell_args(["git", "-C", holder, "switch", "--detach"], cwd=repo, role="git", title="Release branch from previous worktree")
        runner.run_shell_args(["git", "worktree", "add", str(wt), branch], cwd=repo, role="git", title="Attach worktree")
    else:
        runner.run_shell_args(["git", "worktree", "add", "-b", branch, str(wt), "HEAD"], cwd=repo, role="git", title="Create worktree")
    snapshot_source(repo, wt, run_dir, cfg, runner)
    return wt, branch


def worktree_holding(repo, branch) -> str:
    """The path of the worktree that has `branch` checked out, if any."""
    p = quiet(["git", "worktree", "list", "--porcelain"], cwd=repo)
    path = ""
    for line in (p.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return ""


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


def default_branch(repo) -> str:
    def git(*args):
        p = quiet(["git", *args], cwd=repo)
        return p.stdout.strip() if p.returncode == 0 else ""
    head = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head:
        return head.split("/", 1)[-1]
    for name in ("main", "master"):
        if git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}"):
            return name
    return git("rev-parse", "--abbrev-ref", "HEAD") or "main"


def base_commit(wt, repo=None) -> str:
    """The commit a task branched from, so its changes still show after Relay commits them."""
    if not wt or not Path(wt).exists():
        return ""
    if repo and Path(repo).exists():
        p = quiet(["git", "merge-base", "HEAD", default_branch(repo)], cwd=wt)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    return quiet(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()


def task_base(task) -> str:
    return task.get("base_commit") or base_commit(task.get("worktree"), task.get("repo"))


def changed_files(wt, base=None) -> list:
    if not wt or not Path(wt).exists():
        return []
    out = []
    if base:
        # Committed and uncommitted changes since the task began, plus new untracked files.
        for line in quiet(["git", "diff", "--name-status", base], cwd=wt).stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                out.append({"status": parts[0][:1] or "M", "path": parts[-1].strip('"')})
        for line in quiet(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout.splitlines():
            if line.strip():
                out.append({"status": "??", "path": line.strip().strip('"')})
        return out
    p = quiet(["git", "status", "--short"], cwd=wt)
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        out.append({"status": line[:2].strip() or "M", "path": line[3:].strip().strip('"')})
    return out


def diff_stat(wt, base=None) -> dict:
    if not wt or not Path(wt).exists():
        return {"files": 0, "insertions": 0, "deletions": 0}
    p = quiet(["git", "diff", "--shortstat", base or "HEAD"], cwd=wt)
    s = p.stdout.strip()
    import re
    files = int((re.search(r"(\d+) files? changed", s) or [0, 0])[1] or 0)
    ins = int((re.search(r"(\d+) insertions?", s) or [0, 0])[1] or 0)
    dele = int((re.search(r"(\d+) deletions?", s) or [0, 0])[1] or 0)
    untracked = len([l for l in quiet(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout.splitlines() if l.strip()])
    return {"files": files + untracked, "insertions": ins, "deletions": dele, "untracked": untracked}


def diff_file(wt, path, base=None) -> str:
    if not wt:
        return ""
    p = quiet(["git", "diff", base or "HEAD", "--", path], cwd=wt, timeout=30)
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


def full_diff(wt, limit=60000, base=None) -> str:
    if not wt or not Path(wt).exists():
        return ""
    p = quiet(["git", "diff", base or "HEAD"], cwd=wt, timeout=60)
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


def push_branch(runner, wt, branch, lease=""):
    if lease:
        # An agent pushed commits Relay then undid; replace exactly that remote state and nothing newer.
        runner.run_shell_args(["git", "push", f"--force-with-lease={branch}:{lease}", "-u", "origin", branch], cwd=wt, role="git",
                              title="Push branch (replacing commits an agent pushed)")
        return
    runner.run_shell_args(["git", "push", "-u", "origin", branch], cwd=wt, role="git", title="Push branch")


def log_since_base(wt, n=20) -> str:
    p = quiet(["git", "log", f"-{n}", "--oneline"], cwd=wt)
    return p.stdout


# ----------------------------------------------------------------------------- branch names
BRANCH_TYPES = {"feature": "feat", "bugfix": "fix", "refactor": "refactor", "tests": "test", "docs": "docs", "review": "chore"}

# Words that carry no meaning in a branch name. Prompts start with "please can you
# make…" far more often than with the thing being changed.
_FILLER = set("""a an the and or but of to in on at for from with by as into onto about via per
i we you it its it's this that these those my our your me us
need needs want wants would could should can will must shall may might please pls kindly
make makes making do does doing done implement implementing add adds adding create creating build
complete completely fully full proper properly better good great nice new really very just also
so then than is are be been being was were have has had get gets got use using follow following
some any all every each thing things stuff etc like md rules""".split())


def _slug_words(text: str, limit: int = 5) -> list[str]:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    keep = [w for w in words if w not in _FILLER and not (len(w) == 1 and not w.isdigit())]
    return (keep or words)[:limit]


# Lines people paste along with a request copied from a web page (seen as a pull request titled "Skip to content").
_BOILERPLATE = re.compile(r"^(skip to (main )?content|navigation menu|toggle navigation|sign in|sign up|menu|search|home|"
                          r"open (in )?(app|sidebar)|you signed (in|out).*|reload to refresh.*|dismiss alert|[-=*#_~`>|]+)$", re.I)


def auto_task_name(requirements: str, limit: int = 60) -> str:
    """A task name from the request: the first meaningful line, cut at a word boundary."""
    for raw in (requirements or "").splitlines():
        line = re.sub(r"^[#>*\-\s]+", "", raw).strip()
        line = re.sub(r"\s+", " ", line)
        if not line or _BOILERPLATE.match(line) or re.fullmatch(r"https?://\S+", line):
            continue
        if len(line) <= limit:
            return line
        cut = line[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
        return (cut if len(cut) >= limit // 2 else line[:limit]).rstrip() + "…"
    return ""


def branch_exists(repo, name: str) -> bool:
    if not repo or not Path(repo).exists():
        return False
    return quiet(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=repo).returncode == 0


def valid_branch_name(name: str) -> bool:
    # Run from a neutral directory: inside a broken or foreign checkout git fails
    # before it looks at the name, which would reject every branch.
    return bool(name) and quiet(["git", "check-ref-format", "--branch", name], cwd=tempfile.gettempdir()).returncode == 0


def suggest_branch(cfg: dict, name: str = "", requirements: str = "", template: str = "feature",
                   issue: str = "", repo=None, taken=()) -> str:
    """A short, readable branch name such as feat/berth-status-page or fix/123-login-timeout."""
    if (cfg.get("branch_naming") or "type") == "prefix":
        head = (cfg.get("branch_prefix") or "agent").strip("/")
    else:
        head = BRANCH_TYPES.get(template or "feature", "feat")
    words = _slug_words(name) or _slug_words(requirements) or ["task"]
    slug = "-".join(words)[:48].strip("-")
    if issue:
        slug = f"{issue}-{slug}"
    base = f"{head}/{slug}"
    candidate, n = base, 2
    taken = set(taken)
    while candidate in taken or branch_exists(repo, candidate):
        candidate, n = f"{base}-{n}", n + 1
    return candidate
