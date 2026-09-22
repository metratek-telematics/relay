"""Knowledge docs: hand-written and agent-maintained markdown about the systems Relay works on.

Layout (DATA_DIR/knowledge, readable by agents at the same path inside the container)

    *.md             platform docs (architecture, hosts, data, testing), shared by every repository of the platform
    repos/<name>.md  one doc per repository, with front-matter:

        ---
        repo: owner/name
        source_commit: <sha of the default branch the doc was written from>
        source_branch: main
        updated: YYYY-MM-DD
        summary: one line
        platform_docs: [ARCHITECTURE, EDGE] | none      (which platform docs apply; missing = all)
        human_sections: [Gotchas]                       (never rewritten by the refresh agent)
        ---

Prompts        task_block() lists the docs that apply to a task (its repositories, related repositories from the
               system map, the platform docs they name) as paths plus one line each; agents read them themselves.
Staleness      check_repo() asks GitHub (read-only `gh api`) how far the default branch moved since source_commit and
               whether it touched key files (manifests, Dockerfiles, compose, CI, migrations, config). A delivery by a
               Relay task that touched key files marks the doc as possibly stale (mark_delivery).
Refresh        refresh_repo() runs ONE cheap agent turn on a shallow clone to rewrite only the affected sections of the
               doc and playbook (human-edited sections are kept), then rescans that repository in the system map.
State          DATA_DIR/learning/knowledge.json (checks, marks, refresh history).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .learning_defaults import DEFAULTS as LEARNING_DEFAULTS
from .util import DATA_DIR, RUNTIME_DIR, now, quiet, read_json, read_text, truncate, write_json, write_text

log = logging.getLogger("relay.knowledge")

KNOWLEDGE_DIR = DATA_DIR / "knowledge"
REPOS_DIR = KNOWLEDGE_DIR / "repos"
STATE_PATH = DATA_DIR / "learning" / "knowledge.json"
CLONE_DIR = RUNTIME_DIR / "_knowledge"
FRONT_ORDER = ("repo", "source_commit", "source_branch", "updated", "summary", "platform_docs", "human_sections")
HUMAN_MARK = "<!-- human -->"
BLOCK_CHARS = 1800
MAX_DOC_BYTES = 400_000
_lock = threading.RLock()
_busy: set = set()

DEFAULTS = {k: v for k, v in LEARNING_DEFAULTS.items() if k.startswith("knowledge_")}

# (category, globs) on repository-relative paths, matched case-insensitively (key_file_category).
KEY_FILES = [
    ("manifest", ["package.json", "requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "pipfile", "go.mod", "cargo.toml",
                  "pom.xml", "build.gradle*", "gemfile", "composer.json", "*.csproj"]),
    ("docker", ["dockerfile*", "*.dockerfile", ".dockerignore"]),
    ("compose", ["docker-compose*.y*ml", "compose*.y*ml", "*stack*.y*ml", "*compose*.y*ml"]),
    ("ci", [".github/workflows/*", ".gitlab-ci.yml", "jenkinsfile", ".circleci/*", ".drone.yml", "azure-pipelines.yml"]),
    ("migration", ["*migrations/*", "*migrate/*", "*alembic/*", "*.sql"]),
    ("config", ["*.conf", "*conf.d/*", "*traefik*", "*nginx*", ".env.example", "*.env.example", "config/*", "*/config/*", "*.toml",
                ".gitattributes", ".git-crypt/*", "makefile", "claude.md", "agents.md", "contributing.md", "*.service"]),
]


# ----------------------------------------------------------------------------- settings and state
def settings(cfg: dict | None) -> dict:
    out = dict(DEFAULTS)
    out.update({k: v for k, v in ((cfg or {}).get("learning") or {}).items() if k in DEFAULTS})
    return out


def load_state() -> dict:
    s = read_json(STATE_PATH, None) or {}
    s.setdefault("repos", {})
    return s


def save_state(s: dict) -> dict:
    with _lock:
        write_json(STATE_PATH, s)
    return s


def _update_repo_state(repo: str, **patch) -> dict:
    with _lock:
        s = load_state()
        row = s["repos"].setdefault(repo, {})
        row.update(patch)
        save_state(s)
        return row


# ----------------------------------------------------------------------------- front-matter and sections (pure)
def _parse_value(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        return [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def split_front(text: str) -> tuple[dict, str]:
    """(front-matter dict, body). A file without front-matter has an empty dict."""
    text = text or ""
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return {}, text
    for i in range(1, min(len(lines), 80)):
        if lines[i].strip() == "---":
            meta = {}
            for line in lines[1:i]:
                if ":" in line and not line.startswith((" ", "#")):
                    k, v = line.split(":", 1)
                    meta[k.strip()] = _parse_value(v)
            return meta, "\n".join(lines[i + 1:]).lstrip("\n")
    return {}, text


def _dump_value(v) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v).replace("\n", " ").strip()


def join_front(meta: dict, body: str) -> str:
    if not meta:
        return body
    keys = [k for k in FRONT_ORDER if k in meta] + [k for k in meta if k not in FRONT_ORDER]
    head = "\n".join(f"{k}: {_dump_value(meta[k])}" for k in keys if meta[k] not in (None, ""))
    return f"---\n{head}\n---\n\n{body.lstrip(chr(10))}"


def sections(body: str) -> list[tuple[str | None, str]]:
    """[(heading or None for the preamble, text including its '## ' line)], split on level-2 headings outside code fences."""
    out, cur, buf, fence = [], None, [], False
    for line in (body or "").split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and line.startswith("## "):
            out.append((cur, "\n".join(buf)))
            cur, buf = line[3:].strip(), [line]
            continue
        buf.append(line)
    out.append((cur, "\n".join(buf)))
    return [(h, t) for h, t in out if h is not None or t.strip()]


def _norm_heading(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def protected_sections(meta: dict, body: str) -> set:
    """Normalised headings the refresh must not rewrite: named in human_sections, or carrying the human marker."""
    names = meta.get("human_sections") or []
    if isinstance(names, str):
        names = [names]
    out = {_norm_heading(n) for n in names if n}
    for h, t in sections(body):
        if h and HUMAN_MARK in t:
            out.add(_norm_heading(h))
    return out


def apply_sections(body: str, updates: dict, protected: set) -> tuple[str, list, list]:
    """Replace the named sections' text (new headings are appended). Returns (body, changed, skipped)."""
    rows = sections(body)
    index = {_norm_heading(h): i for i, (h, _) in enumerate(rows) if h}
    changed, skipped = [], []
    for heading, text in (updates or {}).items():
        heading = str(heading).lstrip("#").strip()
        if not heading:
            continue
        key = _norm_heading(heading)
        text = str(text or "").strip("\n")
        # The agent may or may not repeat the heading line.
        if text.startswith("## "):
            text = text.split("\n", 1)[1] if "\n" in text else ""
        if key in protected:
            skipped.append(heading)
            continue
        new = f"## {rows[index[key]][0] if key in index else heading}\n\n{text.strip()}\n"
        if key in index:
            i = index[key]
            if rows[i][1].strip() == new.strip():
                continue
            rows[i] = (rows[i][0], new)
        else:
            rows.append((heading, new))
            index[key] = len(rows) - 1
        changed.append(heading)
    out = "\n".join(t.rstrip("\n") + "\n" for _, t in rows)
    return out, changed, skipped


def changed_sections(old_body: str, new_body: str) -> list[str]:
    """Headings whose text differs between two versions (added or edited), for recording human edits."""
    old = {_norm_heading(h): t.strip() for h, t in sections(old_body) if h}
    out = []
    for h, t in sections(new_body):
        if h and old.get(_norm_heading(h)) != t.strip():
            out.append(h)
    return out


def title_of(body: str, fallback: str) -> str:
    for line in (body or "").split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def clip(text: str, n: int) -> str:
    """One line, cut at a word boundary (truncate() marks omissions mid-text, which reads badly in a list)."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= n:
        return text
    cut = text[:n - 1].rsplit(" ", 1)[0] if " " in text[:n - 1] else text[:n - 1]
    return cut.rstrip(",;:") + "…"


def _first_sentence(text: str, n: int = 180) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.{12,}?[.!?])(\s|$)", text)
    return clip(m.group(1) if m else text, n)


def summary_of(meta: dict, body: str, fallback: str, kind: str = "repo") -> str:
    """front-matter summary, else (repository docs) the opening sentence or a Purpose table row, else the title."""
    if meta.get("summary"):
        return clip(str(meta["summary"]), 200)
    para = [] if kind == "repo" else None
    for line in (body or "").split("\n") if para is not None else []:
        if line.startswith("## "):
            break
        st = line.strip()
        if not st:
            if para:
                break
            continue
        if st.startswith(("#", ">", "|", "```", "<!--", "---")):
            if para:
                break
            continue
        para.append(st)
    if para:
        return _first_sentence(" ".join(para))
    m = re.search(r"^\|\s*\**purpose\**\s*\|\s*(.+?)\s*\|?\s*$", body or "", re.I | re.M) if kind == "repo" else None
    if m:
        return _first_sentence(m.group(1))
    return clip(title_of(body, fallback), 200)


# ----------------------------------------------------------------------------- key files (pure)
def key_file_category(path: str) -> str:
    p = (path or "").lower().removeprefix("./")
    name = p.rsplit("/", 1)[-1]
    for cat, pats in KEY_FILES:
        for pat in pats:
            # A pattern with a folder matches the path at any depth; a bare pattern matches the file name or the whole path
            # ("*traefik*" also catches traefik/dynamic.yml).
            if fnmatch.fnmatch(name, pat) and "/" not in pat or fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, "*/" + pat):
                return cat
    return ""


def key_files(paths) -> list[dict]:
    out = []
    for p in paths or []:
        c = key_file_category(p)
        if c:
            out.append({"path": p, "category": c})
    return out


# ----------------------------------------------------------------------------- files
def _safe(rel: str) -> Path:
    rel = str(rel or "").strip().lstrip("/")
    if not rel.endswith(".md") or "\\" in rel or any(part in ("", ".", "..") for part in rel.split("/")) or rel.count("/") > 1:
        raise ValueError("A knowledge doc is a .md file in the knowledge folder or its repos/ folder")
    if rel.split("/")[-1].startswith("."):
        raise ValueError("Hidden files are not knowledge docs")
    if "/" in rel and not rel.startswith("repos/"):
        raise ValueError("Only the repos/ subfolder holds docs")
    p = (KNOWLEDGE_DIR / rel).resolve()
    if KNOWLEDGE_DIR.resolve() not in p.parents:
        raise ValueError("Outside the knowledge folder")
    return p


def doc_path_for(repo: str) -> Path | None:
    """The doc of a repository (owner/name or name): repos/<name>.md, or any repos/*.md whose front-matter names it."""
    if not repo:
        return None
    name = repo.rstrip("/").split("/")[-1].removesuffix(".git").lower()
    direct = REPOS_DIR / f"{name}.md"
    if direct.is_file():
        return direct
    if REPOS_DIR.is_dir():
        for p in sorted(REPOS_DIR.glob("*.md")):
            meta, _ = split_front(_head(p))
            if str(meta.get("repo") or "").lower() in (repo.lower(), name):
                return p
    return None


_SHA_IN_TEXT = re.compile(r"\b(?:main|master)\s*@\s*`?([0-9a-f]{7,40})\b|@\s*`([0-9a-f]{7,40})`")


def doc_meta(p: Path, text: str | None = None) -> dict:
    """The doc's front-matter, completed for docs written without it: the repository from the system map (or the
    usual owner) and the commit the text says it was verified against ("main@abc1234"). Inferred keys are listed."""
    text = read_text(p) if text is None else text
    meta, body = split_front(text)
    meta = dict(meta)
    inferred = []
    if not meta.get("repo"):
        name = p.stem
        repo = ""
        try:
            from . import systemmap
            data = systemmap.load()
            comp = systemmap.find(data, name)
            repo = (comp or {}).get("repo") or ""
            if not repo:
                owners = [c["repo"].split("/")[0] for c in data["components"] if "/" in (c.get("repo") or "")]
                repo = f"{max(set(owners), key=owners.count)}/{name}" if owners else ""
        except Exception:
            repo = ""
        if repo:
            meta["repo"] = repo
            inferred.append("repo")
    if not meta.get("source_commit"):
        m = _SHA_IN_TEXT.search(body[:3000])
        if m:
            meta["source_commit"] = m.group(1) or m.group(2)
            inferred.append("source_commit")
    if inferred:
        meta["_inferred"] = inferred
    return meta


def _head(p: Path, n: int = 4000) -> str:
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(n)
    except OSError:
        return ""


def _row(p: Path, kind: str, state: dict) -> dict:
    text = read_text(p)
    _, body = split_front(text)
    meta = doc_meta(p, text) if kind == "repo" else split_front(text)[0]
    rel = p.relative_to(KNOWLEDGE_DIR).as_posix()
    st = (state.get("repos") or {}).get(meta.get("repo") or "", {}) if kind == "repo" else {}
    return {"path": rel, "abs": str(p), "kind": kind, "title": title_of(body, p.stem), "summary": summary_of(meta, body, p.stem, kind),
            "repo": meta.get("repo") or "", "source_commit": meta.get("source_commit") or "", "updated": meta.get("updated") or "",
            "platform_docs": meta.get("platform_docs"), "human_sections": meta.get("human_sections") or [],
            "inferred": meta.get("_inferred") or [],
            "size": len(text), "modified": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "stale": _stale_view(st)}


def _stale_view(st: dict) -> dict:
    if not st:
        return {}
    return {k: st.get(k) for k in ("checked_at", "head", "behind", "key_files", "stale", "untracked", "reasons", "delivery_marks",
                                   "last_refresh", "error")
            if st.get(k) not in (None, [], "")}


def list_docs() -> list[dict]:
    state = load_state()
    rows = []
    if KNOWLEDGE_DIR.is_dir():
        rows += [_row(p, "platform", state) for p in sorted(KNOWLEDGE_DIR.glob("*.md")) if not p.name.startswith(".")]
    if REPOS_DIR.is_dir():
        rows += [_row(p, "repo", state) for p in sorted(REPOS_DIR.glob("*.md")) if not p.name.startswith(".")]
    return rows


def read_doc(rel: str) -> dict:
    p = _safe(rel)
    if not p.is_file():
        raise KeyError(f"No knowledge doc {rel}")
    text = read_text(p)
    return {**_row(p, "repo" if rel.startswith("repos/") else "platform", load_state()), "text": text, "sha": sha(text)}


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def snapshot(rel: str) -> dict | None:
    """What the audit log records about a doc before and after an edit."""
    try:
        p = _safe(rel)
    except ValueError:
        return None
    if not p.is_file():
        return {"path": rel, "exists": False}
    text = read_text(p)
    return {"path": rel, "sha256": sha(text), "length": len(text), "lines": text.count("\n") + 1}


def save_doc(rel: str, text: str, base_sha: str = "", by: str = "") -> dict:
    """Write a doc from the editor. Sections the person changed become human sections the refresh agent keeps."""
    p = _safe(rel)
    text = str(text or "")
    if len(text.encode("utf-8")) > MAX_DOC_BYTES:
        raise ValueError("The doc is larger than 400 KB")
    with _lock:
        old = read_text(p) if p.is_file() else ""
        if base_sha and old and sha(old) != base_sha:
            raise RuntimeError("The doc changed since you opened it (the refresh job or another person saved it). Reload and edit again.")
        meta, body = split_front(text)
        if rel.startswith("repos/") and meta:
            _, old_body = split_front(old)
            human = list(meta.get("human_sections") or [])
            if isinstance(human, str):
                human = [human]
            for h in changed_sections(old_body, body):
                if _norm_heading(h) not in {_norm_heading(x) for x in human}:
                    human.append(h)
            if human:
                meta["human_sections"] = human
            text = join_front(meta, body)
        write_text(p, text if text.endswith("\n") else text + "\n")
    return {**read_doc(rel), "saved_by": by}


def search(q: str, limit: int = 40) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    words = [w for w in re.split(r"\s+", q.lower()) if w]
    out = []
    for row in list_docs():
        text = read_text(KNOWLEDGE_DIR / row["path"])
        low = text.lower()
        if not all(w in low for w in words):
            continue
        hits = []
        for i, line in enumerate(text.split("\n"), 1):
            if any(w in line.lower() for w in words):
                hits.append({"line": i, "text": truncate(line.strip(), 220)})
                if len(hits) >= 6:
                    break
        score = sum(low.count(w) for w in words) + (5 if all(w in row["title"].lower() for w in words) else 0)
        out.append({**{k: row[k] for k in ("path", "kind", "title", "summary", "repo")}, "hits": hits, "score": score})
    return sorted(out, key=lambda r: -r["score"])[:limit]


# ----------------------------------------------------------------------------- prompts
def _task_repos(task: dict) -> tuple[list[str], list[str]]:
    """(the task's GitHub names, related GitHub names from the approved system map)."""
    from . import github, multirepo, systemmap
    mine, related = [], []
    for full in [task.get("github_repo") or ""] + [r.get("github_repo") or "" for r in task.get("repos") or [] if isinstance(r, dict)]:
        if full and full not in mine:
            mine.append(full)
    paths = multirepo.task_repo_paths(task)
    for p in paths:
        full = github.remote_repo_name(p) or ""
        if full and full not in mine:
            mine.append(full)
    try:
        data = systemmap.load()
        for p in paths:
            comp = systemmap.for_repo(data, p)
            if comp and comp.get("repo") and comp["repo"] not in mine:
                mine.append(comp["repo"])
            for s in systemmap.related(p, include_proposed=False)["suggestions"]:
                if s.get("repo") and s["repo"] not in mine and s["repo"] not in related:
                    related.append(s["repo"])
    except Exception:
        pass
    return mine, related


def _in_map(repos: list[str]) -> bool:
    try:
        from . import systemmap
        known = {(c.get("repo") or "").lower() for c in systemmap.load()["components"]}
        return any(r.lower() in known for r in repos)
    except Exception:
        return False


def docs_for(mine: list[str], related: list[str]) -> list[dict]:
    """The docs that apply: repository docs (task repositories first), then the platform docs they name."""
    state = load_state()
    rows, seen, platform = [], set(), None
    for repo, why in [(r, "this task") for r in mine] + [(r, "related") for r in related]:
        p = doc_path_for(repo)
        if not p or p in seen:
            continue
        seen.add(p)
        row = _row(p, "repo", state)
        row["why"] = why
        rows.append(row)
        pd = row.get("platform_docs")
        if isinstance(pd, str) and pd.strip() and pd.strip().lower() != "none":
            pd = [pd.strip()]
        if why == "this task":
            if (pd is None or pd == "") and _in_map([row["repo"] or repo]):
                platform = "all"   # no platform_docs line: a component of the mapped platform gets them all
            elif isinstance(pd, list) and platform != "all":
                platform = (platform or set()) | {x.lower().removesuffix(".md") for x in pd}
    if platform is None and not rows and _in_map(mine):
        platform = "all"   # a platform component without its own doc still gets the platform docs
    if platform and KNOWLEDGE_DIR.is_dir():
        for p in sorted(KNOWLEDGE_DIR.glob("*.md")):
            if p.name.startswith("."):
                continue
            if platform == "all" or p.stem.lower() in platform:
                row = _row(p, "platform", state)
                row["why"] = "platform"
                rows.append(row)
    return rows


def block_for(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = [f"KNOWLEDGE DOCS (read the ones that apply before planning or changing anything; plain markdown under {KNOWLEDGE_DIR}. "
             "They hold deployment, safe-change and testing rules that are not in the code. Where a doc and the code disagree, trust the "
             "code and say so in your report.)"]
    for r in rows:
        extra = []
        if r["kind"] == "repo":
            if r.get("source_commit"):
                extra.append(f"written from {r['source_commit'][:7]}" + (f" on {r['updated']}" if r.get("updated") else ""))
            st = r.get("stale") or {}
            if st.get("stale") or st.get("delivery_marks"):
                extra.append("may be stale" + (f", {st['behind']} commits behind" if st.get("behind") else ""))
            if r.get("why") == "related":
                extra.append("related repository")
        line = f"- {r['abs']}: {clip(r['summary'], 150)}" + (f" ({'; '.join(extra)})" if extra else "")
        if sum(len(x) + 1 for x in lines) + len(line) > BLOCK_CHARS:
            lines.append(f"- … and {len(rows) - len(lines) + 1} more under {KNOWLEDGE_DIR}")
            break
        lines.append(line)
    return "\n".join(lines)


def task_block(task: dict, cfg: dict | None = None) -> str:
    """The short Knowledge section for a task's prompts: paths and one line each, never the docs themselves."""
    if not settings(cfg).get("knowledge_inject", True):
        return ""
    mine, related = _task_repos(task)
    return block_for(docs_for(mine, related))


# ----------------------------------------------------------------------------- staleness (read-only GitHub)
def _gh_json(args, timeout=60):
    from . import github
    return github.gh_json(args, timeout=timeout)


def default_branch(repo: str) -> str:
    return str((_gh_json(["api", f"repos/{repo}"]) or {}).get("default_branch") or "main")


def check_repo(repo: str, meta: dict, min_commits: int = 10) -> dict:
    """How far the default branch moved since the doc's source commit, and whether key files changed. Read-only."""
    branch = str(meta.get("source_branch") or "") or default_branch(repo)
    head = str((_gh_json(["api", f"repos/{repo}/commits/{branch}"]) or {}).get("sha") or "")
    base = str(meta.get("source_commit") or "")
    out = {"checked_at": now(), "branch": branch, "head": head, "base": base, "behind": 0, "key_files": [], "files": [], "commits": [],
           "stale": False, "reasons": []}
    if not head:
        raise RuntimeError(f"GitHub returned no head commit for {repo}@{branch}")
    if not base:
        # Nothing to compare with: shown as untracked; a forced refresh brings the doc under tracking.
        out.update(untracked=True, reasons=["the doc records no source commit (refresh it once to start tracking)"])
        return out
    if head.startswith(base) or base.startswith(head):
        return out
    try:
        cmp = _gh_json(["api", f"repos/{repo}/compare/{base}...{head}"], timeout=120) or {}
    except RuntimeError as e:
        out.update(stale=True, reasons=[f"cannot compare with the recorded source commit ({truncate(str(e), 120)})"])
        return out
    files = [f.get("filename") for f in cmp.get("files") or [] if f.get("filename")]
    out["behind"] = int(cmp.get("ahead_by") or len(cmp.get("commits") or []))
    out["files"] = files[:300]
    out["patches"] = {f["filename"]: f.get("patch") or "" for f in cmp.get("files") or [] if f.get("filename") and key_file_category(f["filename"])}
    out["commits"] = [truncate(((c.get("commit") or {}).get("message") or "").split("\n")[0], 120) for c in cmp.get("commits") or []][-40:]
    out["key_files"] = key_files(files)
    if out["key_files"]:
        out["reasons"].append(f"{len(out['key_files'])} key file(s) changed: " + ", ".join(k["path"] for k in out["key_files"][:6]))
    if out["behind"] >= max(1, int(min_commits)):
        out["reasons"].append(f"{out['behind']} commits since the doc was written")
    out["stale"] = bool(out["reasons"])
    return out


def mark_delivery(repo: str, files, task_id: str = "", pr_url: str = "") -> dict | None:
    """After a task delivers: its doc may be stale when the diff touched key files. Returns the mark, or None."""
    if not repo or not doc_path_for(repo):
        return None
    hits = key_files(files)
    if not hits:
        return None
    mark = {"time": now(), "task_id": task_id, "pr_url": pr_url or "", "key_files": [h["path"] for h in hits[:20]]}
    with _lock:
        s = load_state()
        row = s["repos"].setdefault(repo, {})
        marks = [m for m in row.get("delivery_marks") or [] if m.get("task_id") != task_id]
        row["delivery_marks"] = (marks + [mark])[-10:]
        save_state(s)
    return mark


def repo_docs() -> list[tuple[str, Path, dict]]:
    out = []
    if REPOS_DIR.is_dir():
        for p in sorted(REPOS_DIR.glob("*.md")):
            if p.name.startswith("."):
                continue
            meta = doc_meta(p)
            if meta.get("repo"):
                out.append((meta["repo"], p, meta))
    return out


# ----------------------------------------------------------------------------- refresh (one cheap agent turn)
def choose_agent(cfg: dict) -> tuple[str, str, str, str]:
    """(agent, model, effort, provider): Settings → learning overrides, else the retrospective agent and its cheap model."""
    from . import config as C, retro
    s = settings(cfg)
    pseudo = {"workflow": {"roles": cfg.get("roles") or {}}}
    agent, model, effort = retro.choose_agent(pseudo, cfg)
    provider = retro.retro_provider(pseudo, cfg)
    if s.get("knowledge_refresh_agent") and s["knowledge_refresh_agent"] in C.AGENTS:
        agent = s["knowledge_refresh_agent"]
        model = ((cfg.get("subagent_models") or {}).get(agent) or "").strip()
        efforts = C.AGENTS.get(agent, {}).get("efforts") or []
        effort = efforts[0] if efforts else ""
        provider = ""
    if s.get("knowledge_refresh_provider") == "openrouter":
        from . import openrouter
        provider, model = "openrouter", (s.get("knowledge_refresh_model") or "").strip() or openrouter.AUTO_FREE
    elif (s.get("knowledge_refresh_model") or "").strip():
        model = s["knowledge_refresh_model"].strip()
    return agent, model, effort, provider


def refresh_prompt(repo: str, doc_text: str, protected: list[str], info: dict, playbook: dict | None, clone_ok: bool) -> str:
    from . import playbooks
    meta, body = split_front(doc_text)
    heads = [h for h, _ in sections(body) if h]
    kf = info.get("key_files") or []
    others = [f for f in info.get("files") or [] if f not in {k["path"] for k in kf}]
    patches = []
    budget = 9000
    for path, patch in (info.get("patches") or {}).items():
        if budget <= 0:
            break
        piece = truncate(patch or "(binary or too large)", min(2500, budget))
        budget -= len(piece)
        patches.append(f"--- {path}\n{piece}")
    pb = {s: ((playbook or {}).get("sections") or {}).get(s, {}) for s in playbooks.SECTIONS}
    pb_text = {s: v.get("text") or "" for s, v in pb.items()}
    pb_edited = [s for s, v in pb.items() if v.get("edited") and v.get("source") != playbooks.KNOWLEDGE_SOURCE]
    where = ("The repository's new default-branch HEAD is checked out (shallow, read-only) in your working directory: read the files "
             "you need (manifests, Dockerfiles, compose, CI workflows, migrations) to confirm facts. Do not modify, build, run or push anything."
             if clone_ok else "No checkout is available; work from the changes below.")
    return f"""You keep a knowledge doc about the repository {repo} correct. Other coding agents read it before changing that repository.
The default branch moved from {str(info.get('base') or '(unknown)')[:12]} to {str(info.get('head') or '')[:12]} ({info.get('behind') or 0} commits).
{where}

Update ONLY the sections the changes below make wrong or incomplete. Keep every other section out of your reply. Keep the doc's
style: terse, factual, markdown bullets and tables, commands in backticks. Never include secrets, passwords, tokens, keys or the
contents of encrypted (git-crypt) files; name such files only. Never invent facts: if unsure, leave the section alone.
Sections you must NOT touch (a person edited them): {', '.join(protected) or '(none)'}.
Existing sections: {', '.join(heads) or '(none)'}.

Reply with ONLY one fenced ```json block:
{{"type":"knowledge","sections":{{"<exact existing heading>":"<full new markdown body of that section, without the ## line>"}},
 "summary":"<new one-line summary, or empty to keep>","changes":["<one line per fact you changed>"],
 "playbook":{{"<run|conventions|gotchas>":"- bullet\\n- bullet"}}}}
"sections" and "playbook" may be empty objects. Playbook sections are at most 8 one-sentence bullets; only include one when the
changes affect it. Playbook sections a person edited (leave them out): {', '.join(pb_edited) or '(none)'}.

KEY FILES CHANGED
{chr(10).join(f"- {k['path']} ({k['category']})" for k in kf[:80]) or '(none)'}

OTHER FILES CHANGED ({len(others)})
{chr(10).join('- ' + f for f in others[:60]) or '(none)'}

COMMITS (oldest first)
{chr(10).join('- ' + c for c in info.get('commits') or []) or '(none)'}

KEY FILE DIFFS (truncated)
{chr(10).join(patches) or '(none)'}

CURRENT PLAYBOOK
{json.dumps(pb_text, indent=1)}

CURRENT DOC
{truncate(doc_text, 24000)}
"""


def parse_reply(text: str) -> dict | None:
    from . import protocol
    env = protocol.parse_envelope(text or "")
    if not env or env.get("type") not in ("knowledge", None) or not isinstance(env.get("sections", {}), dict):
        return None
    pb = env.get("playbook") if isinstance(env.get("playbook"), dict) else {}
    out_pb = {}
    for k, v in pb.items():
        if isinstance(v, list):
            v = "\n".join(f"- {str(x).lstrip('- ')}" for x in v)
        out_pb[str(k)] = truncate(str(v or "").strip(), 2000)
    return {"sections": {str(k): str(v or "") for k, v in (env.get("sections") or {}).items()},
            "summary": truncate(str(env.get("summary") or "").strip(), 200),
            "changes": [truncate(str(x), 200) for x in (env.get("changes") or []) if str(x).strip()][:20], "playbook": out_pb}


def forbidden_hits(text: str, cfg: dict) -> int:
    """How many configured forbidden terms (design_forbidden_terms) appear, compared without case, spaces or punctuation."""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s or "").lower())  # noqa: E731
    body = norm(text)
    return sum(1 for t in cfg.get("design_forbidden_terms") or [] if norm(t) and norm(t) in body)


def shallow_clone(repo: str, branch: str) -> Path | None:
    dest = CLONE_DIR / re.sub(r"[^A-Za-z0-9._-]+", "__", repo)
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = quiet(["gh", "repo", "clone", repo, str(dest), "--", "--depth", "1", "--single-branch", "--branch", branch, "-q"], timeout=600)
    if p.returncode != 0 or not (dest / ".git").exists():
        log.warning("knowledge clone of %s failed: %s", repo, truncate((p.stdout or "") + (p.stderr or ""), 300))
        shutil.rmtree(dest, ignore_errors=True)
        return None
    # The agent reads this checkout; without a remote nothing it does can reach GitHub.
    quiet(["git", "remote", "remove", "origin"], cwd=dest)
    return dest


def refresh_repo(cfg: dict, repo: str, force: bool = False, run_agent=None, estimate_cost=None, clone=None) -> dict:
    """Check one repository's doc and, when stale (or forced), refresh it with one agent turn. Never touches the repository."""
    from . import playbooks, retro, systemmap
    p = doc_path_for(repo)
    if not p:
        raise KeyError(f"No knowledge doc for {repo}")
    with _lock:
        if repo in _busy:
            return {"repo": repo, "status": "busy"}
        _busy.add(repo)
    clone_path = None
    try:
        s = settings(cfg)
        doc = read_text(p)
        meta = doc_meta(p, doc)
        body = split_front(doc)[1]
        info = check_repo(repo, meta, int(s.get("knowledge_min_commits") or 10))
        _update_repo_state(repo, **{k: info.get(k) for k in ("checked_at", "head", "behind", "key_files", "stale", "reasons", "untracked")}, error="")
        if not info["stale"] and not force:
            return {"repo": repo, "status": "fresh", "behind": info["behind"]}
        if force and not info["stale"]:
            info["reasons"] = ["refresh requested"]
        clone_path = (clone or shallow_clone)(repo, info["branch"])
        protected = sorted({h for h, _ in sections(body) if h and _norm_heading(h) in protected_sections(meta, body)})
        pb = playbooks.load(repo)
        agent, model, effort, provider = choose_agent(cfg)
        if not agent:
            raise RuntimeError("No agent configured for the knowledge refresh (Settings → retrospective agent or the default supervisor).")
        text = refresh_prompt(repo, doc, protected, info, pb, bool(clone_path))
        workdir = clone_path or (CLONE_DIR / "_empty")
        runner = run_agent or (lambda *a, **k: retro.run_agent(*a, **k))
        res = runner(agent, model, effort, text, cfg, Path(workdir), float(cfg.get("retro_timeout_seconds") or 300), provider=provider)
        reply = parse_reply(res.get("last_message") or res.get("text") or "")
        if not reply:
            raise RuntimeError("The agent did not return a knowledge block: " + truncate(res.get("error") or res.get("text") or "", 300))
        rejected = []
        for h in list(reply["sections"]):
            if forbidden_hits(reply["sections"][h], cfg):
                rejected.append(h)
                reply["sections"].pop(h)
        if reply["summary"] and forbidden_hits(reply["summary"], cfg):
            reply["summary"] = ""
        with _lock:
            cur = read_text(p)            # a person may have saved while the agent worked
            meta, body = split_front(cur)
            new_body, changed, skipped = apply_sections(body, reply["sections"], protected_sections(meta, body))
            meta.update(repo=meta.get("repo") or repo, source_commit=info["head"], source_branch=info["branch"],
                        updated=datetime.now().date().isoformat())
            if reply["summary"]:
                meta["summary"] = reply["summary"]
            write_text(p, join_front(meta, new_body))
        pb_changed = []
        pb_sections = {k: v for k, v in reply["playbook"].items() if k in playbooks.SECTIONS and v.strip() and not forbidden_hits(v, cfg)}
        if pb_sections:
            pb_now = playbooks.load(repo)
            pb_now, pb_changed = playbooks.merge_partial(pb_now, repo, (pb_now or {}).get("repo_label") or repo, pb_sections, "agent")
            if pb_changed:
                playbooks.save(pb_now)
        map_result = {}
        try:
            if clone_path:
                map_result = systemmap.rescan_repo(repo, clone_path)
        except Exception as e:
            map_result = {"error": truncate(str(e), 200)}
        usage = res.get("usage") or {}
        cost = estimate_cost(agent, usage)[0] if estimate_cost else 0.0
        entry = {"time": now(), "from": info.get("base"), "to": info["head"], "behind": info["behind"], "reasons": info["reasons"],
                 "sections_changed": changed, "sections_kept": skipped, "sections_rejected": rejected, "playbook_changed": pb_changed,
                 "changes": reply["changes"], "map": map_result, "agent": agent, "model": res.get("model") or model, "provider": provider,
                 "cost_usd": round(float(cost or 0), 4), "forced": bool(force)}
        with _lock:
            st = load_state()
            row = st["repos"].setdefault(repo, {})
            row.update(stale=False, untracked=False, reasons=[], behind=0, key_files=[], delivery_marks=[], last_refresh=entry, error="",
                       head=info["head"], checked_at=now())
            row["history"] = ((row.get("history") or []) + [entry])[-20:]
            save_state(st)
        return {"repo": repo, "status": "refreshed", **entry}
    except Exception as e:
        _update_repo_state(repo, error=truncate(str(e), 300), error_at=now())
        raise
    finally:
        with _lock:
            _busy.discard(repo)
        if clone_path:
            shutil.rmtree(clone_path, ignore_errors=True)


def check_all(cfg: dict, refresh: bool = True, force: bool = False, only: str = "", run_agent=None, estimate_cost=None) -> list[dict]:
    """The daily job: check every repository doc, refresh the stale ones. One repository failing never stops the others."""
    out = []
    for repo, _, _ in repo_docs():
        if only and only.lower() not in (repo.lower(), repo.split("/")[-1].lower()):
            continue
        try:
            if refresh and settings(cfg).get("knowledge_refresh", True) or force:
                out.append(refresh_repo(cfg, repo, force=force, run_agent=run_agent, estimate_cost=estimate_cost))
            else:
                info = check_repo(repo, doc_meta(doc_path_for(repo)), int(settings(cfg).get("knowledge_min_commits") or 10))
                _update_repo_state(repo, **{k: info.get(k) for k in ("checked_at", "head", "behind", "key_files", "stale", "reasons", "untracked")},
                                   error="")
                out.append({"repo": repo, "status": "stale" if info["stale"] else "fresh", "behind": info["behind"]})
        except Exception as e:
            log.warning("knowledge refresh of %s: %s", repo, e)
            out.append({"repo": repo, "status": "error", "error": truncate(str(e), 300)})
    with _lock:
        s = load_state()
        s["last_run"] = {"time": now(), "results": [{k: r.get(k) for k in ("repo", "status", "behind", "error")} for r in out]}
        save_state(s)
    return out


def due(cfg: dict) -> bool:
    s = settings(cfg)
    if not s.get("knowledge_refresh", True):
        return False
    last = (load_state().get("last_run") or {}).get("time")
    hours = max(1.0, float(s.get("knowledge_refresh_hours") or 24))
    try:
        return not last or datetime.fromisoformat(last) < datetime.now() - timedelta(hours=hours)
    except ValueError:
        return True


def busy() -> list[str]:
    with _lock:
        return sorted(_busy)


def start_daily(manager, interval_seconds: int = 1800):
    """Background loop: every half hour, run check_all when the daily interval has passed."""
    def loop():
        time.sleep(120)   # let Relay finish starting
        while True:
            try:
                cfg = manager.cfg()
                if due(cfg):
                    check_all(cfg, estimate_cost=getattr(manager, "estimate_cost", None))
            except Exception:
                log.exception("knowledge daily check failed")
            time.sleep(interval_seconds)
    t = threading.Thread(target=loop, name="relay-knowledge", daemon=True)
    t.start()
    return t
