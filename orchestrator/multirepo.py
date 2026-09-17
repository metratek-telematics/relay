"""Multi-repository tasks: one change across several repositories.

A task always has a primary repository (`task["repo"]`, the agents' working directory, unchanged for single
repository tasks). `task["repos"]` lists every repository of the task, primary first:

    [{"repo": "/repos/web", "role": "primary"},
     {"repo": "/repos/api", "role": "related", "reason": "web calls /items/* on api", "component": "api"}]

Each related repository gets its own worktree on the same branch name, its own environment and setup, its own
checks, design gate, commit, push and pull request. What the pipeline recorded per repository at run time lives
in `task["repo_worktrees"]`, keyed by a short name:

    {"api": {"repo", "name", "worktree", "branch", "base_commit", "github_repo", "verify_commands",
             "environment", "pr_url", "pr_number", "committed", "diffstat", "changed_count"}}

The Pipeline mixes in `MultiRepo`; every method here is a no-op for a single-repository task.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from . import designcheck, environment, gitops, github, judge, repo_env
from .util import WORKTREES_DIR, now, quiet, safe_slug, truncate, write_text


# ----------------------------------------------------------------------------- task records
def related_entries(task: dict) -> list[dict]:
    primary = _resolve(task.get("repo"))
    out = []
    for r in task.get("repos") or []:
        if not isinstance(r, dict) or not r.get("repo") or r.get("role") == "primary" or _resolve(r["repo"]) == primary:
            continue
        out.append(r)
    return out


def _resolve(p) -> str:
    try:
        return str(Path(p).resolve()) if p else ""
    except OSError:
        return str(p)


def repo_name(path, taken=()) -> str:
    base = safe_slug(Path(str(path)).name.lower(), 40) or "repo"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def normalize_repos(primary: str, entries) -> list[dict]:
    """Validate a task's repository list. Returns [] when the task has only its primary repository."""
    prim = _resolve(primary)
    seen, related = {prim}, []
    for e in entries or []:
        if isinstance(e, str):
            e = {"repo": e}
        if not isinstance(e, dict):
            continue
        path = str(e.get("repo") or e.get("path") or "").strip().strip('"')
        if not path or e.get("role") == "primary":
            continue
        if not Path(path).is_dir():
            raise ValueError(f"Related repository folder does not exist: {path}")
        if not gitops.is_git_repo(path):
            raise ValueError(f"Related repository is not a Git repository: {path}")
        r = _resolve(path)
        if r in seen:
            continue
        seen.add(r)
        related.append({"repo": r, "role": "related", "reason": truncate(str(e.get("reason") or ""), 400),
                        "component": str(e.get("component") or ""), "github_repo": github.remote_repo_name(r) or ""})
    if not related:
        return []
    return [{"repo": prim, "role": "primary", "github_repo": github.remote_repo_name(prim) or ""}, *related]


def task_worktrees(task: dict) -> list[dict]:
    """Every (repo, worktree, branch) a task owns, primary first."""
    rows = []
    if task.get("worktree"):
        rows.append({"repo": task.get("repo"), "worktree": task["worktree"], "branch": task.get("branch") or task.get("branch_name"), "primary": True})
    for name, w in (task.get("repo_worktrees") or {}).items():
        if w.get("worktree"):
            rows.append({"repo": w.get("repo"), "worktree": w["worktree"], "branch": w.get("branch"), "name": name, "primary": False})
    return rows


def task_repo_paths(task: dict) -> list[str]:
    paths = [task.get("repo")] + [r["repo"] for r in related_entries(task)]
    return [p for p in paths if p]


# ----------------------------------------------------------------------------- work packages and system design
def normalize_design(value) -> dict:
    """The plan's upfront system design: contracts between components, data model changes, the call sequence."""
    if not isinstance(value, dict):
        return {}
    def rows(key, fields):
        out = []
        for x in value.get(key) or []:
            if isinstance(x, dict):
                out.append({f: truncate(str(x.get(f) or ""), 1200) for f in fields if x.get(f)})
            elif str(x).strip():
                out.append({fields[-1]: truncate(str(x).strip(), 1200)})
        return [r for r in out if r]
    design = {
        "summary": truncate(str(value.get("summary") or ""), 2000),
        "components": [str(c) for c in value.get("components") or [] if str(c).strip()][:20],
        "api_contracts": rows("api_contracts", ["provider", "consumer", "endpoint", "request", "response", "notes"]),
        "data_model": rows("data_model", ["component", "change"]),
        "sequence": [truncate(str(s), 500) for s in value.get("sequence") or [] if str(s).strip()][:30],
    }
    return design if any(design.values()) else {}


def normalize_packages(value) -> list[dict]:
    out = []
    for i, x in enumerate(value or [], 1):
        if isinstance(x, str) and x.strip():
            x = {"summary": x}
        if not isinstance(x, dict):
            continue
        deps = x.get("depends_on") or []
        out.append({"id": str(x.get("id") or f"W{i}"), "repo": str(x.get("repo") or ""), "summary": truncate(str(x.get("summary") or x.get("title") or ""), 400),
                    "depends_on": [str(d) for d in (deps if isinstance(deps, list) else [deps]) if str(d).strip()]})
    return out[:40]


def design_md(design: dict) -> str:
    if not design:
        return ""
    lines = ["# System design", ""]
    if design.get("summary"):
        lines += [design["summary"], ""]
    if design.get("components"):
        lines += ["**Components:** " + ", ".join(design["components"]), ""]
    if design.get("api_contracts"):
        lines += ["## API contracts", ""]
        for c in design["api_contracts"]:
            lines.append(f"- **{c.get('endpoint', '(endpoint)')}** · {c.get('consumer', '?')} → {c.get('provider', '?')}"
                         + (f"\n  - request: {c['request']}" if c.get("request") else "") + (f"\n  - response: {c['response']}" if c.get("response") else "")
                         + (f"\n  - {c['notes']}" if c.get("notes") else ""))
        lines.append("")
    if design.get("data_model"):
        lines += ["## Data model", ""] + [f"- {d.get('component', '')}: {d.get('change', '')}" for d in design["data_model"]] + [""]
    if design.get("sequence"):
        lines += ["## Sequence", ""] + [f"{i}. {s}" for i, s in enumerate(design["sequence"], 1)] + [""]
    return "\n".join(lines)


def design_block(design: dict, packages: list[dict]) -> str:
    lines = []
    if design:
        lines += ["SYSTEM DESIGN (agreed at planning; work packages implement it, change it only through the supervisor)"]
        if design.get("summary"):
            lines.append(design["summary"])
        for c in design.get("api_contracts") or []:
            lines.append(f"- contract {c.get('endpoint', '')}: {c.get('consumer', '?')} → {c.get('provider', '?')}"
                         + (f" · request {c['request']}" if c.get("request") else "") + (f" · response {c['response']}" if c.get("response") else "")
                         + (f" · {c['notes']}" if c.get("notes") else ""))
        for d in design.get("data_model") or []:
            lines.append(f"- data model {d.get('component', '')}: {d.get('change', '')}")
        for i, s in enumerate(design.get("sequence") or [], 1):
            lines.append(f"  {i}. {s}")
        lines.append("")
    if packages:
        lines.append("WORK PACKAGES (in order; a package starts after the ones it depends on)")
        for p in packages:
            lines.append(f"- {p['id']}{' [' + p['repo'] + ']' if p.get('repo') else ''}: {p['summary']}"
                         + (f" · after {', '.join(p['depends_on'])}" if p.get("depends_on") else ""))
        lines.append("")
    return "\n".join(lines).strip()


CROSS_COMPONENT_RULE = """CROSS-COMPONENT CHANGES
- Identify which components the request affects end to end: the user-visible behaviour, the service that implements it, and the data it relies on. Read the code on the other side of every call the change relies on.
- If the behaviour depends on a component whose repository is not in this task (the backend keeps the state, the endpoint does not exist or rejects the new flow), do not plan a change on one side on assumptions. Reply with a question envelope proposing to add that repository:
  {"type":"question","to":"user","question":"<what depends on it and why>","add_repo":{"repo":"<component id or owner/name from the system map>","reason":"<one line>"},"options":["Add <name> to this task","Continue without it"]}
  Relay clones it if needed, adds a worktree on the same branch and tells you its path. Ask for all missing repositories you can see at once (one question per repository, one per turn).
- With more than one repository, the plan carries "work_packages":[{"id":"W1","repo":"<name>","summary":"…","depends_on":[]}] in dependency order (data and SQL, then the API, then the UI), and "system_design":{"summary":"…","api_contracts":[{"provider":"…","consumer":"…","endpoint":"POST /x","request":"…","response":"…"}],"data_model":[{"component":"…","change":"…"}],"sequence":["…"]} for the contracts between them. Each instruction names its package and repository: {"type":"instruction","package":"W2","repo":"<name>",…}.
- Acceptance criteria may be end to end (the UI action reaches the service and the service behaves as required); prove each side in its own repository."""


# ----------------------------------------------------------------------------- the pipeline mixin
class MultiRepo:
    """Hooks the Pipeline calls. Attributes it relies on: task, r (runner), m (manager), tid, cfg, run_dir, state, wt, branch, base."""

    related: list[dict]

    # ------------------------------------------------------------ records
    def _related_meta(self) -> dict:
        return dict((self.task_meta().get("repo_worktrees") or {}))

    def _save_related(self):
        rows = {r["name"]: {k: v for k, v in r.items() if k not in ("env",)} for r in self.related}
        self.m.set_meta(self.tid, repo_worktrees=rows)

    def repo_by_name(self, name: str) -> dict | None:
        name = str(name or "").lower()
        return next((r for r in self.related if r["name"] == name or Path(r["repo"]).name.lower() == name
                     or (r.get("github_repo") or "").lower() == name or (r.get("component") or "").lower() == name), None)

    # ------------------------------------------------------------ attach
    def attach_related(self, fresh: bool):
        """Worktrees for every related repository of the task. Reattaches existing ones on resume."""
        self.related = getattr(self, "related", None) or []
        entries = related_entries(self.task_meta() or self.task)
        if not entries:
            return
        recorded = self._related_meta()
        for e in entries:
            if any(_resolve(r["repo"]) == _resolve(e["repo"]) for r in self.related):
                continue
            prior = next((w for w in recorded.values() if _resolve(w.get("repo")) == _resolve(e["repo"])), None)
            self.attach_repo(e, prior if (prior and not fresh) else None)
        self._update_masker()

    def attach_repo(self, entry: dict, prior: dict | None = None) -> dict:
        repo = Path(entry["repo"]).resolve()
        taken = {r["name"] for r in self.related}
        name = (prior or {}).get("name") or repo_name(repo, taken)
        row = {"name": name, "repo": str(repo), "reason": entry.get("reason") or "", "component": entry.get("component") or "",
               "github_repo": entry.get("github_repo") or github.remote_repo_name(repo) or ""}
        if prior and prior.get("worktree") and Path(prior["worktree"]).exists():
            row.update({k: prior.get(k) for k in ("worktree", "branch", "base_commit", "environment", "pr_url", "pr_number", "committed")})
            self.r.timeline("git", f"Reattached {name}", f"{row['branch']} → {row['worktree']}")
        else:
            wt_path = WORKTREES_DIR / f"{safe_slug(repo.name)}-{self.tid[-6:]}"
            if _resolve(wt_path) in {_resolve(self.wt), *[_resolve(r["worktree"]) for r in self.related]}:
                wt_path = WORKTREES_DIR / f"{safe_slug(repo.name)}-{name}-{self.tid[-6:]}"
            sub = Path(self.run_dir) / "repos" / name
            sub.mkdir(parents=True, exist_ok=True)
            self.r.status("running", f"Preparing worktree for {name}")
            wt, branch = gitops.create_worktree(self.r, {**self.task, "branch_name": self.branch}, self.cfg, sub, repo=repo, wt_path=wt_path)
            row.update(worktree=str(wt), branch=branch, base_commit=quiet(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip())
            self.r.timeline("git", f"Worktree ready · {name}", f"{branch} → {wt}")
        row["env"] = repo_env.load(repo)
        if not (prior and prior.get("environment")):
            row["environment"] = self.prepare_related_environment(row)
        if row["env"].get("services_up") and self.state.get("phase") != "delivered":
            self._start_related_services(row)
        cmds = [c["command"] for c in row["env"].get("checks") or []]
        row["optional_checks"] = [c["command"] for c in row["env"].get("checks") or [] if not c.get("required", True)]
        if not cmds and (self.task.get("workflow") or {}).get("auto_detect_verification", self.cfg.get("auto_detect_verification", True)):
            cmds = gitops.detect_verify(row["worktree"])
        row["verify_commands"] = [] if self.verify_mode == "off" else cmds
        if not (self.state.get("related_heads") or {}).get(name):
            self._remember_head(row)
        self.related.append(row)
        self._save_related()
        return row

    def prepare_related_environment(self, row) -> dict:
        wf_less = {**self.task, "workflow": {**(self.task.get("workflow") or {}), "setup_command": ""}}
        cmd = environment.setup_command(wf_less, self.cfg, row["worktree"])
        info = {"command": cmd, "ok": None, "skipped": not cmd, "duration": 0}
        if cmd and environment.already_prepared(row["worktree"]):
            info.update(skipped=True, note="dependencies already present in the worktree")
        elif cmd:
            self.r.status("preparing", f"Preparing {row['name']} · {cmd}")
            res = self.r.run_shell(cmd, row["worktree"], "setup", timeout=float(self.cfg.get("env_prepare_timeout_minutes") or 20) * 60,
                                   title=f"Prepare environment · {row['name']} · {cmd}")
            info.update(ok=res["ok"], duration=round(res.get("duration") or 0, 1))
            if not res["ok"]:
                self.record_blocked([{"check": f"Environment setup in {row['name']} ({cmd})", "action_required": False,
                                      "reason": truncate((res.get("output") or "").strip().splitlines()[-1] if (res.get("output") or "").strip() else f"exit {res.get('rc')}", 300),
                                      "impact": f"checks in {row['name']} that need dependencies cannot run"}])
        return info

    def _start_related_services(self, row):
        cmd = row["env"]["services_up"]
        res = self.r.run_shell(cmd, row["worktree"], "setup", timeout=float(self.cfg.get("env_prepare_timeout_minutes") or 20) * 60,
                               title=f"Start services · {row['name']} · {cmd}")
        row["services_started"] = bool(res["ok"])

    def stop_related_services(self):
        for row in getattr(self, "related", None) or []:
            cmd = (row.get("env") or {}).get("services_down")
            if cmd and row.get("services_started"):
                try:
                    self.r.run_shell(cmd, row["worktree"], "setup", timeout=300, title=f"Stop services · {row['name']} · {cmd}")
                except Exception:
                    pass
                row["services_started"] = False

    def repo_masker(self):
        """Secrets of every repository of the task, masked in logs and messages."""
        maskers = [repo_env.masker(getattr(self, "repo_env", None) or repo_env.empty())] + [repo_env.masker(r["env"]) for r in getattr(self, "related", None) or []]
        def mask(s):
            for fn in maskers:
                s = fn(s)
            return s
        return mask

    def _update_masker(self):
        rm, cm = self.repo_masker(), getattr(self, "_conn_mask", None)
        self.r.set_masker((lambda s: cm(rm(s))) if cm else rm)

    def guard_related_commits(self, role, agent):
        """The commit guard for the other repositories: an agent commit there is undone too, keeping its changes."""
        from . import commitguard
        heads = self.state.get("related_heads") or {}
        for r in getattr(self, "related", None) or []:
            expected = heads.get(r["name"])
            if not expected:
                continue
            info = commitguard.rewind(r["worktree"], expected, r["branch"])
            if info and info.get("rewound"):
                self.r.timeline("git", f"Agent commit undone · {r['name']}", f"{role}: {truncate('; '.join(info.get('commits') or []), 240)} · changes kept")

    def _remember_head(self, row):
        from . import commitguard
        heads = dict(self.state.get("related_heads") or {})
        heads[row["name"]] = commitguard.head(row["worktree"])
        self.state["related_heads"] = heads

    def agent_cfg(self) -> dict:
        """Settings for an agent turn: related worktrees are directories the agent may read and write."""
        dirs = [r["worktree"] for r in getattr(self, "related", None) or [] if r.get("worktree")]
        return {**self.cfg, "extra_dirs": dirs} if dirs else self.cfg

    # ------------------------------------------------------------ prompts
    def repos_text(self) -> str:
        if not getattr(self, "related", None):
            return ""
        primary = Path(self.task.get("repo") or "").name
        lines = ["REPOSITORIES IN THIS TASK (one branch name in every repository; Relay commits, pushes and opens a pull request in each)",
                 f"- {primary} (primary): {self.wt} · your working directory"]
        for r in self.related:
            lines.append(f"- {r['name']}: {r['worktree']}" + (f" · {r['github_repo']}" if r.get("github_repo") else "")
                         + (f" · why: {r['reason']}" if r.get("reason") else ""))
            env = r.get("environment") or {}
            if env.get("command"):
                lines.append(f"  dependencies: `{env['command']}` ({'installed' if env.get('ok') else 'already present' if env.get('skipped') else 'FAILED'})")
            described = repo_env.describe(r.get("env") or repo_env.empty())
            if described:
                lines += ["  " + x for x in described.splitlines()]
            if r.get("verify_commands"):
                lines.append("  checks Relay runs in this worktree: " + "; ".join(f"`{c}`" for c in r["verify_commands"]))
        lines.append("Work in each repository through its absolute path above (cd there for its commands). Never edit the user's own checkouts.")
        return "\n".join(lines)

    def planning_text(self) -> str:
        """For the supervisor's kickoff: the system map slice and the cross-component rule."""
        from . import systemmap
        parts = []
        try:
            block = systemmap.prompt_block(task_repo_paths(self.task_meta() or self.task))
        except Exception:
            block = ""
        if block:
            parts.append(block)
        parts.append(CROSS_COMPONENT_RULE)
        return "\n\n".join(parts)

    def context_text(self, role: str) -> str:
        parts = [self.repos_text()]
        plan = self.state.get("plan") or {}
        parts.append(design_block(plan.get("system_design") or {}, plan.get("work_packages") or []) if role != "supervisor" else "")
        if role == "supervisor" and self.state.get("phase") == "kickoff":
            parts.append(self.planning_text())
        return "\n\n".join(p for p in parts if p)

    def take_guidance(self, role):
        text = self.m.take_guidance(self.tid, role)
        notes = self.state.pop(f"repo_notes_{role}", "")
        if notes:
            self.save()
            text = (text + "\n\n" if text else "") + notes
        return text

    # ------------------------------------------------------------ work packages across repositories
    def package_prefix(self, env: dict) -> str:
        """A header naming the work package and repository an instruction is for, with a note on unmet dependencies."""
        pkg_id = str(env.get("package") or "").strip()
        repo = str(env.get("repo") or "").strip()
        packages = (self.state.get("plan") or {}).get("work_packages") or []
        pkg = next((p for p in packages if p["id"] == pkg_id), None)
        if pkg and not repo:
            repo = pkg.get("repo") or ""
        if not pkg_id and not repo:
            return ""
        where = ""
        if repo:
            row = self.repo_by_name(repo)
            primary = Path(self.task.get("repo") or "").name.lower()
            where = f" · repository {row['name']} ({row['worktree']})" if row else (f" · repository {repo} (primary, {self.wt})" if repo.lower() in (primary, "primary") else f" · repository {repo}")
        head = f"WORK PACKAGE {pkg_id or ''}{where}".replace("PACKAGE  ·", "PACKAGE ·")
        if pkg_id:
            self.state["current_package"] = pkg_id
        done = set(self.state.get("packages_done") or [])
        waiting = [d for d in (pkg or {}).get("depends_on") or [] if d not in done]
        if waiting:
            head += f"\n(Relay note: this package depends on {', '.join(waiting)}, which no worker report has marked complete yet.)"
            self.r.timeline("supervisor", f"Package {pkg_id} starts before its dependencies", ", ".join(waiting))
        return head + "\n"

    def note_package_report(self, env: dict):
        pkg_id = str((env or {}).get("package") or self.state.get("current_package") or "").strip()
        if pkg_id and (env or {}).get("status", "complete") == "complete":
            done = list(self.state.get("packages_done") or [])
            if pkg_id not in done:
                done.append(pkg_id)
            self.state["packages_done"] = done
            self.m.set_meta(self.tid, packages_done=done)

    # ------------------------------------------------------------ adding a repository mid-task
    def ask_add_repo(self, asker_role, env) -> str:
        from . import protocol, systemmap
        spec = env.get("add_repo") or {}
        if isinstance(spec, str):
            spec = {"repo": spec}
        ref = str(spec.get("repo") or spec.get("component") or "").strip()
        reason = str(spec.get("reason") or "").strip()
        comp = systemmap.find(systemmap.load(), ref)
        label = (comp or {}).get("name") or ref.split("/")[-1] or "the repository"
        accept = f"Add {label} to this task"
        options = [accept, "Continue without it"]
        question = env.get("question") or f"Add {label} to this task? {reason}"
        env = {**env, "question": question, "options": options}
        text = self._ask(asker_role, env, extra={"add_repo": {"repo": ref, "component": (comp or {}).get("id", ""), "reason": reason,
                                                            "cloned": bool(comp and comp.get("path") and Path(comp["path"]).is_dir())}})
        answer = (text or {}).get("text") or ""
        extra = (text or {}).get("extra") or {}
        yes = extra.get("add_repo") is True or answer.strip().lower().startswith(("add", "yes", "y ", "ok", "accept")) or answer.strip() == accept
        if extra.get("add_repo") is False or answer.strip().lower().startswith(("continue without", "no")):
            yes = False
        if not yes:
            self.r.timeline("user", f"Declined adding {label}", truncate(answer, 200))
            return protocol.human_answer(question, answer or "Continue without it.") + \
                f"\nDo not ask to add {label} again unless the human says so; plan within the repositories you have and record the gap as an unknown."
        try:
            comp, path = systemmap.ensure_local(ref)
            existing = next((r for r in self.related if _resolve(r["repo"]) == _resolve(path)), None)
            if _resolve(path) == _resolve(self.task.get("repo")):
                return protocol.human_answer(question, "That repository is already the task's primary repository.")
            row = existing or self.add_related_repo(path, reason or f"added at the {asker_role}'s request", (comp or {}).get("id", ""))
        except Exception as e:
            self.r.timeline("system", f"Could not add {label}", truncate(str(e), 300))
            return protocol.human_answer(question, f"The human accepted, but Relay could not add {label}: {truncate(str(e), 400)}. Continue without it and record the gap.")
        note = (f"ORCHESTRATOR · {row['name']} was added to this task. Its worktree is {row['worktree']} on branch {row['branch']}"
                + (f"; checks Relay runs there: {', '.join(row['verify_commands'])}" if row.get("verify_commands") else "") + ".")
        self.state[f"repo_notes_{'worker' if asker_role == 'supervisor' else 'supervisor'}"] = note + "\n" + self.repos_text()
        self.save()
        return protocol.human_answer(question, answer or accept) + "\n\n" + note + "\n\n" + self.repos_text() + \
            "\n\nContinue: inspect it, then reply with your plan (or next envelope) covering every repository the request needs."

    def _ask(self, asker_role, env, extra=None) -> dict:
        from .util import new_id
        question = env["question"]
        options = env.get("options") or []
        agent = self.role_agent(asker_role)[0] if asker_role in ("supervisor", "worker", "reviewer") else None
        if not self.allow_questions:
            return {"text": options[0] if options else "", "extra": {"add_repo": True}}
        qid = new_id("q")
        qmsg = self.r.msg(role=asker_role, agent=agent, kind="question", qid=qid, content=question, options=options, answered=False,
                          turn=self.state.get("turn"), **(extra or {}))
        self.m.ask_user(self.tid, {"id": qid, "kind": "question", "from": asker_role, "agent": agent, "question": question,
                                   "options": options, "message_id": qmsg["id"], "time": now(), **(extra or {})})
        self.r.status("needs_input", f"The {asker_role} proposes adding a repository")
        self.m.notify("warning", "A repository should be added", truncate(question, 140), self.tid, kind="needs_input")
        ans = self.r.wait_for_answer(qid) or {}
        self.m.clear_pending(self.tid)
        self.r.msg_update(qmsg["id"], answered=True, answer=ans.get("text") or "")
        self.r.msg(role="user", agent=None, kind="user", content=ans.get("text") or "(no text)", to=asker_role, reply_to=qid, turn=self.state.get("turn"))
        return ans

    def add_related_repo(self, path, reason="", component="") -> dict:
        entries = list((self.task_meta() or {}).get("repos") or [])
        repos = normalize_repos(self.task["repo"], [*[e for e in entries if e.get("role") != "primary"],
                                                    {"repo": path, "reason": reason, "component": component}])
        self.task["repos"] = repos
        self.m.set_meta(self.tid, repos=repos)
        entry = next(e for e in repos if _resolve(e["repo"]) == _resolve(path))
        self.related = getattr(self, "related", None) or []
        row = self.attach_repo(entry)
        self._update_masker()
        self.r.timeline("user", f"Repository added · {row['name']}", reason)
        return row

    # ------------------------------------------------------------ changes, diff, fingerprint
    def all_changed_files(self):
        rows = gitops.changed_files(self.wt, self.base)
        for r in getattr(self, "related", None) or []:
            rows += [{**c, "path": f"{r['name']}: {c['path']}"} for c in gitops.changed_files(r["worktree"], r.get("base_commit"))]
        return rows

    def all_diffstat(self):
        ds = dict(gitops.diff_stat(self.wt, self.base))
        for r in getattr(self, "related", None) or []:
            d = gitops.diff_stat(r["worktree"], r.get("base_commit"))
            for k in ("files", "insertions", "deletions", "untracked"):
                ds[k] = int(ds.get(k) or 0) + int(d.get(k) or 0)
        return ds

    def all_diff(self, limit):
        if not getattr(self, "related", None):
            return gitops.full_diff(self.wt, limit, self.base)
        share = max(4000, limit // (len(self.related) + 1))
        parts = [f"### {Path(self.task.get('repo') or '').name} (primary) · {self.wt}\n" + gitops.full_diff(self.wt, share, self.base)]
        for r in self.related:
            parts.append(f"### {r['name']} · {r['worktree']}\n" + gitops.full_diff(r["worktree"], share, r.get("base_commit")))
        return "\n\n".join(parts)

    def fingerprint(self):
        fp = judge.worktree_fingerprint(self.wt, self.base)
        for r in getattr(self, "related", None) or []:
            fp = f"{fp}|{r['name']}:{judge.worktree_fingerprint(r['worktree'], r.get('base_commit'))}"
        return fp

    # ------------------------------------------------------------ verification
    def verify_related(self, timeout, quiet_pass, fail_chars):
        """Each related repository's own checks and design gate, in its own worktree. Returns (items, parts)."""
        items, parts = [], []
        for r in getattr(self, "related", None) or []:
            wt, base = r["worktree"], r.get("base_commit")
            if not gitops.changed_files(wt, base):
                if r.get("verify_commands"):
                    items.append({"command": f"[{r['name']}] checks", "repo": r["name"], "ok": True, "skipped": True, "rc": 0, "duration": 0,
                                  "note": "no changes in this repository"})
                    parts.append(f"[{r['name']}] no changes in this repository; its checks were not run")
                continue
            before = {line[3:] for line in quiet(["git", "status", "--porcelain"], cwd=wt).stdout.splitlines() if line.strip()}
            for c in r.get("verify_commands") or []:
                res = self.r.run_shell(c, wt, "verify", timeout=timeout, title=f"{r['name']} · {c}")
                skipped = res.get("rc") == 5 and "pytest" in c
                if skipped:
                    res["ok"] = True
                cannot_run = res.get("rc") in (126, 127)
                pre_existing = False
                if cannot_run:
                    self.record_blocked([{"check": f"{r['name']}: {c}", "action_required": True,
                                          "reason": truncate((res.get("output") or "").strip().splitlines()[-1] if (res.get("output") or "").strip() else f"exit {res.get('rc')}", 300),
                                          "impact": f"this check in {r['name']} could not run, so it proves nothing"}])
                elif not res["ok"]:
                    b = self.baseline_result(c, timeout, repo=r["repo"], base=base, wt=wt, key=f"{r['name']}::{c}")
                    pre_existing = bool(b and not b["ok"])
                optional = c in (r.get("optional_checks") or [])
                items.append({"command": f"[{r['name']}] {c}", "repo": r["name"], "ok": res["ok"] or pre_existing or optional, "rc": res.get("rc"),
                              "skipped": skipped, "pre_existing": pre_existing, "optional": optional, "passed": res["ok"],
                              "duration": round(res.get("duration") or 0, 1)})
                verdict = ("SKIPPED (no tests collected)" if skipped else "PASS" if res["ok"]
                           else "PRE-EXISTING FAILURE (also fails on the starting commit; does not block)" if pre_existing
                           else "FAIL (optional check; reported, does not block)" if optional else "FAIL")
                body = "" if (res["ok"] and quiet_pass) else "\n" + truncate(res["output"], fail_chars, tail=True)
                parts.append(f"$ [{r['name']}] {c}\n{verdict} (exit {res.get('rc')}, {round(res.get('duration') or 0)}s)" + body)
            if self.design_gate and base:
                _, terms = self.forbidden_terms_file()
                started = time.time()
                result = designcheck.check(wt, base, terms)
                items.append({"command": f"[{r['name']}] Design gate", "repo": r["name"], "ok": result["ok"], "rc": 0 if result["ok"] else 1,
                              "skipped": False, "duration": round(time.time() - started, 1)})
                parts.append(f"[{r['name']}]\n" + designcheck.report_text(result))
            touched = [line[3:].strip('"') for line in quiet(["git", "status", "--porcelain"], cwd=wt).stdout.splitlines()
                       if line[:2].strip() in ("M", "D") and line[3:] not in before]
            if touched:
                quiet(["git", "checkout", "--", *touched], cwd=wt)
        return items, parts

    # ------------------------------------------------------------ delivery
    def guard_related_branches(self):
        for r in getattr(self, "related", None) or []:
            current = quiet(["git", "branch", "--show-current"], cwd=r["worktree"]).stdout.strip()
            if current != r["branch"]:
                raise RuntimeError(f"The {r['name']} worktree is on '{current or 'a detached HEAD'}', not the task branch '{r['branch']}', so Relay "
                                   f"did not commit. Switch it back (git -C {r['worktree']} switch {r['branch']}) and resume the task.")

    def deliver_related(self, primary_pr: dict, title: str, prefix: str) -> list[dict]:
        """Commit, push and open a pull request in every related repository, then cross-link all the pull requests."""
        if not getattr(self, "related", None):
            return []
        cfg = self.cfg
        t = self.task_meta()
        for r in self.related:
            self.r.status("delivering", f"Committing {r['name']}")
            shutil.rmtree(Path(r["worktree"]) / ".orchestrator_refs", ignore_errors=True)
            r["committed"] = gitops.commit_all(self.r, r["worktree"], f"{prefix} {title}".strip()) or bool(r.get("committed"))
            self.r.timeline("git", f"Committed · {r['name']}" if r["committed"] else f"Nothing new to commit · {r['name']}", r["branch"])
            self._remember_head(r)
            self.save()
            r["diffstat"] = gitops.diff_stat(r["worktree"], r.get("base_commit"))
            r["changed_count"] = len(gitops.changed_files(r["worktree"], r.get("base_commit")))
            if not r.get("github_repo") or not cfg.get("github_auto_push_on_pass", True):
                continue
            if not r["changed_count"] and not r.get("pr_url"):
                self.r.timeline("github", f"No changes in {r['name']}", "no branch pushed, no pull request")
                continue
            gitops.push_branch(self.r, r["worktree"], r["branch"])
            self.r.timeline("github", f"Branch pushed · {r['name']}", r["branch"])
            if cfg.get("github_auto_create_pr", True) and not r.get("pr_url"):
                existing = github.pr_for_branch(r["github_repo"], r["branch"])
                if existing:
                    r.update(pr_url=existing.get("url"), pr_number=existing.get("number"))
                else:
                    body_file = Path(self.run_dir) / "repos" / r["name"] / "PR_BODY.md"
                    write_text(body_file, self._related_pr_body(r, t))
                    base = (cfg.get("github_pr_base") or "").strip()
                    pr = github.create_pr(self.r, r["worktree"], r["github_repo"], r["branch"], title, body_file, base, cfg.get("github_pr_draft", True))
                    r.update(pr_url=pr.get("url"), pr_number=pr.get("number"))
                    self.r.timeline("github", f"Draft pull request opened · {r['name']}", pr.get("url") or "")
            self._save_related()
        self._save_related()
        prs = [{"github_repo": self.repo_full, "number": primary_pr.get("number"), "url": primary_pr.get("url"), "cwd": self.wt}] if primary_pr.get("number") and self.repo_full else []
        prs += [{"github_repo": r["github_repo"], "number": r.get("pr_number"), "url": r.get("pr_url"), "cwd": r["worktree"]} for r in self.related if r.get("pr_number")]
        if len(prs) > 1:
            self.cross_link(prs)
        return prs

    def _related_pr_body(self, r, t) -> str:
        summary = self.state.get("pr_summary") or self.state.get("summary") or t.get("name") or ""
        return (f"{summary}\n\nChanges in **{r['name']}** for a multi-repository task"
                + (f" (primary: {self.repo_full})" if self.repo_full else "") + f".\n\n{self.details_md()}\n")

    def cross_link(self, prs: list[dict]):
        for pr in prs:
            others = [f"{o['github_repo']}#{o['number']}" for o in prs if o is not pr]
            try:
                cur = github.gh_json(["pr", "view", str(pr["number"]), "--repo", pr["github_repo"], "--json", "body"], timeout=60) or {}
                body = cur.get("body") or ""
                marker = "Part of a multi-repo change:"
                body = "\n".join(line for line in body.splitlines() if not line.startswith(marker)).rstrip()
                body += f"\n\n{marker} " + ", ".join(others) + "\n"
                section = self.changeset_text() if hasattr(self, "changeset_text") else ""
                if section:
                    # The shared change set now knows every pull request, so the merge order links them.
                    from . import design as _design
                    body = _design.replace_changeset(body, section)
                f = Path(self.run_dir) / f"PR_LINK_{safe_slug(pr['github_repo'])}.md"
                write_text(f, body)
                self.r.run_shell_args(["gh", "pr", "edit", str(pr["number"]), "--repo", pr["github_repo"], "--body-file", str(f)],
                                      cwd=pr["cwd"], role="github", title=f"Link pull requests · {pr['github_repo']}#{pr['number']}")
            except Exception as e:
                self.r.timeline("github", "Could not cross-link a pull request", truncate(str(e), 200))
        self.r.timeline("github", "Pull requests cross-linked", ", ".join(f"{p['github_repo']}#{p['number']}" for p in prs))

    def related_report_md(self) -> str:
        if not getattr(self, "related", None):
            return ""
        lines = ["## Repositories", "", f"- **{Path(self.task.get('repo') or '').name}** (primary) · `{self.branch}`"]
        for r in self.related:
            ds = r.get("diffstat") or {}
            lines.append(f"- **{r['name']}** · `{r['branch']}` · {r.get('changed_count', 0)} files (+{ds.get('insertions', 0)} / -{ds.get('deletions', 0)})"
                         + (f" · PR {r['pr_url']}" if r.get("pr_url") else "") + (f" · {r['reason']}" if r.get("reason") else ""))
        return "\n".join(lines) + "\n"
