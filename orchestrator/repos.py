"""Repositories page: local repository status, Relay worktrees and a compact branch graph.

Everything here reads local git state. Nothing fetches from the network except
the explicit Fetch and Pull actions, so the page stays fast and works offline.
"""
from __future__ import annotations

import heapq
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gitops, multirepo
from .github import clone_root, normalize_repo_full_name
from .store import ACTIVE, WAITING
from .util import WORKTREES_DIR, quiet, truncate

MAX_REPOS = 200
MAX_GRAPH_BRANCHES = 12
MAX_BRANCH_COMMITS = 15
MAIN_COMMITS = 30
# Waiting tasks still hold a pipeline that resumes inside the worktree, so they
# are as untouchable as running ones.
BUSY = ACTIVE | WAITING

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="repos")


# ----------------------------------------------------------------------------- validation
def _no_option(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError(f"Missing {what}")
    # git would read a leading dash as an option rather than a name or path.
    if value.startswith("-"):
        raise ValueError(f"Invalid {what}")
    return value


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def repos_root() -> Path:
    # Same folder the clone endpoint writes to, so a fresh clone shows up here.
    return clone_root()


def resolve_repo(raw, extra_roots=()) -> Path:
    """A client-supplied repository path, allowed only inside the repositories root.

    extra_roots lets task repositories that live elsewhere (chosen by browsing in
    the New task wizard) be inspected too; the caller derives them from task
    records, never from the request.
    """
    p = Path(_no_option(str(raw or ""), "repository path")).expanduser().resolve()
    roots = [repos_root(), *[Path(r).resolve() for r in extra_roots]]
    if not (_inside(p, roots[0]) or p in roots[1:]):
        raise PermissionError("Repository is outside the repositories folder")
    if not (p / ".git").exists():
        raise FileNotFoundError(f"{p} is not a git repository")
    return p


def resolve_worktree(raw) -> Path:
    p = Path(_no_option(str(raw or ""), "worktree path")).expanduser().resolve()
    root = WORKTREES_DIR.resolve()
    if p == root or root not in p.parents:
        raise PermissionError("Path is not a Relay worktree")
    return p


def _git(cwd, *args, timeout=30):
    return quiet(["git", *args], cwd=cwd, timeout=timeout)


def _out(cwd, *args, timeout=30) -> str:
    try:
        p = _git(cwd, *args, timeout=timeout)
    except Exception:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _err(p) -> str:
    return truncate(((p.stderr or "") + (p.stdout or "")).strip(), 800)


def _same(a, b) -> bool:
    try:
        return bool(a) and bool(b) and Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def github_name(url: str):
    if not url or "github.com" not in url:
        return None
    return normalize_repo_full_name(url.split("github.com", 1)[1].lstrip(":/")) or None


# ----------------------------------------------------------------------------- repository list
def _status(path) -> dict:
    """Branch, upstream, ahead/behind and change count from one porcelain v2 call."""
    out = {"branch": None, "detached": False, "upstream": None, "ahead": 0, "behind": 0, "changes": 0, "untracked": 0}
    p = _git(path, "status", "--porcelain=v2", "--branch")
    if p.returncode != 0:
        out["error"] = _err(p)
        return out
    for line in p.stdout.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head "):]
            out["detached"] = head == "(detached)"
            out["branch"] = None if out["detached"] else head
        elif line.startswith("# branch.upstream "):
            out["upstream"] = line[len("# branch.upstream "):]
        elif line.startswith("# branch.ab "):
            m = re.match(r"# branch\.ab \+(\d+) -(\d+)", line)
            if m:
                out["ahead"], out["behind"] = int(m[1]), int(m[2])
        elif line.startswith("?"):
            out["untracked"] += 1
            out["changes"] += 1
        elif line and not line.startswith("#"):
            out["changes"] += 1
    return out


def _last_commit(cwd, ref="HEAD"):
    raw = _out(cwd, "log", "-1", "--format=%H%x00%h%x00%s%x00%ct%x00%an", ref, "--")
    parts = raw.split("\x00")
    if len(parts) < 5:
        return None
    return {"sha": parts[0], "short": parts[1], "subject": parts[2], "time": int(parts[3] or 0), "author": parts[4]}


def _git_dir(path: Path) -> Path:
    g = path / ".git"
    if g.is_file():
        found = _out(path, "rev-parse", "--absolute-git-dir")
        return Path(found) if found else g
    return g


def _worktree_entries(repo) -> list[dict]:
    rows, cur = [], None
    for line in _out(repo, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:], "head": None, "branch": None, "prunable": False, "locked": False, "detached": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].removeprefix("refs/heads/")
        elif line == "detached":
            cur["detached"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
        elif not line:
            rows.append(cur)
            cur = None
    return rows


def _relay_worktrees(repo) -> list[dict]:
    root = WORKTREES_DIR.resolve()
    out = []
    for e in _worktree_entries(repo):
        try:
            if root in Path(e["path"]).resolve().parents:
                out.append(e)
        except OSError:
            continue
    return out


def repo_tasks(path, tasks) -> list[dict]:
    # A multi-repository task belongs to every repository it changes.
    return [t for t in tasks if _same(t.get("repo"), path) or any(_same(p, path) for p in multirepo.task_repo_paths(t)[1:])]


def summarize(path: Path, tasks) -> dict:
    row = {"name": path.name, "path": str(path)}
    try:
        row.update(_status(path))
        remote = _out(path, "remote", "get-url", "origin")
        row["remote"] = remote or None
        row["github"] = github_name(remote)
        row["last_commit"] = _last_commit(path)
        fetch_head = _git_dir(path) / "FETCH_HEAD"
        row["last_fetch"] = int(fetch_head.stat().st_mtime) if fetch_head.exists() else None
        mine = repo_tasks(path, tasks)
        row["tasks"] = len(mine)
        row["active_tasks"] = len([t for t in mine if t.get("status") in BUSY])
        row["worktrees"] = len(_relay_worktrees(path))
    except Exception as e:  # one broken repository must not blank the whole page
        row["error"] = str(e)[:300]
    return row


def candidate_repos() -> list[Path]:
    root = repos_root()
    try:
        dirs = sorted((c for c in root.iterdir() if c.is_dir() and (c / ".git").exists()), key=lambda c: c.name.lower())
    except OSError:
        return []
    return [d.resolve() for d in dirs[:MAX_REPOS]]


def list_repos(tasks) -> dict:
    paths = candidate_repos()
    rows = list(_pool.map(lambda p: summarize(p, tasks), paths))
    return {"root": str(repos_root()), "repos": rows, "capped": len(paths) >= MAX_REPOS}


def fetch(path: Path, tasks) -> dict:
    p = _git(path, "fetch", "--prune", timeout=300)
    if p.returncode != 0:
        raise RuntimeError(_err(p) or "git fetch failed")
    return summarize(path, tasks)


def pull(path: Path, tasks) -> dict:
    st = _status(path)
    if st["detached"]:
        raise ValueError("HEAD is detached; check out a branch before pulling.")
    if not st["upstream"]:
        raise ValueError(f"{st['branch']} has no upstream branch to pull from.")
    # Untracked files are left alone by a fast-forward (git itself refuses if one
    # would be overwritten), but modified tracked files are the user's work.
    tracked = st["changes"] - st["untracked"]
    if tracked:
        raise ValueError(f"{tracked} uncommitted change(s) in {path.name}. Commit or stash them first; Relay only fast-forwards clean checkouts.")
    f = _git(path, "fetch", "--prune", timeout=300)
    if f.returncode != 0:
        raise RuntimeError(_err(f) or "git fetch failed")
    st = _status(path)
    if st["ahead"] and st["behind"]:
        raise ValueError(f"{st['branch']} has diverged from {st['upstream']} ({st['ahead']} ahead, {st['behind']} behind). "
                         "Merge or rebase it yourself; Relay only fast-forwards.")
    before = st["behind"]
    if before:
        m = _git(path, "merge", "--ff-only", "@{upstream}", timeout=120)
        if m.returncode != 0:
            raise RuntimeError(_err(m) or "Fast-forward failed")
    return {**summarize(path, tasks), "pulled": before}


# ----------------------------------------------------------------------------- worktrees
def _task_for(path, tasks):
    for t in tasks:
        if any(_same(w["worktree"], path) for w in multirepo.task_worktrees(t)):
            return t
    return None


def _task_brief(t):
    if not t:
        return None
    return {"id": t["id"], "name": t.get("name"), "status": t.get("status"), "archived": bool(t.get("archived"))}


def _remote_branches(repo) -> set:
    names = set()
    for ref in _out(repo, "for-each-ref", "--format=%(refname)", "refs/remotes/").splitlines():
        parts = ref.split("/", 3)  # refs, remotes, <remote>, <branch>
        if len(parts) == 4 and parts[3] != "HEAD":
            names.add(parts[3])
    return names


def _repo_facts(repo) -> dict:
    default = gitops.default_branch(repo)
    merged = set(_out(repo, "branch", "--format=%(refname:short)", "--merged", default).splitlines()) if default else set()
    return {"default": default, "merged": merged, "remote": _remote_branches(repo)}


def _wt_row(repo, e, facts, tasks, busy) -> dict:
    path = Path(e["path"])
    exists = path.is_dir()
    task = _task_for(path, tasks)
    branch = e["branch"]
    row = {"path": str(path), "name": path.name, "repo": str(repo), "repo_name": Path(repo).name, "branch": branch,
           "head": (e["head"] or "")[:10], "exists": exists, "prunable": e["prunable"] or not exists, "locked": e["locked"],
           "detached": e["detached"], "task": _task_brief(task), "busy": bool(task and busy(task)),
           "default_branch": facts["default"], "size": size_cached(path)}
    row["record_only"] = bool(e.get("record_only")) and not exists
    row["unregistered"] = bool(e.get("record_only")) and exists
    row["changes"] = _status(path)["changes"] if exists else 0
    row["branch_exists"] = bool(branch) and _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    ref = branch if row["branch_exists"] else e["head"]
    last = _last_commit(repo, ref) if ref else None
    row["last_commit"] = last
    row["pushed"] = bool(branch and branch in facts["remote"])
    row["merged"] = bool(branch and branch != facts["default"] and branch in facts["merged"])
    if row["branch_exists"] and facts["default"]:
        n = _out(repo, "rev-list", "--count", f"{facts['default']}..{branch}", "--")
        row["ahead"] = int(n) if n.isdigit() else 0
    else:
        row["ahead"] = 0
    return row


def _known_repos(tasks) -> list[Path]:
    seen, out = set(), []
    for p in candidate_repos() + [Path(r) for t in tasks for r in multirepo.task_repo_paths(t)]:
        try:
            r = p.resolve()
        except OSError:
            continue
        if r not in seen and (r / ".git").exists():
            seen.add(r)
            out.append(r)
    return out


def list_worktrees(tasks, busy) -> dict:
    jobs, facts = [], {}
    for repo in _known_repos(tasks):
        entries = _relay_worktrees(repo)
        if entries:
            facts[repo] = _repo_facts(repo)
            jobs += [(repo, e) for e in entries]
    root = WORKTREES_DIR.resolve()
    claimed = {Path(e["path"]).resolve() for _, e in jobs}
    # A task whose worktree git no longer lists (removed, or its folder deleted and
    # pruned) still has a branch worth judging: merged branches can be deleted from here.
    for t in tasks:
        for w in multirepo.task_worktrees(t):
            wt, repo = w["worktree"], w["repo"]
            if not wt or not repo:
                continue
            p = Path(wt).resolve()
            if p in claimed or root not in p.parents or not (Path(repo) / ".git").exists():
                continue
            claimed.add(p)
            repo = Path(repo).resolve()
            if repo not in facts:
                facts[repo] = _repo_facts(repo)
            jobs.append((repo, {"path": str(p), "head": None, "branch": w.get("branch"), "prunable": True,
                                "locked": False, "detached": False, "record_only": True}))
    rows = list(_pool.map(lambda j: _wt_row(j[0], j[1], facts[j[0]], tasks, busy), jobs))
    # Folders git does not know about (a repository was moved, or git metadata was lost) still take disk space.
    try:
        strays = [c for c in root.iterdir() if c.is_dir() and c.resolve() not in claimed]
    except OSError:
        strays = []
    for c in strays:
        t = _task_for(c, tasks)
        rows.append({"path": str(c), "name": c.name, "repo": t.get("repo") if t else None,
                     "repo_name": Path(t["repo"]).name if t and t.get("repo") else None, "branch": t.get("branch") if t else None,
                     "head": "", "exists": True, "prunable": False, "unregistered": True, "locked": False, "detached": False,
                     "task": _task_brief(t), "busy": bool(t and busy(t)), "changes": 0, "last_commit": None, "branch_exists": False,
                     "pushed": False, "merged": False, "ahead": 0, "size": size_cached(c), "default_branch": None})
    rows.sort(key=lambda r: ((r.get("last_commit") or {}).get("time") or 0), reverse=True)
    return {"root": str(root), "worktrees": rows}


def _find_row(path: Path, tasks, busy) -> dict:
    for r in list_worktrees(tasks, busy)["worktrees"]:
        if _same(r["path"], path):
            return r
    raise KeyError("Worktree not found")


def remove_worktree(path: Path, tasks, busy, discard=False) -> dict:
    row = _find_row(path, tasks, busy)
    if row["busy"]:
        raise PermissionError(f"Task \"{row['task']['name']}\" is {row['task']['status']}; stop it before removing its worktree.")
    if row["changes"] and not discard:
        raise ValueError(f"{row['changes']} uncommitted change(s) in {row['name']}. Tick \"discard changes\" to remove it anyway.")
    if row.get("record_only"):
        # Nothing on disk; prune so git forgets the stale entry, if the repository is still here.
        if row.get("repo") and Path(row["repo"]).exists():
            _git(row["repo"], "worktree", "prune", timeout=60)
    elif not gitops.remove_worktree(row.get("repo"), path):
        raise RuntimeError("Could not remove the worktree")
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _sizes.pop(str(path), None)
    return {"ok": True, "path": str(path), "branch": row.get("branch"), "repo": row.get("repo")}


def delete_branch(repo: Path, branch: str, tasks, busy) -> dict:
    branch = _no_option(branch, "branch name")
    if _git(repo, "check-ref-format", "--branch", branch).returncode != 0:
        raise ValueError("Invalid branch name")
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
        raise KeyError(f"Branch {branch} not found")
    default = gitops.default_branch(repo)
    if branch == default:
        raise ValueError("The default branch cannot be deleted here")
    for t in repo_tasks(repo, tasks):
        if (t.get("branch") or t.get("branch_name")) == branch and busy(t):
            raise PermissionError(f"Task \"{t.get('name')}\" is still using {branch}")
    _git(repo, "worktree", "prune", timeout=60)  # a deleted worktree folder would otherwise still pin the branch
    for e in _worktree_entries(repo):
        if e["branch"] == branch and Path(e["path"]).exists():
            raise ValueError(f"{branch} is checked out in {e['path']}. Remove that worktree first.")
    if _git(repo, "merge-base", "--is-ancestor", f"refs/heads/{branch}", default).returncode != 0:
        raise ValueError(f"{branch} is not merged into {default}; Relay only deletes merged branches.")
    p = _git(repo, "branch", "-D", "--", branch)
    if p.returncode != 0:
        raise RuntimeError(_err(p) or "git branch -D failed")
    return {"ok": True, "branch": branch}


def cleanup_candidates(tasks, busy) -> list[dict]:
    return [r for r in list_worktrees(tasks, busy)["worktrees"]
            if r.get("task") and r["task"]["status"] == "done" and r["merged"] and not r["busy"] and r["exists"]
            and not r["changes"] and not r.get("unregistered")]


def cleanup(paths, tasks, busy) -> dict:
    """Remove the confirmed worktrees that are still eligible when the request arrives."""
    wanted = {str(resolve_worktree(p)) for p in paths or []}
    removed, skipped = [], []
    eligible = {str(Path(r["path"]).resolve()): r for r in cleanup_candidates(tasks, busy)}
    for p in sorted(wanted):
        r = eligible.get(p)
        if not r:
            skipped.append({"path": p, "reason": "no longer eligible"})
            continue
        if gitops.remove_worktree(r["repo"], p):
            _sizes.pop(p, None)
            removed.append(p)
        else:
            skipped.append({"path": p, "reason": "git could not remove it"})
    return {"removed": removed, "skipped": skipped}


# ----------------------------------------------------------------------------- disk size
_sizes: dict[str, tuple[float, int]] = {}
_sizes_lock = threading.Lock()
SIZE_TTL = 600


def size_cached(path):
    hit = _sizes.get(str(path))
    return hit[1] if hit and time.time() - hit[0] < SIZE_TTL else None


def disk_size(path: Path) -> int:
    """Bytes on disk, walked without following symlinks, cached for SIZE_TTL.

    Worktrees often hold node_modules or build output, so the walk is only done
    when the page asks for one row at a time, never while listing.
    """
    cached = size_cached(path)
    if cached is not None:
        return cached
    total, stack = 0, [str(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    with _sizes_lock:
        _sizes[str(path)] = (time.time(), total)
    return total


# ----------------------------------------------------------------------------- branch graph
def _commits(repo, *args) -> list[dict]:
    raw = _out(repo, "log", "--format=%H%x00%h%x00%s%x00%ct%x00%an%x00%P", *args, timeout=60)
    rows = []
    for line in raw.splitlines():
        parts = line.split("\x00")
        if len(parts) >= 6:
            rows.append({"sha": parts[0], "short": parts[1], "subject": parts[2], "time": int(parts[3] or 0),
                         "author": parts[4], "parents": parts[5].split()})
    return rows


def branch_graph(repo: Path, tasks) -> dict:
    """The default branch's recent first-parent line with local branches forking off and joining back.

    Columns are a topological order that prefers commit time, so a branch commit
    always sits right of its fork point and left of the merge that brought it in.
    Lanes are packed greedily so branches that do not overlap share a row.
    """
    default = gitops.default_branch(repo)
    if _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{default}").returncode != 0:
        return {"default": default, "nodes": [], "edges": [], "branches": [], "lanes": 1, "columns": 0, "empty": True}
    main = list(reversed(_commits(repo, "--first-parent", f"-{MAIN_COMMITS}", f"refs/heads/{default}", "--")))
    on_main = {c["sha"]: i for i, c in enumerate(main)}
    oldest = main[0]["sha"] if main else None

    by_branch = {}
    for t in repo_tasks(repo, tasks):
        b = t.get("branch") or t.get("branch_name")
        if b:
            by_branch.setdefault(b, t)
    refs = []
    for line in _out(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)%00%(objectname)", "refs/heads/").splitlines():
        name, _, sha = line.partition("\x00")
        if name and name != default:
            refs.append((name, sha))
    # Task branches first so a busy repository never pushes Relay's own work off the graph.
    refs.sort(key=lambda r: r[0] not in by_branch)
    hidden = max(0, len(refs) - MAX_GRAPH_BRANCHES)
    refs = refs[:MAX_GRAPH_BRANCHES]

    branches = []
    for name, tip in refs:
        info = {"name": name, "tip": tip[:10], "task": _task_brief(by_branch.get(name)), "merged": False,
                "fork": None, "join": None, "commits": [], "more": 0, "fork_outside": False, "at": None}
        ref = f"refs/heads/{name}"
        if _git(repo, "merge-base", "--is-ancestor", ref, f"refs/heads/{default}").returncode == 0:
            info["merged"] = True
            chain = _out(repo, "rev-list", "--first-parent", "--ancestry-path", f"{ref}..refs/heads/{default}").splitlines()
            if tip in on_main:
                info["at"] = tip  # fast-forwarded or never committed: the tip is on the main line itself
            elif chain and chain[-1] in on_main:
                join = chain[-1]
                p1 = next(c for c in main if c["sha"] == join)["parents"][0]
                info["join"] = join
                info["fork"] = _out(repo, "merge-base", p1, ref)
                info["commits"] = list(reversed(_commits(repo, f"-{MAX_BRANCH_COMMITS}", f"{p1}..{ref}", "--")))
            else:
                continue  # merged before the visible window; nothing to draw
        else:
            info["fork"] = _out(repo, "merge-base", f"refs/heads/{default}", ref)
            count = _out(repo, "rev-list", "--count", f"refs/heads/{default}..{ref}")
            info["commits"] = list(reversed(_commits(repo, f"-{MAX_BRANCH_COMMITS}", f"refs/heads/{default}..{ref}", "--")))
            info["more"] = max(0, (int(count) if count.isdigit() else 0) - len(info["commits"]))
        if info["fork"] and info["fork"] not in on_main:
            info["fork_outside"] = True
            info["fork"] = oldest
        branches.append(info)

    # Topological column order, earliest commit time first among ready nodes.
    nodes, after = {}, {}
    for i, c in enumerate(main):
        nodes[c["sha"]] = {**c, "lane": 0, "kind": "merge" if len(c["parents"]) > 1 else "main"}
        if i:
            after.setdefault(c["sha"], set()).add(main[i - 1]["sha"])
    for b in branches:
        prev = b["fork"]
        for c in b["commits"]:
            if c["sha"] in nodes:
                continue  # shared with an earlier branch
            nodes[c["sha"]] = {**c, "lane": None, "kind": "branch", "branch": b["name"]}
            if prev:
                after.setdefault(c["sha"], set()).add(prev)
            prev = c["sha"]
        if b["join"] and prev:
            after.setdefault(b["join"], set()).add(prev)
    indeg = {s: len([p for p in after.get(s, ()) if p in nodes]) for s in nodes}
    children = {}
    for s, ps in after.items():
        for p in ps:
            if p in nodes and s in nodes:
                children.setdefault(p, []).append(s)
    ready = [(n["time"], s) for s, n in nodes.items() if indeg[s] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, s = heapq.heappop(ready)
        order.append(s)
        for ch in children.get(s, ()):
            indeg[ch] -= 1
            if indeg[ch] == 0:
                heapq.heappush(ready, (nodes[ch]["time"], ch))
    for x, s in enumerate(order):
        nodes[s]["x"] = x

    # Lane packing over each branch's column span, latest fork first: a branch that
    # forks earlier then sits further from the main line, so its fork curve never
    # has to cross a lane that is already busy at that column.
    lanes: list[list[tuple[int, int]]] = []
    edges = []
    for i in range(1, len(main)):
        edges.append({"from": main[i - 1]["sha"], "to": main[i]["sha"], "kind": "main"})

    def own_commits(b):
        return [c["sha"] for c in b["commits"] if nodes.get(c["sha"], {}).get("branch") == b["name"]]

    def span(b):
        own = own_commits(b)
        if not own:
            return None
        start = nodes[b["fork"]]["x"] if b["fork"] in nodes else nodes[own[0]]["x"]
        # A merged lane frees up where it joins; an open one keeps room for its tip label (~4 characters a column).
        end = nodes[b["join"]]["x"] if b["join"] else nodes[own[-1]]["x"] + 1 + (len(b["name"]) + 14) // 4
        return start, end

    for b in sorted(branches, key=lambda b: -(span(b) or (-1, 0))[0]):
        own = own_commits(b)
        sp = span(b)
        if not sp:
            b["lane"] = 0
            continue
        lane = next((i for i, used in enumerate(lanes) if all(sp[1] < s0 or sp[0] > e0 for s0, e0 in used)), None)
        if lane is None:
            lanes.append([])
            lane = len(lanes) - 1
        lanes[lane].append(sp)
        b["lane"] = lane + 1
        for s in own:
            nodes[s]["lane"] = lane + 1
        prev = b["fork"]
        for s in own:
            if prev:
                edges.append({"from": prev, "to": s, "kind": "fork" if prev == b["fork"] else "branch", "branch": b["name"],
                              "dashed": prev == b["fork"] and b["fork_outside"]})
            prev = s
        if b["join"]:
            edges.append({"from": own[-1], "to": b["join"], "kind": "join", "branch": b["name"]})

    out_nodes = [{k: n.get(k) for k in ("sha", "short", "subject", "time", "author", "kind", "lane", "x", "branch")}
                 for n in sorted(nodes.values(), key=lambda n: n["x"])]
    return {"default": default, "nodes": out_nodes, "edges": edges, "branches": branches,
            "lanes": len(lanes) + 1, "columns": len(order), "hidden_branches": hidden, "empty": not main}
