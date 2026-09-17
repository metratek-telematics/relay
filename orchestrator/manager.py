"""Manager: task lifecycle, scheduling, control actions, event fan-out."""
from __future__ import annotations

import copy
import shutil
import json
import threading
import time
import traceback
from pathlib import Path

from . import agents, config as C, github, gitops, judge, multirepo, stacks
from .autopilot import Autopilot
from .learning import Learning
from .pipeline import TurnBudget, orchestrate
from .runner import Runner, Stopped
from .store import ACTIVE, TERMINAL, WAITING, TaskStore
from .util import RUNTIME_DIR, new_id, new_task_id, now, truncate


MAX_TURN_LOG = 400

# Statuses whose branch another task may take over. Interrupted tasks resume on their branch, so they keep it.
BRANCH_REUSABLE = {"done", "failed", "stopped"}
QUEUE_PRIORITY = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


class Manager:
    def __init__(self, emit):
        self._emit = emit
        self.store = TaskStore()
        self.lock = threading.RLock()
        self.runners: dict[str, Runner] = {}
        self.process_state: dict[str, dict] = {}
        self.max_parallel = 1
        self.scheduler = False
        self.github_watcher = False
        self.github_status = {"last_poll": None, "error": None, "login": None}
        self._cfg = None
        self._cfg_at = 0
        self.notifications: list[dict] = []
        self.learning = Learning(self)  # scorecards, retrospectives, lessons
        self.autopilot = Autopilot(self)  # dependencies, limits, windows, watchdog, digest (orchestrator/autopilot.py)
        self.number_tasks()

    def number_tasks(self):
        """Give every task a short number (#12) in creation order, so waiting tasks can say what they wait for."""
        rows = sorted(self.store.list(), key=lambda t: (t.get("created_at") or "", t["id"]))
        top = max([int(t.get("number") or 0) for t in rows] or [0])
        for t in rows:
            if not t.get("number"):
                top += 1
                self.store.update(t["id"], touch=False, number=top)

    def next_number(self) -> int:
        return max([int(t.get("number") or 0) for t in self.store.list()] or [0]) + 1

    def clean_dependencies(self, tid, deps) -> list:
        """Validated dependency ids: existing tasks, not itself, no cycles."""
        if deps in (None, ""):
            return []
        if not isinstance(deps, list):
            deps = [deps]
        rows = self.store.list()
        by_id = {t["id"]: t for t in rows}
        by_num = {str(t.get("number")): t["id"] for t in rows if t.get("number")}
        out = []
        for d in deps:
            key = str(d or "").strip().lstrip("#")
            if not key:
                continue
            ref = key if key in by_id else by_num.get(key)
            if not ref:
                raise ValueError(f"The task this depends on ({d}) does not exist.")
            if ref == tid:
                raise ValueError("A task cannot depend on itself.")
            if ref not in out:
                out.append(ref)
        from .autopilot import dependency_cycle
        if tid and dependency_cycle(tid, out, by_id):
            raise ValueError("These dependencies would make a loop: a task would wait for itself.")
        return out[:20]

    # ------------------------------------------------------------ config
    def cfg(self) -> dict:
        if not self._cfg or time.time() - self._cfg_at > 2:
            self._cfg = C.load()
            self._cfg_at = time.time()
        return self._cfg

    def config_changed(self):
        self._cfg_at = 0

    # ------------------------------------------------------------ emission
    def emit(self, typ, payload):
        try:
            self._emit(typ, payload)
        except Exception:
            pass

    def task_view(self, t: dict) -> dict:
        if not t:
            return t
        t = dict(t)
        t["process"] = self.process_state.get(t["id"], {"state": "idle"})
        t["message_count"] = self.store.message_count(t["id"])
        return t

    def emit_task(self, tid):
        t = self.store.get(tid)
        if t:
            self.emit("task", self.task_view(t))

    def notify(self, level, title, body="", tid=None, kind="info"):
        # `kind` lets each browser decide which events deserve a desktop alert or a
        # sound without guessing from titles: delivered, failed, stopped,
        # needs_input, approval, pr_opened, github_issue or info.
        n = {"id": new_id("n"), "level": level, "kind": kind, "title": title, "body": body, "task_id": tid, "time": now(), "read": False}
        self.notifications.insert(0, n)
        self.notifications = self.notifications[:100]
        self.emit("notify", n)

    # ------------------------------------------------------------ tasks
    @property
    def tasks(self):
        return [self.task_view(t) for t in self.store.list()]

    def get(self, tid):
        t = self.store.get(tid)
        return self.task_view(t) if t else None

    def build_workflow(self, payload: dict) -> dict:
        cfg = self.cfg()
        wf_in = payload.get("workflow") or {}
        preset_id = wf_in.get("preset") or payload.get("preset") or cfg.get("workflow_preset")
        preset = C.preset(preset_id)
        roles = copy.deepcopy(cfg.get("roles") or {})
        if preset:
            for r, v in preset["roles"].items():
                roles.setdefault(r, {})
                roles[r]["agent"] = v.get("agent", "")
        for r, v in (wf_in.get("roles") or {}).items():
            if r in C.ROLES and isinstance(v, dict):
                roles.setdefault(r, {})
                if "agent" in v:
                    roles[r]["agent"] = (v.get("agent") or "").strip()
                if "model" in v:
                    roles[r]["model"] = (v.get("model") or "").strip()
                if "effort" in v:
                    roles[r]["effort"] = (v.get("effort") or "").strip().lower()
        for r in C.ROLES:
            roles.setdefault(r, {"agent": "", "model": "", "effort": ""})
            roles[r].setdefault("model", "")
            roles[r].setdefault("effort", "")
        if not roles["supervisor"]["agent"] or not roles["worker"]["agent"]:
            raise ValueError("Both a supervisor and a worker agent are required.")
        for r in C.ROLES:
            if roles[r]["agent"] and roles[r]["agent"] not in C.AGENTS:
                raise ValueError(f"Unknown agent '{roles[r]['agent']}' for {r}.")
            allowed = C.AGENTS.get(roles[r]["agent"], {}).get("efforts") or []
            if roles[r].get("effort") and roles[r]["effort"] not in allowed:
                if allowed:
                    raise ValueError(f"Effort '{roles[r]['effort']}' is not valid for {roles[r]['agent']} (choose {', '.join(allowed)}).")
                roles[r]["effort"] = ""
            if roles[r].get("model"):
                C.remember_model(roles[r]["agent"], roles[r]["model"])
        wf = {
            "preset": preset_id if preset else "custom",
            "roles": roles,
            "max_turns": max(1, int(wf_in.get("max_turns") or cfg.get("max_turns") or 12)),
            "max_review_rounds": max(1, int(wf_in.get("max_review_rounds") or cfg.get("max_review_rounds") or 3)),
            "verify_mode": wf_in.get("verify_mode") or cfg.get("verify_mode") or "each_report",
            "approval_before_delivery": bool(wf_in.get("approval_before_delivery", cfg.get("approval_before_delivery", False))),
            "allow_agent_questions": bool(wf_in.get("allow_agent_questions", cfg.get("allow_agent_questions", True))),
            "verification_commands": [c for c in (wf_in.get("verification_commands") or []) if str(c).strip()],
            "auto_detect_verification": bool(wf_in.get("auto_detect_verification", cfg.get("auto_detect_verification", True))),
            "setup_command": str(wf_in.get("setup_command") or "").strip(),
            # Integration stack id for this task ("none" disables it); empty uses the repository's stack.
            "stack": str(wf_in.get("stack") or "").strip(),
        }
        return wf

    def create_task(self, payload: dict) -> dict:
        repo = (payload.get("repo") or "").strip().strip('"')
        requirements = (payload.get("requirements") or "").strip()
        issue = str(payload.get("issue") or "").strip().lstrip("#")
        if not repo or not Path(repo).exists():
            raise ValueError("Repository folder does not exist.")
        if not gitops.is_git_repo(repo):
            raise ValueError("The folder is not a Git repository. Run `git init` and create an initial commit first.")
        if not requirements and not issue:
            raise ValueError("Describe the task or provide a GitHub issue number.")
        wf = self.build_workflow(payload)
        parent = str(payload.get("follow_up_of") or "").strip()
        if parent and not self.store.get(parent):
            raise ValueError("The task this follows up no longer exists.")
        # Related repositories: given explicitly, or inherited from the task this follows up.
        wanted = payload.get("repos") if "repos" in payload else ((self.store.get(parent) or {}).get("repos") if parent else None)
        repos = multirepo.normalize_repos(repo, wanted)
        tid = new_task_id()
        depends_on = self.clean_dependencies(tid, payload.get("depends_on"))
        name = (payload.get("name") or "").strip() or (gitops.auto_task_name(requirements) or requirements[:60] if requirements else f"Issue #{issue}")
        branch = self.branch_for(payload, repo, name, requirements, issue)
        t = {
            "id": tid, "name": name, "repo": repo, "requirements": requirements, "issue": issue,
            "template": payload.get("template") or "feature",
            "priority": payload.get("priority") or "normal",
            "tags": [x.strip() for x in (payload.get("tags") or []) if str(x).strip()][:10],
            "attachments": [a for a in (payload.get("attachments") or []) if a],
            "workflow": wf,
            "status": "queued" if payload.get("queue", True) else "draft",
            "detail": "Waiting in queue" if payload.get("queue", True) else "Draft · not queued",
            "created_at": now(), "updated_at": now(), "started_at": None, "finished_at": None,
            "events": [], "artifacts": {}, "sessions": {}, "metrics": {}, "guidance": [],
            "checkpoint": None, "pending": None, "archived": False,
            "github_repo": github.remote_repo_name(repo),
            "branch_name": branch,
            "queue_pos": time.time(),
            "number": self.next_number(),
        }
        if depends_on:
            t["depends_on"] = depends_on
        if isinstance(payload.get("retry_policy"), dict):
            t["retry_policy"] = {"infra": max(0, int(payload["retry_policy"].get("infra") or 0))}
        if payload.get("cost_cap_usd"):
            t["cost_cap_usd"] = max(0.0, float(payload.get("cost_cap_usd") or 0))
        if parent:
            t["follow_up_of"] = parent
        if repos:
            t["repos"] = repos
        if isinstance(payload.get("connectors"), list):  # None keeps the repository's default connectors
            t["connectors"] = [str(n) for n in payload["connectors"]][:50]
        if isinstance(payload.get("tools"), list):  # None: the global and repository tools (orchestrator/toolbox.py)
            t["tools"] = [str(n) for n in payload["tools"]][:50]
        self.store.add(t)
        C.remember_repo(repo)
        self.config_changed()
        self.timeline(tid, "user", "Task created", f"{C.AGENTS[wf['roles']['supervisor']['agent']]['label']} supervises {C.AGENTS[wf['roles']['worker']['agent']]['label']}")
        if repos:
            self.timeline(tid, "user", "Multi-repository task", ", ".join(Path(r["repo"]).name for r in repos))
        if parent:
            self.timeline(tid, "user", "Follow-up", f"Builds on {(self.store.get(parent) or {}).get('name', parent)} · {branch}")
        self.emit_task(tid)
        return self.get(tid)

    def update_task(self, tid, patch: dict):
        t = self.store.get(tid)
        if not t:
            raise KeyError("Task not found")
        allowed = {}
        for k in ("name", "requirements", "priority", "tags", "archived"):
            if k in patch:
                allowed[k] = patch[k]
        if "repos" in patch:
            repos = multirepo.normalize_repos(t["repo"], patch["repos"]) or None
            if [r["repo"] for r in repos or []] != [r["repo"] for r in t.get("repos") or []]:
                if tid in self.runners or t["status"] in ACTIVE:
                    raise ValueError("Stop the task before changing its repositories; a running team adds one by asking you.")
                allowed["repos"] = repos
        if "depends_on" in patch:
            allowed["depends_on"] = self.clean_dependencies(tid, patch["depends_on"])
            allowed["waiting"] = None
        if isinstance(patch.get("retry_policy"), dict):
            allowed["retry_policy"] = {"infra": max(0, int(patch["retry_policy"].get("infra") or 0))}
        if "cost_cap_usd" in patch:
            allowed["cost_cap_usd"] = max(0.0, float(patch.get("cost_cap_usd") or 0))
        if "connectors" in patch and (patch["connectors"] is None or isinstance(patch["connectors"], list)):
            allowed["connectors"] = patch["connectors"]
        if "tools" in patch and (patch["tools"] is None or isinstance(patch["tools"], list)):
            allowed["tools"] = patch["tools"]
        if "workflow" in patch and isinstance(patch["workflow"], dict):
            wf = dict(t.get("workflow") or {})
            for k in ("max_turns", "max_review_rounds", "verify_mode", "approval_before_delivery", "allow_agent_questions"):
                if k in patch["workflow"]:
                    wf[k] = patch["workflow"][k]
            if t["status"] in ("queued", "draft") and "roles" in patch["workflow"]:
                wf = self.build_workflow({"workflow": {**wf, **patch["workflow"]}})
            allowed["workflow"] = wf
        if "status" in patch and patch["status"] == "queued" and t["status"] == "draft":
            allowed["status"] = "queued"
            allowed["detail"] = "Waiting in queue"
            allowed["queue_pos"] = time.time()  # queueing again joins the back of the queue
        self.store.update(tid, immediate=True, **allowed)
        self.emit_task(tid)
        return self.get(tid)

    def taken_branches(self, repo) -> set:
        return {t.get("branch_name") or t.get("branch") for t in self.store.list()
                if t.get("repo") == repo or repo in multirepo.task_repo_paths(t)} - {None}

    def branch_for(self, payload, repo, name, requirements, issue) -> str:
        wanted = (payload.get("branch") or "").strip()
        if wanted:
            if not gitops.valid_branch_name(wanted):
                raise ValueError(f"'{wanted}' is not a valid Git branch name.")
            # A finished task's branch can be built on by a follow-up; an unfinished one is still being written to.
            holders = [t for t in self.store.list() if t.get("repo") == repo and wanted in (t.get("branch_name"), t.get("branch"))]
            busy = [t for t in holders if t.get("status") not in BRANCH_REUSABLE]
            if busy:
                raise ValueError(f"The task \"{busy[0].get('name')}\" still uses the branch {wanted}. "
                                 "Follow up once it has finished, or stop or delete it first.")
            # An existing branch is fine: the task builds on it, as a retry would.
            return wanted
        return gitops.suggest_branch(self.cfg(), name, requirements, payload.get("template") or "feature",
                                     issue, repo, self.taken_branches(repo))

    def duplicate(self, tid):
        t = self.store.get(tid)
        if not t:
            raise KeyError("Task not found")
        payload = {"repo": t["repo"], "requirements": t["requirements"], "issue": t.get("issue"), "name": t["name"] + " (copy)",
                   "template": t.get("template"), "priority": t.get("priority"), "tags": t.get("tags"),
                   "workflow": t.get("workflow"), "queue": False, "repos": t.get("repos") or []}
        return self.create_task(payload)

    def archive(self, tid, archived=True):
        self.store.update(tid, immediate=True, archived=bool(archived))
        self.emit_task(tid)

    def delete(self, tid, delete_worktree=False):
        """Delete a task for good: stop it, remove its record, history and worktree."""
        t = self.store.get(tid)
        if not t:
            # Already gone from memory; still tombstone it so a stale copy on disk
            # can never come back, and tell every open browser.
            self.store.remove(tid)
            self.emit("task_deleted", {"id": tid})
            return
        # Tombstone first: from here on nothing may write this task back.
        self.store.remove(tid)
        self.notifications = [n for n in self.notifications if n.get("task_id") != tid]
        self.process_state.pop(tid, None)
        self.emit("task_deleted", {"id": tid})

        runner = self.runners.get(tid)
        if runner:
            runner.stop()
            # Wait for the agent process to exit so Windows releases file handles
            # before we delete the worktree and logs underneath it.
            for _ in range(60):
                if tid not in self.runners:
                    break
                time.sleep(0.25)

        stacks.teardown_quietly(tid, reason="task deleted")
        if delete_worktree:
            # Every repository of a multi-repository task has its own worktree.
            for w in multirepo.task_worktrees(t):
                gitops.remove_worktree(w["repo"], w["worktree"])
        # Remove conversation, artifacts and logs, as the delete dialog promises.
        shutil.rmtree(RUNTIME_DIR / tid, ignore_errors=True)

    # ------------------------------------------------------------ startup recovery
    def recover_on_start(self):
        """Re-queue runs a restart interrupted, and restart the queue if it was running."""
        cfg = self.cfg()
        resumed = []
        # Commands and tool calls that were running when the process stopped never got their final update,
        # so they would show a spinner and a growing timer forever. Nothing is running at startup.
        for t in self.store.list():
            for m in self.store.messages(t["id"]):
                if m.get("status") == "running":
                    self.store.update_message(t["id"], m["id"], {"status": "interrupted"})
        if cfg.get("auto_resume_interrupted", True):
            for t in self.store.list():
                if t.get("status") != "interrupted" or t.get("archived"):
                    continue
                # Only resume runs that actually have a checkpoint and a live worktree.
                if not (t.get("checkpoint") and t.get("worktree") and Path(t["worktree"]).exists()):
                    continue
                self.store.update(t["id"], immediate=True, status="queued",
                                  detail="Queued · resuming from checkpoint after restart", pending=None)
                self.timeline(t["id"], "system", "Resuming after restart",
                              f"phase {(t.get('checkpoint') or {}).get('phase')} · work package {(t.get('checkpoint') or {}).get('turn')}")
                resumed.append(t["name"])
        if resumed:
            self.notify("info", "Resuming interrupted task" + ("s" if len(resumed) > 1 else ""),
                        ", ".join(resumed[:3]) + (f" +{len(resumed)-3} more" if len(resumed) > 3 else ""))
        if cfg.get("queue_running") or resumed:
            self.start()
        return resumed

    # ------------------------------------------------------------ scheduler
    def start(self, n=None):
        if n:
            self.max_parallel = max(1, int(n))
        else:
            self.max_parallel = max(1, int(self.cfg().get("max_parallel") or 1))
        if not self.cfg().get("queue_running"):
            C.update({"queue_running": True})
            self.config_changed()
        if self.scheduler:
            self.emit("queue", {"running": True, "max_parallel": self.max_parallel})
            return
        self.scheduler = True
        threading.Thread(target=self._loop, daemon=True).start()
        self.emit("queue", {"running": True, "max_parallel": self.max_parallel})
        if self.cfg().get("github_intake_enabled", True):
            self.start_github_watcher()

    def stop_queue(self):
        self.scheduler = False
        C.update({"queue_running": False})
        self.config_changed()
        self.emit("queue", {"running": False, "max_parallel": self.max_parallel})

    def queue_state(self):
        rows = self.store.list()
        return {"running": self.scheduler, "max_parallel": self.max_parallel,
                "active": sum(1 for t in rows if t.get("status") in ACTIVE),
                "queued": sum(1 for t in rows if t.get("status") == "queued"),
                "order": [t["id"] for t in self.queue_order(rows)]}

    # ------------------------------------------------------------ queue order
    @staticmethod
    def queue_key(t):
        """Run order: priority first, then the explicit queue position (creation time when never moved)."""
        pos = t.get("queue_pos")
        if pos is None:
            try:
                from datetime import datetime
                pos = datetime.fromisoformat(t.get("created_at") or "").timestamp()
            except (TypeError, ValueError):
                pos = 0.0
        return (QUEUE_PRIORITY.get(t.get("priority", "normal"), 2), float(pos), t.get("created_at", ""))

    def queue_order(self, rows=None):
        rows = self.store.list() if rows is None else rows
        return sorted([t for t in rows if t.get("status") == "queued" and not t.get("archived")], key=self.queue_key)

    def _busy(self, t) -> bool:
        return t["id"] in self.runners or t.get("status") in ACTIVE

    def chain_blocked(self, t, rows, queued) -> bool:
        """A task in a chain waits while another task of the chain runs, waits for you, or is ahead of it in the queue."""
        return self.chain_blocker(t, rows, queued) is not None

    def chain_blocker(self, t, rows, queued):
        """The chain task t waits for, or None."""
        chain = t.get("chain_id")
        if not chain:
            return None
        for o in rows:
            if o["id"] == t["id"] or o.get("chain_id") != chain:
                continue
            if self._busy(o):
                return o
        for o in queued:
            if o["id"] == t["id"]:
                return None
            if o.get("chain_id") == chain:
                return o
        return None

    def move_in_queue(self, tid, direction):
        """Swap a queued task with its neighbour in run order. It takes the neighbour's priority, so the order you see is the order that runs."""
        rows = self.queue_order()
        ids = [t["id"] for t in rows]
        if tid not in ids:
            raise ValueError("Only queued tasks can be reordered.")
        i = ids.index(tid)
        j = {"up": i - 1, "down": i + 1, "top": 0, "bottom": len(ids) - 1}.get(direction)
        if j is None:
            raise ValueError("Direction must be up, down, top or bottom.")
        j = max(0, min(len(ids) - 1, j))
        if i == j:
            return self.get(tid)
        order = rows[:]
        moved = order.pop(i)
        order.insert(j, moved)
        # Renumber everything so positions stay distinct; the moved task adopts its new neighbour's priority.
        neighbour = order[j + 1] if j < i else order[j - 1]
        base = time.time() - len(order)
        for k, t in enumerate(order):
            patch = {"queue_pos": base + k}
            if t["id"] == tid and neighbour.get("priority") != t.get("priority"):
                patch["priority"] = neighbour.get("priority") or "normal"
            self.store.update(t["id"], immediate=True, **patch)
        self.timeline(tid, "user", "Moved in queue", f"now #{j + 1} of {len(order)}")
        for t in order:
            self.emit_task(t["id"])
        self.emit("queue", self.queue_state())
        return self.get(tid)

    def unqueue(self, tid):
        """Take a task out of the queue without deleting it; it stays as a draft you can start or queue again."""
        t = self.store.get(tid)
        if not t:
            raise KeyError("Task not found")
        if t.get("status") != "queued" or tid in self.runners:
            raise ValueError("Only a waiting task can be removed from the queue.")
        self.store.update(tid, immediate=True, status="draft", detail="Draft · removed from the queue")
        self.timeline(tid, "user", "Removed from queue", "")
        self.emit_task(tid)
        self.emit("queue", self.queue_state())
        return self.get(tid)

    def hold_chain(self, tid):
        """After a chain task fails, park the rest of its chain when the chain asks for that."""
        t = self.store.get(tid) or {}
        if not t.get("chain_id") or not t.get("chain_stop_on_failure") or t.get("status") != "failed":
            return
        held = []
        for o in self.store.list():
            if o.get("chain_id") == t["chain_id"] and o["id"] != tid and o.get("status") == "queued" and o["id"] not in self.runners:
                self.store.update(o["id"], immediate=True, status="draft", detail=f"Held · {t.get('name', 'an earlier task')} failed")
                self.timeline(o["id"], "system", "Chain stopped", f"{t.get('name', tid)} failed; start this task yourself when ready")
                self.emit_task(o["id"])
                held.append(o.get("name"))
        if held:
            self.notify("warning", "Chain stopped after a failure", f"{len(held)} task(s) held: " + ", ".join(held[:3]), tid, kind="failed")

    def _loop(self):
        while self.scheduler:
            try:
                # Dependencies, parking, run windows, limits and fallbacks: orchestrator/autopilot.py
                self.autopilot.schedule()
            except Exception:
                traceback.print_exc()
            time.sleep(0.5)

    def launch(self, tid):
        t = self.store.get(tid)
        if not t or tid in self.runners:
            return
        self.store.update(tid, immediate=True, status="running", detail="Starting", started_at=t.get("started_at") or now(),
                          finished_at=None, error=None, pending=None)
        r = Runner(tid, self)
        self.runners[tid] = r
        self.emit_task(tid)

        def work():
            try:
                orchestrate(self.store.get(tid), r, self)
            except Stopped:
                self.store.update(tid, immediate=True, status="stopped", detail="Stopped by user", finished_at=now(), pending=None)
                self.timeline(tid, "user", "Stopped", "")
                if not self.store.is_deleted(tid):  # deleting a task stops it too; that needs no alert
                    self.notify("info", "Task stopped", (self.store.get(tid) or {}).get("name", ""), tid, kind="stopped")
            except TurnBudget as e:
                self.store.update(tid, immediate=True, status="failed", detail=str(e), finished_at=now(), pending=None, error=str(e))
                self.timeline(tid, "system", "Turn budget exhausted", str(e))
                self.notify("error", "Turn budget exhausted", self.store.get(tid).get("name", ""), tid, kind="failed")
            except Exception as e:
                tb = traceback.format_exc()
                self.store.update(tid, immediate=True, status="failed", detail=truncate(str(e), 300), finished_at=now(), pending=None,
                                  error=str(e), traceback=tb)
                self.message(tid, {"id": new_id("m"), "role": "orchestrator", "kind": "error", "content": truncate(str(e), 3000)})
                self.timeline(tid, "system", "Task failed", truncate(str(e), 300))
                self.notify("error", "Task failed", truncate(str(e), 140), tid, kind="failed")
            finally:
                try:
                    # Still counted as running here, so the next task of its chain cannot slip in before it is held.
                    self.hold_chain(tid)
                except Exception:
                    pass
                self.runners.pop(tid, None)
                self.process_state[tid] = {"state": "idle"}
                self.emit_task(tid)
                self.emit("queue", self.queue_state())
                self.learning.task_ended(tid)
                try:
                    self.autopilot.after_run(tid)  # automatic retry of infrastructure failures
                except Exception:
                    traceback.print_exc()

        r.thread = threading.Thread(target=work, daemon=True)  # the watchdog checks it is still alive
        r.thread.start()

    # ------------------------------------------------------------ control actions
    def start_task(self, tid):
        """Launch one task right now, independently of the queue and its parallel limit.

        Each task owns its own worktree, CLI processes and agent sessions, so several
        can run side by side without interfering.
        """
        t = self.store.get(tid)
        if not t:
            raise KeyError("Task not found")
        if tid in self.runners or t.get("status") in ACTIVE:
            raise ValueError("Task is already running")
        if t.get("pending"):
            raise ValueError("Task is waiting for your answer")
        fresh = not (t.get("checkpoint") and t.get("worktree") and Path(t.get("worktree") or "").exists())
        self.store.update(tid, immediate=True, status="queued", finished_at=None, error=None, traceback=None,
                          detail="Starting now" if fresh else "Starting now · resuming from checkpoint")
        self.timeline(tid, "user", "Started manually", "Runs alongside any other active tasks")
        self.launch(tid)
        return self.get(tid)

    def stop(self, tid):
        r = self.runners.get(tid)
        if r:
            r.stop()
        else:
            t = self.store.get(tid)
            if t and t.get("status") in ACTIVE | WAITING | {"queued"}:
                self.store.update(tid, immediate=True, status="stopped", detail="Stopped", finished_at=now(), pending=None)
                threading.Thread(target=stacks.teardown_quietly, args=(tid, "task stopped"), daemon=True).start()
                self.emit_task(tid)
                self.notify("info", "Task stopped", t.get("name", ""), tid, kind="stopped")
                self.learning.task_ended(tid, retro_turn=False)

    def pause(self, tid):
        r = self.runners.get(tid)
        if not r:
            raise ValueError("Task is not running")
        r.pause()
        self.store.update(tid, immediate=True, pause_requested=True)
        self.timeline(tid, "user", "Pause requested", "Takes effect after the current agent turn")
        self.emit_task(tid)

    def resume(self, tid):
        r = self.runners.get(tid)
        t = self.store.get(tid)
        if r:
            if t and t.get("autopilot_parked"):
                self.autopilot.resume_parked(tid)
            r.resume()
            self.store.update(tid, immediate=True, pause_requested=False)
            if t and t.get("status") == "paused":
                self.store.update(tid, immediate=True, status="running", detail="Resuming")
            self.emit_task(tid)
            return
        if t and t.get("status") in TERMINAL | {"paused"}:
            self.retry(tid, fresh=False)

    def retry(self, tid, fresh=False, start_queue=True):
        t = self.store.get(tid)
        if not t:
            raise KeyError("Task not found")
        if tid in self.runners:
            raise ValueError("Task is still running")
        patch = {"status": "queued", "detail": "Queued for retry", "finished_at": None, "pending": None, "error": None, "traceback": None}
        if fresh or not (t.get("checkpoint") and t.get("worktree") and Path(t.get("worktree") or "").exists()):
            patch.update({"checkpoint": None, "sessions": {}, "plan": None, "verification": None, "review": None,
                          "acceptance": None, "follow_ups": None})
            patch["detail"] = "Queued · fresh start"
            if t.get("started_at"):
                # A new run: elapsed time, the work chart and the signals describe it alone.
                patch.update({"started_at": None, "runs": int(t.get("runs") or 1) + 1, "blocked_checks": None, "design_gate": None,
                              "environment": None, "diffstat": None, "changed_count": 0, "base_commit": None, "summary": "",
                              "repo_worktrees": None, "system_design": None, "packages_done": None})
        else:
            patch["detail"] = "Queued · resuming from checkpoint"
            if str(t.get("error") or "").startswith("Turn budget exhausted"):
                # Failed at the work-package budget by an older build: resuming grants the extension.
                patch["checkpoint"] = {**t["checkpoint"], "budget_exhausted": True}
        self.store.update(tid, immediate=True, **patch)
        self.timeline(tid, "user", "Requeued", patch["detail"])
        self.emit_task(tid)
        if start_queue and not self.scheduler:
            self.start()

    def answer(self, tid, qid, text, extra=None):
        r = self.runners.get(tid)
        t = self.store.get(tid)
        if not r or not t or not t.get("pending"):
            raise ValueError("Nothing is waiting for an answer on this task")
        if qid and t["pending"].get("id") != qid:
            raise ValueError("That question is no longer pending")
        r.answer(t["pending"].get("id"), text, extra)

    def approve(self, tid, approved=True, note=""):
        self.answer(tid, None, note, {"approved": bool(approved)})

    def guidance(self, tid, text, to="next", mode="queue"):
        t = self.store.get(tid)
        if not t:
            raise KeyError("Task not found")
        text = (text or "").strip()
        if not text:
            raise ValueError("Guidance is empty")
        row = {"id": new_id("g"), "time": now(), "text": text, "to": to or "next", "consumed": False}
        rows = list(t.get("guidance") or []) + [row]
        self.store.update(tid, immediate=True, guidance=rows)
        self.message(tid, {"id": new_id("m"), "role": "user", "kind": "user", "content": text, "to": to or "next",
                           "mode": mode, "turn": (t.get("checkpoint") or {}).get("turn")})
        self.timeline(tid, "user", "Guidance queued" if mode != "interrupt" else "Interrupting with guidance", truncate(text, 200))
        r = self.runners.get(tid)
        if mode == "interrupt" and r and r.current.get("state") == "running" and r.current.get("agent"):
            row["consumed"] = True
            self.store.update(tid, immediate=True, guidance=rows)
            r.interrupt(text)
            return {"applied": "interrupt"}
        if r and t.get("pending") and t["pending"].get("kind") == "question":
            # Treat guidance during a pending question as the answer.
            r.answer(t["pending"]["id"], text, {})
            row["consumed"] = True
            self.store.update(tid, immediate=True, guidance=rows)
            return {"applied": "answer"}
        if not r and t.get("status") in ("failed", "stopped", "interrupted"):
            # Writing to a task that has ended means "carry on with this": resume it from its checkpoint
            # (or start it over when there is none) and hand the message to the next agent turn.
            try:
                self.start_task(tid)
                self.timeline(tid, "user", "Resumed by your message", truncate(text, 200))
                return {"applied": "resumed"}
            except (ValueError, KeyError) as e:
                return {"applied": "next_boundary", "note": str(e)}
        if not r and t.get("status") in TERMINAL:
            # A finished task has no next turn: nothing would ever read this (seen: a question left unanswered after delivery).
            return {"applied": "finished", "note": "This task has finished, so no agent will read it. Start a follow-up task to act on it."}
        return {"applied": "next_boundary"}

    def edit_acceptance(self, tid, ops: dict):
        """Human edit of the acceptance contract. The supervisor hears about it at its next turn."""
        t = self.store.get(tid)
        if not t:
            raise KeyError("Task not found")
        before = list(t.get("acceptance") or [])
        if not before and not (ops or {}).get("add"):
            raise ValueError("This task has no acceptance contract to edit")
        after = judge.edit_acceptance(before, ops or {})
        self.store.update(tid, immediate=True, acceptance=after)
        run_dir = t.get("run_dir")
        if run_dir and Path(run_dir).is_dir():
            p = Path(run_dir) / "ACCEPTANCE.md"
            p.write_text(judge.acceptance_md(after), encoding="utf-8")
            self.artifact(tid, "acceptance", str(p))
        if t.get("status") not in TERMINAL:
            text = ("The human edited the acceptance contract. It is now:\n" + judge.acceptance_block(after)
                    + "\nJudge done against this version.")
            rows = list(t.get("guidance") or []) + [{"id": new_id("g"), "time": now(), "text": text, "to": "supervisor", "consumed": False}]
            self.store.update(tid, immediate=True, guidance=rows)
        self.timeline(tid, "user", "Acceptance criteria edited", f"{len(before)} → {len(after)} criteria")
        self.emit_task(tid)
        return self.get(tid)

    def take_guidance(self, tid, role):
        t = self.store.get(tid)
        rows = list(t.get("guidance") or []) if t else []
        picked = []
        for g in rows:
            if g.get("consumed"):
                continue
            if g.get("to") in (role, "next", "both", "all", None, ""):
                g["consumed"] = True
                g["consumed_by"] = role
                picked.append(g)
        if picked:
            self.store.update(tid, guidance=rows)
            self.timeline(tid, "user", "Guidance delivered", f"{len(picked)} message(s) → {role}")
        return "\n\n".join(f"[{g['time']}] {g['text']}" for g in picked)

    # ------------------------------------------------------------ callbacks from runner / pipeline
    def set_status(self, tid, status, detail=""):
        self.store.update(tid, immediate=True, status=status, detail=detail)
        self.emit_task(tid)

    def set_meta(self, tid, **kw):
        self.store.update(tid, **kw)
        self.emit_task(tid)

    def artifact(self, tid, kind, path):
        t = self.store.get(tid) or {}
        arts = dict(t.get("artifacts") or {})
        arts[kind] = path
        self.store.update(tid, artifacts=arts)
        self.emit("artifact", {"task_id": tid, "kind": kind})

    def timeline(self, tid, role, title, detail=""):
        ev = {"id": new_id("e"), "role": role, "title": title, "detail": detail or "", "time": now()}
        self.store.add_event(tid, ev)
        self.emit("event", {"task_id": tid, **ev})

    def message(self, tid, fields: dict) -> dict:
        fields = dict(fields)
        fields.setdefault("id", new_id("m"))
        msg = self.store.append_message(tid, fields)
        self.emit("message", {"task_id": tid, **msg})
        return msg

    def message_update(self, tid, mid, patch: dict):
        self.store.update_message(tid, mid, patch)
        self.emit("message_update", {"task_id": tid, "id": mid, **patch})

    def process(self, tid, payload: dict):
        self.process_state[tid] = payload
        self.emit("process", {"task_id": tid, **payload})

    def turn_started(self, tid, role, agent, label, turn):
        self.store.update(tid, current_role=role, current_agent=agent, current_turn=turn)

    def session_seen(self, tid, role, agent, session_id, model):
        t = self.store.get(tid) or {}
        sessions = dict(t.get("sessions") or {})
        s = dict(sessions.get(role) or {})
        s.update({"agent": agent, "id": session_id or s.get("id"), "model": model or s.get("model")})
        sessions[role] = s
        self.store.update(tid, sessions=sessions)

    def estimate_cost(self, agent, usage) -> tuple[float, bool]:
        """Return (cost_usd, estimated). Uses reported cost when present, else the pricing table."""
        reported = float(usage.get("cost_usd") or 0)
        if reported > 0:
            return reported, False
        price = (self.cfg().get("pricing") or {}).get(agent) or {}
        if not price or not self.cfg().get("show_estimated_cost", True):
            return 0.0, False
        inp = int(usage.get("input") or 0)
        cached = min(int(usage.get("cached") or 0), inp)
        out = int(usage.get("output") or 0)
        cost = ((inp - cached) * float(price.get("input") or 0) + cached * float(price.get("cached") or 0)
                + out * float(price.get("output") or 0)) / 1_000_000
        return round(cost, 5), cost > 0

    def metrics_add(self, tid, agent, role, usage, elapsed, tool_calls, turn=None, extra=None):
        t = self.store.get(tid) or {}
        m = copy.deepcopy(t.get("metrics") or {})
        m.setdefault("agents", {})
        m.setdefault("roles", {})
        cost, estimated = self.estimate_cost(agent, usage or {})
        # Totals alone cannot say which phase spent what, so each turn is also kept, compactly, for the work chart.
        end = time.time()
        m["log"] = (list(m.get("log") or []) + [{
            "start": round(end - float(elapsed or 0), 1), "end": round(end, 1), "role": role, "agent": agent, "turn": turn,
            "input": int(usage.get("input") or 0), "output": int(usage.get("output") or 0), "cached": int(usage.get("cached") or 0),
            "cost_usd": round(cost, 5), "estimated": estimated, "tool_calls": int(tool_calls or 0), **(extra or {})}])[-MAX_TURN_LOG:]
        if extra and extra.get("sections"):  # prompt size by section, summed over the task (tokens.py)
            sec = m.setdefault("sections", {})
            for k, v in extra["sections"].items():
                sec[k] = int(sec.get(k) or 0) + int(v or 0)
        for key, bucket in ((agent, m["agents"]), (role, m["roles"])):
            d = bucket.setdefault(key, {"turns": 0, "input": 0, "output": 0, "cached": 0, "cost_usd": 0.0, "seconds": 0.0, "tool_calls": 0, "estimated": False})
            d["turns"] += 1
            d["input"] += int(usage.get("input") or 0)
            d["output"] += int(usage.get("output") or 0)
            d["cached"] += int(usage.get("cached") or 0)
            d["cost_usd"] = round(d["cost_usd"] + cost, 4)
            d["estimated"] = bool(d.get("estimated")) or estimated
            d["seconds"] = round(d["seconds"] + float(elapsed or 0), 1)
            d["tool_calls"] += int(tool_calls or 0)
        tot = m.setdefault("total", {"turns": 0, "input": 0, "output": 0, "cost_usd": 0.0, "seconds": 0.0, "tool_calls": 0, "estimated": False})
        tot["turns"] += 1
        tot["input"] += int(usage.get("input") or 0)
        tot["output"] += int(usage.get("output") or 0)
        tot["cost_usd"] = round(tot["cost_usd"] + cost, 4)
        tot["estimated"] = bool(tot.get("estimated")) or estimated
        tot["seconds"] = round(tot["seconds"] + float(elapsed or 0), 1)
        tot["tool_calls"] += int(tool_calls or 0)
        self.store.update(tid, metrics=m)
        self.emit_task(tid)
        try:
            self.autopilot.on_cost(tid)  # pause at the task's cost cap
        except Exception:
            traceback.print_exc()

    def ask_user(self, tid, pending: dict):
        self.store.update(tid, immediate=True, pending=pending)
        self.emit_task(tid)

    def clear_pending(self, tid):
        self.store.update(tid, immediate=True, pending=None)
        self.emit_task(tid)

    def complete(self, tid, payload: dict):
        self.store.update(tid, immediate=True, status="done", detail="Delivered", finished_at=now(), pending=None, **payload)
        self.emit_task(tid)
        t = self.store.get(tid) or {}
        self.notify("success", "Task delivered", t.get("name", ""), tid, kind="delivered")

    # ------------------------------------------------------------ github intake
    def start_github_watcher(self):
        if self.github_watcher:
            return
        self.github_watcher = True
        threading.Thread(target=self._github_loop, daemon=True).start()

    def stop_github_watcher(self):
        self.github_watcher = False

    def _github_loop(self):
        while self.github_watcher:
            cfg = self.cfg()
            try:
                auth = github.auth_info()
                if not auth.get("ready"):
                    self.github_status = {"last_poll": now(), "error": auth.get("error"), "login": None}
                else:
                    err = None
                    sources = github.load_sources()
                    existing = {t.get("github_issue_key") for t in self.store.list() if t.get("github_issue_key")}
                    for source in sources:
                        if not source.get("enabled", True):
                            continue
                        repo = github.normalize_repo_full_name(source.get("repo", ""))
                        if not repo:
                            continue
                        for issue in github.issue_candidates(source):
                            key = f"{repo}#{issue['number']}"
                            if key in existing:
                                continue
                            local = source.get("local_path", "")
                            if not local or not Path(local).exists():
                                try:
                                    local = str(github.ensure_local_repo(repo, local))
                                    source["local_path"] = local
                                    github.save_sources(sources)
                                except Exception as e:
                                    err = f"{repo}: clone failed: {e}"
                                    continue
                            body = (issue.get("body") or "").strip()
                            try:
                                t = self.create_task({
                                    "repo": local, "name": f"#{issue['number']} {issue['title']}", "issue": str(issue["number"]),
                                    "requirements": f"GitHub issue #{issue['number']}: {issue['title']}\n\n{body}\n\nImplement this issue completely, preserving repository conventions. The deliverable is a reviewable branch and draft PR.",
                                    "workflow": {"preset": source.get("preset") or cfg.get("workflow_preset"),
                                                 "max_turns": source.get("max_turns") or cfg.get("max_turns")},
                                    "tags": ["github"], "queue": bool(source.get("auto_queue", True)),
                                })
                                self.store.update(t["id"], immediate=True, github_repo=repo, github_issue_number=issue["number"],
                                                  github_issue_title=issue["title"], github_issue_url=issue.get("url"),
                                                  github_issue_key=key, github_source="watcher")
                                existing.add(key)
                                self.notify("info", "Issue queued from GitHub", f"#{issue['number']} {issue['title']}", t["id"], kind="github_issue")
                                self.emit_task(t["id"])
                            except Exception as e:
                                err = f"{repo}#{issue['number']}: {e}"
                    self.github_status = {"last_poll": now(), "error": err, "login": auth.get("login")}
            except Exception as e:
                self.github_status = {"last_poll": now(), "error": str(e), "login": None}
            self.emit("github", self.github_status)
            seconds = max(15, int(cfg.get("github_poll_seconds", 60)))
            for _ in range(seconds):
                if not self.github_watcher:
                    return
                time.sleep(1)

    # ------------------------------------------------------------ dashboard
    def dashboard(self) -> dict:
        rows = [t for t in self.store.list() if not t.get("archived")]
        by_status = {}
        for t in rows:
            by_status[t.get("status", "queued")] = by_status.get(t.get("status", "queued"), 0) + 1
        done = [t for t in rows if t.get("status") == "done"]
        failed = [t for t in rows if t.get("status") in ("failed",)]
        finished = len(done) + len(failed)
        durations = []
        for t in done:
            try:
                from datetime import datetime
                a = datetime.fromisoformat(t["started_at"])
                b = datetime.fromisoformat(t["finished_at"])
                durations.append((b - a).total_seconds())
            except Exception:
                pass
        agents_tot = {}
        cost = 0.0
        turns = 0
        any_estimated = False
        for t in rows:
            m = t.get("metrics") or {}
            cost += float((m.get("total") or {}).get("cost_usd") or 0)
            turns += int((m.get("total") or {}).get("turns") or 0)
            any_estimated = any_estimated or bool((m.get("total") or {}).get("estimated"))
            for a, d in (m.get("agents") or {}).items():
                x = agents_tot.setdefault(a, {"turns": 0, "input": 0, "output": 0, "cost_usd": 0.0, "seconds": 0.0, "tool_calls": 0, "estimated": False})
                for k in x:
                    if k == "estimated":
                        x[k] = x[k] or bool(d.get(k))
                    elif k == "cost_usd":
                        x[k] = round(x[k] + float(d.get(k) or 0), 4)
                    else:
                        x[k] = x[k] + (d.get(k) or 0)
        recent = []
        for t in sorted(rows, key=lambda t: t.get("updated_at", ""), reverse=True)[:12]:
            for e in (t.get("events") or [])[-3:]:
                recent.append({"task_id": t["id"], "task": t["name"], **e})
        recent = sorted(recent, key=lambda e: e.get("time", ""), reverse=True)[:20]
        return {
            "total": len(rows), "by_status": by_status,
            "success_rate": (len(done) / finished) if finished else None,
            "avg_duration": (sum(durations) / len(durations)) if durations else None,
            "total_cost_usd": round(cost, 2), "total_turns": turns, "cost_estimated": any_estimated,
            "agents": agents_tot, "recent": recent,
            "prs": [{"task_id": t["id"], "name": t["name"], "url": t.get("pr_url"), "number": t.get("pr_number"), "repo": t.get("github_repo")} for t in done if t.get("pr_url")][:10],
            "queue": self.queue_state(),
            "median_duration": _median(durations),
            "insights": self.insights(rows),
            "success": self.learning.summary(),
        }

    def insights(self, rows, days=14) -> dict:
        """Per-day outcomes, spend and duration for the dashboard charts.

        Tasks are bucketed by the local day they finished, so a task that ran over
        midnight counts once, on the day its result appeared. Unfinished tasks have
        no outcome yet but have already spent money, so their cost lands on the day
        they were last updated.
        """
        from datetime import date, datetime, timedelta
        today = date.today()
        first = today - timedelta(days=days - 1)
        series = [{"date": (first + timedelta(days=i)).isoformat(), "done": 0, "failed": 0, "stopped": 0,
                   "cost_usd": 0.0, "cost_by_agent": {}} for i in range(days)]
        durations = [[] for _ in range(days)]

        def day_index(iso):
            try:
                i = (datetime.fromisoformat(iso).date() - first).days
            except (TypeError, ValueError):
                return None
            return i if 0 <= i < days else None

        repos = {}
        for t in rows:
            status = t.get("status")
            finished = status in ("done", "failed", "stopped")
            i = day_index(t.get("finished_at") if finished else t.get("updated_at"))
            if i is None:
                continue
            bucket = series[i]
            if finished:
                bucket[status] += 1
            if status == "done":
                try:
                    durations[i].append((datetime.fromisoformat(t["finished_at"]) - datetime.fromisoformat(t["started_at"])).total_seconds())
                except (KeyError, TypeError, ValueError):
                    pass
            for agent, d in ((t.get("metrics") or {}).get("agents") or {}).items():
                cost = float(d.get("cost_usd") or 0)
                bucket["cost_by_agent"][agent] = round(bucket["cost_by_agent"].get(agent, 0.0) + cost, 4)
                bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 4)
            key = t.get("github_repo") or t.get("repo") or ""
            r = repos.setdefault(key, {"repo": key, "path": t.get("repo") or "", "tasks": 0, "done": 0, "failed": 0})
            r["tasks"] += 1
            r["done"] += status == "done"
            r["failed"] += status in ("failed", "stopped")

        for b, ds in zip(series, durations):
            outcomes = b["done"] + b["failed"] + b["stopped"]
            b["success_rate"] = (b["done"] / outcomes) if outcomes else None
            b["median_duration"] = _median(ds)

        def window(lo, hi):
            part = series[lo:hi]
            finished = sum(b["done"] + b["failed"] + b["stopped"] for b in part)
            done = sum(b["done"] for b in part)
            return {"finished": finished, "done": done, "success_rate": (done / finished) if finished else None,
                    "median_duration": _median([x for ds in durations[lo:hi] for x in ds]),
                    "cost_usd": round(sum(b["cost_usd"] for b in part), 2)}

        # Two equal halves let the dashboard say whether things are getting better.
        half = days // 2
        return {
            "days": series, "window_days": half,
            "current": window(days - half, days), "previous": window(0, days - half),
            "top_repos": sorted(repos.values(), key=lambda r: (-r["tasks"], r["repo"]))[:6],
        }


def _median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
